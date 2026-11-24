"""Data contracts of the walk-forward backtest and the leakage guard.

The engine (:mod:`cs2bot.backtest.runner`) owns the full history and hands models only what was
known before the moment they are used:

* a **trainer** receives a :class:`TrainingSet` - every record visible strictly before its
  ``cutoff`` (a match's records become visible ``result_delay`` after its last map started), checked
  by :func:`assert_no_leakage` when the set is built;
* the trained **predictor** receives a :class:`MatchQuery` - the pre-match description of one match
  (no result) and the decision time ``as_of``;
* the engine checks the predictor's ``trained_through`` (latest data timestamp it used, when it
  exposes one) and the prediction's own time stamp (``as_of`` / ``created_at``) against the
  decision time and raises :class:`LeakageError` on any violation.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from cs2bot.domain.enums import MatchFormat, Tier
from cs2bot.domain.markets import MarketOutcome, MarketSpec
from cs2bot.domain.match import MatchContext
from cs2bot.domain.predictions import OutcomeProbability
from cs2bot.domain.records import MapResultRecord, PlayerMapRecord, RoundRecord, VetoRecord
from cs2bot.domain.results import MatchActuals
from cs2bot.utils.time import ensure_utc

__all__ = [
    "BacktestMatch",
    "HistoricalData",
    "LeakageError",
    "MatchQuery",
    "Predictor",
    "ProbabilityTable",
    "Trainer",
    "TrainingSet",
    "assert_no_leakage",
    "outcome_key",
]


class LeakageError(RuntimeError):
    """Information from at or after the decision time reached a model (look-ahead)."""


def outcome_key(spec: MarketSpec, selection: str) -> str:
    """``"<spec key>#<selection>"`` - the same key as :meth:`MarketOutcome.key`."""
    return MarketOutcome(spec=spec, selection=selection).key()


@dataclass(frozen=True)
class BacktestMatch:
    """One historical match: what was known before it (``context``) and how it ended (``actuals``).

    ``start_time`` is the kick-off (UTC). ``end_time`` (when the result became known) defaults to
    the start of the last map plus the engine's ``result_delay``. ``context`` should hold pre-match
    information only (lineups, the published veto); the engine strips the veto from it when
    ``BacktestConfig.veto_known_at_decision`` is off.
    """

    match_id: int
    start_time: datetime
    format: MatchFormat
    team1_id: int
    team2_id: int
    actuals: MatchActuals | None = None
    context: MatchContext | None = None
    tier: Tier = Tier.UNRANKED
    is_lan: bool = False
    end_time: datetime | None = None

    def __post_init__(self) -> None:
        if self.start_time.tzinfo is None:
            raise ValueError("BacktestMatch.start_time must be timezone-aware (UTC)")
        if self.end_time is not None and self.end_time.tzinfo is None:
            raise ValueError("BacktestMatch.end_time must be timezone-aware (UTC)")


@dataclass(frozen=True)
class MatchQuery:
    """What a predictor may see about a match it predicts: everything but the result."""

    match_id: int
    start_time: datetime
    format: MatchFormat
    team1_id: int
    team2_id: int
    as_of: datetime  # decision time; the prediction must only use data strictly before it
    context: MatchContext | None = None
    tier: Tier = Tier.UNRANKED
    is_lan: bool = False

    def to_context(self) -> MatchContext:
        """The query's :class:`MatchContext` (a minimal one when none was supplied)."""
        if self.context is not None:
            return self.context
        return MatchContext(
            match_id=self.match_id,
            scheduled_at=self.start_time,
            format=self.format,
            team1_id=self.team1_id,
            team2_id=self.team2_id,
            tier=self.tier,
            is_lan=self.is_lan,
        )


def _time_of(record: Any) -> datetime:
    return ensure_utc(record.played_at)


def assert_no_leakage(records: Iterable[Any], cutoff: datetime, *, what: str = "training data") -> None:
    """Raise :class:`LeakageError` unless every record's ``played_at`` is strictly before ``cutoff``."""
    t = ensure_utc(cutoff)
    for rec in records:
        when = rec.played_at if rec.played_at.tzinfo is not None else ensure_utc(rec.played_at)
        if when >= t:
            raise LeakageError(
                f"{what} contains a record of match {getattr(rec, 'match_id', '?')} at {when.isoformat()}, "
                f"not before the cutoff {t.isoformat()}"
            )


@dataclass(frozen=True)
class TrainingSet:
    """Everything a trainer may use: records visible strictly before ``cutoff``.

    Built by :meth:`HistoricalData.until`; the constructor re-checks every record
    (:func:`assert_no_leakage`), so a set holding a record from at or after the cutoff cannot exist.
    ``matches`` are the finished :class:`BacktestMatch` es (with actuals) whose result was known.
    """

    cutoff: datetime
    map_results: tuple[MapResultRecord, ...] = ()
    vetoes: tuple[VetoRecord, ...] = ()
    rounds: tuple[RoundRecord, ...] = ()
    player_maps: tuple[PlayerMapRecord, ...] = ()
    matches: tuple[BacktestMatch, ...] = ()

    def __post_init__(self) -> None:
        if self.cutoff.tzinfo is None:
            raise ValueError("TrainingSet.cutoff must be timezone-aware")
        for name in ("map_results", "vetoes", "rounds", "player_maps"):
            assert_no_leakage(getattr(self, name), self.cutoff, what=f"TrainingSet.{name}")
        for m in self.matches:
            end = m.end_time or m.start_time
            if ensure_utc(end) >= ensure_utc(self.cutoff):
                raise LeakageError(
                    f"TrainingSet.matches holds match {m.match_id} that ended at/after the cutoff"
                )

    @property
    def latest_time(self) -> datetime | None:
        """The latest ``played_at`` in the set (None when empty)."""
        times = [
            _time_of(r)
            for coll in (self.map_results, self.vetoes, self.rounds, self.player_maps)
            for r in coll
        ]
        return max(times, default=None)


@dataclass
class HistoricalData:
    """The full history the engine slices into :class:`TrainingSet` s.

    A match's records become visible ``result_delay`` after its last map started (``visible_at``;
    or at ``BacktestMatch.end_time`` when that is later); records of matches without map results
    (e.g. a veto only) ``result_delay`` after their own ``played_at``. ``until(t)`` returns the
    records visible at ``t`` - all of them played strictly before ``t`` since ``result_delay > 0``.
    """

    map_results: Sequence[MapResultRecord] = ()
    vetoes: Sequence[VetoRecord] = ()
    rounds: Sequence[RoundRecord] = ()
    player_maps: Sequence[PlayerMapRecord] = ()
    matches: Sequence[BacktestMatch] = ()
    result_delay: timedelta = timedelta(hours=1)
    _visible: dict[int, datetime] = field(default_factory=dict, init=False, repr=False)
    _index: dict[str, tuple[list[Any], list[datetime]]] = field(default_factory=dict, init=False, repr=False)
    _ends: list[tuple[datetime, BacktestMatch]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.result_delay <= timedelta(0):
            raise ValueError("result_delay must be positive")
        last: dict[int, datetime] = {}
        for rec in self.map_results:
            t = _time_of(rec)
            if rec.match_id not in last or t > last[rec.match_id]:
                last[rec.match_id] = t
        self._visible = {mid: t + self.result_delay for mid, t in last.items()}
        for m in self.matches:
            if m.end_time is not None:
                end = ensure_utc(m.end_time)
                self._visible[m.match_id] = max(self._visible.get(m.match_id, end), end)
        for name in ("map_results", "vetoes", "rounds", "player_maps"):
            keyed = sorted(
                ((self.visible_at(r), _time_of(r), i, r) for i, r in enumerate(getattr(self, name))),
                key=lambda x: (x[0], x[1], x[2]),
            )
            self._index[name] = ([r for *_, r in keyed], [v for v, *_ in keyed])
            setattr(self, name, sorted(getattr(self, name), key=lambda r: (_time_of(r), r.match_id)))
        self._ends = sorted(
            ((self.match_end(m), m) for m in self.matches), key=lambda x: (x[0], x[1].match_id)
        )

    def visible_at(self, record: Any) -> datetime:
        """When the result behind ``record`` became known."""
        known = self._visible.get(int(getattr(record, "match_id", -1)))
        own = _time_of(record) + self.result_delay
        return known if known is not None and known > own else own

    def match_end(self, match: BacktestMatch) -> datetime:
        """When the result of ``match`` became known (settlement time of its bets)."""
        if match.end_time is not None:
            return ensure_utc(match.end_time)
        known = self._visible.get(match.match_id)
        if known is not None:
            return known
        return ensure_utc(match.start_time) + self.result_delay * max(1, match.format.n_maps)

    def _visible_prefix(self, name: str, t: datetime) -> tuple[Any, ...]:
        records, times = self._index[name]
        return tuple(records[: bisect_right(times, t)])

    def until(self, cutoff: datetime) -> TrainingSet:
        """Records known at ``cutoff`` (visible at or before it; played strictly before it)."""
        t = ensure_utc(cutoff)
        return TrainingSet(
            cutoff=t,
            map_results=self._visible_prefix("map_results", t),
            vetoes=self._visible_prefix("vetoes", t),
            rounds=self._visible_prefix("rounds", t),
            player_maps=self._visible_prefix("player_maps", t),
            matches=tuple(m for end, m in self._ends if end < t),
        )


@runtime_checkable
class Predictor(Protocol):
    """A trained model as the engine uses it.

    ``predict`` returns any object the configured pricer understands (a
    :class:`~cs2bot.domain.predictions.MatchPrediction` for the engine pricer, a
    :class:`ProbabilityTable`, ...). Optional members the engine uses when present:

    * ``trained_through: datetime | None`` - the latest data timestamp the model was trained on;
      it must be strictly before every decision time it is used at;
    * ``observe(training: TrainingSet) -> None`` - called before each prediction with everything
      visible at that decision time, so a model can update online between retrains.
    """

    def predict(self, query: MatchQuery) -> Any: ...


class Trainer(Protocol):
    """``train(training_set) -> Predictor``; it must use nothing but the training set."""

    def __call__(self, training: TrainingSet) -> Predictor: ...


@dataclass(frozen=True)
class ProbabilityTable:
    """A prediction given directly as outcome probabilities (``outcome_key`` -> probability).

    Used by simple and synthetic models (oracle, random) that do not build a ``MatchPrediction``.
    ``as_of`` is the information time of the prediction (checked by the engine).
    """

    match_id: int
    as_of: datetime
    outcomes: Mapping[str, OutcomeProbability]
    model_version: str = "table"
    data_completeness: float = 1.0
    veto_known: bool = True

    def get(self, spec: MarketSpec, selection: str) -> OutcomeProbability | None:
        return self.outcomes.get(outcome_key(spec, selection))
