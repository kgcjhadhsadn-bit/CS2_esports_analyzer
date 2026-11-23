"""Map-level team ratings: an online, hierarchical Glicko-2 adapted to CS2 maps.

Model
-----
Every team ``i`` has an **overall** Glicko-2 rating ``(mu_i, phi_i, sigma_i)`` (Glickman, *Example of
the Glicko-2 system*, 2022; the pure maths lives in :mod:`cs2bot.models.ratings_glicko`) and, for
every map ``m`` it has played, a **map offset** ``delta_im ~ N(d_im, psi_im^2)``. The strength used
for a game on map ``m`` is the sum of the components

    theta_im = mu_i + delta_im (+ ell_i on LAN, optional per-team LAN offset)

with variance ``Phi_im^2 = phi_i^2 + psi_im^2 (+ lambda_i^2)``. The *map rating* of a team is
therefore ``mu_i + d_im`` with deviation ``Phi_im`` and a team's first game on a map starts from its
overall rating with an inflated deviation (``d = 0``, ``psi = map_offset_sd``).

Shrinkage (the documented choice). This is the hierarchical formulation of "blend the map rating
with the overall rating": map offsets have a zero-mean prior with standard deviation
``map_offset_sd`` (``psi_0``) and are updated by Kalman gains, so a map offset after ``n`` games on
the map is the precision-weighted mean of the map-specific residuals shrunk toward zero by

    w = 1 - psi^2 / psi_0^2  ~=  n / (n + k_shrink),   k_shrink = v_bar / psi_0^2,

where ``v_bar ~ 4`` is the per-game outcome variance ``v`` of Glicko-2 for evenly matched,
well-established opponents (the identity is exact for constant ``v`` and no drift). ``w`` is
reported as ``map_weight`` (``RatingSnapshot`` / ``MapWinEstimate.components``) and
:attr:`RatingConfig.equivalent_k_shrink` gives ``k_shrink``. Compared with two independent rating
chains blended by ``n / (n + k)``, every game informs both levels consistently and the opponent's
map strength always reflects its *current* overall form. ``map_offset_sd = 0`` gives the plain
overall-only Glicko-2.

Online update of one map (both teams simultaneously, each against the other's pre-game values):

1. **Time.** ``c`` = rating periods (``period_days``) since the team's previous game; the overall
   deviation becomes ``phi_t = min(phi_0, sqrt(phi^2 + c sigma^2))`` and offsets drift with
   ``psi_t^2 = min(psi_0^2, psi^2 + c_m * map_offset_drift^2)``.
2. **Roster continuity.** If the record's lineup replaced ``k`` players of the team's previous
   lineup, a reset fraction ``rho = roster_reset_fractions[k]`` (default 0, .12, .35, .7, .9, 1: one
   change is mild, three or more is close to a reset) moves ``phi^2`` toward ``phi_0^2`` by ``rho``
   and regresses ``mu`` toward ``initial_mu`` by ``rho * roster_mu_regression``; map/LAN offsets get
   the same treatment scaled by ``roster_offset_scale``.
3. **Glicko-2 quantities** with the opponent's combined deviation:
   ``g_o = kappa g(Phi_opp)``, ``E = sigmoid(g_o (theta_own - theta_opp))``,
   ``v = 1 / (w_tier g_o^2 E (1 - E))``, ``Delta = (s - E) / (g_o E (1 - E))``. ``kappa = 1 + lan_scale``
   on LAN (default ``lan_scale = 0``) and ``w_tier`` comes from ``tier_weights`` (default 1).
4. **Kalman split.** With prior variances ``P_k`` of the components (``phi_t^2``, ``psi_t^2``, ...),
   ``Phi^2 = sum P_k`` and ``info = 1 / v``, every component moves by
   ``P_k w_tier g_o (s - E) / (1 + Phi^2 info)`` and its variance becomes
   ``P_k - P_k^2 info / (1 + Phi^2 info)``. With a single component this is exactly Glicko-2's
   ``phi' = 1 / sqrt(1/phi*^2 + 1/v)`` and ``mu' = mu + phi'^2 g (s - E)``.
5. **Volatility, once per rating period.** The games of a team's current rating period accumulate
   ``1/v = sum w g^2 E (1 - E)`` and ``sum w g (s - E)``; when ``period_days`` have elapsed since the
   period's first game, ``sigma'`` is computed with the Illinois algorithm from the aggregated
   ``v`` and ``Delta`` (Step 5 of the paper, verbatim) and drives the time inflation from then on.
   Evidence about the volatility from a single game is negligible - the paper recommends 10-15
   games per rating period - and even per period Glicko-2's volatility moves slowly unless results
   are far outside the expected range (that is its design; ``tau`` bounds the change).

Actual score (margin of victory): ``s = (1 - mov_weight) 1[win] + mov_weight F_MR12(round share)``
where ``F_MR12`` is the exact MR12/MR3 map-win probability for i.i.d. rounds - the implied map-win
probability of the observed round share. A win always gives ``s > 0.5``.

Prediction: ``theta_1 - theta_2 ~ N(kappa (theta_1 - theta_2), kappa^2 (Phi_1^2 + Phi_2^2))`` at
``as_of`` (deviations inflated to ``as_of``); ``p_team1_win`` is the mean of the logistic over this
normal (Gauss-Hermite) and ``sigma`` its standard deviation, i.e. the uncertainty of ``p`` induced
by rating uncertainty. ``components["overall"]`` is the same computation with overall ratings only.

No look-ahead: :meth:`MapRatingModel.predict`, :meth:`~MapRatingModel.rating` and
:meth:`~MapRatingModel.map_advantage` refuse an ``as_of`` that is not strictly after the last
processed game. Fit up to a cut-off with :meth:`~MapRatingModel.fit_until`, keep a copy with
:meth:`~MapRatingModel.copy`, or fit once with ``record_history=True`` and use
:meth:`~MapRatingModel.snapshot_before` / :meth:`~MapRatingModel.historical_map_advantage` for
leakage-free historical queries (veto-model training, walk-forward backtests).

Default parameters were chosen by walk-forward log-loss on synthetic universes with known ground
truth (several seeds, with and without strength drift); re-tune on real data with :func:`tune`.
"""

from __future__ import annotations

import bisect
import itertools
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cs2bot.domain.enums import TeamSlot, Tier
from cs2bot.domain.predictions import MapWinEstimate
from cs2bot.domain.records import MapResultRecord
from cs2bot.models.ratings_glicko import (
    g,
    logistic_normal_moments,
    logit,
    mov_score,
    sigmoid,
    to_display,
    to_display_rd,
    volatility_update,
)
from cs2bot.utils.names import normalize_map_name
from cs2bot.utils.time import ensure_utc

__all__ = [
    "MapRatingModel",
    "RatingConfig",
    "RatingSnapshot",
    "ReliabilityBin",
    "WalkForwardPrediction",
    "WalkForwardResult",
    "chronological_split",
    "evaluate_walk_forward",
    "tune",
]

_SECONDS_PER_DAY: Final[float] = 86_400.0
_E_CLIP: Final[float] = 1e-9
_FORMAT_KIND: Final[str] = "cs2bot.MapRatingModel"
_FORMAT_VERSION: Final[int] = 1


# ------------------------------------------------------------------------------------------ config


class RatingConfig(BaseModel):
    """All knobs of :class:`MapRatingModel` (Glicko-2 scale: 1.0 = 173.7 display points).

    Defaults were tuned by walk-forward log-loss on synthetic universes (see module docstring).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    initial_mu: float = 0.0
    initial_phi: float = Field(default=2.0, gt=0.0)
    initial_sigma: float = Field(default=0.06, gt=0.0)
    tau: float = Field(default=0.5, gt=0.0)
    period_days: float = Field(default=7.0, gt=0.0)
    min_phi: float = Field(default=0.0, ge=0.0)
    min_sigma: float = Field(default=0.005, gt=0.0)
    max_sigma: float = Field(default=0.5, gt=0.0)
    volatility_eps: float = Field(default=1e-6, gt=0.0)
    # Hierarchical map offsets (0 disables them: overall-only Glicko-2).
    map_offset_sd: float = Field(default=0.6, ge=0.0)
    map_offset_drift: float = Field(default=0.02, ge=0.0)
    # Margin of victory: weight of the implied map-win probability of the round share (mild: on
    # synthetic data the round margin added no information beyond the result; re-tune on real data).
    mov_weight: float = Field(default=0.1, ge=0.0, le=1.0)
    # LAN: global discriminability multiplier kappa = 1 + lan_scale and optional per-team offsets.
    lan_scale: float = Field(default=0.0, gt=-1.0)
    lan_offset_sd: float = Field(default=0.0, ge=0.0)
    lan_offset_drift: float = Field(default=0.0, ge=0.0)
    # Optional information weight per tournament tier value ("s", "a", "b", ...); default 1.
    tier_weights: dict[str, float] = Field(default_factory=dict)
    # Roster continuity: reset fraction by number of replaced players (index = changes).
    roster_reset_fractions: tuple[float, ...] = (0.0, 0.12, 0.35, 0.7, 0.9, 1.0)
    roster_mu_regression: float = Field(default=0.5, ge=0.0, le=1.0)
    roster_offset_scale: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("tier_weights")
    @classmethod
    def _check_tiers(cls, v: dict[str, float]) -> dict[str, float]:
        valid = {t.value for t in Tier}
        for key, weight in v.items():
            if key not in valid:
                raise ValueError(f"unknown tier {key!r} in tier_weights (valid: {sorted(valid)})")
            if not 0.0 < weight <= 10.0:
                raise ValueError("tier weights must be in (0, 10]")
        return v

    @field_validator("roster_reset_fractions")
    @classmethod
    def _check_roster(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if not v:
            raise ValueError("roster_reset_fractions must not be empty")
        if any(not 0.0 <= x <= 1.0 for x in v):
            raise ValueError("roster reset fractions must be in [0, 1]")
        if any(b < a for a, b in itertools.pairwise(v)):
            raise ValueError("roster reset fractions must be non-decreasing")
        return v

    @model_validator(mode="after")
    def _check_bounds(self) -> RatingConfig:
        if not self.min_sigma <= self.initial_sigma <= self.max_sigma:
            raise ValueError("need min_sigma <= initial_sigma <= max_sigma")
        if self.min_phi > self.initial_phi:
            raise ValueError("min_phi must not exceed initial_phi")
        return self

    @property
    def equivalent_k_shrink(self) -> float:
        """``k`` of the equivalent ``w = n / (n + k)`` map shrinkage (``inf`` without map offsets).

        Uses the outcome variance of an even game between well-known teams (``v = 4``).
        """
        if self.map_offset_sd <= 0.0:
            return math.inf
        return 4.0 / (self.map_offset_sd * self.map_offset_sd)

    def tier_weight(self, tier: Tier | str | None) -> float:
        if tier is None:
            return 1.0
        key = tier.value if isinstance(tier, Tier) else str(tier)
        return self.tier_weights.get(key, 1.0)

    def lan_kappa(self, is_lan: bool) -> float:
        return 1.0 + self.lan_scale if is_lan else 1.0

    def roster_fraction(self, n_changes: int) -> float:
        if n_changes <= 0:
            return 0.0
        table = self.roster_reset_fractions
        return table[min(n_changes, len(table) - 1)]


class RatingSnapshot(BaseModel):
    """A team's rating (overall or on one map) at a point in time.

    ``mu``/``phi`` are on the Glicko-2 scale; ``r``/``rd`` the usual display units
    (``r = 1500 + 173.7178 mu``). For a map snapshot ``mu = mu_overall + map_offset`` and
    ``phi = sqrt(phi_overall^2 + map_offset_sd^2)``; ``map_weight`` is the shrinkage weight
    ``1 - psi^2 / psi_0^2`` (~ ``n / (n + k)``).
    """

    model_config = ConfigDict(frozen=True)

    team_id: int
    map_name: str | None = None
    mu: float
    phi: float
    sigma: float
    n_games: int
    last_played: datetime | None = None
    r: float
    rd: float
    map_offset: float = 0.0
    map_offset_sd: float = 0.0
    map_weight: float = 0.0
    is_prior: bool = False


# -------------------------------------------------------------------------------------- state types


@dataclass(frozen=True, slots=True)
class GlickoState:
    """Overall Glicko-2 component. ``phi`` is the post-update deviation at time ``last``."""

    mu: float
    phi: float
    sigma: float
    n: int = 0
    last: datetime | None = None


@dataclass(frozen=True, slots=True)
class OffsetState:
    """Gaussian offset component (map or LAN): mean, variance at time ``last``, games counted."""

    mean: float
    var: float
    n: int = 0
    last: datetime | None = None


@dataclass(frozen=True, slots=True)
class VolatilityPeriod:
    """Glicko-2 evidence accumulated over the team's current (open) rating period.

    ``phi2`` is the pre-period rating variance, ``inv_v`` = ``sum_j w_j g_j^2 E_j (1 - E_j)`` and
    ``score`` = ``sum_j w_j g_j (s_j - E_j)`` over the period's games, so that ``v = 1 / inv_v`` and
    ``Delta = score / inv_v`` exactly as in Steps 3-4 of the paper.
    """

    start: datetime
    phi2: float
    inv_v: float
    score: float
    n: int


@dataclass(frozen=True, slots=True)
class _Team:
    """Immutable per-team state (updates create new objects; history can share references)."""

    overall: GlickoState
    maps: Mapping[str, OffsetState] = field(default_factory=dict)
    lan: OffsetState | None = None
    lineup: tuple[int, ...] = ()
    vol: VolatilityPeriod | None = None


@dataclass(frozen=True, slots=True)
class _View:
    """A team as it stands at time ``t`` for one game / prediction."""

    overall: GlickoState  # roster-adjusted, not time-inflated
    periods: float  # rating periods since overall.last
    phi2_now: float  # overall variance inflated to t with the current sigma (capped)
    map_off: OffsetState | None  # variance inflated to t; None when map offsets are disabled
    lan_off: OffsetState | None  # None unless a LAN game with LAN offsets enabled
    roster_changes: int = 0

    @property
    def theta(self) -> float:
        out = self.overall.mu
        if self.map_off is not None:
            out += self.map_off.mean
        if self.lan_off is not None:
            out += self.lan_off.mean
        return out

    @property
    def offset_var(self) -> float:
        out = 0.0
        if self.map_off is not None:
            out += self.map_off.var
        if self.lan_off is not None:
            out += self.lan_off.var
        return out

    @property
    def var_now(self) -> float:
        return self.phi2_now + self.offset_var


def _periods(last: datetime | None, t: datetime | None, period_days: float) -> float:
    if last is None or t is None:
        return 0.0
    return max(0.0, (t - last).total_seconds() / (_SECONDS_PER_DAY * period_days))


def _record_sort_key(r: MapResultRecord) -> tuple[datetime, int, int]:
    return ensure_utc(r.played_at), r.match_id, r.map_number


def _roster_changes(previous: Sequence[int], current: Sequence[int]) -> int:
    """Number of players of ``previous`` that were *replaced* in ``current``.

    Lineup data can be incomplete (three known players one day, five the next), so players are only
    counted as replacements when somebody had to make room for them:
    ``changes = min(|current - previous|, |previous - current|)``. A lineup that merely lists fewer
    or more of the same players is not a roster change.
    """
    prev, cur = set(previous), set(current)
    return min(len(cur - prev), len(prev - cur))


# -------------------------------------------------------------------------------------------- model


class MapRatingModel:
    """Online hierarchical Glicko-2 ratings per team and per (team, map). See the module docstring."""

    def __init__(self, config: RatingConfig | None = None) -> None:
        self._config = config or RatingConfig()
        self._teams: dict[int, _Team] = {}
        self._processed: set[tuple[int, int]] = set()
        self._processed_matches: set[int] = set()
        self._processed_log: list[tuple[datetime, int, int]] = []
        self._last: datetime | None = None
        self._history: dict[int, tuple[list[datetime], list[_Team]]] | None = None

    # ------------------------------------------------------------------ properties / housekeeping

    @property
    def config(self) -> RatingConfig:
        return self._config

    @property
    def last_processed(self) -> datetime | None:
        """``played_at`` of the latest processed map (``None`` before any update)."""
        return self._last

    @property
    def n_processed(self) -> int:
        return len(self._processed)

    @property
    def team_ids(self) -> list[int]:
        return sorted(self._teams)

    @property
    def records_history(self) -> bool:
        """Whether per-update snapshots are being recorded (see :meth:`snapshot_before`)."""
        return self._history is not None

    def has_processed_match(self, match_id: int) -> bool:
        return match_id in self._processed_matches

    def reset(self, *, record_history: bool = False) -> None:
        self._teams = {}
        self._processed = set()
        self._processed_matches = set()
        self._processed_log = []
        self._last = None
        self._history = {} if record_history else None

    def copy(self) -> MapRatingModel:
        """Independent copy of the full state (team states are immutable and shared safely)."""
        out = MapRatingModel(self._config)
        out._teams = dict(self._teams)
        out._processed = set(self._processed)
        out._processed_matches = set(self._processed_matches)
        out._processed_log = list(self._processed_log)
        out._last = self._last
        if self._history is not None:
            out._history = {tid: (list(ts), list(st)) for tid, (ts, st) in self._history.items()}
        return out

    # ------------------------------------------------------------------------------- fitting

    def fit(self, records: Iterable[MapResultRecord], *, record_history: bool = False) -> MapRatingModel:
        """Reset and process ``records`` in chronological order. Returns ``self``."""
        self.reset(record_history=record_history)
        for rec in sorted(records, key=_record_sort_key):
            self.update(rec)
        return self

    def fit_until(
        self, records: Iterable[MapResultRecord], as_of: datetime, *, record_history: bool = False
    ) -> MapRatingModel:
        """Reset and process only the records played strictly before ``as_of``. Returns ``self``."""
        cut = ensure_utc(as_of)
        return self.fit((r for r in records if ensure_utc(r.played_at) < cut), record_history=record_history)

    def update(self, record: MapResultRecord) -> bool:
        """Process one finished map. Records must arrive in chronological order.

        Returns ``False`` (and changes nothing) for an already processed ``(match_id, map_number)``
        or a record without a winner / with identical teams; raises ``ValueError`` for a record
        older than the last processed one.
        """
        key = (record.match_id, record.map_number)
        if key in self._processed:
            return False
        t = ensure_utc(record.played_at)
        if self._last is not None and t < self._last:
            raise ValueError(
                f"records must be processed chronologically: {t.isoformat()} is before the last "
                f"processed game {self._last.isoformat()}"
            )
        if record.team1_id == record.team2_id or record.team1_rounds == record.team2_rounds:
            return False
        self._process(record, t)
        self._processed.add(key)
        self._processed_matches.add(record.match_id)
        self._processed_log.append((t, record.match_id, record.map_number))
        self._last = t if self._last is None or t > self._last else self._last
        return True

    def _process(self, rec: MapResultRecord, t: datetime) -> None:
        cfg = self._config
        map_name = normalize_map_name(rec.map_name)
        team1 = self._rolled(self._teams.get(rec.team1_id) or self._new_team(), t)
        team2 = self._rolled(self._teams.get(rec.team2_id) or self._new_team(), t)
        team1, ch1 = self._apply_roster(team1, rec.team1_lineup)
        team2, ch2 = self._apply_roster(team2, rec.team2_lineup)
        view1 = self._view(team1, map_name, t, rec.is_lan, roster_changes=ch1)
        view2 = self._view(team2, map_name, t, rec.is_lan, roster_changes=ch2)
        s1 = mov_score(rec.team1_rounds, rec.team2_rounds, cfg.mov_weight)
        weight = cfg.tier_weight(rec.tier)
        kappa = cfg.lan_kappa(rec.is_lan)
        new1 = self._update_team(team1, view1, view2, s1, weight, kappa, t, map_name)
        new2 = self._update_team(team2, view2, view1, 1.0 - s1, weight, kappa, t, map_name)
        self._teams[rec.team1_id] = new1
        self._teams[rec.team2_id] = new2
        if self._history is not None:
            for tid, st in ((rec.team1_id, new1), (rec.team2_id, new2)):
                times, states = self._history.setdefault(tid, ([], []))
                times.append(t)
                states.append(st)

    def _new_team(self) -> _Team:
        cfg = self._config
        return _Team(overall=GlickoState(mu=cfg.initial_mu, phi=cfg.initial_phi, sigma=cfg.initial_sigma))

    def _rolled(self, team: _Team, t: datetime | None) -> _Team:
        """Close the team's rating period if it has ended by ``t`` (Glicko-2 Step 5, pure).

        The volatility is re-estimated once per completed rating period from all games of the
        period (``v``/``Delta`` aggregated as in the paper, ``c = 1``); per-game evidence about the
        volatility is negligible, which is why the paper recommends 10-15 games per period.
        """
        acc = team.vol
        if acc is None or t is None:
            return team
        cfg = self._config
        if (t - acc.start).total_seconds() < cfg.period_days * _SECONDS_PER_DAY:
            return team
        sigma_new = team.overall.sigma
        if acc.inv_v > 0.0:
            sigma_new = volatility_update(
                phi2=acc.phi2,
                v=1.0 / acc.inv_v,
                delta=acc.score / acc.inv_v,
                sigma=team.overall.sigma,
                tau=cfg.tau,
                periods=1.0,
                eps=cfg.volatility_eps,
            )
            sigma_new = min(max(sigma_new, cfg.min_sigma), cfg.max_sigma)
        return replace(team, overall=replace(team.overall, sigma=sigma_new), vol=None)

    def _apply_roster(self, team: _Team, lineup: Sequence[int]) -> tuple[_Team, int]:
        """Roster-continuity adjustment for a new lineup; returns the new state and #changes."""
        new_lineup = tuple(dict.fromkeys(int(p) for p in lineup))
        if not new_lineup:
            return team, 0
        if not team.lineup:
            return replace(team, lineup=new_lineup), 0
        changes = _roster_changes(team.lineup, new_lineup)
        if changes == 0:
            if set(new_lineup) <= set(team.lineup):
                # Same players, or an incomplete listing of them: keep the fuller reference lineup.
                return team, 0
            return replace(team, lineup=new_lineup), 0
        return self._roster_adjusted(team, changes, new_lineup), changes

    def _roster_adjusted(self, team: _Team, changes: int, lineup: tuple[int, ...]) -> _Team:
        cfg = self._config
        rho = cfg.roster_fraction(changes)
        if rho <= 0.0:
            return replace(team, lineup=lineup)
        ov = team.overall
        phi0_2 = cfg.initial_phi * cfg.initial_phi
        phi2 = ov.phi * ov.phi
        phi2 = phi2 + rho * max(phi0_2 - phi2, 0.0)
        mu = ov.mu + rho * cfg.roster_mu_regression * (cfg.initial_mu - ov.mu)
        overall = replace(ov, mu=mu, phi=math.sqrt(phi2))
        rho_o = rho * cfg.roster_offset_scale
        maps: Mapping[str, OffsetState] = team.maps
        lan = team.lan
        if rho_o > 0.0:
            psi0_2 = cfg.map_offset_sd * cfg.map_offset_sd
            maps = {
                m: replace(
                    o,
                    mean=o.mean * (1.0 - rho_o * cfg.roster_mu_regression),
                    var=o.var + rho_o * max(psi0_2 - o.var, 0.0),
                )
                for m, o in team.maps.items()
            }
            if lan is not None:
                lam0_2 = cfg.lan_offset_sd * cfg.lan_offset_sd
                lan = replace(
                    lan,
                    mean=lan.mean * (1.0 - rho_o * cfg.roster_mu_regression),
                    var=lan.var + rho_o * max(lam0_2 - lan.var, 0.0),
                )
        return _Team(overall=overall, maps=maps, lan=lan, lineup=lineup, vol=team.vol)

    def _view(
        self,
        team: _Team,
        map_name: str | None,
        t: datetime | None,
        is_lan: bool,
        *,
        roster_changes: int = 0,
    ) -> _View:
        cfg = self._config
        ov = team.overall
        periods = _periods(ov.last, t, cfg.period_days)
        phi0_2 = cfg.initial_phi * cfg.initial_phi
        phi2_now = min(phi0_2, ov.phi * ov.phi + periods * ov.sigma * ov.sigma)
        map_off: OffsetState | None = None
        if map_name is not None and cfg.map_offset_sd > 0.0:
            map_off = self._inflate(team.maps.get(map_name), cfg.map_offset_sd, cfg.map_offset_drift, t)
        lan_off: OffsetState | None = None
        if is_lan and cfg.lan_offset_sd > 0.0:
            lan_off = self._inflate(team.lan, cfg.lan_offset_sd, cfg.lan_offset_drift, t)
        return _View(
            overall=ov,
            periods=periods,
            phi2_now=phi2_now,
            map_off=map_off,
            lan_off=lan_off,
            roster_changes=roster_changes,
        )

    def _inflate(
        self, off: OffsetState | None, prior_sd: float, drift: float, t: datetime | None
    ) -> OffsetState:
        prior_var = prior_sd * prior_sd
        if off is None:
            return OffsetState(mean=0.0, var=prior_var)
        periods = _periods(off.last, t, self._config.period_days)
        var = min(prior_var, off.var + drift * drift * periods)
        return replace(off, var=var)

    def _update_team(
        self,
        team: _Team,
        own: _View,
        opp: _View,
        s: float,
        weight: float,
        kappa: float,
        t: datetime,
        map_name: str,
    ) -> _Team:
        cfg = self._config
        g_opp = kappa * g(math.sqrt(opp.var_now))
        e = sigmoid(g_opp * (own.theta - opp.theta))
        e = min(max(e, _E_CLIP), 1.0 - _E_CLIP)
        info = weight * g_opp * g_opp * e * (1.0 - e)  # 1 / v
        score = weight * g_opp * (s - e)  # info * Delta
        ov = own.overall
        offset_var = own.offset_var
        phi2_star = own.phi2_now  # min(phi_0^2, phi^2 + c sigma^2)
        total = phi2_star + offset_var
        shrink = 1.0 + total * info
        step = score / shrink  # mean change per unit of prior variance

        phi2_new = max(phi2_star - phi2_star * phi2_star * info / shrink, cfg.min_phi * cfg.min_phi)
        overall = GlickoState(
            mu=ov.mu + phi2_star * step,
            phi=math.sqrt(phi2_new),
            sigma=ov.sigma,
            n=ov.n + 1,
            last=t,
        )
        acc = team.vol
        if acc is None:
            acc = VolatilityPeriod(start=t, phi2=total, inv_v=info, score=score, n=1)
        else:
            acc = replace(acc, inv_v=acc.inv_v + info, score=acc.score + score, n=acc.n + 1)
        maps = team.maps
        if own.map_off is not None:
            mo = own.map_off
            new_map = OffsetState(
                mean=mo.mean + mo.var * step,
                var=max(mo.var - mo.var * mo.var * info / shrink, 1e-12),
                n=mo.n + 1,
                last=t,
            )
            maps = {**team.maps, map_name: new_map}
        lan = team.lan
        if own.lan_off is not None:
            lo = own.lan_off
            lan = OffsetState(
                mean=lo.mean + lo.var * step,
                var=max(lo.var - lo.var * lo.var * info / shrink, 1e-12),
                n=lo.n + 1,
                last=t,
            )
        return _Team(overall=overall, maps=maps, lan=lan, lineup=team.lineup, vol=acc)

    # ----------------------------------------------------------------------------- prediction

    def _check_as_of(self, as_of: datetime) -> datetime:
        t = ensure_utc(as_of)
        if self._last is not None and t <= self._last:
            raise ValueError(
                f"as_of {t.isoformat()} is not after the last processed game {self._last.isoformat()}: "
                "predictions may only use games strictly before as_of (use fit_until(records, as_of), "
                "copy() before updating, or snapshot_before(as_of) on a model fitted with "
                "record_history=True)"
            )
        return t

    def _prediction_view(
        self,
        teams: Mapping[int, _Team],
        team_id: int,
        lineup: Sequence[int],
        map_name: str | None,
        t: datetime | None,
        is_lan: bool,
    ) -> tuple[_View, _Team | None]:
        team = teams.get(team_id)
        if team is None:
            return self._view(self._new_team(), map_name, t, is_lan), None
        team = self._rolled(team, t)
        changes = 0
        if lineup:
            team, changes = self._apply_roster(team, lineup)
        return self._view(team, map_name, t, is_lan, roster_changes=changes), team

    def _estimate(
        self,
        teams: Mapping[int, _Team],
        team1_id: int,
        team2_id: int,
        map_name: str,
        t: datetime,
        is_lan: bool,
        team1_lineup: Sequence[int],
        team2_lineup: Sequence[int],
        picked_by: TeamSlot | None,
    ) -> MapWinEstimate:
        cfg = self._config
        v1, _ = self._prediction_view(teams, team1_id, team1_lineup, map_name, t, is_lan)
        v2, _ = self._prediction_view(teams, team2_id, team2_lineup, map_name, t, is_lan)
        kappa = cfg.lan_kappa(is_lan)
        mean = kappa * (v1.theta - v2.theta)
        var = kappa * kappa * (v1.var_now + v2.var_now)
        p, sd = logistic_normal_moments(mean, var)
        p_overall, _ = logistic_normal_moments(
            kappa * (v1.overall.mu - v2.overall.mu), kappa * kappa * (v1.phi2_now + v2.phi2_now)
        )
        components: dict[str, float] = {
            "overall": p_overall,
            "map": p,
            "logit_mean": mean,
            "logit_sd": math.sqrt(var),
            "team1_phi": math.sqrt(v1.var_now),
            "team2_phi": math.sqrt(v2.var_now),
            "team1_map_weight": self._map_weight(v1.map_off),
            "team2_map_weight": self._map_weight(v2.map_off),
            "team1_map_games": float(v1.map_off.n if v1.map_off is not None else 0),
            "team2_map_games": float(v2.map_off.n if v2.map_off is not None else 0),
            "team1_roster_changes": float(v1.roster_changes),
            "team2_roster_changes": float(v2.roster_changes),
            "lan": 1.0 if is_lan else 0.0,
        }
        return MapWinEstimate(
            map_name=map_name, p_team1_win=p, sigma=sd, picked_by=picked_by, components=components
        )

    def _map_weight(self, off: OffsetState | None) -> float:
        psi0 = self._config.map_offset_sd
        if off is None or psi0 <= 0.0:
            return 0.0
        return min(1.0, max(0.0, 1.0 - off.var / (psi0 * psi0)))

    def predict(
        self,
        team1_id: int,
        team2_id: int,
        map_name: str,
        as_of: datetime,
        *,
        is_lan: bool = False,
        team1_lineup: Sequence[int] = (),
        team2_lineup: Sequence[int] = (),
        picked_by: TeamSlot | None = None,
    ) -> MapWinEstimate:
        """P(team1 wins ``map_name``) using only games strictly before ``as_of``.

        ``team1_lineup``/``team2_lineup`` (player ids), when given and different from the team's
        last known lineup, apply the roster-continuity adjustment hypothetically (no state change).
        Unknown teams get the prior (``p`` near 0.5 when both are unknown, large ``sigma``).
        Raises ``ValueError`` if ``as_of`` is not after the last processed game.
        """
        t = self._check_as_of(as_of)
        return self._estimate(
            self._teams,
            team1_id,
            team2_id,
            normalize_map_name(map_name),
            t,
            is_lan,
            team1_lineup,
            team2_lineup,
            picked_by,
        )

    def map_advantage(
        self, team_id: int, opponent_id: int, map_name: str, as_of: datetime, *, is_lan: bool = False
    ) -> float:
        """Logit of P(``team_id`` beats ``opponent_id`` on ``map_name``) as of ``as_of``.

        Used as the opponent-relative veto term: within one veto step the overall strength
        difference is a constant, so only the map-specific part changes the veto choice.
        Raises ``ValueError`` if ``as_of`` is not after the last processed game.
        """
        est = self.predict(team_id, opponent_id, map_name, as_of, is_lan=is_lan)
        return logit(est.p_team1_win)

    def rating(
        self, team_id: int, map_name: str | None = None, as_of: datetime | None = None
    ) -> RatingSnapshot:
        """Overall (``map_name=None``) or map rating of a team.

        With ``as_of`` the deviation is inflated to that time (which must be after the last
        processed game); without it the snapshot is as of the team's last game.
        """
        t = self._check_as_of(as_of) if as_of is not None else None
        return self._snapshot(self._teams, team_id, map_name, t)

    def _snapshot(
        self, teams: Mapping[int, _Team], team_id: int, map_name: str | None, t: datetime | None
    ) -> RatingSnapshot:
        cfg = self._config
        mname = normalize_map_name(map_name) if map_name is not None else None
        team = teams.get(team_id)
        is_prior = team is None
        team = self._rolled(team, t) if team is not None else self._new_team()
        view = self._view(team, mname, t, False)
        ov = view.overall
        if mname is None:
            mu = ov.mu
            phi = math.sqrt(view.phi2_now)
            return RatingSnapshot(
                team_id=team_id,
                map_name=None,
                mu=mu,
                phi=phi,
                sigma=ov.sigma,
                n_games=ov.n,
                last_played=ov.last,
                r=to_display(mu),
                rd=to_display_rd(phi),
                is_prior=is_prior,
            )
        off = view.map_off
        offset = off.mean if off is not None else 0.0
        offset_var = off.var if off is not None else 0.0
        mu = ov.mu + offset
        phi = math.sqrt(view.phi2_now + offset_var)
        stored = team.maps.get(mname)
        return RatingSnapshot(
            team_id=team_id,
            map_name=mname,
            mu=mu,
            phi=phi,
            sigma=ov.sigma,
            n_games=stored.n if stored is not None else 0,
            last_played=stored.last if stored is not None else None,
            r=to_display(mu),
            rd=to_display_rd(phi),
            map_offset=offset,
            map_offset_sd=math.sqrt(offset_var),
            map_weight=self._map_weight(off) if cfg.map_offset_sd > 0 else 0.0,
            is_prior=is_prior,
        )

    def ratings_table(
        self, map_name: str | None = None, as_of: datetime | None = None
    ) -> list[RatingSnapshot]:
        """Snapshots of every known team, strongest first (dashboard helper)."""
        t = self._check_as_of(as_of) if as_of is not None else None
        rows = [self._snapshot(self._teams, tid, map_name, t) for tid in self._teams]
        return sorted(rows, key=lambda s: s.mu, reverse=True)

    # ------------------------------------------------------------------------- history views

    def _teams_before(self, as_of: datetime, team_ids: Iterable[int] | None = None) -> dict[int, _Team]:
        if self._history is None:
            raise RuntimeError("history was not recorded: fit(..., record_history=True) first")
        out: dict[int, _Team] = {}
        ids = self._history.keys() if team_ids is None else team_ids
        for tid in ids:
            entry = self._history.get(tid)
            if entry is None:
                continue
            times, states = entry
            idx = bisect.bisect_left(times, as_of) - 1
            if idx >= 0:
                out[tid] = states[idx]
        return out

    def snapshot_before(self, as_of: datetime) -> MapRatingModel:
        """A new model holding exactly the information of the games played strictly before ``as_of``.

        Equivalent to ``MapRatingModel(config).fit_until(records, as_of)`` but O(teams * log n).
        Requires ``record_history=True`` at fit time; the snapshot itself records no history.
        """
        t = ensure_utc(as_of)
        out = MapRatingModel(self._config)
        out._teams = self._teams_before(t)
        kept = [entry for entry in self._processed_log if entry[0] < t]
        out._processed_log = kept
        out._processed = {(mid, num) for _, mid, num in kept}
        out._processed_matches = {mid for _, mid, _ in kept}
        out._last = max((e[0] for e in kept), default=None)
        return out

    def historical_map_advantage(
        self, team_id: int, opponent_id: int, map_name: str, as_of: datetime, *, is_lan: bool = False
    ) -> float:
        """Leakage-free :meth:`map_advantage` for any ``as_of`` (uses only games before ``as_of``).

        Requires ``record_history=True`` at fit time. Pass this (bound method) as the
        ``map_advantage`` callable when fitting :class:`cs2bot.models.veto.VetoModel`.
        """
        t = ensure_utc(as_of)
        teams = self._teams_before(t, (team_id, opponent_id))
        est = self._estimate(
            teams, team_id, opponent_id, normalize_map_name(map_name), t, is_lan, (), (), None
        )
        return logit(est.p_team1_win)

    def historical_predict(
        self,
        team1_id: int,
        team2_id: int,
        map_name: str,
        as_of: datetime,
        *,
        is_lan: bool = False,
        team1_lineup: Sequence[int] = (),
        team2_lineup: Sequence[int] = (),
        picked_by: TeamSlot | None = None,
    ) -> MapWinEstimate:
        """Leakage-free :meth:`predict` for any ``as_of`` (requires ``record_history=True``)."""
        t = ensure_utc(as_of)
        teams = self._teams_before(t, (team1_id, team2_id))
        return self._estimate(
            teams,
            team1_id,
            team2_id,
            normalize_map_name(map_name),
            t,
            is_lan,
            team1_lineup,
            team2_lineup,
            picked_by,
        )

    # ------------------------------------------------------------------------------ persistence

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable state (the optional history is not included)."""

        def iso(dt: datetime | None) -> str | None:
            return dt.isoformat() if dt is not None else None

        def off(o: OffsetState | None) -> list[Any] | None:
            return None if o is None else [o.mean, o.var, o.n, iso(o.last)]

        teams: dict[str, Any] = {}
        for tid, team in sorted(self._teams.items()):
            ov = team.overall
            vol = team.vol
            teams[str(tid)] = {
                "overall": [ov.mu, ov.phi, ov.sigma, ov.n, iso(ov.last)],
                "maps": {m: off(o) for m, o in sorted(team.maps.items())},
                "lan": off(team.lan),
                "lineup": list(team.lineup),
                "vol": None if vol is None else [iso(vol.start), vol.phi2, vol.inv_v, vol.score, vol.n],
            }
        return {
            "kind": _FORMAT_KIND,
            "version": _FORMAT_VERSION,
            "config": self._config.model_dump(mode="json"),
            "last_processed": iso(self._last),
            "processed": [[iso(t), mid, num] for t, mid, num in self._processed_log],
            "teams": teams,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MapRatingModel:
        if data.get("kind") != _FORMAT_KIND:
            raise ValueError("not a MapRatingModel payload")
        if int(data.get("version", 0)) != _FORMAT_VERSION:
            raise ValueError(f"unsupported MapRatingModel payload version {data.get('version')}")

        def dt(value: str | None) -> datetime | None:
            return ensure_utc(datetime.fromisoformat(value)) if value else None

        def off(raw: Sequence[Any] | None) -> OffsetState | None:
            if raw is None:
                return None
            return OffsetState(mean=float(raw[0]), var=float(raw[1]), n=int(raw[2]), last=dt(raw[3]))

        def vol(raw: Sequence[Any] | None) -> VolatilityPeriod | None:
            if raw is None:
                return None
            start = dt(raw[0])
            if start is None:
                return None
            return VolatilityPeriod(
                start=start, phi2=float(raw[1]), inv_v=float(raw[2]), score=float(raw[3]), n=int(raw[4])
            )

        model = cls(RatingConfig.model_validate(data["config"]))
        for tid, raw in data.get("teams", {}).items():
            o = raw["overall"]
            maps = {str(m): off(v) for m, v in raw.get("maps", {}).items()}
            model._teams[int(tid)] = _Team(
                overall=GlickoState(
                    mu=float(o[0]), phi=float(o[1]), sigma=float(o[2]), n=int(o[3]), last=dt(o[4])
                ),
                maps={m: v for m, v in maps.items() if v is not None},
                lan=off(raw.get("lan")),
                lineup=tuple(int(p) for p in raw.get("lineup", [])),
                vol=vol(raw.get("vol")),
            )
        for t_raw, mid, num in data.get("processed", []):
            t = dt(t_raw)
            if t is None:
                continue
            model._processed_log.append((t, int(mid), int(num)))
            model._processed.add((int(mid), int(num)))
            model._processed_matches.add(int(mid))
        model._last = dt(data.get("last_processed"))
        return model

    def save(self, path: str | Path) -> Path:
        """Write the state as JSON (parent directories are created). Returns the path."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), separators=(",", ":")), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> MapRatingModel:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @staticmethod
    def tune(
        records: Iterable[MapResultRecord],
        grid: Mapping[str, Sequence[Any]],
        *,
        holdout_fraction: float = 0.25,
        base_config: RatingConfig | None = None,
    ) -> tuple[RatingConfig, dict[str, Any]]:
        """Alias of :func:`cs2bot.models.ratings.tune`."""
        return tune(records, grid, holdout_fraction=holdout_fraction, base_config=base_config)


# ------------------------------------------------------------------------- walk-forward evaluation


@dataclass(frozen=True, slots=True)
class WalkForwardPrediction:
    """One out-of-sample prediction made at the start of its match, before any of its maps."""

    match_id: int
    map_number: int
    map_name: str
    as_of: datetime
    team1_id: int
    team2_id: int
    p_team1_win: float
    sigma: float
    p_overall: float
    team1_won: bool


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    lower: float
    upper: float
    n: int
    mean_predicted: float
    observed_rate: float


@dataclass(frozen=True)
class WalkForwardResult:
    """Out-of-sample predictions with standard scoring rules."""

    predictions: tuple[WalkForwardPrediction, ...]

    @property
    def n(self) -> int:
        return len(self.predictions)

    def _probs(self, overall: bool = False) -> list[tuple[float, float]]:
        return [
            (
                min(max(p.p_overall if overall else p.p_team1_win, 1e-12), 1 - 1e-12),
                1.0 if p.team1_won else 0.0,
            )
            for p in self.predictions
        ]

    def log_loss_of(self, *, overall: bool = False) -> float:
        rows = self._probs(overall)
        if not rows:
            return math.nan
        return -sum(y * math.log(p) + (1 - y) * math.log(1 - p) for p, y in rows) / len(rows)

    def brier_of(self, *, overall: bool = False) -> float:
        rows = self._probs(overall)
        if not rows:
            return math.nan
        return sum((p - y) ** 2 for p, y in rows) / len(rows)

    @property
    def log_loss(self) -> float:
        return self.log_loss_of()

    @property
    def brier(self) -> float:
        return self.brier_of()

    @property
    def accuracy(self) -> float:
        if not self.predictions:
            return math.nan
        return sum((p.p_team1_win > 0.5) == p.team1_won for p in self.predictions) / self.n

    def reliability(self, n_bins: int = 10) -> list[ReliabilityBin]:
        """Equal-width reliability bins over ``[0, 1]`` (empty bins are omitted)."""
        buckets: dict[int, list[tuple[float, float]]] = defaultdict(list)
        for p, y in self._probs():
            buckets[min(int(p * n_bins), n_bins - 1)].append((p, y))
        out: list[ReliabilityBin] = []
        for b in sorted(buckets):
            rows = buckets[b]
            out.append(
                ReliabilityBin(
                    lower=b / n_bins,
                    upper=(b + 1) / n_bins,
                    n=len(rows),
                    mean_predicted=sum(p for p, _ in rows) / len(rows),
                    observed_rate=sum(y for _, y in rows) / len(rows),
                )
            )
        return out

    def ece(self, n_bins: int = 10) -> float:
        """Expected calibration error (sample-weighted mean |predicted - observed| over bins)."""
        bins = self.reliability(n_bins)
        total = sum(b.n for b in bins)
        if total == 0:
            return math.nan
        return sum(b.n * abs(b.mean_predicted - b.observed_rate) for b in bins) / total


def evaluate_walk_forward(model: MapRatingModel, records: Iterable[MapResultRecord]) -> WalkForwardResult:
    """Walk forward through ``records``: predict every match at its start, then learn from it.

    Events are processed in time order; each match's maps are predicted at ``as_of`` = the earliest
    ``played_at`` of the match (before that match's first map is processed, so a later map never
    uses an earlier map of the same match), and every map updates the model at its own
    ``played_at`` (matches may overlap in time). Matches the model has already (partly) processed
    are only used for updating. ``model`` is mutated.
    """
    recs = sorted(records, key=_record_sort_key)
    by_match: dict[int, list[MapResultRecord]] = defaultdict(list)
    for r in recs:
        by_match[r.match_id].append(r)
    # (time, kind, sequence, payload): kind 0 = predict (before updates at the same instant), 1 = update
    events: list[tuple[datetime, int, int, int]] = []
    for mid, maps in by_match.items():
        if not model.has_processed_match(mid):
            events.append((ensure_utc(maps[0].played_at), 0, mid, -1))
    for i, r in enumerate(recs):
        events.append((ensure_utc(r.played_at), 1, r.match_id, i))
    events.sort(key=lambda e: (e[0], e[1], e[2], e[3]))
    preds: list[WalkForwardPrediction] = []
    for t, kind, mid, idx in events:
        if kind == 1:
            model.update(recs[idx])
            continue
        for r in by_match[mid]:
            if r.team1_rounds == r.team2_rounds or r.team1_id == r.team2_id:
                continue
            est = model.predict(
                r.team1_id,
                r.team2_id,
                r.map_name,
                t,
                is_lan=r.is_lan,
                team1_lineup=r.team1_lineup,
                team2_lineup=r.team2_lineup,
            )
            preds.append(
                WalkForwardPrediction(
                    match_id=r.match_id,
                    map_number=r.map_number,
                    map_name=est.map_name,
                    as_of=t,
                    team1_id=r.team1_id,
                    team2_id=r.team2_id,
                    p_team1_win=est.p_team1_win,
                    sigma=est.sigma,
                    p_overall=est.components.get("overall", est.p_team1_win),
                    team1_won=r.team1_rounds > r.team2_rounds,
                )
            )
    return WalkForwardResult(predictions=tuple(preds))


def chronological_split(
    records: Iterable[MapResultRecord], holdout_fraction: float = 0.25
) -> tuple[list[MapResultRecord], list[MapResultRecord]]:
    """Split records into (train, holdout) at the start of the first holdout *match*.

    The last ``holdout_fraction`` of matches (by start time) form the holdout; train holds every
    map played strictly before the split instant, holdout everything at or after it.
    """
    if not 0.0 < holdout_fraction < 1.0:
        raise ValueError("holdout_fraction must be in (0, 1)")
    recs = sorted(records, key=_record_sort_key)
    starts: dict[int, datetime] = {}
    for r in recs:
        starts.setdefault(r.match_id, ensure_utc(r.played_at))
    if len(starts) < 2:
        raise ValueError("need at least two matches to split")
    ordered = sorted(starts.values())
    n_hold = min(len(ordered) - 1, max(1, round(len(ordered) * holdout_fraction)))
    split = ordered[len(ordered) - n_hold]
    train = [r for r in recs if ensure_utc(r.played_at) < split]
    hold = [r for r in recs if ensure_utc(r.played_at) >= split]
    return train, hold


def tune(
    records: Iterable[MapResultRecord],
    grid: Mapping[str, Sequence[Any]],
    *,
    holdout_fraction: float = 0.25,
    base_config: RatingConfig | None = None,
) -> tuple[RatingConfig, dict[str, Any]]:
    """Grid search minimising walk-forward log-loss on a chronological holdout.

    Every combination of ``grid`` values (``RatingConfig`` field -> candidate values) is applied on
    top of ``base_config``; the model is fitted on the training part and evaluated with
    :func:`evaluate_walk_forward` on the holdout (predict at match start, then update). Returns the
    best config and a JSON-friendly report: ``best_params``, ``best_log_loss``, ``best_brier``,
    ``baseline_log_loss`` (ln 2, the coin flip), ``n_train``/``n_holdout`` (maps) and ``results``
    (one entry per combination, sorted best first).
    """
    base = base_config or RatingConfig()
    fields = set(RatingConfig.model_fields)
    unknown = sorted(set(grid) - fields)
    if unknown:
        raise ValueError(f"unknown RatingConfig fields in grid: {unknown}")
    recs = sorted(records, key=_record_sort_key)
    train, hold = chronological_split(recs, holdout_fraction)
    keys = list(grid)
    combos = list(itertools.product(*(list(grid[k]) for k in keys))) if keys else [()]
    results: list[dict[str, Any]] = []
    best_cfg = base
    best_ll = math.inf
    for combo in combos:
        params = dict(zip(keys, combo, strict=True))
        cfg = RatingConfig.model_validate({**base.model_dump(), **params})
        model = MapRatingModel(cfg).fit(train)
        wf = evaluate_walk_forward(model, hold)
        ll = wf.log_loss
        results.append({"params": params, "log_loss": ll, "brier": wf.brier, "n": wf.n})
        if ll < best_ll:
            best_ll = ll
            best_cfg = cfg
    results.sort(key=lambda row: row["log_loss"])
    report: dict[str, Any] = {
        "best_params": results[0]["params"] if results else {},
        "best_log_loss": results[0]["log_loss"] if results else math.nan,
        "best_brier": results[0]["brier"] if results else math.nan,
        "baseline_log_loss": math.log(2.0),
        "n_train": len(train),
        "n_holdout": len(hold),
        "results": results,
    }
    return best_cfg, report
