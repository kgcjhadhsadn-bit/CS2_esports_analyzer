"""Vectorised round-by-round Monte Carlo simulation of one CS2 map (MR12 + MR3 overtime + economy).

All ``n_sims`` simulations advance together, one round at a time, with numpy arrays (one element per
simulation), so 20k simulations of a map take a few tens of milliseconds.

Per round, for both teams (team-level economy, money = average per player):

1. **Buy decision** (:class:`~cs2bot.models.economy.BuyPolicy`): pistol rounds at the start of each
   regulation half; full buy when ``money + carried gear`` covers a rifle buy; otherwise a
   probabilistic force/eco choice that depends on money, the loss bonus the team would get, the
   round's importance (opponent on map point / last round of a half) and the pistol-loss round;
   anti-eco SMG buys by a team that just won against an opponent that cannot buy.
2. **Outcome**: ``P(team1 wins) = RoundModel(side, buy matchup, map, d)``.
3. **Round flow** (:class:`~cs2bot.models.economy.RoundFlowParams`): end reason (elimination, bomb,
   defuse, time), deaths on both sides (survivors keep part of their equipment value).
4. **Economy update** (:class:`~cs2bot.models.economy.EconomyParams`): win rewards by end reason,
   CS2 loss-bonus ladder with the step-down-on-win rule, plant/defuse bonuses, kill rewards, the
   CT team kill bonus, no loss bonus for Ts alive at time expiry, the $16k cap.
5. **Structure**: sides swap after round 12 (money resets to $800), regulation ends at 13; at 12-12
   MR3 overtime is played with $10k per half, teams keep their sides into each overtime period and
   swap at the overtime halftime; an overtime period is won 4-x, 3-3 starts another one. The first
   ``OT_DETAILED_PERIODS`` overtime periods are simulated with the full economy; the (rare)
   longer overtimes continue with full-buy-vs-full-buy rounds.

Unknown starting side: half of the simulations start team1 on CT (stratified 50/50 mixture).

Race to N is derived exactly from score snapshots: after ``2N - 1`` rounds exactly one team has
reached N, and if the map ended earlier only its winner can have reached N.

Randomness is drawn as a fixed block of uniforms per round (one uniform per decision), so runs with
the same seed but different strength ``d`` share common random numbers. :func:`calibrate_strength`
exploits this to find the ``d`` whose simulated map-win probability matches a target probability.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import numpy.typing as npt
from scipy import optimize, special, stats

from cs2bot.domain.enums import Side
from cs2bot.models.economy import (
    DEFAULT_BUY_POLICY,
    DEFAULT_ECONOMY,
    DEFAULT_ROUND_FLOW,
    ECO_CODE,
    FORCE_CODE,
    FULL_CODE,
    PISTOL_CODE,
    BuyPolicy,
    EconomyParams,
    RoundFlowParams,
)
from cs2bot.models.round_model import RoundModel
from cs2bot.models.sim_stats import expit, logit, sort_score_keys
from cs2bot.utils.names import normalize_map_name

FloatArray = npt.NDArray[np.float64]
I16Array = npt.NDArray[np.int16]
I8Array = npt.NDArray[np.int8]
BoolArray = npt.NDArray[np.bool_]

#: Race-to-N targets simulated by default: every N from 1 to 16. N <= 13 is always decided; 14-16 are only
#: reachable in overtime (``p_neither_race_to`` carries the void mass). Betclic lists "first to N rounds"
#: markets for varying N, so every plausible line is priceable.
DEFAULT_RACE_TARGETS: tuple[int, ...] = tuple(range(1, 17))
P_MIN, P_MAX = 0.001, 0.999
D_MIN, D_MAX = -8.0, 8.0
OT_DETAILED_PERIODS = 3  # overtime periods simulated with the full economy (P(4th OT) ~ 0.4 %)
IID_GAIN = 1.09  # simulated-vs-i.i.d. strength ratio at equal map-win probability (initial guess only)
_N_UNIFORMS = 6  # per round: win, end reason, force team1, force team2, winner deaths, loser deaths
# End reasons
_ELIM, _DETONATION, _DEFUSE, _TIME = 0, 1, 2, 3


# ====================================================================================== inputs


@dataclass(frozen=True)
class MapSimInput:
    """Everything that defines one map simulation (``strength_diff`` = d, team1 minus team2)."""

    map_name: str
    strength_diff: float = 0.0
    team1_start_side: Side | None = None
    economy: EconomyParams = DEFAULT_ECONOMY
    round_model: RoundModel = field(default_factory=RoundModel.default, compare=False)
    policy: BuyPolicy = DEFAULT_BUY_POLICY
    flow: RoundFlowParams = DEFAULT_ROUND_FLOW
    race_targets: tuple[int, ...] = DEFAULT_RACE_TARGETS

    def __post_init__(self) -> None:
        object.__setattr__(self, "map_name", normalize_map_name(self.map_name))
        targets = tuple(sorted({int(t) for t in self.race_targets}))
        if any(t <= 0 for t in targets):
            raise ValueError("race targets must be positive")
        object.__setattr__(self, "race_targets", targets)
        if not math.isfinite(self.strength_diff):
            raise ValueError("strength_diff must be finite")

    def with_strength(self, d: float) -> MapSimInput:
        return replace(self, strength_diff=float(d))

    def cache_key(self, *, include_strength: bool = True) -> tuple[Any, ...]:
        return (
            self.map_name,
            round(self.strength_diff, 12) if include_strength else None,
            self.team1_start_side,
            self.economy,
            self.policy,
            self.flow,
            self.race_targets,
            self.round_model.fingerprint(),
        )


# ====================================================================================== outputs


@dataclass(frozen=True)
class PerSimRecords:
    """Per-simulation raw results (index i = simulation i)."""

    team1_rounds: I16Array
    team2_rounds: I16Array
    team1_start_ct: BoolArray
    round1_team1: BoolArray  # team1 won round 1
    round_half2_team1: BoolArray  # team1 won the first round of the second half (round 13)
    half1_team1: I16Array  # team1 rounds won in rounds 1-12
    half2_team1: I16Array  # team1 rounds won in rounds 13-24 (regulation only)
    half2_team2: I16Array
    race: I8Array  # (n_targets, n): 0 = nobody reached N, 1 = team1 first, 2 = team2 first


@dataclass(frozen=True)
class MapSimOutput:
    """Result of :meth:`MapSimulator.simulate`.

    Probabilities are from team1's perspective. ``weights`` (if set, normalised to sum 1) are
    per-simulation importance weights (see :meth:`reweighted`); every summary already uses them.

    * ``score_pmf`` - final score ``"13-9"`` (team1 first) -> p, overtime scores included;
    * ``p_team1_pistol`` - ``{"1": P(team1 wins round 1), "13": P(team1 wins round 13)}``;
    * ``p_team1_half`` / ``p_half_draw`` - half 1 = rounds 1-12, half 2 = rounds 13-24 (regulation
      rounds actually played; overtime excluded); a 6-6 (or otherwise level) half is a draw;
    * ``p_team1_race_to`` / ``p_neither_race_to`` - first to N rounds (neither only possible for
      N > 13).
    """

    map_name: str
    strength_diff: float
    team1_start_side: Side | None
    n_sims: int
    rounds_per_half: int
    race_targets: tuple[int, ...]
    score_pmf: dict[str, float]
    p_team1_win: float
    p_overtime: float
    p_team1_pistol: dict[str, float]
    p_team1_half: dict[str, float]
    p_half_draw: dict[str, float]
    p_team1_race_to: dict[str, float]
    p_neither_race_to: dict[str, float]
    total_rounds_pmf: dict[int, float]
    records: PerSimRecords = field(repr=False)
    weights: FloatArray | None = field(default=None, repr=False)
    diagnostics: dict[str, float] = field(default_factory=dict)
    elapsed_s: float = 0.0
    round_log: dict[str, npt.NDArray[Any]] | None = field(default=None, repr=False)

    # ---------------------------------------------------------------- arrays for player props
    @property
    def team1_rounds(self) -> I16Array:
        return self.records.team1_rounds

    @property
    def team2_rounds(self) -> I16Array:
        return self.records.team2_rounds

    @property
    def total_rounds(self) -> I16Array:
        return (self.records.team1_rounds + self.records.team2_rounds).astype(np.int16)

    @property
    def team1_wins(self) -> BoolArray:
        return np.asarray(self.records.team1_rounds > self.records.team2_rounds)

    @property
    def mean_total_rounds(self) -> float:
        return float(sum(k * p for k, p in self.total_rounds_pmf.items()))

    def reweighted(self, target_p: float) -> MapSimOutput:
        """Return a copy whose map-win probability equals ``target_p`` exactly.

        Simulations won by team1 get weight ``target_p / p_sim`` and the others
        ``(1 - target_p) / (1 - p_sim)``. Distributions *conditional on the map winner* are
        unchanged; only the winner mix is corrected (removes the residual calibration error).
        """
        target = min(max(float(target_p), 0.0), 1.0)
        p_sim = self.p_team1_win
        if abs(target - p_sim) < 1e-12 or p_sim <= 0.0 or p_sim >= 1.0:
            return self
        base = self.weights if self.weights is not None else np.full(self.n_sims, 1.0 / self.n_sims)
        w = np.where(self.team1_wins, base * (target / p_sim), base * ((1.0 - target) / (1.0 - p_sim)))
        w = w / w.sum()
        return _build_output(
            self.map_name,
            self.strength_diff,
            self.team1_start_side,
            self.rounds_per_half,
            self.race_targets,
            self.records,
            w,
            diagnostics={**self.diagnostics, "reweighted_from": p_sim},
            elapsed_s=self.elapsed_s,
            round_log=self.round_log,
        )


def _wmean(x: npt.NDArray[Any], w: FloatArray | None) -> float:
    if w is None:
        return float(np.mean(x))
    return float(np.dot(w, x))


def _weighted_counts(
    values: npt.NDArray[np.int64], w: FloatArray | None
) -> tuple[npt.NDArray[np.int64], FloatArray]:
    uniq, inv = np.unique(values, return_inverse=True)
    if w is None:
        probs = np.bincount(inv, minlength=uniq.size) / float(values.size)
    else:
        probs = np.bincount(inv, weights=w, minlength=uniq.size)
    return uniq, np.asarray(probs, dtype=np.float64)


def _build_output(
    map_name: str,
    d: float,
    side: Side | None,
    rph: int,
    targets: tuple[int, ...],
    rec: PerSimRecords,
    weights: FloatArray | None,
    *,
    diagnostics: dict[str, float],
    elapsed_s: float,
    round_log: dict[str, npt.NDArray[Any]] | None = None,
) -> MapSimOutput:
    t1 = rec.team1_rounds.astype(np.int64)
    t2 = rec.team2_rounds.astype(np.int64)
    total = t1 + t2
    uniq, probs = _weighted_counts(t1 * 1000 + t2, weights)
    score_pmf = sort_score_keys(
        {f"{int(c) // 1000}-{int(c) % 1000}": float(p) for c, p in zip(uniq, probs, strict=True) if p > 0}
    )
    tu, tprobs = _weighted_counts(total, weights)
    half = rph // 2
    return MapSimOutput(
        map_name=map_name,
        strength_diff=float(d),
        team1_start_side=side,
        n_sims=int(t1.size),
        rounds_per_half=rph,
        race_targets=targets,
        score_pmf=score_pmf,
        p_team1_win=_wmean(t1 > t2, weights),
        p_overtime=_wmean(total > 2 * rph, weights),
        p_team1_pistol={
            "1": _wmean(rec.round1_team1, weights),
            str(rph + 1): _wmean(rec.round_half2_team1, weights),
        },
        p_team1_half={
            "1": _wmean(rec.half1_team1 > half, weights),
            "2": _wmean(rec.half2_team1 > rec.half2_team2, weights),
        },
        p_half_draw={
            "1": _wmean(rec.half1_team1 == half, weights),
            "2": _wmean(rec.half2_team1 == rec.half2_team2, weights),
        },
        p_team1_race_to={str(tg): _wmean(rec.race[i] == 1, weights) for i, tg in enumerate(targets)},
        p_neither_race_to={str(tg): _wmean(rec.race[i] == 0, weights) for i, tg in enumerate(targets)},
        total_rounds_pmf={int(k): float(p) for k, p in zip(tu, tprobs, strict=True) if p > 0},
        records=rec,
        weights=weights,
        diagnostics=diagnostics,
        elapsed_s=elapsed_s,
        round_log=round_log,
    )


# ====================================================================================== core


@dataclass
class _State:
    """Per-team state, arrays of shape (2, m): row 0 = team1, row 1 = team2."""

    money: FloatArray
    carried: FloatArray
    streak: I16Array
    winstreak: I16Array
    score: I16Array
    won_prev: BoolArray

    @classmethod
    def new(cls, m: int, money: float, streak: int) -> _State:
        return cls(
            money=np.full((2, m), float(money)),
            carried=np.zeros((2, m)),
            streak=np.full((2, m), streak, dtype=np.int16),
            winstreak=np.zeros((2, m), dtype=np.int16),
            score=np.zeros((2, m), dtype=np.int16),
            won_prev=np.zeros((2, m), dtype=bool),
        )

    def reset_half(self, money: float, streak: int) -> None:
        self.money[:] = float(money)
        self.carried[:] = 0.0
        self.streak[:] = streak
        self.winstreak[:] = 0
        self.won_prev[:] = False

    def take(self, idx: npt.NDArray[np.intp]) -> _State:
        return _State(
            money=self.money[:, idx],
            carried=self.carried[:, idx],
            streak=self.streak[:, idx],
            winstreak=self.winstreak[:, idx],
            score=self.score[:, idx],
            won_prev=self.won_prev[:, idx],
        )


@dataclass(frozen=True)
class _Ctx:
    """Constants of one simulation, pre-computed into flat lookup tables (``np.take`` is fast)."""

    econ: EconomyParams
    pol: BuyPolicy
    flow: RoundFlowParams
    d: float
    ct_bias: float
    pistol_adj: float
    table: FloatArray  # (4, 4) buy matchup logits E[b1, b2]
    table_flat: FloatArray  # (16,) E[b1 * 4 + b2]
    kill_reward: FloatArray  # by buy code
    ladder: FloatArray  # loss bonus by loss streak
    reward_by_reason: FloatArray  # win reward by end reason
    winner_cdf: FloatArray  # (4, 16): CDF at k of winner deaths ~ Bin(4, q(bw, bl)), column bw * 4 + bl
    loser_cdf: FloatArray  # (5, 4): CDF at k of loser deaths by end reason (elimination: all 5 die)


def _make_ctx(inp: MapSimInput) -> _Ctx:
    econ, flow = inp.economy, inp.flow
    ct_bias, padj, table = inp.round_model.logit_tables(inp.map_name)
    q_w = np.clip(
        flow.winner_death_base - flow.winner_death_slope * table, flow.winner_death_min, flow.winner_death_max
    ).ravel()
    k4 = np.arange(4, dtype=np.float64)
    winner_cdf = np.asarray(stats.binom.cdf(k4[:, None], 4, q_w[None, :]), dtype=np.float64)
    k5 = np.arange(5, dtype=np.float64)
    loser_cdf = np.full((5, 4), -1.0)  # elimination column: u > -1 always -> 5 deaths
    for reason, q in (
        (_DETONATION, flow.loser_death_after_detonation),
        (_DEFUSE, flow.loser_death_after_defuse),
        (_TIME, flow.loser_death_after_time),
    ):
        loser_cdf[:, reason] = stats.binom.cdf(k5, 5, q)
    return _Ctx(
        econ=econ,
        pol=inp.policy,
        flow=flow,
        d=float(inp.strength_diff),
        ct_bias=float(ct_bias),
        pistol_adj=float(padj),
        table=table,
        table_flat=np.ascontiguousarray(table.ravel()),
        kill_reward=np.asarray(econ.kill_reward_by_buy, dtype=np.float64),
        ladder=np.asarray(econ.loss_bonus_ladder, dtype=np.float64),
        reward_by_reason=np.asarray(
            [econ.win_elimination, econ.win_detonation, econ.win_defuse, econ.win_time], dtype=np.float64
        ),
        winner_cdf=winner_cdf,
        loser_cdf=loser_cdf,
    )


# NOTE on style: ``np.where`` with random masks is slow in numpy (branchy, ~3 ns/element), so the
# hot path below blends with arithmetic (``y + mask * (x - y)``) and gathers with ``np.take``.


def _decide_buys(
    st: _State,
    ct: BoolArray,
    u_force: npt.NDArray[np.float32],
    ctx: _Ctx,
    *,
    after_pistol: bool,
    last_of_half: bool,
    map_point_score: int,
) -> tuple[I8Array, FloatArray, FloatArray]:
    """Team-level buy decision for both teams -> (buy codes, money spent, equipment value)."""
    pol = ctx.pol
    money = st.money
    carried = st.carried
    full_min = pol.full_min_t + ct * (pol.full_min_ct - pol.full_min_t)
    full_target = pol.full_target_t + ct * (pol.full_target_ct - pol.full_target_t)
    avail = money + carried
    can_full = avail >= full_min
    can_force = money >= pol.force_min_money
    must_win = np.ones(ct.shape, dtype=bool) if last_of_half else st.score[::-1] >= map_point_score
    # Anti-eco: just won, the opponent cannot full buy -> SMG/armour buy (always if a rifle buy is
    # not affordable anyway, with probability p_anti_eco otherwise), unless rich or the round is critical.
    anti = st.won_prev & ~can_full[::-1] & ~must_win & (avail < full_target + pol.anti_eco_rich_margin)
    anti &= (can_full & (u_force < pol.p_anti_eco)) | (~can_full & can_force)
    eco_next = money + (np.take(ctx.ladder, st.streak) + (pol.expected_loss_kill_income - pol.eco_spend))
    p_force = pol.p_force_eco_futile + (eco_next >= full_min) * (pol.p_force_base - pol.p_force_eco_futile)
    if after_pistol:
        p_force = p_force + ~st.won_prev * (pol.p_force_after_pistol_loss - p_force)
    p_force = p_force + must_win * (pol.p_force_must_win - p_force)
    full = can_full & ~anti
    force = ~can_full & ~anti & can_force & (u_force < p_force)
    buy = (ECO_CODE + full * (FULL_CODE - ECO_CODE) + (anti | force) * (FORCE_CODE - ECO_CODE)).astype(
        np.int8
    )
    eco = pol.eco_spend
    spend = (
        eco
        + full * (np.maximum(full_target - carried, pol.min_refill) - eco)
        + anti * (np.maximum(pol.anti_eco_spend - carried, pol.min_refill) - eco)
        + force * (np.maximum(full_target - carried, 0.0) - eco)
    )
    spend = np.minimum(spend, money)
    equip = carried + spend + ctx.econ.default_pistol_value
    return buy, spend, equip


def _play_round(
    st: _State,
    t1ct: BoolArray,
    u: npt.NDArray[np.float32],
    ctx: _Ctx,
    *,
    pistol: bool,
    after_pistol: bool,
    last_of_half: bool,
    map_point_score: int,
) -> tuple[BoolArray, I8Array]:
    """Play one round for every simulation in ``st``; returns (team1 won, buy codes (2, m))."""
    econ, flow = ctx.econ, ctx.flow
    ct = np.stack((t1ct, ~t1ct))
    if pistol:
        buy = np.full(ct.shape, PISTOL_CODE, dtype=np.int8)
        spend = st.money.copy()
        equip = spend + econ.default_pistol_value
    else:
        buy, spend, equip = _decide_buys(
            st,
            ct,
            u[2:4],
            ctx,
            after_pistol=after_pistol,
            last_of_half=last_of_half,
            map_point_score=map_point_score,
        )
    st.money -= spend

    # ---- outcome: u < 1 / (1 + exp(-eta))  <=>  u * (1 + exp(-eta)) < 1
    b1 = buy[0].astype(np.intp)
    b2 = buy[1].astype(np.intp)
    sb = ctx.ct_bias + (ctx.pistol_adj if pistol else 0.0)
    eta = np.take(ctx.table_flat, b1 * 4 + b2) + (ctx.d - sb) + t1ct * (2.0 * sb)
    t1w = u[0] * (1.0 + np.exp(np.minimum(-eta, 60.0))) < 1.0
    ct_won = t1w == t1ct

    # ---- end reason: CT wins by defuse / time / elimination, T wins by detonation / elimination
    ur = u[1]
    p_def = flow.p_ct_win_defuse
    reason = ct_won * (
        _DEFUSE * (ur < p_def) + _TIME * ((ur >= p_def) & (ur < p_def + flow.p_ct_win_time))
    ) + (~ct_won * (_DETONATION * (ur < flow.p_t_win_detonation)))

    # ---- deaths (inverse CDF with one uniform each, thresholds from lookup tables)
    bw = b2 + t1w * (b1 - b2)
    bl = b1 + b2 - bw
    combo = bw * 4 + bl
    w_dead = np.zeros(t1ct.shape, dtype=np.float64)
    for k in range(4):
        w_dead += u[4] > np.take(ctx.winner_cdf[k], combo)
    l_dead = np.zeros(t1ct.shape, dtype=np.float64)
    for k in range(5):
        l_dead += u[5] > np.take(ctx.loser_cdf[k], reason)
    dead1 = l_dead + t1w * (w_dead - l_dead)
    dead2 = w_dead + l_dead - dead1
    dead = np.stack((dead1, dead2))
    kills = np.stack((dead2, dead1))
    won = np.stack((t1w, ~t1w))

    # ---- income
    loser_bonus = np.take(ctx.ladder, st.streak)
    if not econ.t_time_loss_survivor_bonus:
        # Terrorists still alive when the timer expires receive no loss bonus (team average).
        loser_bonus = loser_bonus * (1.0 + (reason == _TIME) * (l_dead * 0.2 - 1.0))
    income = loser_bonus + won * (np.take(ctx.reward_by_reason, reason) - loser_bonus)
    income += np.take(ctx.kill_reward, buy) * kills * 0.2
    defuse = reason == _DEFUSE
    planted = (reason == _DETONATION) | defuse
    t_deaths = dead1 + t1ct * (dead2 - dead1)
    extra_t = econ.plant_bonus_team * defuse + (econ.planter_bonus * 0.2) * planted
    extra_ct = (econ.defuser_bonus * 0.2) * defuse + econ.ct_team_kill_bonus * t_deaths
    income += extra_t + ct * (extra_ct - extra_t)
    st.money = np.minimum(st.money + income, float(econ.max_money))

    # ---- loss streak (CS2: loss +1 up to the max; win steps down per mp_consecutive_loss_aversion)
    s = st.streak
    up = np.minimum(s + 1, econ.loss_streak_max)
    if econ.loss_aversion == 0:
        down = np.zeros_like(s)
    elif econ.loss_aversion == 1:
        down = np.maximum(s - 1, 0)
    else:
        step = (st.winstreak >= 1).astype(np.int16)
        down = np.maximum(s - step, 0)
        st.winstreak = ((st.winstreak + 1) * won).astype(np.int16)
    st.streak = (up + won * (down - up)).astype(np.int16)
    st.carried = (5.0 - dead) * (0.2 * flow.carry_fraction) * equip
    st.won_prev = won
    return t1w, buy


def _start_sides(side: Side | None, n: int) -> BoolArray:
    if side is Side.CT:
        return np.ones(n, dtype=bool)
    if side is Side.T:
        return np.zeros(n, dtype=bool)
    out = np.zeros(n, dtype=bool)
    out[: (n + 1) // 2] = True  # stratified 50/50 mixture
    return out


def _count_buys(buy: I8Array, active: BoolArray) -> FloatArray:
    codes = buy.astype(np.int64) + ~active * 4
    return np.bincount(codes.ravel(), minlength=8)[:4].astype(np.float64)


def _simulate_core(
    inp: MapSimInput, n: int, rng: np.random.Generator, *, record_rounds: bool = False
) -> tuple[PerSimRecords, dict[str, float], dict[str, npt.NDArray[Any]] | None]:
    econ = inp.economy
    rph = econ.rounds_per_half
    n_reg = econ.regulation_rounds
    win_n = econ.rounds_to_win
    ctx = _make_ctx(inp)
    targets = inp.race_targets
    t1_start_ct = _start_sides(inp.team1_start_side, n)
    st = _State.new(n, econ.start_money, econ.starting_losses)
    uniforms = rng.random((n_reg, _N_UNIFORMS, n), dtype=np.float32)

    active = np.ones(n, dtype=bool)
    race = np.zeros((len(targets), n), dtype=np.int8)
    # race to N is decided by the leader after 2N - 1 rounds (index 2N - 2)
    reg_snaps = {2 * tg - 2: i for i, tg in enumerate(targets) if 2 * tg - 2 < n_reg}
    r1 = np.zeros(n, dtype=bool)
    r13 = np.zeros(n, dtype=bool)
    h1 = np.zeros(n, dtype=np.int16)
    won_r2 = np.zeros(n, dtype=bool)
    buy_counts = np.zeros(4, dtype=np.float64)
    diag: dict[str, float] = {}
    log: dict[str, npt.NDArray[Any]] | None = None
    if record_rounds:
        log = {
            "buys": np.zeros((n_reg, 2, n), dtype=np.int8),
            "team1_won": np.zeros((n_reg, n), dtype=bool),
            "active": np.zeros((n_reg, n), dtype=bool),
            "team1_start_ct": t1_start_ct,
        }

    for r in range(n_reg):
        half_round = r % rph
        if r == rph:
            st.reset_half(econ.start_money, econ.starting_losses)
        t1ct = t1_start_ct if r < rph else ~t1_start_ct
        t1w, buy = _play_round(
            st,
            t1ct,
            uniforms[r],
            ctx,
            pistol=half_round == 0,
            after_pistol=half_round == 1,
            last_of_half=half_round == rph - 1,
            map_point_score=win_n - 1,
        )
        if log is not None:
            log["buys"][r] = buy
            log["team1_won"][r] = t1w
            log["active"][r] = active
        st.score[0] += t1w & active
        st.score[1] += active & ~t1w
        buy_counts += _count_buys(buy, active)
        if r == 0:
            r1 = t1w.copy()
        elif r == 1:
            loser_buy = np.where(r1, buy[1], buy[0])
            diag["r2_pistol_loser_eco"] = float(np.mean(loser_buy == ECO_CODE))
            diag["r2_pistol_loser_force"] = float(np.mean(loser_buy == FORCE_CODE))
            won_r2 = t1w == r1
            diag["r2_pistol_winner_wins"] = float(np.mean(won_r2))
        elif r == 2 and won_r2.any():
            diag["r3_bonus_round_winner_wins"] = float(np.mean((t1w == r1)[won_r2]))
        if r == rph - 1:
            h1 = st.score[0].copy()
        elif r == rph:
            r13 = t1w.copy()
        snap = reg_snaps.get(r)
        if snap is not None:
            race[snap] = np.where(st.score[0] > st.score[1], 1, 2)
        active &= (st.score[0] < win_n) & (st.score[1] < win_n)

    reg1 = st.score[0].copy()
    reg2 = st.score[1].copy()
    ot_idx = np.flatnonzero(active)
    late = {2 * tg - 2: i for i, tg in enumerate(targets) if 2 * tg - 2 >= n_reg}
    if ot_idx.size:
        _simulate_overtime(st, ot_idx, ~t1_start_ct[ot_idx], ctx, rng, n, race, late, buy_counts)
    # targets decided after regulation: simulations that ended before round 2N - 1 -> the winner
    # reached N iff its final score >= N (the loser cannot have).
    s1, s2 = st.score[0], st.score[1]
    for i in late.values():
        tg = targets[i]
        unset = race[i] == 0
        race[i] = np.where(
            unset & (s1 > s2) & (s1 >= tg), 1, np.where(unset & (s2 > s1) & (s2 >= tg), 2, race[i])
        )

    total_buys = buy_counts.sum()
    if total_buys > 0:
        names = ((PISTOL_CODE, "pistol"), (ECO_CODE, "eco"), (FORCE_CODE, "force"), (FULL_CODE, "full"))
        for code, name in names:
            diag[f"buy_share_{name}"] = float(buy_counts[code] / total_buys)
    rec = PerSimRecords(
        team1_rounds=s1.copy(),
        team2_rounds=s2.copy(),
        team1_start_ct=t1_start_ct,
        round1_team1=r1,
        round_half2_team1=r13,
        half1_team1=h1,
        half2_team1=(reg1 - h1).astype(np.int16),
        half2_team2=(reg2 - (rph - h1)).astype(np.int16),
        race=race,
    )
    return rec, diag, log


def _simulate_overtime(
    st: _State,
    idx: npt.NDArray[np.intp],
    t1ct_end_of_regulation: BoolArray,
    ctx: _Ctx,
    rng: np.random.Generator,
    n_total: int,
    race: I8Array,
    snaps: dict[int, int],
    buy_counts: FloatArray,
) -> None:
    """Play MR3 overtime periods for the tied simulations ``idx`` (updates ``st``/``race`` in place)."""
    econ = ctx.econ
    sub = st.take(idx)
    t1ct = t1ct_end_of_regulation.copy()
    active = np.ones(idx.size, dtype=bool)
    sub_race = race[:, idx]
    half = econ.ot_rounds_per_half
    n_reg = econ.regulation_rounds
    for k in range(econ.max_overtimes):
        base = econ.rounds_to_win - 1 + k * half
        detailed = k < OT_DETAILED_PERIODS
        for j in range(econ.ot_rounds):
            if j == 0:
                if econ.ot_switch_sides_at_start:
                    t1ct = ~t1ct
                sub.reset_half(econ.ot_start_money, econ.starting_losses)
            elif j == half:
                t1ct = ~t1ct
                sub.reset_half(econ.ot_start_money, econ.starting_losses)
            # Draw for every simulation and index by the original id: common random numbers.
            if detailed:
                u = rng.random((_N_UNIFORMS, n_total), dtype=np.float32)[:, idx]
                t1w, buy = _play_round(
                    sub,
                    t1ct,
                    u,
                    ctx,
                    pistol=False,
                    after_pistol=False,
                    last_of_half=j in (half - 1, econ.ot_rounds - 1),
                    map_point_score=base + econ.ot_rounds_to_win - 1,
                )
                buy_counts += _count_buys(buy, active)
            else:  # long overtimes: full buy vs full buy (economy irrelevant at $10k per half)
                u0 = rng.random(n_total, dtype=np.float32)[idx]
                eta = ctx.table[FULL_CODE, FULL_CODE] + np.where(
                    t1ct, ctx.d + ctx.ct_bias, ctx.d - ctx.ct_bias
                )
                t1w = u0 < special.expit(eta)
            sub.score[0] += t1w & active
            sub.score[1] += active & ~t1w
            snap = snaps.get(n_reg + k * econ.ot_rounds + j)
            if snap is not None:
                sub_race[snap] = np.where(active, np.where(sub.score[0] > sub.score[1], 1, 2), sub_race[snap])
            active &= (sub.score[0] - base < econ.ot_rounds_to_win) & (
                sub.score[1] - base < econ.ot_rounds_to_win
            )
            if not active.any():
                break
        if not active.any():
            break
    if active.any():  # beyond the safety cap (probability ~0.3**max_overtimes): coin flip by strength
        coin = rng.random(idx.size) < expit(ctx.d)
        sub.score[0] += active & coin
        sub.score[1] += active & ~coin
    st.score[:, idx] = sub.score
    race[:, idx] = sub_race


# ====================================================================================== analytic helpers


def iid_map_win_prob(p_round: float, economy: EconomyParams = DEFAULT_ECONOMY) -> float:
    """P(win map) if every round were won independently with probability ``p_round`` (MR12 + MR3 OT).

    Used for initial guesses of the strength calibration and as a sanity reference.
    """
    p = min(max(p_round, 1e-12), 1.0 - 1e-12)
    q = 1.0 - p
    n = economy.rounds_to_win
    p_reg = sum(math.comb(n - 1 + k, k) * p**n * q**k for k in range(n - 1))
    p_tie = math.comb(2 * (n - 1), n - 1) * (p * q) ** (n - 1)
    mo = economy.ot_rounds_to_win
    p_ot_period = sum(math.comb(mo - 1 + k, k) * p**mo * q**k for k in range(mo - 1))
    p_ot_tie = math.comb(2 * (mo - 1), mo - 1) * (p * q) ** (mo - 1)
    p_ot = p_ot_period / (1.0 - p_ot_tie)
    return float(p_reg + p_tie * p_ot)


def iid_round_prob_for_map(target_p: float, economy: EconomyParams = DEFAULT_ECONOMY) -> float:
    """Inverse of :func:`iid_map_win_prob`."""
    t = min(max(target_p, 1e-9), 1.0 - 1e-9)
    return float(optimize.brentq(lambda x: iid_map_win_prob(x, economy) - t, 1e-9, 1.0 - 1e-9, xtol=1e-12))


# ====================================================================================== simulator


class MapSimulator:
    """Runs map simulations with LRU caches for outputs and strength calibrations.

    Thread-safe: cache and statistics updates are guarded by a lock; simulations themselves run
    outside it (two threads asking for the same uncached run may both compute it - same result).
    """

    def __init__(self, *, cache_size: int = 64, calibration_cache_size: int = 4096) -> None:
        self._out_cache: OrderedDict[tuple[Any, ...], MapSimOutput] = OrderedDict()
        self._cal_cache: OrderedDict[tuple[Any, ...], float] = OrderedDict()
        self.cache_size = cache_size
        self.calibration_cache_size = calibration_cache_size
        self._lock = threading.RLock()
        self.stats: dict[str, int] = {
            "simulations": 0,
            "sim_cache_hits": 0,
            "calibrations": 0,
            "calibration_cache_hits": 0,
            "calibration_evaluations": 0,
        }

    def __getstate__(self) -> dict[str, Any]:
        """Picklable / deep-copyable: the lock is dropped and recreated."""
        state = self.__dict__.copy()
        state.pop("_lock", None)
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- simulate
    def simulate(
        self,
        inp: MapSimInput,
        n_sims: int = 20_000,
        rng: np.random.Generator | int | None = None,
        *,
        record_rounds: bool = False,
    ) -> MapSimOutput:
        """Simulate ``n_sims`` plays of the map. ``rng``: Generator, integer seed (cached) or None."""
        if n_sims < 2:
            raise ValueError("n_sims must be >= 2")
        key: tuple[Any, ...] | None = None
        if isinstance(rng, int | np.integer) and not record_rounds:
            key = (inp.cache_key(), int(n_sims), int(rng))
            with self._lock:
                hit = self._out_cache.get(key)
                if hit is not None:
                    self._out_cache.move_to_end(key)
                    self.stats["sim_cache_hits"] += 1
                    return hit
        gen = rng if isinstance(rng, np.random.Generator) else np.random.default_rng(rng)
        t0 = time.perf_counter()
        rec, diag, log = _simulate_core(inp, int(n_sims), gen, record_rounds=record_rounds)
        out = _build_output(
            inp.map_name,
            inp.strength_diff,
            inp.team1_start_side,
            inp.economy.rounds_per_half,
            inp.race_targets,
            rec,
            None,
            diagnostics=diag,
            elapsed_s=time.perf_counter() - t0,
            round_log=log,
        )
        with self._lock:
            self.stats["simulations"] += 1
            if key is not None:
                self._out_cache[key] = out
                while len(self._out_cache) > self.cache_size:
                    self._out_cache.popitem(last=False)
        return out

    # ---------------------------------------------------------------- calibration
    def calibrate_strength(
        self,
        target_p: float,
        inp: MapSimInput,
        *,
        n_sims: int = 20_000,
        rng_seed: int = 0,
        tol: float = 0.003,
        max_iter: int = 40,
    ) -> float:
        """Strength ``d`` such that the simulated P(team1 wins the map) is within ``tol`` of ``target_p``.

        The simulated win probability is (almost) monotone increasing in ``d`` and, with common
        random numbers (same ``rng_seed`` for every evaluation), a fine step function of ``d``.
        Root finding: deterministic initial guess (:meth:`initial_guess`), a bracketing search, then
        Illinois regula falsi. The result is a pure function of (input, target, n_sims, rng_seed,
        tol), so it is memoised. Targets are clipped to [0.001, 0.999].
        """
        d, _ = self.calibrate_and_simulate(
            target_p, inp, n_sims=n_sims, rng_seed=rng_seed, tol=tol, max_iter=max_iter
        )
        return d

    def calibrate_and_simulate(
        self,
        target_p: float,
        inp: MapSimInput,
        *,
        n_sims: int = 20_000,
        rng_seed: int = 0,
        tol: float = 0.003,
        max_iter: int = 40,
    ) -> tuple[float, MapSimOutput]:
        """:meth:`calibrate_strength` plus the simulation output at the calibrated ``d``."""
        target = min(max(float(target_p), P_MIN), P_MAX)
        base_key = inp.cache_key(include_strength=False)
        cal_key = (base_key, round(target, 9), int(n_sims), int(rng_seed), round(float(tol), 9))
        with self._lock:
            self.stats["calibrations"] += 1
            cached = self._cal_cache.get(cal_key)
            if cached is not None:
                self._cal_cache.move_to_end(cal_key)
                self.stats["calibration_cache_hits"] += 1
        if cached is not None:
            return cached, self.simulate(inp.with_strength(cached), n_sims, int(rng_seed))

        evaluated: dict[float, MapSimOutput] = {}

        def f(d: float) -> float:
            dd = float(min(max(d, D_MIN), D_MAX))
            out = self.simulate(inp.with_strength(dd), n_sims, int(rng_seed))
            evaluated[dd] = out
            with self._lock:
                self.stats["calibration_evaluations"] += 1
            return out.p_team1_win - target

        d_best = self._root(f, target, inp.economy, tol, max_iter, evaluated)
        with self._lock:
            self._cal_cache[cal_key] = d_best
            while len(self._cal_cache) > self.calibration_cache_size:
                self._cal_cache.popitem(last=False)
        return d_best, evaluated[d_best]

    @staticmethod
    def initial_guess(target: float, economy: EconomyParams = DEFAULT_ECONOMY) -> float:
        """Deterministic starting point: the i.i.d.-rounds strength scaled by :data:`IID_GAIN`.

        The economy adds momentum (round outcomes are positively correlated through money), which
        makes the simulated map more decisive per unit of ``d`` than i.i.d. rounds would be; for the
        default model the simulated curve needs ~9 % more ``d`` than the i.i.d. inversion. The guess
        is a pure function of the target, so calibrations (and therefore predictions) are
        reproducible and safe to memoise.
        """
        return IID_GAIN * logit(iid_round_prob_for_map(target, economy))

    def _root(
        self,
        f: Callable[[float], float],
        target: float,
        economy: EconomyParams,
        tol: float,
        max_iter: int,
        evaluated: dict[float, MapSimOutput],
    ) -> float:
        """Bracketing search + Illinois regula falsi; always returns an evaluated ``d``."""

        def best() -> float:
            return min(evaluated, key=lambda d: abs(evaluated[d].p_team1_win - target))

        d0 = float(min(max(self.initial_guess(target, economy), D_MIN), D_MAX))
        f0 = f(d0)
        if abs(f0) <= tol:
            return d0
        # first step from the local slope of the i.i.d.-rounds approximation
        h = 1e-3
        slope = (iid_map_win_prob(expit(d0 + h), economy) - iid_map_win_prob(expit(d0 - h), economy)) / (
            2 * h
        )
        slope = max(slope, 0.05 * expit(d0) * (1.0 - expit(d0)), 1e-3)
        step = float(np.clip(-f0 / slope, -2.0, 2.0))
        if abs(step) < 1e-3:
            step = -1e-3 if f0 > 0 else 1e-3
        d1 = float(min(max(d0 + step, D_MIN), D_MAX))
        f1 = f(d1)
        if abs(f1) <= tol:
            return d1
        expand = 0
        while f0 * f1 > 0 and expand < 14:
            if (d1 >= D_MAX and f1 < 0) or (d1 <= D_MIN and f1 > 0):
                return best()  # target unreachable within the bounds
            new_step = (d1 - d0) * 2.0
            d0, f0 = d1, f1
            d1 = float(min(max(d1 + new_step, D_MIN), D_MAX))
            f1 = f(d1)
            if abs(f1) <= tol:
                return d1
            expand += 1
        if f0 * f1 > 0:
            return best()
        a, fa, b, fb = d0, f0, d1, f1
        for _ in range(max_iter):
            c = b - fb * (b - a) / (fb - fa) if fb != fa else 0.5 * (a + b)
            if not (min(a, b) < c < max(a, b)):
                c = 0.5 * (a + b)
            fc = f(c)
            if abs(fc) <= tol:
                return c
            if fc * fb < 0:
                a, fa = b, fb
            else:
                fa *= 0.5  # Illinois modification: avoid a stagnant endpoint
            b, fb = c, fc
            if abs(b - a) < 1e-7:
                break
        return best()

    def clear_caches(self) -> None:
        with self._lock:
            self._out_cache.clear()
            self._cal_cache.clear()

    def stats_snapshot(self) -> dict[str, int]:
        """A consistent copy of :attr:`stats`."""
        with self._lock:
            return dict(self.stats)


#: Process-wide default simulator (caches shared across predictions in one process).
DEFAULT_SIMULATOR = MapSimulator()


def simulate_map(
    inp: MapSimInput, n_sims: int = 20_000, rng: np.random.Generator | int | None = None
) -> MapSimOutput:
    """Module-level shortcut for :meth:`MapSimulator.simulate` on :data:`DEFAULT_SIMULATOR`."""
    return DEFAULT_SIMULATOR.simulate(inp, n_sims, rng)


def calibrate_strength(
    target_p: float,
    inp: MapSimInput,
    *,
    n_sims: int = 20_000,
    rng_seed: int = 0,
    tol: float = 0.003,
) -> float:
    """Module-level shortcut for :meth:`MapSimulator.calibrate_strength` (uses the shared cache)."""
    return DEFAULT_SIMULATOR.calibrate_strength(target_p, inp, n_sims=n_sims, rng_seed=rng_seed, tol=tol)


__all__ = [
    "DEFAULT_RACE_TARGETS",
    "DEFAULT_SIMULATOR",
    "OT_DETAILED_PERIODS",
    "MapSimInput",
    "MapSimOutput",
    "MapSimulator",
    "PerSimRecords",
    "calibrate_strength",
    "iid_map_win_prob",
    "iid_round_prob_for_map",
    "simulate_map",
]
