"""Match engine: veto distribution x per-map estimates x map simulations -> :class:`MatchPrediction`.

Steps
-----
1. **Logit shift.** Context flags arrive as one bounded ``logit_shift`` applied to team1's win logit
   on every map: ``p' = expit(logit(p) + shift)``.
2. **Scenarios.** ``central`` uses ``p'``; ``low`` / ``high`` move every map's logit by
   ``-/+ sigma_logit`` with ``sigma_logit = sigma / (p (1 - p))`` (delta method at the estimate's own
   ``p``, capped at :data:`MAX_SIGMA_LOGIT`), perfectly correlated across maps, i.e. team1's
   strength shifted by one standard deviation everywhere at once.
3. **Simulations.** Per scenario and map: :meth:`MapSimulator.calibrate_and_simulate` finds the round
   strength ``d`` that reproduces the map probability, the output is reweighted to hit it exactly
   (:meth:`MapSimOutput.reweighted`), and results are cached per (map, start side, p rounded to
   1e-4). Every map uses one seed in all scenarios (common random numbers -> low < central < high).
4. **Series math (exact).** For each veto sequence (probability ``q``) the series dynamic programme
   (:mod:`cs2bot.models.sim_series`) gives the final map-score distribution and P(slot k played).
   Slot k's map distribution is ``P(map | slot k played)``; all slot distributions are mixtures over
   maps conditional on the slot being played (a map's outcome is independent of whether it is
   played, which depends only on earlier maps).
5. **Player props.** Per map, each player's kill pmf is a NB mixture over the simulated (rounds,
   margin) distribution (:mod:`cs2bot.models.player_props`). Scopes: ``"map:k"`` (mixture over the
   slot's maps, conditional on the slot being played), ``"maps:1,2"`` (and ``"maps:1,2,3"`` for BO5;
   always-played slots, convolution given the scenario), ``"match"`` (sum over played maps through
   the series programme with per-map pmfs conditional on that map's winner, so the number of maps
   played and the kills stay consistently correlated). Top-fragger probabilities come from one joint
   simulation of whole series (veto sequence -> simulated scorelines -> kills of all players), ties
   split evenly; besides the documented scopes, team-restricted versions are stored under
   ``"<scope>|team1"`` / ``"<scope>|team2"``.
6. ``sigma_team1_win = |p_high - p_low| / 2``.

Runtime: one calibration needs 1-2 simulations of ~0.1 s (20k simulations); a BO3 whose veto
distribution covers all 7 maps needs 21 calibrations (~2-4 s in total including player props). The
low/high scenarios use ``scenario_n_sims`` simulations (default ``max(2000, n_sims // 2)``): their
only use is the sigma of each outcome, and they share common random numbers with each other (same
seed and size per map), so ``p_high - p_low`` is far less noisy than either estimate. Maps whose
``sigma`` is 0 reuse the central run in all scenarios.

Not thread-safe when the shared :data:`~cs2bot.models.map_sim.DEFAULT_SIMULATOR` is used; pass
``simulator=MapSimulator()`` per thread.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import numpy as np

from cs2bot.domain.context import ContextFlag
from cs2bot.domain.enums import MatchFormat, Side, TeamSlot
from cs2bot.domain.predictions import (
    MapSequenceProbability,
    MapSlotPrediction,
    MapWinEstimate,
    MatchPrediction,
    PlayerProjection,
    PredictionCore,
)
from cs2bot.models.economy import (
    DEFAULT_BUY_POLICY,
    DEFAULT_ECONOMY,
    DEFAULT_ROUND_FLOW,
    BuyPolicy,
    EconomyParams,
    RoundFlowParams,
)
from cs2bot.models.map_sim import (
    DEFAULT_RACE_TARGETS,
    DEFAULT_SIMULATOR,
    MapSimInput,
    MapSimOutput,
    MapSimulator,
)
from cs2bot.models.player_props import (
    KillModelConfig,
    KillsMixture,
    PlayerInput,
    PlayerKillModel,
    PlayerRate,
    effective_size,
    kills_mixture,
    top_fragger_probs,
)
from cs2bot.models.round_model import RoundModel
from cs2bot.models.sim_series import played_mask, series_outcome, series_sum_pmf
from cs2bot.models.sim_stats import (
    FloatArray,
    expit,
    logit,
    mix_dicts,
    mix_pmfs,
    pmf_moments,
    pmf_to_dict,
    sort_score_keys,
    stable_seed,
)
from cs2bot.utils.names import normalize_map_name
from cs2bot.utils.time import ensure_utc, utcnow

ENGINE_VERSION = "p6-match-engine/1"
SCENARIOS: tuple[str, ...] = ("central", "low", "high")
MAX_SIGMA_LOGIT = 2.5
P_CLIP = 1e-4

__all__ = [
    "ENGINE_VERSION",
    "MAX_SIGMA_LOGIT",
    "SCENARIOS",
    "PlayerInput",
    "build_match_prediction",
    "scenario_probabilities",
]


@dataclass(frozen=True)
class _Seq:
    maps: tuple[str, ...]
    pickers: tuple[TeamSlot | None, ...]
    q: float


@dataclass(frozen=True)
class _MapRun:
    map_name: str
    side: Side | None
    target_p: float
    d: float
    out: MapSimOutput


@dataclass(frozen=True)
class _Kernel:
    """Kill distribution of one player on one map under one scenario."""

    mixture: KillsMixture
    pmf: FloatArray
    pmf_team1_wins: FloatArray
    pmf_team2_wins: FloatArray
    deaths_mean: float


def _side_value(side: Side | None) -> str | None:
    return side.value if side is not None else None


def _clip_p(p: float) -> float:
    return min(max(float(p), P_CLIP), 1.0 - P_CLIP)


def scenario_probabilities(
    estimate: MapWinEstimate, logit_shift: float = 0.0, max_sigma_logit: float = MAX_SIGMA_LOGIT
) -> tuple[float, float, float, float]:
    """(central, low, high, sigma_logit) team1 map-win probabilities for one map estimate."""
    p0 = _clip_p(estimate.p_team1_win)
    centre = logit(p0) + float(logit_shift)
    sigma_logit = 0.0
    if estimate.sigma > 0:
        sigma_logit = min(float(estimate.sigma) / (p0 * (1.0 - p0)), max_sigma_logit)
    return (
        _clip_p(expit(centre)),
        _clip_p(expit(centre - sigma_logit)),
        _clip_p(expit(centre + sigma_logit)),
        sigma_logit,
    )


def _prepare_sequences(seqs: Sequence[MapSequenceProbability], fmt: MatchFormat) -> tuple[list[_Seq], float]:
    """Normalise names, truncate to the format's map count, merge duplicates, renormalise q."""
    n = fmt.n_maps
    merged: dict[tuple[str, ...], tuple[tuple[TeamSlot | None, ...], float]] = {}
    total = 0.0
    for s in seqs:
        q = float(s.probability)
        if not np.isfinite(q) or q < 0:
            raise ValueError("veto sequence probabilities must be finite and >= 0")
        if q == 0.0:
            continue
        maps = tuple(normalize_map_name(m) for m in s.maps)
        if len(maps) < n:
            raise ValueError(f"veto sequence {maps} has fewer than the {n} maps a {fmt.value} needs")
        maps = maps[:n]
        if len(set(maps)) != len(maps):
            raise ValueError(f"veto sequence {maps} repeats a map")
        pickers = (*tuple(s.pickers[:n]), *([None] * max(0, n - len(s.pickers))))
        old = merged.get(maps)
        merged[maps] = (old[0] if old else pickers, (old[1] if old else 0.0) + q)
        total += q
    if total <= 0:
        raise ValueError("veto_distribution has no positive probability")
    out = [_Seq(maps=m, pickers=pk, q=q / total) for m, (pk, q) in merged.items()]
    out.sort(key=lambda s: -s.q)
    return out, total


def _validate_players(players: Sequence[PlayerInput]) -> list[PlayerInput]:
    seen: set[int] = set()
    out: list[PlayerInput] = []
    for pl in players:
        if pl.player_id in seen:
            raise ValueError(f"duplicate player {pl.player_id}")
        seen.add(pl.player_id)
        out.append(pl)
    return out


def _kernel(rate: PlayerRate, out: MapSimOutput, cfg: KillModelConfig) -> _Kernel:
    t1 = out.team1_rounds.astype(np.int64)
    t2 = out.team2_rounds.astype(np.int64)
    own, opp = (t1, t2) if rate.team is not TeamSlot.TEAM2 else (t2, t1)
    margin = own - opp
    w = out.weights
    center = float(np.average(margin, weights=w))
    mix = kills_mixture(
        rate,
        t1 + t2,
        margin=margin,
        weights=w,
        margin_center=center,
        score_skew=cfg.score_skew,
        hist_margin_var=cfg.hist_margin_var,
    )
    own_won = mix.margins > 0  # no draws on a map
    team1_won = own_won if rate.team is not TeamSlot.TEAM2 else ~own_won
    return _Kernel(
        mixture=mix,
        pmf=mix.pmf(),
        pmf_team1_wins=mix.pmf(team1_won),
        pmf_team2_wins=mix.pmf(~team1_won),
        deaths_mean=rate.dpr * mix.expected_rounds,
    )


def _projection(pl: PlayerInput, pmf: FloatArray, deaths_mean: float | None) -> PlayerProjection:
    mean, var = pmf_moments(pmf)
    return PlayerProjection(
        player_id=pl.player_id,
        nickname=pl.nickname,
        team=pl.team,
        kills_pmf=pmf_to_dict(pmf),
        kills_mean=mean,
        kills_var=var,
        deaths_mean=deaths_mean,
    )


def _top_fragger(
    fmt: MatchFormat,
    seqs: Sequence[_Seq],
    runs: Mapping[str, _MapRun],
    players: Sequence[PlayerInput],
    rates: Mapping[str, Mapping[int, PlayerRate]],
    cfg: KillModelConfig,
    *,
    seed: int,
    n_samples: int,
    always_played_scopes: Sequence[tuple[str, tuple[int, ...]]],
) -> dict[str, dict[str, float]]:
    """Joint simulation of whole series -> P(top fragger) per scope (ties split evenly)."""
    rng = np.random.default_rng(seed)
    n_maps = fmt.n_maps
    names = sorted(runs)
    mid = {m: i for i, m in enumerate(names)}
    q = np.asarray([s.q for s in seqs], dtype=np.float64)
    seq_idx = rng.choice(len(seqs), size=n_samples, p=q / q.sum())
    seq_maps = np.asarray([[mid[m] for m in s.maps] for s in seqs], dtype=np.int64)
    slot_map = seq_maps[seq_idx].T  # (n_maps, n_samples)
    w1 = np.zeros((n_maps, n_samples), dtype=np.int64)
    w2 = np.zeros((n_maps, n_samples), dtype=np.int64)
    for m, i in mid.items():
        out = runs[m].out
        for k in range(n_maps):
            rows = np.flatnonzero(slot_map[k] == i)
            if rows.size == 0:
                continue
            if out.weights is None:
                pick = rng.integers(0, out.n_sims, size=rows.size)
            else:
                pick = rng.choice(out.n_sims, size=rows.size, p=out.weights)
            w1[k, rows] = out.team1_rounds[pick]
            w2[k, rows] = out.team2_rounds[pick]
    played = played_mask(fmt, w1 > w2)

    centers = np.zeros(len(names))
    for m, i in mid.items():
        out = runs[m].out
        centers[i] = float(
            np.average(out.team1_rounds.astype(np.int64) - out.team2_rounds, weights=out.weights)
        )
    kills = np.zeros((n_maps, n_samples, len(players)), dtype=np.int64)
    skew, hmv = cfg.score_skew, cfg.hist_margin_var
    for j, pl in enumerate(players):
        kpr = np.asarray([rates[m][pl.player_id].kpr for m in names])
        s_eff = np.asarray(
            [
                effective_size(rates[m][pl.player_id].size, rates[m][pl.player_id].cv2, skew, hmv)
                for m in names
            ]
        )
        sign = 1.0 if pl.team is TeamSlot.TEAM1 else -1.0
        for k in range(n_maps):
            ids = slot_map[k]
            r = (w1[k] + w2[k]).astype(np.float64)
            margin = sign * (w1[k] - w2[k]) - sign * np.take(centers, ids)
            mult = np.maximum(r + skew * margin, 0.1 * r) if skew > 0 else r
            mu = np.maximum(np.take(kpr, ids) * mult, 1e-9)
            shape = np.take(s_eff, ids)
            lam = rng.gamma(shape=shape, scale=mu / shape)
            kills[k, :, j] = rng.poisson(lam) * played[k]

    ids_all = [pl.player_id for pl in players]
    team_cols = {
        slot: [j for j, pl in enumerate(players) if pl.team is slot]
        for slot in (TeamSlot.TEAM1, TeamSlot.TEAM2)
    }

    def add(scope: str, mat: np.ndarray, mask: np.ndarray | None) -> None:
        result[scope] = top_fragger_probs(mat, ids_all, row_mask=mask)
        for slot, cols in team_cols.items():
            if cols:
                result[f"{scope}|{slot.value}"] = top_fragger_probs(
                    mat[:, cols], [ids_all[c] for c in cols], row_mask=mask
                )

    result: dict[str, dict[str, float]] = {}
    for k in range(n_maps):
        add(f"map:{k + 1}", kills[k], played[k])
    for scope, slots in always_played_scopes:
        add(scope, kills[list(slots)].sum(axis=0), None)
    add("match", kills.sum(axis=0), None)
    return result


def _build_core(
    fmt: MatchFormat,
    seqs: Sequence[_Seq],
    runs: Mapping[str, _MapRun],
    players: Sequence[PlayerInput],
    rates: Mapping[str, Mapping[int, PlayerRate]],
    cfg: KillModelConfig | None,
    *,
    rounds_to_win: int,
    tf_seed: int,
    tf_samples: int,
) -> PredictionCore:
    n_maps = fmt.n_maps
    p_map = {m: r.out.p_team1_win for m, r in runs.items()}
    results = [series_outcome(fmt, [p_map[m] for m in s.maps]) for s in seqs]

    final: dict[tuple[int, int], float] = {}
    for s, res in zip(seqs, results, strict=True):
        for state, pr in res.final.items():
            final[state] = final.get(state, 0.0) + s.q * pr
    map_score_pmf = {
        f"{a}-{b}": p for (a, b), p in sorted(final.items(), key=lambda kv: (-kv[0][0], kv[0][1])) if p > 0
    }
    p_team1 = sum(p for (a, b), p in final.items() if a > b)
    p_draw = sum(p for (a, b), p in final.items() if a == b)

    slot_dists: list[dict[str, float]] = []
    slot_played: list[float] = []
    for k in range(n_maps):
        weights: dict[str, float] = {}
        for s, res in zip(seqs, results, strict=True):
            weights[s.maps[k]] = weights.get(s.maps[k], 0.0) + s.q * res.p_played[k]
        pp = sum(weights.values())
        if pp <= 1e-15:  # practically never played: fall back to the unconditional veto mix
            weights = {}
            for s in seqs:
                weights[s.maps[k]] = weights.get(s.maps[k], 0.0) + s.q
            norm = sum(weights.values())
        else:
            norm = pp
        slot_played.append(min(max(pp, 0.0), 1.0))
        slot_dists.append({m: v / norm for m, v in sorted(weights.items(), key=lambda kv: -kv[1]) if v > 0})

    kernels: dict[tuple[str, int], _Kernel] = {}
    if players and cfg is not None:
        for m, run in runs.items():
            for pl in players:
                kernels[(m, pl.player_id)] = _kernel(rates[m][pl.player_id], run.out, cfg)

    slots: list[MapSlotPrediction] = []
    for k in range(n_maps):
        dist = slot_dists[k]
        outs = [(w, runs[m].out) for m, w in dist.items()]
        race_keys = [
            str(t)
            for t in runs[next(iter(dist))].out.race_targets
            if t <= rounds_to_win and all(o.p_neither_race_to.get(str(t), 0.0) < 1e-12 for _, o in outs)
        ]
        slot_players: dict[str, PlayerProjection] = {}
        for pl in players if kernels else ():
            pmf = mix_pmfs([(w, kernels[(m, pl.player_id)].pmf) for m, w in dist.items()])
            deaths = sum(w * kernels[(m, pl.player_id)].deaths_mean for m, w in dist.items())
            slot_players[str(pl.player_id)] = _projection(pl, pmf, deaths)
        slots.append(
            MapSlotPrediction(
                map_number=k + 1,
                p_played=slot_played[k],
                map_distribution=dist,
                p_team1_win=float(sum(w * o.p_team1_win for w, o in outs)),
                score_pmf=sort_score_keys(mix_dicts([(w, o.score_pmf) for w, o in outs])),
                p_overtime=float(sum(w * o.p_overtime for w, o in outs)),
                p_team1_pistol=mix_dicts([(w, o.p_team1_pistol) for w, o in outs]),
                p_team1_half=mix_dicts([(w, o.p_team1_half) for w, o in outs]),
                p_half_draw=mix_dicts([(w, o.p_half_draw) for w, o in outs]),
                p_team1_race_to={
                    key: v
                    for key, v in mix_dicts([(w, o.p_team1_race_to) for w, o in outs]).items()
                    if key in race_keys
                },
                players=slot_players,
            )
        )

    always_scopes: list[tuple[str, tuple[int, ...]]] = []
    if fmt.min_maps >= 2:
        always_scopes.append(("maps:1,2", (0, 1)))
    if fmt.min_maps >= 3:
        always_scopes.append(("maps:1,2,3", (0, 1, 2)))

    by_scope: dict[str, dict[str, PlayerProjection]] = {}
    top: dict[str, dict[str, float]] = {}
    if kernels:
        for k, slot in enumerate(slots):
            by_scope[f"map:{k + 1}"] = dict(slot.players)
        for scope, idx in always_scopes:
            scope_proj: dict[str, PlayerProjection] = {}
            for pl in players:
                items: list[tuple[float, FloatArray]] = []
                deaths = 0.0
                for s in seqs:
                    pmf = kernels[(s.maps[idx[0]], pl.player_id)].pmf
                    for i in idx[1:]:
                        pmf = np.convolve(pmf, kernels[(s.maps[i], pl.player_id)].pmf)
                    items.append((s.q, pmf))
                    deaths += s.q * sum(kernels[(s.maps[i], pl.player_id)].deaths_mean for i in idx)
                scope_proj[str(pl.player_id)] = _projection(pl, mix_pmfs(items), deaths)
            by_scope[scope] = scope_proj
        match_proj: dict[str, PlayerProjection] = {}
        for pl in players:
            items = []
            deaths = 0.0
            for s, res in zip(seqs, results, strict=True):
                ks = [kernels[(m, pl.player_id)] for m in s.maps]
                pmf = series_sum_pmf(
                    fmt,
                    [p_map[m] for m in s.maps],
                    [kk.pmf_team1_wins for kk in ks],
                    [kk.pmf_team2_wins for kk in ks],
                )
                items.append((s.q, pmf))
                deaths += s.q * sum(res.p_played[i] * ks[i].deaths_mean for i in range(n_maps))
            match_proj[str(pl.player_id)] = _projection(pl, mix_pmfs(items), deaths)
        by_scope["match"] = match_proj
        assert cfg is not None
        top = _top_fragger(
            fmt,
            seqs,
            runs,
            players,
            rates,
            cfg,
            seed=tf_seed,
            n_samples=tf_samples,
            always_played_scopes=always_scopes,
        )
    return PredictionCore(
        p_team1_win=float(p_team1),
        p_draw=float(p_draw),
        map_score_pmf=map_score_pmf,
        slots=slots,
        player_kills_by_scope=by_scope,
        top_fragger=top,
    )


def build_match_prediction(
    *,
    match_id: int,
    fmt: MatchFormat,
    team1_id: int,
    team2_id: int,
    veto_distribution: list[MapSequenceProbability],
    map_estimates: dict[str, MapWinEstimate],
    round_model: RoundModel,
    economy: EconomyParams | None = None,
    start_sides: dict[str, Side | None] | None = None,
    players: list[PlayerInput] | None = None,
    kill_model: PlayerKillModel | None = None,
    logit_shift: float = 0.0,
    context_flags: Sequence[ContextFlag] = (),
    n_sims: int = 20000,
    seed: int | None = None,
    model_version: str,
    data_completeness: float = 0.0,
    veto_known: bool = False,
    created_at: datetime | None = None,
    as_of: datetime | None = None,
    scenario_n_sims: int | None = None,
    policy: BuyPolicy | None = None,
    flow: RoundFlowParams | None = None,
    race_targets: tuple[int, ...] = DEFAULT_RACE_TARGETS,
    tol: float = 0.003,
    top_fragger_samples: int | None = None,
    simulator: MapSimulator | None = None,
) -> MatchPrediction:
    """Full :class:`MatchPrediction` (central + low/high scenarios) for one match.

    Required: ``veto_distribution`` (ordered map sequences with probabilities; renormalised, truncated
    to the format's map count) and a :class:`MapWinEstimate` for every map that appears in it.
    ``start_sides`` maps map name -> team1's starting side (missing/None = unknown, 50/50 mixture).
    ``players`` + ``kill_model`` enable player kill and top-fragger distributions; projections use
    data strictly before ``as_of`` (default ``created_at``, default now). ``seed`` makes the result
    reproducible (a random seed is drawn and recorded in ``components["seed"]`` otherwise).
    Extra keyword-only knobs: ``scenario_n_sims`` (simulations for low/high, default
    ``max(2000, n_sims // 2)``), ``policy`` / ``flow`` (behaviour assumptions), ``race_targets``,
    ``tol`` (calibration tolerance), ``top_fragger_samples`` (joint series simulations, default
    ``n_sims``), ``simulator`` (defaults to the shared cached :data:`DEFAULT_SIMULATOR`).
    """
    t_start = time.perf_counter()
    if created_at is not None and created_at.tzinfo is None:
        raise ValueError("created_at must be timezone-aware (UTC)")
    if as_of is not None and as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware (UTC)")
    created = ensure_utc(created_at) if created_at is not None else utcnow()
    as_of_dt = ensure_utc(as_of) if as_of is not None else created
    if n_sims < 200:
        raise ValueError("n_sims must be >= 200")
    sc_sims = int(scenario_n_sims) if scenario_n_sims is not None else max(2000, n_sims // 2)
    sc_sims = max(200, min(sc_sims, n_sims))
    econ = economy or DEFAULT_ECONOMY
    pol = policy or DEFAULT_BUY_POLICY
    flw = flow or DEFAULT_ROUND_FLOW
    sim = simulator or DEFAULT_SIMULATOR
    warnings: list[str] = []

    seqs, veto_mass = _prepare_sequences(veto_distribution, fmt)
    estimates = {normalize_map_name(k): v for k, v in map_estimates.items()}
    maps_needed = sorted({m for s in seqs for m in s.maps})
    missing = [m for m in maps_needed if m not in estimates]
    if missing:
        raise ValueError(f"no MapWinEstimate for maps in the veto distribution: {missing}")
    sides = {normalize_map_name(k): v for k, v in (start_sides or {}).items()}
    player_list = _validate_players(players or []) if kill_model is not None else []
    if players and kill_model is None:
        warnings.append("players given without kill_model: player props skipped")

    scen_p: dict[str, dict[str, float]] = {sc: {} for sc in SCENARIOS}
    sigma_logit: dict[str, float] = {}
    for m in maps_needed:
        c, lo, hi, sl = scenario_probabilities(estimates[m], logit_shift)
        scen_p["central"][m], scen_p["low"][m], scen_p["high"][m] = c, lo, hi
        sigma_logit[m] = sl

    base_seed = int(seed) if seed is not None else int(np.random.default_rng().integers(0, 2**62))
    t_sim = time.perf_counter()
    stats_before = dict(sim.stats)
    runs: dict[str, dict[str, _MapRun]] = {sc: {} for sc in SCENARIOS}
    local: dict[tuple[str, Side | None, float, int], _MapRun] = {}
    hits = misses = 0
    for scenario in SCENARIOS:
        n_sc = n_sims if scenario == "central" else sc_sims
        for m in maps_needed:
            side = sides.get(m)
            p = scen_p[scenario][m]
            if scenario != "central" and round(p, 4) == round(scen_p["central"][m], 4):
                # sigma == 0 for this map: reuse the central run so derived markets get no MC noise
                central_run = runs["central"][m]
                if central_run.target_p != p:
                    central_run = replace(central_run, target_p=p, out=central_run.out.reweighted(p))
                runs[scenario][m] = central_run
                hits += 1
                continue
            key = (m, side, round(p, 4), n_sc)
            cached_run = local.get(key)
            if cached_run is None:
                misses += 1
                inp = MapSimInput(
                    map_name=m,
                    team1_start_side=side,
                    economy=econ,
                    round_model=round_model,
                    policy=pol,
                    flow=flw,
                    race_targets=race_targets,
                )
                map_seed = stable_seed(base_seed, m, side.value if side is not None else None)
                d, out = sim.calibrate_and_simulate(round(p, 4), inp, n_sims=n_sc, rng_seed=map_seed, tol=tol)
                if abs(out.p_team1_win - p) > max(5 * tol, 0.02):
                    warnings.append(f"{m}: calibration reached {out.p_team1_win:.4f} for target {p:.4f}")
                run = _MapRun(map_name=m, side=side, target_p=p, d=d, out=out.reweighted(p))
                local[key] = run
            else:
                hits += 1
                run = cached_run
                if run.target_p != p:
                    run = _MapRun(map_name=m, side=side, target_p=p, d=run.d, out=run.out.reweighted(p))
            runs[scenario][m] = run
    t_sim_done = time.perf_counter()

    rates: dict[str, dict[int, PlayerRate]] = {}
    cfg = kill_model.config if kill_model is not None else None
    if kill_model is not None and player_list:
        for m in maps_needed:
            rates[m] = kill_model.lineup_rates(
                player_list, map_name=m, team1_id=team1_id, team2_id=team2_id, as_of=as_of_dt
            )
        unknown = [pl.player_id for pl in player_list if not rates[maps_needed[0]][pl.player_id].known]
        if unknown:
            warnings.append(f"players without history (role/global prior used): {unknown}")
    tf_seed = stable_seed(base_seed, "top-fragger")
    tf_samples = int(top_fragger_samples or n_sims)
    cores = {
        sc: _build_core(
            fmt,
            seqs,
            runs[sc],
            player_list,
            rates,
            cfg,
            rounds_to_win=econ.rounds_to_win,
            tf_seed=tf_seed,
            tf_samples=tf_samples,
        )
        for sc in SCENARIOS
    }
    t_done = time.perf_counter()

    central, low, high = cores["central"], cores["low"], cores["high"]
    components: dict[str, Any] = {
        "engine": ENGINE_VERSION,
        "seed": base_seed,
        "n_sims": {"central": n_sims, "low": sc_sims, "high": sc_sims},
        "scenario_map_p": {sc: {m: round(p, 6) for m, p in scen_p[sc].items()} for sc in SCENARIOS},
        "map_strength_d": {sc: {m: round(r.d, 6) for m, r in runs[sc].items()} for sc in SCENARIOS},
        "sigma_logit": {m: round(v, 6) for m, v in sigma_logit.items()},
        "start_sides": {m: _side_value(sides.get(m)) for m in maps_needed},
        "veto": {"input_mass": veto_mass, "n_sequences": len(seqs)},
        "cache": {
            "local_hits": hits,
            "local_misses": misses,
            "calibration_cache_hits": sim.stats["calibration_cache_hits"]
            - stats_before.get("calibration_cache_hits", 0),
            "simulations_run": sim.stats["simulations"] - stats_before.get("simulations", 0),
            "sim_cache_hits": sim.stats["sim_cache_hits"] - stats_before.get("sim_cache_hits", 0),
        },
        "timing_s": {
            "simulate": round(t_sim_done - t_sim, 4),
            "assemble": round(t_done - t_sim_done, 4),
            "total": round(t_done - t_start, 4),
        },
        "round_model": round_model.fingerprint(),
        "economy": {"ot_start_money": econ.ot_start_money, "rounds_to_win": econ.rounds_to_win},
        "map_diagnostics": {
            m: {
                "mean_total_rounds": round(r.out.mean_total_rounds, 4),
                "p_overtime": round(r.out.p_overtime, 6),
            }
            for m, r in runs["central"].items()
        },
        "player_props": {
            "n_players": len(player_list),
            "as_of": as_of_dt.isoformat(),
            "score_skew": cfg.score_skew if cfg is not None else None,
            "team_normalisation": cfg.normalise_teams if cfg is not None else None,
            "top_fragger_samples": tf_samples if player_list else 0,
        },
        "warnings": warnings,
    }
    return MatchPrediction(
        match_id=match_id,
        created_at=created,
        model_version=model_version,
        format=fmt,
        team1_id=team1_id,
        team2_id=team2_id,
        central=central,
        low=low,
        high=high,
        sigma_team1_win=abs(high.p_team1_win - low.p_team1_win) / 2.0,
        map_estimates=estimates,
        veto_distribution=list(veto_distribution),
        veto_known=veto_known,
        context_flags=list(context_flags),
        logit_shift_applied=float(logit_shift),
        data_completeness=float(data_completeness),
        n_simulations=n_sims,
        components=components,
    )
