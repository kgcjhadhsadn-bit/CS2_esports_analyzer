"""Feature rows per (match, map, team1-vs-team2): training matrix and live features.

A row is a flat ``dict[str, float]`` (NaN = not available) made of:

* ``t1_<name>`` / ``t2_<name>``: every single-team feature of :meth:`FeatureStore.team_features`
  for team1 and team2, and ``diff_<name>`` = team1 minus team2 (NaN when either side is NaN);
* matchup features: head-to-head and the strength proxy (:meth:`FeatureStore.matchup_features`);
* match context: ``is_lan``, ``tier_rank``, ``best_of``, the map pick indicator
  (``pick_team1`` / ``pick_team2`` / ``pick_diff``; all 0 for the decider *or an unknown pick* - the
  record type cannot tell these apart, so live predictions made before the veto look exactly like
  deciders did in training) and, optionally, ``team1_starts_ct``;
* map and patch meta: :meth:`FeatureStore.map_features`, :meth:`FeatureStore.meta_features`;
* rating features when a :class:`RatingProvider` is given: ``rating_p``, ``rating_logit``,
  ``rating_sigma`` and one ``rating_c_<component>`` per numeric entry of
  ``MapWinEstimate.components``.

Leakage: every training row is computed as of the *start of its match* (the earliest ``played_at``
of the match's maps) and the match itself is excluded, so map 2 of a series never sees map 1 of the
same series - the bot bets before the match starts.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Final, Literal, Protocol, runtime_checkable

import numpy as np
import pandas as pd

from cs2bot.domain.enums import MatchFormat, Side, TeamSlot, Tier, VetoAction
from cs2bot.domain.match import MatchContext
from cs2bot.domain.predictions import MapWinEstimate
from cs2bot.domain.records import MapResultRecord
from cs2bot.features.store import NAN, FeatureStore
from cs2bot.utils.names import normalize_map_name
from cs2bot.utils.time import ensure_utc

#: Ordinal encoding of the tournament tier (higher = stronger field). UNRANKED is "unknown" -> NaN.
TIER_RANK: Final[dict[Tier, float]] = {Tier.S: 5.0, Tier.A: 4.0, Tier.B: 3.0, Tier.C: 2.0, Tier.D: 1.0}
#: The feature the ensemble puts a monotone-increasing constraint on.
RATING_LOGIT_FEATURE: Final[str] = "rating_logit"
META_COLUMNS: Final[tuple[str, ...]] = (
    "match_id",
    "map_number",
    "map_name",
    "as_of",
    "played_at",
    "team1_id",
    "team2_id",
    "mirrored",
)
_P_CLIP: Final[float] = 1e-6


@runtime_checkable
class RatingProvider(Protocol):
    """Anything that estimates P(team1 wins a map) from games strictly before ``as_of``.

    ``cs2bot.models.ratings.MapRatingModel`` satisfies it. The provider is responsible for being
    leakage-free at the ``as_of`` it is asked for. Providers that also expose
    ``historical_predict(team1_id, team2_id, map_name, as_of, *, is_lan=False)`` (a rating model
    fitted with ``record_history=True``) are queried through that method, which answers for any past
    ``as_of`` - that is what :func:`build_training_rows` needs.
    """

    def predict(
        self, team1_id: int, team2_id: int, map_name: str, as_of: datetime, *, is_lan: bool = False
    ) -> MapWinEstimate: ...


def rating_estimate(
    provider: RatingProvider, team1_id: int, team2_id: int, map_name: str, as_of: datetime, *, is_lan: bool
) -> MapWinEstimate:
    """Ask the provider, preferring its history-aware entry point when it has one.

    ``MapRatingModel.predict`` refuses an ``as_of`` that is not after its last processed game
    (ValueError) - by design, to make leakage impossible. For training rows fit the model with
    ``record_history=True``; its ``historical_predict`` is used automatically. The error is not
    swallowed here: silently missing rating features would be worse than a loud failure.
    """
    historical = getattr(provider, "historical_predict", None)
    if callable(historical) and bool(getattr(provider, "records_history", True)):
        estimate = historical(team1_id, team2_id, map_name, as_of, is_lan=is_lan)
        if isinstance(estimate, MapWinEstimate):
            return estimate
    return provider.predict(team1_id, team2_id, map_name, as_of, is_lan=is_lan)


def rating_features(estimate: MapWinEstimate) -> dict[str, float]:
    """``rating_p``, ``rating_logit``, ``rating_sigma`` and ``rating_c_<component>`` values."""
    p = min(max(float(estimate.p_team1_win), _P_CLIP), 1.0 - _P_CLIP)
    out = {
        "rating_p": p,
        RATING_LOGIT_FEATURE: math.log(p / (1.0 - p)),
        "rating_sigma": float(estimate.sigma),
    }
    for name, value in estimate.components.items():
        try:
            out[f"rating_c_{name}"] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def match_map_features(
    store: FeatureStore,
    *,
    team1_id: int,
    team2_id: int,
    map_name: str,
    as_of: datetime,
    team1_lineup: Sequence[int] = (),
    team2_lineup: Sequence[int] = (),
    team1_standins: int | None = None,
    team2_standins: int | None = None,
    is_lan: bool = False,
    tier: Tier = Tier.UNRANKED,
    fmt: MatchFormat | None = None,
    picked_by: TeamSlot | None = None,
    team1_start_side: Side | None = None,
    exclude_match_id: int | None = None,
    rating_provider: RatingProvider | None = None,
) -> dict[str, float]:
    """The feature row of one map between two teams, using only data strictly before ``as_of``.

    This is the single code path behind :func:`build_training_rows` and
    :func:`features_for_match_map`, so training and live rows cannot drift apart. ``picked_by=None``
    means decider or unknown. ``team1_start_side`` is only used when
    ``store.config.use_start_side`` is on and the map was picked (see ``FeatureConfig``).
    """
    map_name = normalize_map_name(map_name)
    # cut at the start of the excluded match (see FeatureStore.cutoff): the meta and rating features
    # must be as of the same instant as the store features
    as_of = store.cutoff(as_of, exclude_match_id)
    f1 = store.team_features(
        team1_id,
        map_name,
        as_of,
        lineup=team1_lineup,
        standins=team1_standins,
        exclude_match_id=exclude_match_id,
    )
    f2 = store.team_features(
        team2_id,
        map_name,
        as_of,
        lineup=team2_lineup,
        standins=team2_standins,
        exclude_match_id=exclude_match_id,
    )
    out: dict[str, float] = {}
    for name, value in f1.items():
        out[f"t1_{name}"] = value
    for name, value in f2.items():
        out[f"t2_{name}"] = value
    for name, value in f1.items():
        out[f"diff_{name}"] = value - f2[name]  # NaN propagates
    out.update(store.matchup_features(team1_id, team2_id, map_name, as_of, exclude_match_id=exclude_match_id))

    out["is_lan"] = 1.0 if is_lan else 0.0
    out["tier_rank"] = TIER_RANK.get(tier, NAN)
    out["best_of"] = float(fmt.n_maps) if fmt is not None else NAN
    out["pick_team1"] = 1.0 if picked_by is TeamSlot.TEAM1 else 0.0
    out["pick_team2"] = 1.0 if picked_by is TeamSlot.TEAM2 else 0.0
    out["pick_diff"] = out["pick_team1"] - out["pick_team2"]
    if store.config.use_start_side:
        known = picked_by is not None and team1_start_side is not None
        out["team1_starts_ct"] = (1.0 if team1_start_side is Side.CT else 0.0) if known else NAN
    out.update(store.map_features(map_name, as_of, exclude_match_id=exclude_match_id))
    out.update(store.meta_features(as_of))
    if rating_provider is not None:
        estimate = rating_estimate(rating_provider, team1_id, team2_id, map_name, as_of, is_lan=is_lan)
        out.update(rating_features(estimate))
    return out


def _picker_from_veto(ctx: MatchContext, map_name: str) -> TeamSlot | None:
    for step in ctx.known_veto:
        if normalize_map_name(step.map_name) == map_name:
            return step.team if step.action is VetoAction.PICK else None
    return None


def features_for_match_map(
    ctx: MatchContext,
    map_name: str,
    *,
    store: FeatureStore,
    as_of: datetime,
    rating_provider: RatingProvider | None = None,
    picked_by: TeamSlot | Literal["auto"] | None = "auto",
) -> dict[str, float]:
    """Features of ``map_name`` for the match described by ``ctx``, as of ``as_of``.

    * ``picked_by="auto"`` reads the pick from ``ctx.known_veto`` (a PICK step on this map ->
      that team; leftover or map not in the veto -> ``None``). Pass a ``TeamSlot`` / ``None`` to
      evaluate a hypothetical veto outcome (``MapSequenceProbability.pickers``).
    * Lineups, stand-in counts, LAN, tier and format come from ``ctx``; a team without a lineup in
      ``ctx`` falls back to its last fielded lineup inside the store (``lineup_known = 0``).
    * The match itself is never used: ``as_of`` is clamped to the start of the match when the store
      already holds its maps (backtests over finished matches), and its records are excluded.
    """
    name = normalize_map_name(map_name)
    t = store.cutoff(as_of, ctx.match_id)
    picker = _picker_from_veto(ctx, name) if picked_by == "auto" else picked_by
    lineup1 = [p.player_id for p in ctx.team1_lineup]
    lineup2 = [p.player_id for p in ctx.team2_lineup]
    return match_map_features(
        store,
        team1_id=ctx.team1_id,
        team2_id=ctx.team2_id,
        map_name=name,
        as_of=t,
        team1_lineup=lineup1,
        team2_lineup=lineup2,
        team1_standins=sum(1 for p in ctx.team1_lineup if p.is_standin) if lineup1 else None,
        team2_standins=sum(1 for p in ctx.team2_lineup if p.is_standin) if lineup2 else None,
        is_lan=ctx.is_lan,
        tier=ctx.tier,
        fmt=ctx.format,
        picked_by=picker if isinstance(picker, TeamSlot) else None,
        team1_start_side=ctx.start_sides.get(name),
        exclude_match_id=ctx.match_id,
        rating_provider=rating_provider,
    )


def _mirrored(rec: MapResultRecord) -> MapResultRecord:
    """The same map seen with team1 and team2 swapped."""
    return MapResultRecord(
        match_id=rec.match_id,
        map_number=rec.map_number,
        map_name=rec.map_name,
        played_at=rec.played_at,
        team1_id=rec.team2_id,
        team2_id=rec.team1_id,
        team1_rounds=rec.team2_rounds,
        team2_rounds=rec.team1_rounds,
        team1_start_side=rec.team1_start_side.other if rec.team1_start_side is not None else None,
        team1_ct_rounds=rec.team2_ct_rounds,
        team1_t_rounds=rec.team2_t_rounds,
        team2_ct_rounds=rec.team1_ct_rounds,
        team2_t_rounds=rec.team1_t_rounds,
        overtime_rounds=rec.overtime_rounds,
        picked_by=rec.picked_by.other if rec.picked_by is not None else None,
        is_lan=rec.is_lan,
        tier=rec.tier,
        format=rec.format,
        team1_lineup=rec.team2_lineup,
        team2_lineup=rec.team1_lineup,
    )


def build_training_rows(
    map_results: Iterable[MapResultRecord],
    *,
    store: FeatureStore,
    rating_provider: RatingProvider | None = None,
    mirror: bool = False,
) -> tuple[pd.DataFrame, np.ndarray, pd.DataFrame]:
    """Feature matrix, labels and row metadata for finished maps.

    Returns ``(X, y, meta)`` with aligned rows in chronological order:

    * ``X``: float DataFrame, one column per feature (NaN = unavailable);
    * ``y``: ``1.0`` when team1 won the map, else ``0.0``;
    * ``meta``: ``match_id, map_number, map_name, as_of, played_at, team1_id, team2_id, mirrored``.

    ``as_of`` of a row is the start of its match: the earliest ``played_at`` among the match's maps
    in ``map_results`` and in the store. The store may hold more (also later) history than
    ``map_results`` - queries never read at or past ``as_of``. A ``rating_provider`` must be able to
    answer at those past timestamps (see :class:`RatingProvider`).

    ``mirror=True`` adds, right after each row, the same map with the teams swapped (label flipped,
    ``meta.mirrored = True``): it doubles a small training set and removes any team1/team2 order
    bias. Keep both rows of a map in the same cross-validation fold (group by ``match_id``).
    Maps without a winner (equal rounds) are skipped.
    """
    records = sorted(map_results, key=lambda r: (ensure_utc(r.played_at), r.match_id, r.map_number))
    starts: dict[int, datetime] = {}
    for rec in records:
        t = ensure_utc(rec.played_at)
        known = starts.get(rec.match_id)
        if known is None:
            known = store.match_start(rec.match_id)
        starts[rec.match_id] = t if known is None or t < known else known

    rows: list[dict[str, float]] = []
    labels: list[float] = []
    meta: list[dict[str, Any]] = []
    for rec in records:
        if rec.team1_rounds == rec.team2_rounds:
            continue
        as_of = starts[rec.match_id]
        for view, mirrored in ((rec, False), (_mirrored(rec), True)) if mirror else ((rec, False),):
            rows.append(
                match_map_features(
                    store,
                    team1_id=view.team1_id,
                    team2_id=view.team2_id,
                    map_name=view.map_name,
                    as_of=as_of,
                    team1_lineup=view.team1_lineup,
                    team2_lineup=view.team2_lineup,
                    team1_standins=store.standin_count(view.match_id, view.team1_id),
                    team2_standins=store.standin_count(view.match_id, view.team2_id),
                    is_lan=view.is_lan,
                    tier=view.tier,
                    fmt=view.format,
                    picked_by=view.picked_by,
                    team1_start_side=view.team1_start_side,
                    exclude_match_id=view.match_id,
                    rating_provider=rating_provider,
                )
            )
            labels.append(1.0 if view.team1_rounds > view.team2_rounds else 0.0)
            meta.append(
                {
                    "match_id": view.match_id,
                    "map_number": view.map_number,
                    "map_name": normalize_map_name(view.map_name),
                    "as_of": as_of,
                    "played_at": ensure_utc(view.played_at),
                    "team1_id": view.team1_id,
                    "team2_id": view.team2_id,
                    "mirrored": mirrored,
                }
            )
    x = pd.DataFrame(rows, dtype=float) if rows else pd.DataFrame()
    meta_frame = pd.DataFrame(meta, columns=list(META_COLUMNS))
    return x, np.asarray(labels, dtype=float), meta_frame
