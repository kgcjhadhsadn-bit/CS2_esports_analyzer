"""FeatureStore: chronological indexes over the training records for fast as-of feature lookups.

Why a store: a feature such as "win rate over the last 90 days" for thousands of training rows must
not rescan the whole history per row. The store sorts every team's / player's / pairing's records
by time once and keeps prefix sums, so a window aggregate is two binary searches and a subtraction.

Leakage rules (enforced here, relied upon by everything above)
--------------------------------------------------------------
* A query at ``as_of`` reads records with ``played_at`` **strictly before** ``as_of``.
* Records of ``exclude_match_id`` are skipped as well, whatever their timestamps say (maps of the
  match being predicted, should the caller pass an ``as_of`` after the match started).
* Values that are themselves derived from history (strength proxy, change-point filter, roster
  streaks) are computed online in chronological order and stored *per record*, so reading the value
  attached to "the last record before ``as_of``" cannot see the future either.

All features are floats; ``NaN`` means "not available" (LightGBM treats NaN as missing).
"""

from __future__ import annotations

import bisect
import logging
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

import numpy as np

from cs2bot.domain.enums import BuyType, Side, TeamSlot, VetoAction
from cs2bot.domain.records import MapResultRecord, PlayerMapRecord, RoundRecord, VetoRecord
from cs2bot.features.meta import PatchTimeline, RegimeBreak, RegimeConfig, RegimeDetector, load_patch_timeline
from cs2bot.features.strength import StrengthConfig, sigmoid, walk_strength
from cs2bot.utils.names import normalize_map_name
from cs2bot.utils.time import ensure_utc

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from cs2bot.datasets.demo import TeamDemoMapRecord

log = logging.getLogger(__name__)

NAN: Final[float] = math.nan
_DAY: Final[float] = 86_400.0


@dataclass(frozen=True)
class FeatureConfig:
    """Window lengths (days unless stated) and thresholds of the feature definitions."""

    form_windows_days: tuple[int, ...] = (30, 90)
    map_form_windows_days: tuple[int, ...] = (30, 90, 365)
    adjusted_window_days: int = 90
    map_adjusted_window_days: int = 365
    schedule_window_days: int = 14
    side_window_days: int = 90
    map_side_window_days: int = 365
    lan_window_days: int = 365
    pick_window_days: int = 365
    pool_window_days: int = 180
    pool_min_maps: int = 3  # plays of a map inside the pool window for it to count as "in the pool"
    round_window_days: int = 180
    demo_window_days: int = 180
    veto_window_days: int = 180
    h2h_window_days: int = 730
    map_meta_window_days: int = 180  # all-teams statistics of a map (CT-sidedness)
    roster_last_maps: int = 20
    player_last_maps: int = 20
    player_window_days: int = 180
    #: Use team1's starting side as a feature for picked maps (the side is chosen during the veto).
    #: Off by default: most pre-match predictions are made before the veto is published, so the
    #: feature would be present in training and missing at prediction time.
    use_start_side: bool = False
    strength: StrengthConfig = field(default_factory=StrengthConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)


class _Series:
    """Time-sorted numeric columns with prefix sums (window sums in O(log n))."""

    __slots__ = ("cum", "match_id", "n", "t")

    def __init__(self, t: Sequence[float], match_id: Sequence[int], cols: Mapping[str, Sequence[float]]):
        self.t: list[float] = list(t)
        self.match_id: list[int] = list(match_id)
        self.n = len(self.t)
        self.cum: dict[str, list[float]] = {
            name: np.concatenate(([0.0], np.cumsum(np.asarray(values, dtype=float)))).tolist()
            for name, values in cols.items()
        }

    def end(self, ts: float, exclude_match_id: int | None = None) -> int:
        """Number of leading records usable at ``ts``: strictly earlier than ``ts``, minus trailing
        records of the excluded match (records of that match followed by other records are not
        removed - callers therefore never pass a ``ts`` after the start of the excluded match).
        """
        i = bisect.bisect_left(self.t, ts)
        if exclude_match_id is not None:
            while i > 0 and self.match_id[i - 1] == exclude_match_id:
                i -= 1
        return i

    def start(self, ts: float) -> int:
        """Index of the first record at or after ``ts``."""
        return bisect.bisect_left(self.t, ts)

    def total(self, name: str, lo: int, hi: int) -> float:
        c = self.cum[name]
        return c[hi] - c[lo]


def _rate(won: float, played: float) -> float:
    return won / played if played > 0 else NAN


_TEAM_COLS: Final[tuple[str, ...]] = (
    "win",
    "rd",
    "resid",
    "wresid",
    "opp_elo",
    "lan",
    "lan_win",
    "pick_own",
    "pick_own_win",
    "pick_opp",
    "pick_opp_win",
    "ct_w",
    "ct_n",
    "t_w",
    "t_n",
    "new_match",
    "rounds_n",  # maps with round-level data
    "pistol_w",
    "pistol_n",
    "ecof_w",
    "ecof_n",
    "anti_w",
    "anti_n",
    "fvf_w",
    "fvf_n",
    "pp_w",
    "pp_n",
    "rt_w",
    "rt_n",
    "demo_n",  # maps with demo-derived team totals
    "demo_rounds",
    "kills",
    "deaths",
    "trade_kills",
    "traded_deaths",
    "util_n",
    "util_rounds",
    "util_dmg",
    "flashed",
)
_TEAM_MAP_COLS: Final[tuple[str, ...]] = ("win", "rd", "resid", "ct_w", "ct_n", "t_w", "t_n", "pick_own")
_PLAYER_COLS: Final[tuple[str, ...]] = (
    "rounds",
    "kills",
    "deaths",
    "rating_sum",
    "rating_n",
    "adr_sum",
    "adr_w",
    "kast_sum",
    "kast_w",
    "fk",
    "fd",
    "fk_rounds",
    "awp",
    "awp_kills_base",
)
_ROUND_KEYS: Final[tuple[str, ...]] = (
    "pistol_w",
    "pistol_n",
    "ecof_w",
    "ecof_n",
    "anti_w",
    "anti_n",
    "fvf_w",
    "fvf_n",
    "pp_w",
    "pp_n",
    "rt_w",
    "rt_n",
)
_LOW_BUYS: Final[frozenset[BuyType]] = frozenset({BuyType.ECO, BuyType.FORCE})


@dataclass
class _TeamIndex:
    series: _Series
    post_elo: list[float]
    lineups: list[frozenset[int]]
    ref_lineup: list[frozenset[int]]  # reference lineup of the streak each record belongs to
    streak_start: list[int]  # index of the first record of that streak


@dataclass
class _TeamMapIndex:
    series: _Series
    post_offset: list[float]


@dataclass
class _H2HIndex:
    series: _Series  # oriented to the lower team id
    maps: list[str]
    win_lo: list[float]
    rd_lo: list[float]


def _same_roster(a: frozenset[int], b: frozenset[int]) -> bool:
    """Two lineup observations describe the same roster when one contains the other.

    Lineup rows are sometimes incomplete (four known players); a subset is not a roster change.
    """
    return a <= b or b <= a


def _round_aggregates(
    rounds: Iterable[RoundRecord],
) -> dict[tuple[int, int], tuple[list[float], list[float]]]:
    """(match id, map number) -> per-team counters in :data:`_ROUND_KEYS` order (team1, team2).

    Definitions, from the point of view of one team in one round:

    * pistol: rounds flagged ``PISTOL`` (or rounds 1 and 13 when the buy type is unknown);
    * eco/force conversion: the team bought ECO or FORCE against a FULL buy;
    * anti-eco: the team bought FULL against ECO or FORCE;
    * full v full: both teams bought FULL;
    * post-plant: the team was T and the bomb was planted - did the T side still win the round;
    * retake: the team was CT and the bomb was planted - did the CT side win the round.

    Rounds without the needed fields (HLTV round history has no buy types / plant flag) simply do
    not count toward the respective denominator.
    """
    out: dict[tuple[int, int], tuple[list[float], list[float]]] = {}
    for r in rounds:
        key = (r.match_id, r.map_number)
        agg = out.get(key)
        if agg is None:
            agg = ([0.0] * len(_ROUND_KEYS), [0.0] * len(_ROUND_KEYS))
            out[key] = agg
        t1_won = r.winner is TeamSlot.TEAM1
        for counters, won, own_buy, opp_buy, side in (
            (agg[0], t1_won, r.team1_buy, r.team2_buy, r.team1_side),
            (agg[1], not t1_won, r.team2_buy, r.team1_buy, r.team1_side.other),
        ):
            w = 1.0 if won else 0.0
            is_pistol = own_buy is BuyType.PISTOL if own_buy is not None else r.round_number in (1, 13)
            if is_pistol:
                counters[0] += w
                counters[1] += 1.0
            if own_buy is not None and opp_buy is not None:
                if own_buy in _LOW_BUYS and opp_buy is BuyType.FULL:
                    counters[2] += w
                    counters[3] += 1.0
                elif own_buy is BuyType.FULL and opp_buy in _LOW_BUYS:
                    counters[4] += w
                    counters[5] += 1.0
                elif own_buy is BuyType.FULL and opp_buy is BuyType.FULL:
                    counters[6] += w
                    counters[7] += 1.0
            if r.bomb_planted:
                if side is Side.T:
                    counters[8] += w
                    counters[9] += 1.0
                else:
                    counters[10] += w
                    counters[11] += 1.0
    return out


class FeatureStore:
    """Immutable (after :meth:`build`) index of the history used for feature computation.

    Build it once from everything known (or everything before a cut-off) and query it with any
    ``as_of``: queries never look at records at or after ``as_of``, so one store serves both the
    whole training set and live predictions.
    """

    def __init__(self) -> None:
        self.config = FeatureConfig()
        self.patch_timeline: PatchTimeline | None = None
        self._records: tuple[MapResultRecord, ...] = ()
        self._teams: dict[int, _TeamIndex] = {}
        self._team_maps: dict[tuple[int, str], _TeamMapIndex] = {}
        self._maps_of_team: dict[int, list[str]] = {}
        self._h2h: dict[tuple[int, int], _H2HIndex] = {}
        self._maps: dict[str, _Series] = {}
        self._players: dict[int, _Series] = {}
        self._standins: dict[tuple[int, int], int] = {}
        self._veto_team: dict[int, _Series] = {}
        self._veto_team_map: dict[tuple[int, str], _Series] = {}
        self._match_start: dict[int, datetime] = {}
        self._regimes: RegimeDetector = RegimeDetector({})
        self.has_round_data = False
        self.has_demo_data = False
        self.has_veto_data = False
        self.has_player_data = False

    # ------------------------------------------------------------------------------ construction

    @classmethod
    def build(
        cls,
        map_results: Iterable[MapResultRecord],
        *,
        player_maps: Iterable[PlayerMapRecord] = (),
        rounds: Iterable[RoundRecord] = (),
        vetoes: Iterable[VetoRecord] = (),
        team_demo_stats: Iterable[TeamDemoMapRecord] = (),
        config: FeatureConfig | None = None,
        patch_timeline: PatchTimeline | Literal["default"] | None = "default",
    ) -> FeatureStore:
        """Index the given records (any order; they are sorted here).

        Only ``map_results`` is required. Without ``player_maps`` the player aggregates are NaN,
        without ``rounds`` the pistol / economy / post-plant features, without ``vetoes`` the ban /
        pick rates, without ``team_demo_stats`` the trade and utility features. ``patch_timeline``
        defaults to ``config/patches.yaml``; pass ``None`` to disable the patch features.
        """
        store = cls()
        store.config = config or FeatureConfig()
        if isinstance(patch_timeline, PatchTimeline):
            store.patch_timeline = patch_timeline
        elif patch_timeline == "default":
            try:
                store.patch_timeline = load_patch_timeline()
            except (OSError, ValueError) as exc:
                log.warning("patch timeline unavailable, patch features will be NaN: %s", exc)

        records = sorted(map_results, key=lambda r: (ensure_utc(r.played_at), r.match_id, r.map_number))
        store._records = tuple(records)
        round_agg = _round_aggregates(rounds)
        store.has_round_data = bool(round_agg)
        demo = {(d.match_id, d.map_number, d.team_id): d for d in team_demo_stats}
        store.has_demo_data = bool(demo)
        trace = walk_strength(records, store.config.strength)

        team_rows: dict[int, list[tuple[float, int, list[float], float, frozenset[int]]]] = defaultdict(list)
        team_map_rows: dict[tuple[int, str], list[tuple[float, int, list[float], float]]] = defaultdict(list)
        h2h_rows: dict[tuple[int, int], list[tuple[float, int, str, float, float]]] = defaultdict(list)
        last_match_of_team: dict[int, int] = {}
        regime_rows: dict[int, list[tuple[float, float, int]]] = defaultdict(list)
        global_map_rows: dict[str, list[tuple[float, int, float, float]]] = defaultdict(list)

        for i, rec in enumerate(records):
            t = ensure_utc(rec.played_at)
            ts = t.timestamp()
            start = store._match_start.get(rec.match_id)
            if start is None or t < start:
                store._match_start[rec.match_id] = t
            total = rec.team1_rounds + rec.team2_rounds
            if total <= 0:
                continue
            map_name = normalize_map_name(rec.map_name)
            share1 = rec.team1_rounds / total
            exp1 = float(trace.exp_share1[i])
            expw1 = float(trace.exp_win1[i])
            rounds_pair = round_agg.get((rec.match_id, rec.map_number))
            has_sides = None not in (
                rec.team1_ct_rounds,
                rec.team1_t_rounds,
                rec.team2_ct_rounds,
                rec.team2_t_rounds,
            )
            sides = (
                (
                    rec.team1_id,
                    rec.team2_id,
                    rec.team1_rounds,
                    rec.team2_rounds,
                    share1 - exp1,
                    (1.0 if rec.team1_rounds > rec.team2_rounds else 0.0) - expw1,
                    float(trace.pre2[i]),
                    float(trace.post1[i]),
                    float(trace.post_map1[i]),
                    TeamSlot.TEAM1,
                    rec.team1_lineup,
                    (rec.team1_ct_rounds, rec.team1_t_rounds, rec.team2_ct_rounds, rec.team2_t_rounds),
                    rounds_pair[0] if rounds_pair else None,
                ),
                (
                    rec.team2_id,
                    rec.team1_id,
                    rec.team2_rounds,
                    rec.team1_rounds,
                    exp1 - share1,
                    (1.0 if rec.team2_rounds > rec.team1_rounds else 0.0) - (1.0 - expw1),
                    float(trace.pre1[i]),
                    float(trace.post2[i]),
                    float(trace.post_map2[i]),
                    TeamSlot.TEAM2,
                    rec.team2_lineup,
                    (rec.team2_ct_rounds, rec.team2_t_rounds, rec.team1_ct_rounds, rec.team1_t_rounds),
                    rounds_pair[1] if rounds_pair else None,
                ),
            )
            for (
                team,
                _opp,
                rf,
                ra,
                resid,
                wresid,
                opp_elo,
                post_elo,
                post_off,
                slot,
                lineup,
                side_rounds,
                round_counters,
            ) in sides:
                win = 1.0 if rf > ra else 0.0
                own_pick = 1.0 if rec.picked_by is slot else 0.0
                opp_pick = 1.0 if rec.picked_by is slot.other else 0.0
                if has_sides:
                    own_ct, own_t, opp_ct, opp_t = (float(v or 0) for v in side_rounds)
                    ct_w, ct_n = own_ct, own_ct + opp_t
                    t_w, t_n = own_t, own_t + opp_ct
                else:
                    ct_w = ct_n = t_w = t_n = 0.0
                new_match = 1.0 if last_match_of_team.get(team) != rec.match_id else 0.0
                last_match_of_team[team] = rec.match_id
                d = demo.get((rec.match_id, rec.map_number, team))
                row = [
                    win,
                    float(rf - ra),
                    resid,
                    wresid,
                    opp_elo,
                    1.0 if rec.is_lan else 0.0,
                    win if rec.is_lan else 0.0,
                    own_pick,
                    own_pick * win,
                    opp_pick,
                    opp_pick * win,
                    ct_w,
                    ct_n,
                    t_w,
                    t_n,
                    new_match,
                    1.0 if round_counters is not None else 0.0,
                    *(round_counters if round_counters is not None else [0.0] * len(_ROUND_KEYS)),
                    1.0 if d is not None else 0.0,
                    float(d.rounds) if d is not None else 0.0,
                    float(d.kills) if d is not None else 0.0,
                    float(d.deaths) if d is not None else 0.0,
                    float(d.trade_kills) if d is not None else 0.0,
                    float(d.traded_deaths) if d is not None else 0.0,
                    1.0 if d is not None and d.utility_damage is not None else 0.0,
                    float(d.rounds) if d is not None and d.utility_damage is not None else 0.0,
                    float(d.utility_damage or 0.0) if d is not None else 0.0,
                    float(d.enemies_flashed or 0) if d is not None else 0.0,
                ]
                team_rows[team].append((ts, rec.match_id, row, post_elo, frozenset(lineup)))
                team_map_rows[(team, map_name)].append(
                    (ts, rec.match_id, [win, float(rf - ra), resid, ct_w, ct_n, t_w, t_n, own_pick], post_off)
                )
                regime_rows[team].append((ts, resid, rec.match_id))
            ct_rounds = float((rec.team1_ct_rounds or 0) + (rec.team2_ct_rounds or 0)) if has_sides else 0.0
            global_map_rows[map_name].append(
                (ts, rec.match_id, ct_rounds, float(total) if has_sides else 0.0)
            )
            lo, hi = sorted((rec.team1_id, rec.team2_id))
            lo_is_team1 = lo == rec.team1_id
            lo_rf, lo_ra = (
                (rec.team1_rounds, rec.team2_rounds) if lo_is_team1 else (rec.team2_rounds, rec.team1_rounds)
            )
            h2h_rows[(lo, hi)].append(
                (ts, rec.match_id, map_name, 1.0 if lo_rf > lo_ra else 0.0, float(lo_rf - lo_ra))
            )

        for team, rows in team_rows.items():
            cols = {name: [r[2][k] for r in rows] for k, name in enumerate(_TEAM_COLS)}
            lineups = [r[4] for r in rows]
            refs: list[frozenset[int]] = []
            starts: list[int] = []
            ref: frozenset[int] = frozenset()
            start_idx = 0
            for k, fielded in enumerate(lineups):
                if fielded:
                    if not ref:
                        ref = fielded
                    elif _same_roster(fielded, ref):
                        ref = fielded if len(fielded) > len(ref) else ref
                    else:
                        ref = fielded
                        start_idx = k
                refs.append(ref)
                starts.append(start_idx)
            store._teams[team] = _TeamIndex(
                series=_Series([r[0] for r in rows], [r[1] for r in rows], cols),
                post_elo=[r[3] for r in rows],
                lineups=lineups,
                ref_lineup=refs,
                streak_start=starts,
            )
        for (team, map_name), map_rows in team_map_rows.items():
            cols = {name: [r[2][k] for r in map_rows] for k, name in enumerate(_TEAM_MAP_COLS)}
            store._team_maps[(team, map_name)] = _TeamMapIndex(
                series=_Series([r[0] for r in map_rows], [r[1] for r in map_rows], cols),
                post_offset=[r[3] for r in map_rows],
            )
            store._maps_of_team.setdefault(team, []).append(map_name)
        for pair, pair_rows in h2h_rows.items():
            store._h2h[pair] = _H2HIndex(
                series=_Series([r[0] for r in pair_rows], [r[1] for r in pair_rows], {}),
                maps=[r[2] for r in pair_rows],
                win_lo=[r[3] for r in pair_rows],
                rd_lo=[r[4] for r in pair_rows],
            )
        for map_name, global_rows in global_map_rows.items():
            store._maps[map_name] = _Series(
                [r[0] for r in global_rows],
                [r[1] for r in global_rows],
                {"ct_w": [r[2] for r in global_rows], "side_n": [r[3] for r in global_rows]},
            )
        store._regimes = RegimeDetector(
            {
                team: (
                    np.array([r[0] for r in rows]),
                    np.array([r[1] for r in rows]),
                    np.array([r[2] for r in rows], dtype=np.int64),
                )
                for team, rows in regime_rows.items()
            },
            store.config.regime,
        )
        store._index_players(player_maps)
        store._index_vetoes(vetoes)
        return store

    def _index_players(self, player_maps: Iterable[PlayerMapRecord]) -> None:
        rows: dict[int, list[tuple[float, int, int, list[float]]]] = defaultdict(list)
        standins: dict[tuple[int, int], set[int]] = defaultdict(set)
        for p in player_maps:
            if p.rounds_played <= 0:
                continue
            if p.is_standin:
                standins[(p.match_id, p.team_id)].add(p.player_id)
            rounds = float(p.rounds_played)
            kast = None if p.kast is None else (p.kast / 100.0 if p.kast > 1.5 else p.kast)
            has_opening = p.first_kills is not None and p.first_deaths is not None
            rows[p.player_id].append(
                (
                    ensure_utc(p.played_at).timestamp(),
                    p.match_id,
                    p.map_number,
                    [
                        rounds,
                        float(p.kills),
                        float(p.deaths),
                        float(p.rating) if p.rating is not None else 0.0,
                        1.0 if p.rating is not None else 0.0,
                        float(p.adr) * rounds if p.adr is not None else 0.0,
                        rounds if p.adr is not None else 0.0,
                        kast * rounds if kast is not None else 0.0,
                        rounds if kast is not None else 0.0,
                        float(p.first_kills or 0) if has_opening else 0.0,
                        float(p.first_deaths or 0) if has_opening else 0.0,
                        rounds if has_opening else 0.0,
                        float(p.awp_kills) if p.awp_kills is not None else 0.0,
                        float(p.kills) if p.awp_kills is not None else 0.0,
                    ],
                )
            )
        for player_id, player_rows in rows.items():
            player_rows.sort(key=lambda r: (r[0], r[1], r[2]))
            cols = {name: [r[3][k] for r in player_rows] for k, name in enumerate(_PLAYER_COLS)}
            self._players[player_id] = _Series([r[0] for r in player_rows], [r[1] for r in player_rows], cols)
        self._standins = {key: len(ids) for key, ids in standins.items()}
        self.has_player_data = bool(self._players)

    def _index_vetoes(self, vetoes: Iterable[VetoRecord]) -> None:
        team_rows: dict[int, list[tuple[float, int]]] = defaultdict(list)
        map_rows: dict[tuple[int, str], list[tuple[float, int, float, float]]] = defaultdict(list)
        for v in sorted(vetoes, key=lambda x: (ensure_utc(x.played_at), x.match_id)):
            ts = ensure_utc(v.played_at).timestamp()
            for slot, team in ((TeamSlot.TEAM1, v.team1_id), (TeamSlot.TEAM2, v.team2_id)):
                team_rows[team].append((ts, v.match_id))
                for step in v.steps:
                    if step.team is not slot or step.action is VetoAction.LEFTOVER:
                        continue
                    is_ban = 1.0 if step.action is VetoAction.BAN else 0.0
                    map_rows[(team, normalize_map_name(step.map_name))].append(
                        (ts, v.match_id, is_ban, 1.0 - is_ban)
                    )
        for team, rows in team_rows.items():
            self._veto_team[team] = _Series([r[0] for r in rows], [r[1] for r in rows], {})
        for key, mrows in map_rows.items():
            self._veto_team_map[key] = _Series(
                [r[0] for r in mrows],
                [r[1] for r in mrows],
                {"ban": [r[2] for r in mrows], "pick": [r[3] for r in mrows]},
            )
        self.has_veto_data = bool(self._veto_team)

    @classmethod
    def from_session(
        cls,
        session: Session,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        in_scope_only: bool = True,
        with_rounds: bool = True,
        with_demo: bool = True,
        config: FeatureConfig | None = None,
        patch_timeline: PatchTimeline | Literal["default"] | None = "default",
    ) -> FeatureStore:
        """Load everything from the database (``cs2bot.datasets``) and build the store.

        ``until`` bounds what the store can ever see (use it for walk-forward cut-offs); for live use
        leave it ``None`` and rely on ``as_of``. ``with_rounds=False`` skips the largest table.
        """
        from cs2bot import datasets  # local import: datasets.context imports features.meta lazily

        def window[R](loader: Callable[..., list[R]]) -> list[R]:
            return loader(session, since=since, until=until, in_scope_only=in_scope_only)

        return cls.build(
            window(datasets.load_map_results),
            player_maps=window(datasets.load_player_map_records),
            rounds=window(datasets.load_round_records) if with_rounds else (),
            vetoes=window(datasets.load_veto_records),
            team_demo_stats=window(datasets.load_team_demo_stats) if with_demo else (),
            config=config,
            patch_timeline=patch_timeline,
        )

    # ---------------------------------------------------------------------------------- lookups

    @property
    def map_results(self) -> tuple[MapResultRecord, ...]:
        """Every indexed map, chronological."""
        return self._records

    def __len__(self) -> int:
        return len(self._records)

    def match_start(self, match_id: int) -> datetime | None:
        """Earliest ``played_at`` among the indexed maps of a match (the pre-match ``as_of``)."""
        return self._match_start.get(match_id)

    def standin_count(self, match_id: int, team_id: int) -> int | None:
        """Stand-ins a team fielded in an indexed match (``None`` without player records)."""
        if not self.has_player_data:
            return None
        return self._standins.get((match_id, team_id), 0)

    def last_lineup(
        self, team_id: int, as_of: datetime, *, exclude_match_id: int | None = None
    ) -> tuple[int, ...]:
        """The team's most recent known lineup before ``as_of`` (empty if unknown)."""
        team = self._teams.get(team_id)
        if team is None:
            return ()
        e = team.series.end(ensure_utc(as_of).timestamp(), exclude_match_id)
        return tuple(sorted(team.ref_lineup[e - 1])) if e > 0 else ()

    def recent_regime_break(
        self, team_id: int, as_of: datetime, *, exclude_match_id: int | None = None
    ) -> RegimeBreak:
        """See :meth:`cs2bot.features.meta.RegimeDetector.recent_regime_break`."""
        return self._regimes.recent_regime_break(team_id, as_of, exclude_match_id=exclude_match_id)

    def form_trend(self, team_id: int, as_of: datetime, *, exclude_match_id: int | None = None) -> float:
        """See :meth:`cs2bot.features.meta.RegimeDetector.form_trend`."""
        return self._regimes.form_trend(team_id, as_of, exclude_match_id=exclude_match_id)

    def team_strength(
        self, team_id: int, map_name: str | None, as_of: datetime, *, exclude_match_id: int | None = None
    ) -> tuple[float, float]:
        """(overall proxy rating, map offset) before ``as_of``; NaN for a team / map without history."""
        return self._strength(team_id, map_name, ensure_utc(as_of).timestamp(), exclude_match_id)

    def _strength(
        self, team_id: int, map_name: str | None, ts: float, exclude: int | None
    ) -> tuple[float, float]:
        team = self._teams.get(team_id)
        elo = NAN
        if team is not None:
            e = team.series.end(ts, exclude)
            if e > 0:
                elo = team.post_elo[e - 1]
        offset = NAN
        if map_name is not None:
            tm = self._team_maps.get((team_id, map_name))
            if tm is not None:
                e = tm.series.end(ts, exclude)
                if e > 0:
                    offset = tm.post_offset[e - 1]
        return elo, offset

    # ----------------------------------------------------------------------------- feature blocks

    def team_features(
        self,
        team_id: int,
        map_name: str,
        as_of: datetime,
        *,
        lineup: Sequence[int] = (),
        standins: int | None = None,
        exclude_match_id: int | None = None,
    ) -> dict[str, float]:
        """All single-team features (un-prefixed names) as of ``as_of``.

        ``lineup`` is the lineup expected for the match being predicted; when empty the team's last
        known lineup is assumed (``lineup_known = 0``). ``standins`` = number of stand-ins in that
        lineup (``None`` = unknown -> NaN).
        """
        cfg = self.config
        ts = ensure_utc(as_of).timestamp()
        map_name = normalize_map_name(map_name)
        out: dict[str, float] = {}
        team = self._teams.get(team_id)
        s = team.series if team is not None else None
        e = s.end(ts, exclude_match_id) if s is not None else 0

        def window(days: int) -> tuple[int, int]:
            return (s.start(ts - days * _DAY), e) if s is not None else (0, 0)

        # -- form: win rate and round differential per map, overall
        for days in cfg.form_windows_days:
            lo, hi = window(days)
            n = hi - lo
            out[f"maps_{days}d"] = float(n)
            out[f"win_rate_{days}d"] = _rate(s.total("win", lo, hi), n) if s is not None else NAN
            out[f"round_diff_{days}d"] = _rate(s.total("rd", lo, hi), n) if s is not None else NAN

        # -- form on this map
        tm = self._team_maps.get((team_id, map_name))
        ms = tm.series if tm is not None else None
        me = ms.end(ts, exclude_match_id) if ms is not None else 0
        for days in cfg.map_form_windows_days:
            lo = ms.start(ts - days * _DAY) if ms is not None else 0
            n = me - lo
            out[f"map_maps_{days}d"] = float(n)
            out[f"map_win_rate_{days}d"] = _rate(ms.total("win", lo, me), n) if ms is not None else NAN
            out[f"map_round_diff_{days}d"] = _rate(ms.total("rd", lo, me), n) if ms is not None else NAN

        # -- opponent-adjusted form (residuals vs the strength proxy) and schedule strength
        lo, hi = window(cfg.adjusted_window_days)
        n = hi - lo
        d = cfg.adjusted_window_days
        out[f"adj_round_share_{d}d"] = _rate(s.total("resid", lo, hi), n) if s is not None else NAN
        out[f"adj_win_{d}d"] = _rate(s.total("wresid", lo, hi), n) if s is not None else NAN
        out[f"opp_strength_{d}d"] = _rate(s.total("opp_elo", lo, hi), n) if s is not None else NAN
        d = cfg.map_adjusted_window_days
        lo = ms.start(ts - d * _DAY) if ms is not None else 0
        out[f"map_adj_round_share_{d}d"] = (
            _rate(ms.total("resid", lo, me), me - lo) if ms is not None else NAN
        )
        elo, offset = self._strength(team_id, map_name, ts, exclude_match_id)
        out["elo"] = elo
        out["map_elo_offset"] = offset
        out["maps_alltime"] = float(e)

        # -- schedule
        out["days_since_last_match"] = (ts - s.t[e - 1]) / _DAY if s is not None and e > 0 else NAN
        lo, hi = window(cfg.schedule_window_days)
        d = cfg.schedule_window_days
        out[f"maps_{d}d"] = float(hi - lo)
        if s is not None and hi > lo:
            # a match counts when at least one of its maps falls in the window
            started_inside = s.total("new_match", lo, hi)
            carried = 1.0 if lo > 0 and s.match_id[lo] == s.match_id[lo - 1] else 0.0
            out[f"matches_{d}d"] = started_inside + carried
        else:
            out[f"matches_{d}d"] = 0.0

        # -- roster
        out.update(self._roster_features(team, e, ts, lineup, standins))

        # -- LAN record, pick record, map pool
        lo, hi = window(cfg.lan_window_days)
        d = cfg.lan_window_days
        lan_n = s.total("lan", lo, hi) if s is not None else 0.0
        out[f"lan_maps_{d}d"] = lan_n
        out[f"lan_win_rate_{d}d"] = _rate(s.total("lan_win", lo, hi), lan_n) if s is not None else NAN
        lo, hi = window(cfg.pick_window_days)
        d = cfg.pick_window_days
        if s is not None:
            out[f"own_pick_win_rate_{d}d"] = _rate(
                s.total("pick_own_win", lo, hi), s.total("pick_own", lo, hi)
            )
            out[f"opp_pick_win_rate_{d}d"] = _rate(
                s.total("pick_opp_win", lo, hi), s.total("pick_opp", lo, hi)
            )
        else:
            out[f"own_pick_win_rate_{d}d"] = out[f"opp_pick_win_rate_{d}d"] = NAN
        out.update(self._pool_features(team_id, map_name, ts, exclude_match_id, s, e))

        # -- side strength
        lo, hi = window(cfg.side_window_days)
        d = cfg.side_window_days
        out[f"ct_win_pct_{d}d"] = (
            _rate(s.total("ct_w", lo, hi), s.total("ct_n", lo, hi)) if s is not None else NAN
        )
        out[f"t_win_pct_{d}d"] = (
            _rate(s.total("t_w", lo, hi), s.total("t_n", lo, hi)) if s is not None else NAN
        )
        d = cfg.map_side_window_days
        lo = ms.start(ts - d * _DAY) if ms is not None else 0
        out[f"map_ct_win_pct_{d}d"] = (
            _rate(ms.total("ct_w", lo, me), ms.total("ct_n", lo, me)) if ms is not None else NAN
        )
        out[f"map_t_win_pct_{d}d"] = (
            _rate(ms.total("t_w", lo, me), ms.total("t_n", lo, me)) if ms is not None else NAN
        )

        # -- round-level (economy, post-plant / retake) and demo-derived (trades, utility)
        lo, hi = window(cfg.round_window_days)
        d = cfg.round_window_days
        for name, won, played in (
            ("pistol_win_pct", "pistol_w", "pistol_n"),
            ("eco_force_conv", "ecof_w", "ecof_n"),
            ("anti_eco_win_pct", "anti_w", "anti_n"),
            ("full_buy_win_pct", "fvf_w", "fvf_n"),
            ("post_plant_win_pct", "pp_w", "pp_n"),
            ("retake_win_pct", "rt_w", "rt_n"),
        ):
            out[f"{name}_{d}d"] = (
                _rate(s.total(won, lo, hi), s.total(played, lo, hi)) if s is not None else NAN
            )
        lo, hi = window(cfg.demo_window_days)
        d = cfg.demo_window_days
        if s is not None:
            out[f"trade_kill_share_{d}d"] = _rate(s.total("trade_kills", lo, hi), s.total("kills", lo, hi))
            out[f"traded_death_share_{d}d"] = _rate(
                s.total("traded_deaths", lo, hi), s.total("deaths", lo, hi)
            )
            util_rounds = s.total("util_rounds", lo, hi)
            out[f"util_dmg_per_round_{d}d"] = _rate(s.total("util_dmg", lo, hi), util_rounds)
            out[f"flashed_per_round_{d}d"] = _rate(s.total("flashed", lo, hi), util_rounds)
        else:
            for name in ("trade_kill_share", "traded_death_share", "util_dmg_per_round", "flashed_per_round"):
                out[f"{name}_{d}d"] = NAN

        # -- veto tendencies on this map
        out.update(self._veto_features(team_id, map_name, ts, exclude_match_id))

        # -- players of the expected lineup
        players = tuple(lineup) or (
            tuple(sorted(team.ref_lineup[e - 1])) if team is not None and e > 0 else ()
        )
        out.update(self._player_features(players, ts, exclude_match_id))

        # -- regime break / trend (change-point filter on the residual series)
        brk = self._regimes.recent_regime_break(team_id, as_of, exclude_match_id=exclude_match_id)
        out["regime_break_prob"] = brk.probability
        out["regime_shift"] = brk.shift
        out["form_trend"] = self._regimes.form_trend(team_id, as_of, exclude_match_id=exclude_match_id)
        return out

    def _roster_features(
        self, team: _TeamIndex | None, e: int, ts: float, lineup: Sequence[int], standins: int | None
    ) -> dict[str, float]:
        """Roster continuity features.

        * ``roster_continuity``: average share of the expected lineup that was fielded in each of
          the team's last ``roster_last_maps`` maps (1.0 = the same five played all of them).
        * ``maps_with_lineup``: consecutive most recent maps played by this roster.
        * ``days_since_roster_change``: days since the first map of that streak. 0 when the expected
          lineup differs from the last fielded one (the change happens with this match). For a
          roster that never changed inside the indexed history this is the time since the team's
          first indexed map - a lower bound.
        * ``roster_new_players``: expected players that were not in the last fielded roster.
        """
        cfg = self.config
        current = frozenset(lineup)
        out = {
            "lineup_known": 1.0 if current else 0.0,
            "standin_count": float(standins) if standins is not None else NAN,
            "roster_continuity": NAN,
            "maps_with_lineup": NAN,
            "days_since_roster_change": NAN,
            "roster_new_players": NAN,
        }
        if team is None or e <= 0:
            return out
        last_ref = team.ref_lineup[e - 1]
        if not current:
            current = last_ref
        if not current:
            return out
        lo = max(0, e - cfg.roster_last_maps)
        overlaps = [len(current & lu) / len(current) for lu in team.lineups[lo:e] if lu]
        out["roster_continuity"] = sum(overlaps) / len(overlaps) if overlaps else NAN
        if not last_ref:
            return out
        if _same_roster(current, last_ref):
            start = team.streak_start[e - 1]
            out["maps_with_lineup"] = float(e - start)
            out["days_since_roster_change"] = (ts - team.series.t[start]) / _DAY
            out["roster_new_players"] = 0.0
        else:
            out["maps_with_lineup"] = 0.0
            out["days_since_roster_change"] = 0.0
            out["roster_new_players"] = float(len(current - last_ref))
        return out

    def _pool_features(
        self, team_id: int, map_name: str, ts: float, exclude: int | None, s: _Series | None, e: int
    ) -> dict[str, float]:
        """Map pool depth and how central this map is to the team.

        * ``pool_depth``: maps the team played at least ``pool_min_maps`` times in the pool window.
        * ``pool_depth_winning``: those of them with a win rate of at least 50 %.
        * ``map_play_share``: share of the team's maps in the window that were on this map.
        * ``map_pick_share``: share of the team's own picks in the window that were this map.
        """
        cfg = self.config
        d = cfg.pool_window_days
        since = ts - d * _DAY
        depth = winning = 0
        this_n = this_picks = 0.0
        for name in self._maps_of_team.get(team_id, ()):
            tm = self._team_maps[(team_id, name)]
            hi = tm.series.end(ts, exclude)
            lo = tm.series.start(since)
            n = hi - lo
            if n >= cfg.pool_min_maps:
                depth += 1
                if tm.series.total("win", lo, hi) / n >= 0.5:
                    winning += 1
            if name == map_name:
                this_n = float(n)
                this_picks = tm.series.total("pick_own", lo, hi)
        total_n = total_picks = 0.0
        if s is not None:
            lo = s.start(since)
            total_n = float(e - lo)
            total_picks = s.total("pick_own", lo, e)
        known = s is not None and e > 0
        return {
            "pool_depth": float(depth) if known else NAN,
            "pool_depth_winning": float(winning) if known else NAN,
            f"map_play_share_{d}d": _rate(this_n, total_n),
            f"map_pick_share_{d}d": _rate(this_picks, total_picks),
        }

    def _veto_features(self, team_id: int, map_name: str, ts: float, exclude: int | None) -> dict[str, float]:
        """Share of the team's vetoes in the window in which it banned / picked this map."""
        d = self.config.veto_window_days
        out = {f"map_ban_rate_{d}d": NAN, f"map_pick_rate_{d}d": NAN}
        vs = self._veto_team.get(team_id)
        if vs is None:
            return out
        since = ts - d * _DAY
        n = vs.end(ts, exclude) - vs.start(since)
        if n <= 0:
            return out
        ms = self._veto_team_map.get((team_id, map_name))
        if ms is None:
            return {f"map_ban_rate_{d}d": 0.0, f"map_pick_rate_{d}d": 0.0}
        lo, hi = ms.start(since), ms.end(ts, exclude)
        return {
            f"map_ban_rate_{d}d": ms.total("ban", lo, hi) / n,
            f"map_pick_rate_{d}d": ms.total("pick", lo, hi) / n,
        }

    def _player_features(self, players: Sequence[int], ts: float, exclude: int | None) -> dict[str, float]:
        """Aggregates of the lineup's individual form over each player's recent maps.

        Per player: his last ``player_last_maps`` maps inside ``player_window_days`` (for any team -
        a new signing brings his numbers with him). Team values are the mean / max / min across the
        players that have data; ``pl_known`` says how many of them do.

        * ``pl_rating_*``: HLTV-style rating (mean of per-map ratings);
        * ``pl_kpr_*`` kills per round, ``pl_kd_diff_pr_mean`` (kills - deaths) per round;
        * ``pl_adr_mean``, ``pl_kast_mean`` (0..1), round-weighted;
        * ``pl_opening_win_pct``: lineup opening kills / opening duels; ``pl_opening_kpr``: opening
          kills per round summed over the lineup;
        * ``pl_awp_share_max`` / ``pl_awp_kpr_max``: AWP kill share and AWP kills per round of the
          lineup's most AWP-heavy player (the AWPer).
        """
        cfg = self.config
        names = (
            "pl_known",
            "pl_maps_mean",
            "pl_rating_mean",
            "pl_rating_max",
            "pl_rating_min",
            "pl_kpr_mean",
            "pl_kpr_max",
            "pl_kd_diff_pr_mean",
            "pl_adr_mean",
            "pl_kast_mean",
            "pl_opening_win_pct",
            "pl_opening_kpr",
            "pl_awp_share_max",
            "pl_awp_kpr_max",
        )
        out = dict.fromkeys(names, NAN)
        if not self._players:
            return out
        since = ts - cfg.player_window_days * _DAY
        maps: list[float] = []
        ratings: list[float] = []
        kprs: list[float] = []
        kd: list[float] = []
        adrs: list[float] = []
        kasts: list[float] = []
        awp_shares: list[float] = []
        awp_kprs: list[float] = []
        fk = fd = fk_kpr = 0.0
        opening_players = 0
        for pid in players:
            ps = self._players.get(pid)
            if ps is None:
                continue
            hi = ps.end(ts, exclude)
            lo = max(ps.start(since), hi - cfg.player_last_maps)
            if hi <= lo:
                continue
            rounds = ps.total("rounds", lo, hi)
            if rounds <= 0:
                continue
            maps.append(float(hi - lo))
            kills = ps.total("kills", lo, hi)
            kprs.append(kills / rounds)
            kd.append((kills - ps.total("deaths", lo, hi)) / rounds)
            if (n := ps.total("rating_n", lo, hi)) > 0:
                ratings.append(ps.total("rating_sum", lo, hi) / n)
            if (w := ps.total("adr_w", lo, hi)) > 0:
                adrs.append(ps.total("adr_sum", lo, hi) / w)
            if (w := ps.total("kast_w", lo, hi)) > 0:
                kasts.append(ps.total("kast_sum", lo, hi) / w)
            if (fk_rounds := ps.total("fk_rounds", lo, hi)) > 0:
                pfk = ps.total("fk", lo, hi)
                fk += pfk
                fd += ps.total("fd", lo, hi)
                fk_kpr += pfk / fk_rounds
                opening_players += 1
            if (base := ps.total("awp_kills_base", lo, hi)) > 0:
                awp = ps.total("awp", lo, hi)
                awp_shares.append(awp / base)
                awp_kprs.append(awp / rounds)
        out["pl_known"] = float(len(maps))
        if not maps:
            return out
        out["pl_maps_mean"] = sum(maps) / len(maps)
        out["pl_kpr_mean"] = sum(kprs) / len(kprs)
        out["pl_kpr_max"] = max(kprs)
        out["pl_kd_diff_pr_mean"] = sum(kd) / len(kd)
        if ratings:
            out["pl_rating_mean"] = sum(ratings) / len(ratings)
            out["pl_rating_max"] = max(ratings)
            out["pl_rating_min"] = min(ratings)
        if adrs:
            out["pl_adr_mean"] = sum(adrs) / len(adrs)
        if kasts:
            out["pl_kast_mean"] = sum(kasts) / len(kasts)
        if opening_players:
            out["pl_opening_win_pct"] = _rate(fk, fk + fd)
            out["pl_opening_kpr"] = fk_kpr
        if awp_shares:
            out["pl_awp_share_max"] = max(awp_shares)
            out["pl_awp_kpr_max"] = max(awp_kprs)
        return out

    def matchup_features(
        self,
        team1_id: int,
        team2_id: int,
        map_name: str,
        as_of: datetime,
        *,
        exclude_match_id: int | None = None,
    ) -> dict[str, float]:
        """Head-to-head record (team1's point of view) and the proxy's expectation for this map.

        * ``h2h_maps`` / ``h2h_win_rate`` / ``h2h_round_diff``: maps between the two teams in the
          h2h window, team1's win rate and average round differential;
        * ``h2h_map_maps`` / ``h2h_map_win_rate``: the same restricted to this map;
        * ``strength_logit``: round-logit difference of the strength proxy including map offsets
          (unknown teams / maps count as average, 0); ``strength_p_win``: the implied map-win
          probability of team1.
        """
        cfg = self.config
        ts = ensure_utc(as_of).timestamp()
        map_name = normalize_map_name(map_name)
        out = {
            "h2h_maps": 0.0,
            "h2h_win_rate": NAN,
            "h2h_round_diff": NAN,
            "h2h_map_maps": 0.0,
            "h2h_map_win_rate": NAN,
        }
        lo_id, hi_id = sorted((team1_id, team2_id))
        idx = self._h2h.get((lo_id, hi_id))
        if idx is not None:
            hi = idx.series.end(ts, exclude_match_id)
            lo = idx.series.start(ts - cfg.h2h_window_days * _DAY)
            if hi > lo:
                flip = team1_id != lo_id
                wins = [1.0 - w if flip else w for w in idx.win_lo[lo:hi]]
                diffs = [-d if flip else d for d in idx.rd_lo[lo:hi]]
                out["h2h_maps"] = float(hi - lo)
                out["h2h_win_rate"] = sum(wins) / len(wins)
                out["h2h_round_diff"] = sum(diffs) / len(diffs)
                same = [w for w, m in zip(wins, idx.maps[lo:hi], strict=True) if m == map_name]
                out["h2h_map_maps"] = float(len(same))
                out["h2h_map_win_rate"] = sum(same) / len(same) if same else NAN
        e1, o1 = self._strength(team1_id, map_name, ts, exclude_match_id)
        e2, o2 = self._strength(team2_id, map_name, ts, exclude_match_id)
        diff = sum(0.0 if math.isnan(v) else v for v in (e1, o1)) - sum(
            0.0 if math.isnan(v) else v for v in (e2, o2)
        )
        out["strength_logit"] = diff
        out["strength_p_win"] = sigmoid(self.config.strength.win_scale * diff)
        return out

    def map_features(
        self, map_name: str, as_of: datetime, *, exclude_match_id: int | None = None
    ) -> dict[str, float]:
        """All-teams statistics of a map in the ``map_meta_window_days`` before ``as_of``.

        ``map_ct_round_share``: share of rounds won by the CT side on this map (how CT-sided the map
        currently plays); ``map_global_maps``: how many times it was played (popularity / sample).
        """
        d = self.config.map_meta_window_days
        ts = ensure_utc(as_of).timestamp()
        ms = self._maps.get(normalize_map_name(map_name))
        if ms is None:
            return {f"map_ct_round_share_{d}d": NAN, f"map_global_maps_{d}d": 0.0}
        hi = ms.end(ts, exclude_match_id)
        lo = ms.start(ts - d * _DAY)
        return {
            f"map_ct_round_share_{d}d": _rate(ms.total("ct_w", lo, hi), ms.total("side_n", lo, hi)),
            f"map_global_maps_{d}d": float(max(hi - lo, 0)),
        }

    def meta_features(self, as_of: datetime) -> dict[str, float]:
        """Patch calendar features (NaN when no timeline is loaded).

        ``days_since_patch``: last patch of at least moderate impact; ``days_since_pool_change``:
        last Active Duty change; ``patch_index``: ordinal of the latest listed patch.
        """
        tl = self.patch_timeline
        if tl is None:
            return {"days_since_patch": NAN, "days_since_pool_change": NAN, "patch_index": NAN}
        index = tl.patch_index(as_of)
        return {
            "days_since_patch": tl.days_since_last_patch(as_of, min_impact="moderate"),
            "days_since_pool_change": tl.days_since_last_patch(
                as_of, min_impact="minor", kinds=("map_pool",)
            ),
            "patch_index": float(index) if index >= 0 else NAN,
        }
