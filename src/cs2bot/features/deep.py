"""Deep match factors (deep_data): schedule load, travel, stakes, format specialists, economy and
clutch detail, player match-ups, coach changes, map-pool recency and the betting market.

:class:`DeepFeatureIndex` is built once per :class:`~cs2bot.features.store.FeatureStore` (from the
store's map results and round records, plus an optional :class:`~cs2bot.datasets.deep.DeepData`) and
queried by :func:`cs2bot.features.rows.match_map_features` - the single code path of training and live
rows - so every training row and every live row get exactly the same definitions.

Leakage rules (same as the store)
---------------------------------
* A query at ``as_of`` reads results (maps, rounds, kills, clutches, market snapshots, coach changes)
  strictly before ``as_of``; ``exclude_match_id`` removes the match being predicted (and the store has
  already cut ``as_of`` at that match's start).
* The match being predicted contributes only **static pre-match facts**: its scheduled time, format,
  stage text, tournament and venue (public before the match), and the market prices captured before
  ``as_of``.
* Schedule features of *other* matches use the public schedule: matches scheduled before the start of
  the match being predicted (its ``scheduled_at``), results only when played before ``as_of``.
* Train / live parity (review round 1): training rows are computed at kick-off, live rows hours before.
  So the schedule windows ("3 / 7 days before", "the match day", rest days) are anchored to the
  match's *scheduled start* rather than to ``as_of``, a series counts as won / lost only once it is
  complete (a live match already has its first maps stored), and the market block of a kick-off row
  reads the prices a per-match lead before the start (``DeepConfig.market_lead_hours``), as the live
  bot does - never the closing line it cannot see when it bets.

Column names
------------
Team features (computed for each team; ``match_map_features`` adds them as ``diff_dp_*`` = team1 minus
team2, and also as ``t1_dp_*`` / ``t2_dp_*`` with ``DeepConfig.team_columns="all"``) start with
``dp_``; match-level features start with ``dp_`` (stage, match-ups, map pool) or ``mkt_`` / ``sharp_``
(market). NaN = not available; counts are 0 when nothing happened.

Team features (windows in days)
* schedule / fatigue: ``dp_maps_3d``, ``dp_maps_7d``, ``dp_matches_3d``, ``dp_matches_7d`` (series
  in the window before the scheduled start; a series is placed at the start of its last map),
  ``dp_rest_days`` (from the last map of the previous series to the scheduled start),
  ``dp_play_streak_days`` (consecutive previous UTC days with a series), ``dp_same_day_series`` (series
  of this team earlier the same UTC day as the match: played ones, or scheduled ones from the metadata),
  ``dp_maps_today``;
* travel: ``dp_tz_shift`` (hours between the venue and the team's home; 0 online), ``dp_away_region``,
  ``dp_first_after_travel`` (first match at a new venue after playing elsewhere), ``dp_days_at_event``;
* stakes inside the event: ``dp_event_wins`` / ``dp_event_losses`` (series before this one in the same
  tournament), ``dp_swiss_elimination`` / ``dp_swiss_qualification`` (Swiss stage only, else 0);
* format specialists (365 d): ``dp_bo1_series_win``, ``dp_bo3_series_win``, ``dp_bo1_minus_bo3``,
  ``dp_bo3_map_win``, ``dp_decider_win``, ``dp_decider_maps``,
  ``dp_pick_edge`` (own-pick map win rate minus overall map win rate),
  ``dp_opp_pick_edge`` (win rate on the opponent's picks minus overall);
* economy (180 d, rounds): ``dp_pistol_ct_win``, ``dp_pistol_t_win``, ``dp_pistol_conversion``
  (rounds 2-3 won after winning the pistol), ``dp_pistol_recovery`` (rounds 2-3 won after losing it),
  ``dp_force_win`` / ``dp_eco_win`` (own force / eco buy vs a full buy), ``dp_lost_to_eco`` /
  ``dp_lost_to_force`` (full buy beaten by an eco / force: upsets conceded);
  on this map (365 d): ``dp_map_post_plant_win``, ``dp_map_retake_win``;
* clutch (180 d, demos): ``dp_clutch_win``, ``dp_clutch_1v1_win``, ``dp_clutch_1vx_win``,
  ``dp_clutches_per_map``;
* lineup duels (180 d, demos): ``dp_open_ct_win``, ``dp_open_t_win``, ``dp_map_open_ct_win``,
  ``dp_map_open_t_win`` (365 d, this map), ``dp_entry_open_t_win`` (the lineup's main T-side opener),
  ``dp_trade_eff`` (share of the lineup's deaths that were traded), ``dp_awper_awp_duel_win``
  (AWP-vs-AWP duels of the lineup's AWPer), ``dp_awper_kill_share``;
* roster: ``dp_days_since_coach_change``, ``dp_coach_change_90d``.

Match features (team1 point of view)
* ``stage_*`` / ``stakes_level`` (:mod:`cs2bot.features.stakes`), ``dp_bo_mismatch`` none;
* ``dp_awp_h2h_share`` / ``dp_awp_h2h_n`` (kills between the two AWPers, any map, 365 d),
  ``dp_lineup_h2h_share`` / ``dp_lineup_h2h_n`` (kills between the two lineups on this map, 365 d),
  ``dp_area_open_edge`` (opening duels by map area: team1's T side vs team2's CT side in the same
  places, minus the reverse, 365 d);
* ``dp_map_days_in_pool`` (days since the map (re-)entered Active Duty; new maps are less predictable);
* market: :data:`cs2bot.odds.movement.MARKET_FEATURE_NAMES` (Betclic match-winner line movement and
  the optional sharp benchmark) read at :meth:`DeepFeatureIndex.market_reference`, plus
  ``mkt_lead_hours`` (hours between that moment and the scheduled start; NaN without a quote). The
  block is NaN when the match metadata cannot tell the orientation (prices are stored in the match's
  team1 / team2 order).
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from cs2bot.domain.enums import BuyType, MatchFormat, Side, TeamSlot
from cs2bot.domain.records import MapResultRecord, RoundRecord
from cs2bot.features.geo import Place, parse_location, team_home, tz_shift_hours
from cs2bot.features.stakes import STAGE_FEATURES, classify_stage, swiss_situation
from cs2bot.odds.movement import MARKET_FEATURE_NAMES, market_features
from cs2bot.utils.names import normalize_map_name
from cs2bot.utils.time import ensure_utc

if TYPE_CHECKING:
    from cs2bot.datasets.deep import DeepData, MatchMetaRecord
    from cs2bot.features.meta import PatchTimeline

NAN: Final[float] = math.nan
_DAY: Final[float] = 86_400.0
#: A query this close to (or after) the scheduled start is a "kick-off row" (training / backtest).
_KICKOFF_BAND_S: Final[float] = 300.0
#: A scheduled start further ahead than this is not trusted as the schedule anchor (stale data).
_MAX_ANCHOR_AHEAD_S: Final[float] = 7 * 86_400.0

__all__ = [
    "DEEP_MATCH_FEATURES",
    "DEEP_TEAM_FEATURES",
    "DeepConfig",
    "DeepFeatureIndex",
    "deep_feature_names",
]

DEEP_TEAM_FEATURES: Final[tuple[str, ...]] = (
    "dp_maps_3d",
    "dp_maps_7d",
    "dp_matches_3d",
    "dp_matches_7d",
    "dp_rest_days",
    "dp_play_streak_days",
    "dp_same_day_series",
    "dp_maps_today",
    "dp_tz_shift",
    "dp_away_region",
    "dp_first_after_travel",
    "dp_days_at_event",
    "dp_event_wins",
    "dp_event_losses",
    "dp_swiss_elimination",
    "dp_swiss_qualification",
    "dp_bo1_series_win",
    "dp_bo3_series_win",
    "dp_bo1_minus_bo3",
    "dp_bo3_map_win",
    "dp_decider_win",
    "dp_decider_maps",
    "dp_pick_edge",
    "dp_opp_pick_edge",
    "dp_pistol_ct_win",
    "dp_pistol_t_win",
    "dp_pistol_conversion",
    "dp_pistol_recovery",
    "dp_force_win",
    "dp_eco_win",
    "dp_lost_to_eco",
    "dp_lost_to_force",
    "dp_map_post_plant_win",
    "dp_map_retake_win",
    "dp_clutch_win",
    "dp_clutch_1v1_win",
    "dp_clutch_1vx_win",
    "dp_clutches_per_map",
    "dp_open_ct_win",
    "dp_open_t_win",
    "dp_map_open_ct_win",
    "dp_map_open_t_win",
    "dp_entry_open_t_win",
    "dp_trade_eff",
    "dp_awper_awp_duel_win",
    "dp_awper_kill_share",
    "dp_days_since_coach_change",
    "dp_coach_change_90d",
)
DEEP_MATCH_FEATURES: Final[tuple[str, ...]] = (
    *STAGE_FEATURES,
    "dp_awp_h2h_share",
    "dp_awp_h2h_n",
    "dp_lineup_h2h_share",
    "dp_lineup_h2h_n",
    "dp_area_open_edge",
    "dp_map_days_in_pool",
    *MARKET_FEATURE_NAMES,
    "mkt_lead_hours",
)


@dataclass(frozen=True)
class DeepConfig:
    """Windows (days) and minimum samples of the deep features."""

    format_window_days: int = 365
    economy_window_days: int = 180
    map_round_window_days: int = 365
    clutch_window_days: int = 180
    duel_window_days: int = 180
    map_duel_window_days: int = 365
    h2h_window_days: int = 365
    min_open_attempts: int = 10  # opening duels a player needs to count as the lineup's entry
    min_awp_kills: int = 30  # kills a player needs before his AWP share identifies the AWPer
    min_awper_share: float = 0.25  # AWP share of kills that makes a player "the AWPer"
    swiss_wins_needed: int = 3
    swiss_losses_needed: int = 3
    with_market: bool = True
    #: Which team columns enter the row: "diff" (default) = only ``diff_dp_*`` (team1 minus team2),
    #: "all" = also ``t1_dp_*`` / ``t2_dp_*``. Differences carry the matchup signal; the per-team
    #: levels triple the column count, which costs more in noise than it gains on small data sets.
    team_columns: Literal["diff", "all"] = "diff"
    #: Hours before the scheduled start at which the market block of a *kick-off row* (a training or
    #: backtest row, computed at the start of a finished match) reads the prices: a deterministic
    #: pseudo-random lead per match, uniform in ``[lo, hi]``. Live rows read the prices on offer at
    #: ``as_of`` (the bot bets 10 min .. 48 h before the start, ``DecisionSettings``). Without this the
    #: model would learn from (nearly) closing prices and meet earlier, noisier prices live - an
    #: over-confident use of the market exactly where a confident wrong bet costs most. The lead is
    #: also a feature (``mkt_lead_hours``) so the model can learn how much a price is worth by its age.
    #: ``None`` = read at ``as_of`` (kick-off) in training too.
    market_lead_hours: tuple[float, float] | None = (0.25, 48.0)

    def __post_init__(self) -> None:
        lead = self.market_lead_hours
        if lead is not None and not (0.0 <= lead[0] <= lead[1]):
            raise ValueError("market_lead_hours must be (lo, hi) with 0 <= lo <= hi")


# ------------------------------------------------------------------------------------- helpers


class _Cum:
    """Time-sorted rows with prefix sums of named numeric columns (as in the store's ``_Series``)."""

    __slots__ = ("cum", "match_id", "t")

    def __init__(self, rows: Sequence[tuple[float, int, Sequence[float]]], names: Sequence[str]):
        rows = sorted(rows, key=lambda r: (r[0], r[1]))
        self.t = [r[0] for r in rows]
        self.match_id = [r[1] for r in rows]
        self.cum: dict[str, list[float]] = {}
        for k, name in enumerate(names):
            acc = [0.0]
            total = 0.0
            for r in rows:
                total += r[2][k]
                acc.append(total)
            self.cum[name] = acc

    def end(self, ts: float, exclude: int | None = None) -> int:
        i = bisect.bisect_left(self.t, ts)
        if exclude is not None:
            while i > 0 and self.match_id[i - 1] == exclude:
                i -= 1
        return i

    def window(
        self, ts: float, days: float, exclude: int | None, ref: float | None = None
    ) -> tuple[int, int]:
        """Rows in ``[ref - days, ts)`` (``ref`` defaults to ``ts``; rows are never read at/after ``ts``)."""
        hi = self.end(ts, exclude)
        start = (ts if ref is None else ref) - days * _DAY
        return min(bisect.bisect_left(self.t, start), hi), hi

    def total(self, name: str, lo: int, hi: int) -> float:
        c = self.cum[name]
        return c[hi] - c[lo]

    def rate(self, won: str, played: str, lo: int, hi: int) -> float:
        n = self.total(played, lo, hi)
        return self.total(won, lo, hi) / n if n > 0 else NAN


def _rate(w: float, n: float) -> float:
    return w / n if n > 0 else NAN


def _unit_hash(match_id: int) -> float:
    """Deterministic value in [0, 1) per match (Knuth multiplicative hash; same for every map / side)."""
    return ((int(match_id) * 2_654_435_761) & 0xFFFFFFFF) / 4_294_967_296.0


def _kickoff(meta: MatchMetaRecord | None) -> float | None:
    if meta is None or meta.scheduled_at is None:
        return None
    return ensure_utc(meta.scheduled_at).timestamp()


_TEAM_MAP_COLS: Final[tuple[str, ...]] = (
    "w",
    "bo1",
    "bo1_w",
    "bo3",
    "bo3_w",
    "dec",
    "dec_w",
    "op",
    "op_w",
    "xp",
    "xp_w",
    "pct_w",
    "pct_n",
    "ptt_w",
    "ptt_n",
    "pconv_w",
    "pconv_n",
    "prec_w",
    "prec_n",
    "force_w",
    "force_n",
    "eco_w",
    "eco_n",
    "lost_eco",
    "vs_eco",
    "lost_force",
    "vs_force",
)
_TEAM_ONE_MAP_COLS: Final[tuple[str, ...]] = ("pp_w", "pp_n", "rt_w", "rt_n")
_MATCH_COLS: Final[tuple[str, ...]] = ("m", "decided", "sw", "bo1", "bo1_sw", "bo3", "bo3_sw")
_CLUTCH_COLS: Final[tuple[str, ...]] = ("c_n", "c_w", "c11_n", "c11_w", "c1x_n", "c1x_w", "maps")
_PLAYER_COLS: Final[tuple[str, ...]] = (
    "oct_w",
    "oct_n",
    "ot_w",
    "ot_n",
    "deaths",
    "traded",
    "kills",
    "awp_k",
    "awpd_w",
    "awpd_n",
)


def _round_counters(rounds: Iterable[RoundRecord]) -> dict[tuple[int, int], tuple[list[float], list[float]]]:
    """(match id, map number) -> (team1 counters, team2 counters) in the order
    ``pct_w, pct_n, ptt_w, ptt_n, pconv_w, pconv_n, prec_w, prec_n, force_w, force_n, eco_w, eco_n,
    lost_eco, vs_eco, lost_force, vs_force, pp_w, pp_n, rt_w, rt_n``.

    Pistol rounds are rounds 1 and 13 (MR12; buy type PISTOL when known). Conversion / recovery look
    at the two rounds after each pistol (2-3, 14-15) when they are present.
    """
    by_map: dict[tuple[int, int], dict[int, RoundRecord]] = defaultdict(dict)
    for r in rounds:
        by_map[(r.match_id, r.map_number)][r.round_number] = r
    out: dict[tuple[int, int], tuple[list[float], list[float]]] = {}
    for key, rs in by_map.items():
        c1 = [0.0] * 20
        c2 = [0.0] * 20
        for num, r in rs.items():
            t1_won = r.winner is TeamSlot.TEAM1
            for c, won, side, own, opp in (
                (c1, t1_won, r.team1_side, r.team1_buy, r.team2_buy),
                (c2, not t1_won, r.team1_side.other, r.team2_buy, r.team1_buy),
            ):
                w = 1.0 if won else 0.0
                pistol = own is BuyType.PISTOL if own is not None else num in (1, 13)
                if pistol and num in (1, 13):
                    base = 0 if side is Side.CT else 2
                    c[base] += w
                    c[base + 1] += 1.0
                    follow = [rs.get(num + 1), rs.get(num + 2)]
                    follow_won = [(f.winner is TeamSlot.TEAM1) == (c is c1) for f in follow if f is not None]
                    if follow_won:
                        idx = 4 if won else 6
                        c[idx] += float(sum(follow_won))
                        c[idx + 1] += float(len(follow_won))
                if own is not None and opp is not None:
                    if own is BuyType.FORCE and opp is BuyType.FULL:
                        c[8] += w
                        c[9] += 1.0
                    elif own is BuyType.ECO and opp is BuyType.FULL:
                        c[10] += w
                        c[11] += 1.0
                    elif own is BuyType.FULL and opp is BuyType.ECO:
                        c[12] += 1.0 - w
                        c[13] += 1.0
                    elif own is BuyType.FULL and opp is BuyType.FORCE:
                        c[14] += 1.0 - w
                        c[15] += 1.0
                if r.bomb_planted:
                    if side is Side.T:
                        c[16] += w
                        c[17] += 1.0
                    else:
                        c[18] += w
                        c[19] += 1.0
        out[key] = (c1, c2)
    return out


@dataclass
class _MatchInfo:
    start: float
    end: float
    team1_id: int
    team2_id: int
    fmt: MatchFormat | None
    maps1: int = 0
    maps2: int = 0


def _series_complete(info: _MatchInfo) -> bool:
    """True when the stored maps finish the series (a BO3 needs a team with two map wins, ...).

    A live match already has its first finished maps in the database; counting it as decided would
    hand the leader a series win that does not exist yet (and, in training, every series is complete,
    so live rows would differ from training rows). Unknown format: any decided map score counts.
    """
    if info.fmt is None:
        return True
    if info.fmt is MatchFormat.BO2:
        return info.maps1 + info.maps2 >= 2
    return max(info.maps1, info.maps2) >= info.fmt.n_maps // 2 + 1


@dataclass
class _TeamMatches:
    cum: _Cum  # one row per series, at the time of its last map
    start_ts: list[float]
    tournament: list[int | None]


@dataclass
class _MetaSched:
    t: list[float]
    match_id: list[int]
    meta: list[MatchMetaRecord]


# --------------------------------------------------------------------------------------- index


class DeepFeatureIndex:
    """As-of indexes for the deep features (see the module docstring)."""

    def __init__(
        self,
        records: Sequence[MapResultRecord],
        *,
        rounds: Iterable[RoundRecord] = (),
        deep: DeepData | None = None,
        patch_timeline: PatchTimeline | None = None,
        config: DeepConfig | None = None,
    ) -> None:
        self.config = config or DeepConfig()
        self.patch_timeline = patch_timeline
        self.has_meta = False
        self.has_demo = False
        self.has_market = False
        self._meta: dict[int, MatchMetaRecord] = {}
        self._homes: dict[int, Place | None] = {}
        self._places: dict[int, Place | None] = {}
        self._build_maps(records, rounds)
        self._build_deep(deep)

    # ------------------------------------------------------------------------ construction
    def _build_maps(self, records: Sequence[MapResultRecord], rounds: Iterable[RoundRecord]) -> None:
        counters = _round_counters(rounds)
        matches: dict[int, _MatchInfo] = {}
        team_rows: dict[int, list[tuple[float, int, list[float]]]] = defaultdict(list)
        team_map_rows: dict[tuple[int, str], list[tuple[float, int, list[float]]]] = defaultdict(list)
        for rec in records:
            if rec.team1_rounds == rec.team2_rounds:
                continue
            ts = ensure_utc(rec.played_at).timestamp()
            info = matches.get(rec.match_id)
            if info is None:
                info = _MatchInfo(ts, ts, rec.team1_id, rec.team2_id, rec.format)
                matches[rec.match_id] = info
            info.start, info.end = min(info.start, ts), max(info.end, ts)
            t1_won = rec.team1_rounds > rec.team2_rounds
            same = rec.team1_id == info.team1_id
            if t1_won == same:
                info.maps1 += 1
            else:
                info.maps2 += 1
            fmt = rec.format
            n_maps = fmt.n_maps if fmt is not None else None
            decider = (
                rec.picked_by is None and n_maps is not None and n_maps >= 3 and rec.map_number == n_maps
            )
            rc = counters.get((rec.match_id, rec.map_number))
            map_name = normalize_map_name(rec.map_name)
            for team, slot, won, c in (
                (rec.team1_id, TeamSlot.TEAM1, t1_won, rc[0] if rc else None),
                (rec.team2_id, TeamSlot.TEAM2, not t1_won, rc[1] if rc else None),
            ):
                w = 1.0 if won else 0.0
                bo1 = 1.0 if fmt is MatchFormat.BO1 else 0.0
                bo3 = 1.0 if fmt in (MatchFormat.BO3, MatchFormat.BO5) else 0.0
                op = 1.0 if rec.picked_by is slot else 0.0
                xp = 1.0 if rec.picked_by is slot.other else 0.0
                row = [
                    w,
                    bo1,
                    bo1 * w,
                    bo3,
                    bo3 * w,
                    float(decider),
                    float(decider) * w,
                    op,
                    op * w,
                    xp,
                    xp * w,
                ]
                row.extend(c[:16] if c is not None else [0.0] * 16)
                team_rows[team].append((ts, rec.match_id, row))
                team_map_rows[(team, map_name)].append(
                    (ts, rec.match_id, c[16:20] if c is not None else [0.0] * 4)
                )
        self._team = {t: _Cum(rows, _TEAM_MAP_COLS) for t, rows in team_rows.items()}
        self._team_map = {k: _Cum(rows, _TEAM_ONE_MAP_COLS) for k, rows in team_map_rows.items()}
        self._matches = matches
        per_team: dict[int, list[tuple[float, int, list[float], float]]] = defaultdict(list)
        for mid, info in matches.items():
            # a series still being played (live: only its first maps are stored) has no result yet
            decided = info.maps1 != info.maps2 and _series_complete(info)
            bo1 = 1.0 if info.fmt is MatchFormat.BO1 else 0.0
            bo3 = 1.0 if info.fmt in (MatchFormat.BO3, MatchFormat.BO5) else 0.0
            for team, won in (
                (info.team1_id, info.maps1 > info.maps2),
                (info.team2_id, info.maps2 > info.maps1),
            ):
                sw = 1.0 if decided and won else 0.0
                # keyed by the start of the series' LAST map: a series counts (and its result is
                # read) only once all its maps were played before ``as_of``
                per_team[team].append(
                    (
                        info.end,
                        mid,
                        [1.0, float(decided), sw, bo1 * decided, bo1 * sw, bo3 * decided, bo3 * sw],
                        info.start,
                    )
                )
        self._team_matches_raw = per_team

    def _build_deep(self, deep: DeepData | None) -> None:
        cfg = self.config
        meta_rows = list(deep.match_meta) if deep is not None else []
        self._meta = {m.match_id: m for m in meta_rows}
        self.has_meta = bool(self._meta)
        # team match series (results) with the tournament from the metadata
        self._team_matches: dict[int, _TeamMatches] = {}
        for team, rows in self._team_matches_raw.items():
            rows.sort(key=lambda r: (r[0], r[1]))
            cum = _Cum([(r[0], r[1], r[2]) for r in rows], _MATCH_COLS)
            tour = [self._meta[r[1]].tournament_id if r[1] in self._meta else None for r in rows]
            self._team_matches[team] = _TeamMatches(cum, [r[3] for r in rows], tour)
        # schedule from metadata (scheduled start of every non-cancelled match)
        sched: dict[int, list[MatchMetaRecord]] = defaultdict(list)
        for m in meta_rows:
            if m.scheduled_at is None:
                continue
            sched[m.team1_id].append(m)
            sched[m.team2_id].append(m)
        self._sched: dict[int, _MetaSched] = {}
        for team, ms in sched.items():
            ms.sort(
                key=lambda m: (ensure_utc(m.scheduled_at).timestamp() if m.scheduled_at else 0.0, m.match_id)
            )
            self._sched[team] = _MetaSched(
                [ensure_utc(m.scheduled_at).timestamp() for m in ms if m.scheduled_at],
                [m.match_id for m in ms],
                ms,
            )
        if deep is not None:
            for team_id, tm in deep.team_meta.items():
                self._homes[int(team_id)] = team_home(tm.country, tuple(tm.player_countries))
        # clutches per team (one row per map)
        clutch_rows: dict[tuple[int, int, int], list[float]] = {}
        clutch_ts: dict[tuple[int, int, int], float] = {}
        for c in deep.clutches if deep is not None else ():
            key = (c.team_id, c.match_id, c.map_number)
            row = clutch_rows.setdefault(key, [0.0] * len(_CLUTCH_COLS))
            clutch_ts[key] = ensure_utc(c.played_at).timestamp()
            w = 1.0 if c.won else 0.0
            row[0] += 1.0
            row[1] += w
            if c.opponents == 1:
                row[2] += 1.0
                row[3] += w
            else:
                row[4] += 1.0
                row[5] += w
            row[6] = 1.0
        by_team: dict[int, list[tuple[float, int, Sequence[float]]]] = defaultdict(list)
        for (team, mid, _num), row in clutch_rows.items():
            by_team[team].append((clutch_ts[(team, mid, _num)], mid, row))
        self._clutch = {t: _Cum(rows, _CLUTCH_COLS) for t, rows in by_team.items()}
        # duels: per player (any map), per (player, map), pairs, opening areas
        pl_rows: dict[tuple[int, int, int], list[float]] = {}
        pl_map: dict[tuple[int, int, int], str] = {}
        pl_ts: dict[tuple[int, int, int], float] = {}
        pair_rows: dict[tuple[int, int, int, int], list[float]] = {}
        pair_meta: dict[tuple[int, int, int, int], tuple[float, str]] = {}
        areas: dict[tuple[int, str, Side], list[tuple[float, int, str, float]]] = defaultdict(list)
        for d in deep.duels if deep is not None else ():
            ts = ensure_utc(d.played_at).timestamp()
            awp = d.weapon == "awp"
            awp_duel = awp and d.victim_weapon == "awp"
            for pid, killer in ((d.attacker_id, True), (d.victim_id, False)):
                key = (pid, d.match_id, d.map_number)
                row = pl_rows.setdefault(key, [0.0] * len(_PLAYER_COLS))
                pl_map[key] = d.map_name
                pl_ts[key] = ts
                side = d.attacker_side if killer else (d.attacker_side.other if d.attacker_side else None)
                if d.is_opening and side is not None:
                    base = 0 if side is Side.CT else 2
                    row[base] += 1.0 if killer else 0.0
                    row[base + 1] += 1.0
                    if d.victim_place:
                        areas[(pid, d.map_name, side)].append(
                            (ts, d.match_id, d.victim_place.lower(), 1.0 if killer else 0.0)
                        )
                if killer:
                    row[6] += 1.0
                    row[7] += 1.0 if awp else 0.0
                else:
                    row[4] += 1.0
                    row[5] += 1.0 if d.was_traded else 0.0
                if awp_duel:
                    row[8] += 1.0 if killer else 0.0
                    row[9] += 1.0
            lo, hi = sorted((d.attacker_id, d.victim_id))
            pkey = (lo, hi, d.match_id, d.map_number)
            prow = pair_rows.setdefault(pkey, [0.0, 0.0])
            prow[0 if d.attacker_id == lo else 1] += 1.0
            pair_meta[pkey] = (ts, d.map_name)
        self.has_demo = bool(pl_rows) or bool(clutch_rows)
        per_player: dict[int, list[tuple[float, int, Sequence[float]]]] = defaultdict(list)
        per_player_map: dict[tuple[int, str], list[tuple[float, int, Sequence[float]]]] = defaultdict(list)
        for (pid, mid, num), row in pl_rows.items():
            per_player[pid].append((pl_ts[(pid, mid, num)], mid, row))
            per_player_map[(pid, pl_map[(pid, mid, num)])].append((pl_ts[(pid, mid, num)], mid, row))
        self._player = {p: _Cum(rows, _PLAYER_COLS) for p, rows in per_player.items()}
        self._player_map = {k: _Cum(rows, _PLAYER_COLS) for k, rows in per_player_map.items()}
        pairs: dict[tuple[int, int], list[tuple[float, int, str, float, float]]] = defaultdict(list)
        for (lo, hi, mid, _num), prow in pair_rows.items():
            ts, map_name = pair_meta[(lo, hi, mid, _num)]
            pairs[(lo, hi)].append((ts, mid, map_name, prow[0], prow[1]))
        self._pairs = {k: sorted(v) for k, v in pairs.items()}
        self._pair_t = {k: [r[0] for r in v] for k, v in self._pairs.items()}
        self._areas = {k: sorted(v) for k, v in areas.items()}
        self._area_t = {k: [r[0] for r in v] for k, v in self._areas.items()}
        # coach changes
        coach: dict[int, list[float]] = defaultdict(list)
        for change in deep.coach_changes if deep is not None else ():
            day = change.started_on
            coach[change.team_id].append(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
        self._coach = {t: sorted(v) for t, v in coach.items()}
        # market
        self._market = dict(deep.market) if deep is not None and cfg.with_market else {}
        self._sharp = dict(deep.sharp) if deep is not None and cfg.with_market else {}
        self.has_market = bool(self._market) or bool(self._sharp)

    # ----------------------------------------------------------------------------- lookups
    def match_meta(self, match_id: int | None) -> MatchMetaRecord | None:
        return self._meta.get(match_id) if match_id is not None else None

    def _place(self, meta: MatchMetaRecord | None) -> Place | None:
        if meta is None:
            return None
        if meta.match_id not in self._places:
            self._places[meta.match_id] = parse_location(meta.location)
        return self._places[meta.match_id]

    def features(
        self,
        team1_id: int,
        team2_id: int,
        map_name: str,
        as_of: datetime,
        *,
        match_id: int | None = None,
        exclude_match_id: int | None = None,
        team1_lineup: Sequence[int] = (),
        team2_lineup: Sequence[int] = (),
        is_lan: bool | None = None,
        stage: str | None = None,
    ) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
        """(team1 features, team2 features, match features) as of ``as_of``.

        ``match_id`` identifies the match being predicted (its static metadata and market history);
        ``stage`` / ``is_lan`` override the metadata (live context). Lineups default to each team's
        most recent duel-active players being unknown (lineup duel features NaN).
        """
        ts = ensure_utc(as_of).timestamp()
        map_name = normalize_map_name(map_name)
        meta = self.match_meta(match_id)
        lan = is_lan if is_lan is not None else (meta.is_lan if meta is not None else None)
        stage_text = stage if stage is not None else (meta.stage if meta is not None else None)
        info = classify_stage(stage_text)
        kick = _kickoff(meta)
        # Anchor of the schedule features: the scheduled start of the match (public before it). A live
        # row computed hours ahead then describes the same "match day" / "days before the match" as the
        # training row computed at kick-off. Results are still only read strictly before ``as_of``.
        ref = kick if kick is not None and ts < kick <= ts + _MAX_ANCHOR_AHEAD_S else ts
        f1 = self._team_block(
            team1_id, map_name, ts, ref, exclude_match_id, team1_lineup, meta, lan, info.swiss
        )
        f2 = self._team_block(
            team2_id, map_name, ts, ref, exclude_match_id, team2_lineup, meta, lan, info.swiss
        )
        m: dict[str, float] = info.features()
        m.update(self._matchup(team1_lineup, team2_lineup, map_name, ts, exclude_match_id))
        m["dp_map_days_in_pool"] = self._days_in_pool(map_name, ts)
        m.update(self._market_block(match_id, meta, team1_id, team2_id, ts, kick))
        return f1, f2, m

    def market_reference(self, match_id: int, ts: float, kick: float | None) -> float:
        """When the market block reads the prices (epoch seconds): ``as_of`` for a live row, a
        per-match lead before the scheduled start for a kick-off row (``DeepConfig.market_lead_hours``)."""
        lead = self.config.market_lead_hours
        if lead is None or kick is None or ts < kick - _KICKOFF_BAND_S:
            return ts
        lo, hi = lead
        return min(ts, kick) - (lo + _unit_hash(match_id) * (hi - lo)) * 3600.0

    def _market_block(
        self,
        match_id: int | None,
        meta: MatchMetaRecord | None,
        team1_id: int,
        team2_id: int,
        ts: float,
        kick: float | None,
    ) -> dict[str, float]:
        """Market features in the row's orientation; all NaN when the orientation is unknown.

        The stored prices are in the match's team1 / team2 order, so the match metadata must say which
        of the row's teams is the match's team1 (a mirrored training row, a live context). Without it, or
        when the row's teams are not the match's teams, the block is empty rather than possibly flipped.
        """
        empty = market_features((), datetime.fromtimestamp(ts, tz=UTC))
        empty["mkt_lead_hours"] = NAN
        if not self.config.with_market or match_id is None or meta is None:
            return empty
        if (meta.team1_id, meta.team2_id) == (team1_id, team2_id):
            flip = False
        elif (meta.team1_id, meta.team2_id) == (team2_id, team1_id):
            flip = True
        else:
            return empty
        ref = self.market_reference(match_id, ts, kick)
        out = market_features(
            self._market.get(match_id, ()),
            datetime.fromtimestamp(ref, tz=UTC),
            flip=flip,
            sharp=self._sharp.get(match_id, ()),
        )
        quoted = not (math.isnan(out["mkt_p_last"]) and math.isnan(out["sharp_p_last"]))
        out["mkt_lead_hours"] = (kick - ref) / 3600.0 if quoted and kick is not None else NAN
        return out

    # ---------------------------------------------------------------------------- team block
    def _team_block(
        self,
        team: int,
        map_name: str,
        ts: float,
        ref: float,
        exclude: int | None,
        lineup: Sequence[int],
        meta: MatchMetaRecord | None,
        is_lan: bool | None,
        swiss: bool,
    ) -> dict[str, float]:
        cfg = self.config
        out = dict.fromkeys(DEEP_TEAM_FEATURES, NAN)
        # -- schedule / fatigue (results)
        tc = self._team.get(team)
        tm = self._team_matches.get(team)
        # windows end at the match's scheduled start ``ref`` but only hold results before ``ts``
        for days in (3, 7):
            if tc is not None:
                lo, hi = tc.window(ts, days, exclude, ref)
                out[f"dp_maps_{days}d"] = float(hi - lo)
            else:
                out[f"dp_maps_{days}d"] = 0.0
            if tm is not None:
                lo, hi = tm.cum.window(ts, days, exclude, ref)
                out[f"dp_matches_{days}d"] = float(hi - lo)
            else:
                out[f"dp_matches_{days}d"] = 0.0
        day_start = math.floor(ref / _DAY) * _DAY  # UTC day of the match
        same_day = 0
        streak = 0.0
        if tm is not None:
            e = tm.cum.end(ts, exclude)
            if e > 0:
                out["dp_rest_days"] = max(0.0, (ref - tm.cum.t[e - 1]) / _DAY)
                days_played = {math.floor(t / _DAY) for t in tm.cum.t[max(0, e - 30) : e]}
                today = math.floor(ref / _DAY)
                same_day = sum(1 for t in tm.cum.t[max(0, e - 10) : e] if t >= day_start)
                d = today - 1
                while d in days_played:
                    streak += 1.0
                    d -= 1
        out["dp_play_streak_days"] = streak
        sched = self._sched.get(team)
        if sched is not None:  # the public schedule: series scheduled earlier on the match day
            hi = bisect.bisect_left(sched.t, ref)
            lo = bisect.bisect_left(sched.t, day_start)
            sched_same = sum(1 for k in range(lo, hi) if sched.match_id[k] != exclude)
            same_day = max(same_day, sched_same)
        out["dp_same_day_series"] = float(same_day)
        if tc is not None:
            lo, hi = tc.window(ts, 0.0, exclude, day_start)
            out["dp_maps_today"] = float(hi - lo)
        else:
            out["dp_maps_today"] = 0.0
        # -- travel
        out.update(self._travel(team, ref, exclude, meta, is_lan))
        # -- stakes inside the event
        if meta is not None and meta.tournament_id is not None and tm is not None:
            e = tm.cum.end(ts, exclude)
            wins = losses = 0
            for k in range(e):
                if tm.tournament[k] == meta.tournament_id and tm.cum.total("decided", k, k + 1) > 0:
                    if tm.cum.total("sw", k, k + 1) > 0:
                        wins += 1
                    else:
                        losses += 1
            out["dp_event_wins"] = float(wins)
            out["dp_event_losses"] = float(losses)
        elif meta is not None and meta.tournament_id is not None:
            out["dp_event_wins"] = out["dp_event_losses"] = 0.0
        if swiss and not math.isnan(out["dp_event_wins"]):
            elim, qual = swiss_situation(
                int(out["dp_event_wins"]),
                int(out["dp_event_losses"]),
                wins_needed=cfg.swiss_wins_needed,
                losses_needed=cfg.swiss_losses_needed,
            )
            out["dp_swiss_elimination"], out["dp_swiss_qualification"] = float(elim), float(qual)
        else:
            out["dp_swiss_elimination"] = out["dp_swiss_qualification"] = 0.0
        # -- format specialists
        if tm is not None:
            lo, hi = tm.cum.window(ts, cfg.format_window_days, exclude)
            bo1 = tm.cum.rate("bo1_sw", "bo1", lo, hi)
            bo3 = tm.cum.rate("bo3_sw", "bo3", lo, hi)
            out["dp_bo1_series_win"], out["dp_bo3_series_win"] = bo1, bo3
            out["dp_bo1_minus_bo3"] = bo1 - bo3
        if tc is not None:
            lo, hi = tc.window(ts, cfg.format_window_days, exclude)
            overall = _rate(tc.total("w", lo, hi), float(hi - lo))
            out["dp_bo3_map_win"] = tc.rate("bo3_w", "bo3", lo, hi)
            out["dp_decider_win"] = tc.rate("dec_w", "dec", lo, hi)
            out["dp_decider_maps"] = tc.total("dec", lo, hi)
            out["dp_pick_edge"] = tc.rate("op_w", "op", lo, hi) - overall
            out["dp_opp_pick_edge"] = tc.rate("xp_w", "xp", lo, hi) - overall
            lo, hi = tc.window(ts, cfg.economy_window_days, exclude)
            for name, w, n in (
                ("dp_pistol_ct_win", "pct_w", "pct_n"),
                ("dp_pistol_t_win", "ptt_w", "ptt_n"),
                ("dp_pistol_conversion", "pconv_w", "pconv_n"),
                ("dp_pistol_recovery", "prec_w", "prec_n"),
                ("dp_force_win", "force_w", "force_n"),
                ("dp_eco_win", "eco_w", "eco_n"),
                ("dp_lost_to_eco", "lost_eco", "vs_eco"),
                ("dp_lost_to_force", "lost_force", "vs_force"),
            ):
                out[name] = tc.rate(w, n, lo, hi)
        tmc = self._team_map.get((team, map_name))
        if tmc is not None:
            lo, hi = tmc.window(ts, cfg.map_round_window_days, exclude)
            out["dp_map_post_plant_win"] = tmc.rate("pp_w", "pp_n", lo, hi)
            out["dp_map_retake_win"] = tmc.rate("rt_w", "rt_n", lo, hi)
        # -- clutch
        cc = self._clutch.get(team)
        if cc is not None:
            lo, hi = cc.window(ts, cfg.clutch_window_days, exclude)
            out["dp_clutch_win"] = cc.rate("c_w", "c_n", lo, hi)
            out["dp_clutch_1v1_win"] = cc.rate("c11_w", "c11_n", lo, hi)
            out["dp_clutch_1vx_win"] = cc.rate("c1x_w", "c1x_n", lo, hi)
            out["dp_clutches_per_map"] = cc.rate("c_n", "maps", lo, hi)
        # -- lineup duels
        out.update(self._lineup_duels(lineup, map_name, ts, exclude))
        # -- coach
        coach = self._coach.get(team)
        if coach is not None:
            k = bisect.bisect_left(coach, ts - _DAY)  # changes on days strictly before as_of's day
            if k > 0:
                since_change = (ref - coach[k - 1]) / _DAY
                out["dp_days_since_coach_change"] = since_change
                out["dp_coach_change_90d"] = 1.0 if since_change <= 90 else 0.0
            else:
                out["dp_coach_change_90d"] = 0.0
        return out

    def _travel(
        self, team: int, ref: float, exclude: int | None, meta: MatchMetaRecord | None, is_lan: bool | None
    ) -> dict[str, float]:
        """Travel / event features from the public schedule before the match's scheduled start ``ref``."""
        out = {
            "dp_tz_shift": NAN,
            "dp_away_region": NAN,
            "dp_first_after_travel": NAN,
            "dp_days_at_event": NAN,
        }
        if meta is None:
            return out
        place = self._place(meta)
        online = is_lan is False or (place is not None and place.online)
        home = self._homes.get(team)
        if online:
            out["dp_tz_shift"] = 0.0
            out["dp_away_region"] = 0.0
        elif place is not None and home is not None:
            shift = tz_shift_hours(place.utc_offset, home.utc_offset)
            out["dp_tz_shift"] = shift if shift is not None else NAN
            if place.region is not None and home.region is not None:
                out["dp_away_region"] = 1.0 if place.region != home.region else 0.0
        sched = self._sched.get(team)
        if sched is None:
            return out
        hi = bisect.bisect_left(sched.t, ref)
        prev: MatchMetaRecord | None = None
        first_at_event: float | None = None
        for k in range(hi - 1, -1, -1):
            other = sched.meta[k]
            if other.match_id in (exclude, meta.match_id):
                continue
            if prev is None:
                prev = other
            if meta.tournament_id is not None and other.tournament_id == meta.tournament_id:
                first_at_event = sched.t[k]
            elif first_at_event is not None or k < hi - 40:
                break
        if meta.tournament_id is not None:
            out["dp_days_at_event"] = (ref - first_at_event) / _DAY if first_at_event is not None else 0.0
        if online:
            out["dp_first_after_travel"] = 0.0
        elif prev is not None and place is not None:
            same_event = meta.tournament_id is not None and prev.tournament_id == meta.tournament_id
            prev_place = self._place(prev)
            moved = (
                prev_place is None
                or prev_place.online
                or not prev.is_lan
                or (prev_place.country or prev_place.region) != (place.country or place.region)
            )
            out["dp_first_after_travel"] = 0.0 if same_event else (1.0 if moved else 0.0)
        elif prev is None and place is not None:
            out["dp_first_after_travel"] = 1.0
        return out

    # ---------------------------------------------------------------------------- players
    def _lineup_duels(
        self, lineup: Sequence[int], map_name: str, ts: float, exclude: int | None
    ) -> dict[str, float]:
        cfg = self.config
        out: dict[str, float] = {}
        if not lineup or not self._player:
            return out
        tot = dict.fromkeys(_PLAYER_COLS, 0.0)
        map_tot = dict.fromkeys(("oct_w", "oct_n", "ot_w", "ot_n"), 0.0)
        best_entry: tuple[float, float] | None = None  # (attempts, win rate)
        awper: tuple[float, float, float] | None = None  # (awp share, kills, awp duel win)
        for pid in lineup:
            pc = self._player.get(pid)
            if pc is not None:
                lo, hi = pc.window(ts, cfg.duel_window_days, exclude)
                vals = {c: pc.total(c, lo, hi) for c in _PLAYER_COLS}
                for c, v in vals.items():
                    tot[c] += v
                if vals["ot_n"] >= cfg.min_open_attempts and (
                    best_entry is None or vals["ot_n"] > best_entry[0]
                ):
                    best_entry = (vals["ot_n"], vals["ot_w"] / vals["ot_n"])
                if vals["kills"] >= cfg.min_awp_kills:
                    share = vals["awp_k"] / vals["kills"]
                    if share >= cfg.min_awper_share and (awper is None or share > awper[0]):
                        awper = (share, vals["kills"], _rate(vals["awpd_w"], vals["awpd_n"]))
            pm = self._player_map.get((pid, map_name))
            if pm is not None:
                lo, hi = pm.window(ts, cfg.map_duel_window_days, exclude)
                for c in map_tot:
                    map_tot[c] += pm.total(c, lo, hi)
        out["dp_open_ct_win"] = _rate(tot["oct_w"], tot["oct_n"])
        out["dp_open_t_win"] = _rate(tot["ot_w"], tot["ot_n"])
        out["dp_map_open_ct_win"] = _rate(map_tot["oct_w"], map_tot["oct_n"])
        out["dp_map_open_t_win"] = _rate(map_tot["ot_w"], map_tot["ot_n"])
        out["dp_trade_eff"] = _rate(tot["traded"], tot["deaths"])
        if best_entry is not None:
            out["dp_entry_open_t_win"] = best_entry[1]
        if awper is not None:
            out["dp_awper_kill_share"] = awper[0]
            out["dp_awper_awp_duel_win"] = awper[2]
        else:
            out["dp_awper_kill_share"] = 0.0 if tot["kills"] > 0 else NAN
        return out

    def _awper(self, lineup: Sequence[int], ts: float, exclude: int | None) -> int | None:
        cfg = self.config
        best: tuple[float, int] | None = None
        for pid in lineup:
            pc = self._player.get(pid)
            if pc is None:
                continue
            lo, hi = pc.window(ts, cfg.duel_window_days, exclude)
            kills = pc.total("kills", lo, hi)
            if kills < cfg.min_awp_kills:
                continue
            share = pc.total("awp_k", lo, hi) / kills
            if share >= cfg.min_awper_share and (best is None or share > best[0]):
                best = (share, pid)
        return best[1] if best is not None else None

    def _pair_kills(
        self, a: int, b: int, ts: float, days: float, exclude: int | None, map_name: str | None
    ) -> tuple[float, float]:
        """Kills a->b and b->a in the window (optionally on one map)."""
        lo_id, hi_id = sorted((a, b))
        rows = self._pairs.get((lo_id, hi_id))
        if not rows:
            return 0.0, 0.0
        times = self._pair_t[(lo_id, hi_id)]
        hi = bisect.bisect_left(times, ts)
        lo = bisect.bisect_left(times, ts - days * _DAY)
        k_lo = k_hi = 0.0
        for _t, mid, m, x, y in rows[lo:hi]:
            if mid == exclude or (map_name is not None and m != map_name):
                continue
            k_lo += x
            k_hi += y
        return (k_lo, k_hi) if a == lo_id else (k_hi, k_lo)

    def _area_rates(
        self, lineup: Sequence[int], map_name: str, side: Side, ts: float, exclude: int | None
    ) -> dict[str, tuple[float, float]]:
        days = self.config.map_duel_window_days
        acc: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for pid in lineup:
            key = (pid, map_name, side)
            rows = self._areas.get(key)
            if not rows:
                continue
            times = self._area_t[key]
            hi = bisect.bisect_left(times, ts)
            lo = bisect.bisect_left(times, ts - days * _DAY)
            for _t, mid, place, won in rows[lo:hi]:
                if mid == exclude:
                    continue
                a = acc[place]
                a[0] += won
                a[1] += 1.0
        return {p: (w, n) for p, (w, n) in acc.items()}

    def _area_edge(
        self, attack: Sequence[int], defend: Sequence[int], map_name: str, ts: float, exclude: int | None
    ) -> tuple[float, float]:
        """Sample-weighted mean over shared areas of (attackers' T opening win - defenders' CT win)."""
        t_rates = self._area_rates(attack, map_name, Side.T, ts, exclude)
        ct_rates = self._area_rates(defend, map_name, Side.CT, ts, exclude)
        num = den = 0.0
        for place, (w1, n1) in t_rates.items():
            other = ct_rates.get(place)
            if other is None or n1 <= 0 or other[1] <= 0:
                continue
            weight = min(n1, other[1])
            num += weight * (w1 / n1 - other[0] / other[1])
            den += weight
        return num, den

    def _matchup(
        self, lineup1: Sequence[int], lineup2: Sequence[int], map_name: str, ts: float, exclude: int | None
    ) -> dict[str, float]:
        cfg = self.config
        out = {
            "dp_awp_h2h_share": NAN,
            "dp_awp_h2h_n": 0.0,
            "dp_lineup_h2h_share": NAN,
            "dp_lineup_h2h_n": 0.0,
            "dp_area_open_edge": NAN,
        }
        if not lineup1 or not lineup2 or not self._pairs:
            return out
        a1, a2 = self._awper(lineup1, ts, exclude), self._awper(lineup2, ts, exclude)
        if a1 is not None and a2 is not None:
            k1, k2 = self._pair_kills(a1, a2, ts, cfg.h2h_window_days, exclude, None)
            out["dp_awp_h2h_n"] = k1 + k2
            out["dp_awp_h2h_share"] = _rate(k1, k1 + k2)
        k1 = k2 = 0.0
        for p1 in lineup1:
            for p2 in lineup2:
                x, y = self._pair_kills(p1, p2, ts, cfg.h2h_window_days, exclude, map_name)
                k1 += x
                k2 += y
        out["dp_lineup_h2h_n"] = k1 + k2
        out["dp_lineup_h2h_share"] = _rate(k1, k1 + k2)
        n1, d1 = self._area_edge(lineup1, lineup2, map_name, ts, exclude)
        n2, d2 = self._area_edge(lineup2, lineup1, map_name, ts, exclude)
        if d1 > 0 or d2 > 0:
            e1 = n1 / d1 if d1 > 0 else 0.0
            e2 = n2 / d2 if d2 > 0 else 0.0
            out["dp_area_open_edge"] = (e1 - e2) / 2.0
        return out

    def _days_in_pool(self, map_name: str, ts: float) -> float:
        tl = self.patch_timeline
        if tl is None:
            return NAN
        entered: datetime | None = None
        previous: tuple[str, ...] | None = None
        current_in = False
        for p in tl.patches:
            if p.effective_at.timestamp() > ts:
                break
            if not p.map_pool:
                continue
            in_pool = map_name in p.map_pool
            if in_pool and (previous is None or map_name not in previous):
                entered = p.effective_at
            current_in = in_pool
            previous = p.map_pool
        if not current_in or entered is None:
            return NAN
        return (ts - entered.timestamp()) / _DAY


def deep_feature_names(team_columns: Literal["diff", "all"] = "diff") -> list[str]:
    """Every column :func:`cs2bot.features.rows.match_map_features` adds for the deep block."""
    names: list[str] = []
    if team_columns == "all":
        names += [f"t1_{n}" for n in DEEP_TEAM_FEATURES]
        names += [f"t2_{n}" for n in DEEP_TEAM_FEATURES]
    names += [f"diff_{n}" for n in DEEP_TEAM_FEATURES]
    names += list(DEEP_MATCH_FEATURES)
    return names
