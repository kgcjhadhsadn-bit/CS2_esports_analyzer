"""PlayerKillModel: EB rates, adjustments, NB dispersion, projections, top fragger, no look-ahead."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from scipy import stats

from cs2bot.domain.enums import PlayerRole, TeamSlot
from cs2bot.domain.records import PlayerMapRecord
from cs2bot.models.player_props import (
    KillModelConfig,
    PlayerInput,
    PlayerKillModel,
    effective_size,
    kills_distribution,
    kills_mixture,
    top_fragger_probs,
)
from cs2bot.testing.synthetic import SynthUniverse
from tests.models.sim.sim_helpers import AS_OF, lineup

T0 = datetime(2025, 1, 1, tzinfo=UTC)


def _records(
    rates: dict[int, float],
    *,
    n_maps: int,
    size: float = 1e9,
    role: PlayerRole = PlayerRole.RIFLER,
    team_of: dict[int, int] | None = None,
    opp: int = 99,
    map_name: str = "mirage",
    rounds: int = 22,
    start: datetime = T0,
    seed: int = 0,
) -> list[PlayerMapRecord]:
    rng = np.random.default_rng(seed)
    out = []
    for j in range(n_maps):
        for pid, kpr in rates.items():
            mu = kpr * rounds
            lam = rng.gamma(size, mu / size) if size < 1e8 else mu
            out.append(
                PlayerMapRecord(
                    match_id=j,
                    map_number=1,
                    map_name=map_name,
                    played_at=start + timedelta(days=j),
                    player_id=pid,
                    team_id=(team_of or {}).get(pid, 1),
                    opponent_team_id=opp,
                    kills=int(rng.poisson(lam)),
                    deaths=int(rng.poisson(0.68 * rounds)),
                    rounds_played=rounds,
                    role=role,
                )
            )
    return out


def test_rates_track_true_skill_on_synthetic(
    fitted_kill_model: PlayerKillModel, synthetic_universe: SynthUniverse
) -> None:
    uni = synthetic_universe
    est, true = [], []
    for p in uni.players.values():
        r = fitted_kill_model.rate(p.id, "mirage", None, as_of=AS_OF)
        assert r.known
        est.append(r.base_kpr)
        true.append(p.skill)
    rho = stats.spearmanr(est, true).statistic
    assert rho > 0.7, rho
    summary = fitted_kill_model.summary(AS_OF)
    assert summary["role_kpr"]["awper"] > summary["role_kpr"]["igl"]
    assert 0.3 < summary["global_kpr"] < 1.0


def test_projection_mean_is_kpr_times_expected_rounds(fitted_kill_model: PlayerKillModel) -> None:
    rounds_pmf = {19: 0.2, 22: 0.5, 25: 0.2, 30: 0.1}
    exp_rounds = sum(k * v for k, v in rounds_pmf.items())
    proj = fitted_kill_model.project(5, TeamSlot.TEAM1, "nuke", 2, rounds_pmf, as_of=AS_OF, nickname="x")
    rate = fitted_kill_model.rate(5, "nuke", 2, as_of=AS_OF)
    assert proj.kills_mean == pytest.approx(rate.kpr * exp_rounds, rel=1e-6)
    assert sum(proj.kills_pmf.values()) == pytest.approx(1.0)
    assert proj.kills_var > proj.kills_mean * 0.99  # NB over-dispersion (never under-dispersed)
    assert proj.deaths_mean == pytest.approx(rate.dpr * exp_rounds)
    assert proj.nickname == "x" and proj.team is TeamSlot.TEAM1
    # simulated arrays with team rounds: the score skew keeps the marginal mean (centred margin)
    rng = np.random.default_rng(0)
    own = rng.integers(5, 14, 5000)
    opp = np.where(own == 13, rng.integers(0, 12, 5000), 13)
    proj_arr = fitted_kill_model.project(
        5, TeamSlot.TEAM1, "nuke", 2, own + opp, as_of=AS_OF, team_rounds=(own, opp)
    )
    assert proj_arr.kills_mean == pytest.approx(rate.kpr * float(np.mean(own + opp)), rel=0.01)


def test_score_skew_makes_winning_maps_higher() -> None:
    model = PlayerKillModel().fit(_records({1: 0.7, 2: 0.7}, n_maps=20))
    rate = model.rate(1, "mirage", None, as_of=T0 + timedelta(days=30))
    own = np.array([13] * 50 + [6] * 50)
    opp = np.array([6] * 50 + [13] * 50)
    mix = kills_mixture(rate, own + opp, margin=own - opp, score_skew=0.3, hist_margin_var=0.12)
    win = mix.distribution(mix.margins > 0)
    loss = mix.distribution(mix.margins < 0)
    total = mix.distribution()
    assert win.mean > total.mean > loss.mean
    assert total.mean == pytest.approx(rate.kpr * 19, rel=1e-6)
    flat = kills_distribution(rate, own + opp)
    assert flat.mean == pytest.approx(total.mean, rel=1e-6)


def test_empirical_bayes_shrinkage_toward_role_mean() -> None:
    rng = np.random.default_rng(3)
    pop = {pid: float(rng.normal(0.70, 0.06)) for pid in range(1, 41)}
    recs = _records(pop, n_maps=40, role=PlayerRole.AWPER, seed=1)
    recs += _records({500: 1.4}, n_maps=2, role=PlayerRole.AWPER, seed=2)  # 2 hot maps only
    model = PlayerKillModel().fit(recs)
    as_of = T0 + timedelta(days=60)
    hot = model.rate(500, "mirage", None, as_of=as_of)
    role_mean = model.summary(as_of)["role_kpr"]["awper"]
    raw = sum(r.kills for r in recs if r.player_id == 500) / sum(
        r.rounds_played for r in recs if r.player_id == 500
    )
    assert role_mean < hot.base_kpr < raw
    assert hot.base_kpr - role_mean < 0.5 * (raw - role_mean)  # most of the way back to the prior
    steady = model.rate(1, "mirage", None, as_of=as_of)
    assert abs(steady.base_kpr - pop[1]) < 0.06  # plenty of data: close to the truth
    assert hot.cv2 > steady.cv2


def test_unknown_player_gets_role_prior_with_wider_dispersion(fitted_kill_model: PlayerKillModel) -> None:
    known = fitted_kill_model.rate(1, "mirage", None, as_of=AS_OF)
    unknown = fitted_kill_model.rate(999_999, "mirage", None, as_of=AS_OF, role=PlayerRole.AWPER)
    assert not unknown.known and unknown.role is PlayerRole.AWPER
    summary = fitted_kill_model.summary(AS_OF)
    assert unknown.base_kpr == pytest.approx(summary["role_kpr"]["awper"])
    assert unknown.cv2 > 5 * known.cv2
    nobody = fitted_kill_model.rate(999_998, "mirage", None, as_of=AS_OF)
    assert nobody.base_kpr == pytest.approx(summary["global_kpr"], rel=0.05)
    rounds = {22: 1.0}
    pk = fitted_kill_model.project(1, TeamSlot.TEAM1, "mirage", None, rounds, as_of=AS_OF)
    pu = fitted_kill_model.project(
        999_999, TeamSlot.TEAM1, "mirage", None, rounds, as_of=AS_OF, role=PlayerRole.AWPER
    )
    assert pu.kills_var / pu.kills_mean > pk.kills_var / pk.kills_mean


def test_no_look_ahead() -> None:
    early = _records({1: 0.6, 2: 0.8}, n_maps=30, seed=4)
    late = _records({1: 1.4, 2: 0.2}, n_maps=30, start=T0 + timedelta(days=100), seed=5)
    as_of = T0 + timedelta(days=50)
    only_early = PlayerKillModel().fit(early)
    everything = PlayerKillModel().fit(early + late)
    for pid in (1, 2):
        a = only_early.rate(pid, "mirage", 99, as_of=as_of)
        b = everything.rate(pid, "mirage", 99, as_of=as_of)
        assert a.base_kpr == pytest.approx(b.base_kpr)
        assert a.cv2 == pytest.approx(b.cv2)
    # records on the cut-off day itself (later the same day) are excluded too
    same_day = _records({1: 3.0}, n_maps=1, start=as_of + timedelta(hours=1), seed=6)
    c = PlayerKillModel().fit(early + same_day).rate(1, "mirage", 99, as_of=as_of)
    assert c.base_kpr == pytest.approx(only_early.rate(1, "mirage", 99, as_of=as_of).base_kpr)
    # nothing before as_of -> default prior
    assert not everything.rate(1, "mirage", 99, as_of=T0 - timedelta(days=1)).known


def test_opponent_and_map_adjustments() -> None:
    base = dict.fromkeys(range(1, 21), 0.7)
    recs = _records(base, n_maps=30, opp=50, seed=7)  # average opponent
    recs += _records(base, n_maps=30, opp=60, seed=8, start=T0 + timedelta(days=40))
    leaky = dict.fromkeys(range(1, 21), 0.7 * 1.3)  # team 70 concedes 30 % more kills
    recs += _records(leaky, n_maps=30, opp=70, seed=9, start=T0 + timedelta(days=80))
    killy = dict.fromkeys(range(1, 21), 0.7 * 1.2)  # map with more kills
    recs += _records(killy, n_maps=30, opp=50, map_name="nuke", seed=10, start=T0 + timedelta(days=120))
    model = PlayerKillModel().fit(recs, half_life_days=10_000)
    as_of = T0 + timedelta(days=200)
    r_avg = model.rate(1, "mirage", 50, as_of=as_of)
    r_leaky = model.rate(1, "mirage", 70, as_of=as_of)
    r_nuke = model.rate(1, "nuke", 50, as_of=as_of)
    assert r_leaky.opp_factor > 1.15 * r_avg.opp_factor
    assert r_nuke.map_factor > 1.1 * r_avg.map_factor
    assert r_leaky.kpr > r_avg.kpr and r_nuke.kpr > r_avg.kpr
    assert model.rate(1, "vertigo", 12345, as_of=as_of).map_factor == 1.0  # unknown map / opponent


def test_nb_dispersion_method_of_moments() -> None:
    rng = np.random.default_rng(11)
    pop = {pid: float(rng.normal(0.7, 0.05)) for pid in range(1, 61)}
    recs = _records(pop, n_maps=60, size=8.0, role=PlayerRole.ENTRY, seed=12)
    model = PlayerKillModel().fit(recs, half_life_days=10_000)
    size = model.summary(T0 + timedelta(days=90))["role_size"]["entry"]
    assert 5.0 < size < 13.0, size
    poisson = PlayerKillModel().fit(
        _records(pop, n_maps=60, role=PlayerRole.ENTRY, seed=13), half_life_days=10_000
    )
    assert poisson.summary(T0 + timedelta(days=90))["role_size"]["entry"] > 40.0


def test_effective_size_combines_uncertainty() -> None:
    assert effective_size(10.0, 0.0) == pytest.approx(10.0)
    assert effective_size(10.0, 0.05) < 10.0
    assert effective_size(10.0, 0.0, score_skew=0.3, hist_margin_var=0.12) > 10.0  # score part removed


def test_top_fragger_ties_split_evenly() -> None:
    kills = np.array([[3, 3, 1], [5, 1, 1], [2, 2, 2]])
    probs = top_fragger_probs(kills, [10, 20, 30])
    assert probs == pytest.approx({"10": (0.5 + 1 + 1 / 3) / 3, "20": (0.5 + 1 / 3) / 3, "30": (1 / 3) / 3})
    assert sum(probs.values()) == pytest.approx(1.0)
    masked = top_fragger_probs(kills, [10, 20, 30], row_mask=np.array([False, True, False]))
    assert masked == {"10": 1.0, "20": 0.0, "30": 0.0}
    with pytest.raises(ValueError):
        top_fragger_probs(kills, [1, 2])


def test_model_top_fragger_probs(
    fitted_kill_model: PlayerKillModel, synthetic_universe: SynthUniverse
) -> None:
    uni = synthetic_universe
    players = lineup(uni, 1, 2)
    t1 = np.full(4000, 13)
    t2 = np.random.default_rng(0).integers(3, 12, 4000)  # team1 dominates
    probs = fitted_kill_model.top_fragger_probs(players, "mirage", 1, 2, t1, t2, as_of=AS_OF, rng=1)
    assert sum(probs.values()) == pytest.approx(1.0)
    team1_mass = sum(probs[str(p.player_id)] for p in players if p.team is TeamSlot.TEAM1)
    assert team1_mass > 0.6
    best = max(probs, key=lambda k: probs[k])
    assert int(best) in {p.id for p in uni.teams[1].players}  # the dominant team provides the favourite


def test_team_normalisation_reacts_to_lineup_changes(
    fitted_kill_model: PlayerKillModel, synthetic_universe: SynthUniverse
) -> None:
    uni = synthetic_universe
    usual = lineup(uni, 1, 2)
    s1, s2 = fitted_kill_model.team_scales(usual, team1_id=1, team2_id=2, as_of=AS_OF)
    assert abs(s1 - 1.0) < 0.06 and abs(s2 - 1.0) < 0.06  # usual lineups: (almost) no change
    # team2 fields the players of its weakest rival -> they die more -> team1 kills more
    dprs = {
        t: np.mean(
            [fitted_kill_model.rate(p.id, "mirage", None, as_of=AS_OF).dpr for p in uni.teams[t].players]
        )
        for t in uni.teams
    }
    worst = max((t for t in uni.teams if t not in (1, 2)), key=lambda t: dprs[t])
    swapped = [p for p in usual if p.team is TeamSlot.TEAM1] + [
        PlayerInput(player_id=p.id, team=TeamSlot.TEAM2, role=p.role) for p in uni.teams[worst].players
    ]
    n1, _ = fitted_kill_model.team_scales(swapped, team1_id=1, team2_id=2, as_of=AS_OF)
    if dprs[worst] > dprs[2]:
        assert n1 > s1
    lo, hi = KillModelConfig().normalisation_bounds
    assert lo <= n1 <= hi
    # incomplete lineups are not normalised
    assert fitted_kill_model.team_scales(usual[:3], team1_id=1, team2_id=2, as_of=AS_OF) == (1.0, 1.0)


def test_save_load_roundtrip(tmp_path: Path, fitted_kill_model: PlayerKillModel) -> None:
    path = fitted_kill_model.save(tmp_path / "kills")
    assert path.suffix == ".npz"
    back = PlayerKillModel.load(tmp_path / "kills")
    assert back.n_records == fitted_kill_model.n_records
    for pid in (1, 7, 33):
        a = fitted_kill_model.rate(pid, "nuke", 3, as_of=AS_OF)
        b = back.rate(pid, "nuke", 3, as_of=AS_OF)
        assert a == b
    assert back.config == fitted_kill_model.config


def test_empty_fit_uses_defaults() -> None:
    model = PlayerKillModel().fit([])
    r = model.rate(1, "mirage", None, as_of=T0, role=PlayerRole.IGL)
    assert not r.known and r.base_kpr == pytest.approx(0.58)
    with pytest.raises(ValueError):
        PlayerKillModel().fit([], half_life_days=0)


# ------------------------------------------------------------------------------------------- review round 1


def _skew_records(
    gamma: float, *, n_maps: int, seed: int = 0, with_scores: bool = True
) -> list[PlayerMapRecord]:
    """Ten players, kills ~ Poisson(kpr * (R + gamma * D)) with random scorelines (D = own - opp)."""
    rng = np.random.default_rng(seed)
    out = []
    for j in range(n_maps):
        loser = int(rng.integers(0, 12))
        team1_won = bool(rng.random() < 0.5)
        own1, own2 = (13, loser) if team1_won else (loser, 13)
        for pid in range(10):
            team = 1 if pid < 5 else 2
            own, opp = (own1, own2) if team == 1 else (own2, own1)
            r = own + opp
            mu = 0.68 * (r + gamma * (own - opp))
            out.append(
                PlayerMapRecord(
                    match_id=j,
                    map_number=1,
                    map_name="mirage",
                    played_at=T0 + timedelta(hours=j),
                    player_id=pid,
                    team_id=team,
                    opponent_team_id=3 - team,
                    kills=int(rng.poisson(mu)),
                    deaths=int(min(r, rng.poisson(0.68 * r))),
                    rounds_played=r,
                    team_rounds_won=own if with_scores else None,
                    opponent_rounds_won=opp if with_scores else None,
                )
            )
    return out


def test_score_skew_is_estimated_from_scorelines() -> None:
    as_of = T0 + timedelta(days=400)
    flat = KillModelConfig(score_skew_prior_sd=10.0)  # (almost) no shrinkage: the raw estimate
    for gamma in (0.15, 0.5):
        model = PlayerKillModel(flat).fit(_skew_records(gamma, n_maps=400, seed=int(gamma * 100)))
        est, hmv = model.score_params(as_of)
        assert est == pytest.approx(gamma, abs=0.06)
        assert 0.0 < hmv < 1.0
        assert model.summary(as_of)["n_skew_records"] == 4000
    # without scorelines (or with too few) the configured defaults are used
    model = PlayerKillModel().fit(_skew_records(0.5, n_maps=400, with_scores=False))
    cfg = KillModelConfig()
    assert model.score_params(as_of) == (cfg.score_skew, cfg.hist_margin_var)
    few = PlayerKillModel().fit(_skew_records(0.5, n_maps=10))
    assert few.score_params(as_of) == (cfg.score_skew, cfg.hist_margin_var)
    # estimation can be switched off; no look-ahead: nothing before the first record -> defaults
    off = PlayerKillModel(KillModelConfig(estimate_score_skew=False)).fit(_skew_records(0.5, n_maps=400))
    assert off.score_params(as_of) == (cfg.score_skew, cfg.hist_margin_var)
    early = PlayerKillModel().fit(_skew_records(0.5, n_maps=400))
    assert early.score_params(T0) == (cfg.score_skew, cfg.hist_margin_var)


def test_score_skew_on_synthetic_universe(fitted_kill_model: PlayerKillModel) -> None:
    """The synthetic generator gives the round winner ~4.3 kills and the loser ~1.3 per round, i.e.
    gamma ~ 0.5; the shrunk estimate must move from the 0.3 prior toward it."""
    gamma, hmv = fitted_kill_model.score_params(AS_OF)
    assert 0.4 < gamma < 0.6
    assert 0.02 < hmv < 0.3


def test_malformed_records_are_skipped_not_fatal() -> None:
    good = [r for r in _records({1: 0.7}, n_maps=20) if r.deaths <= r.rounds_played]  # one death per round
    base = good[0]
    bad = [
        base.model_copy(update={"kills": -1}),
        base.model_copy(update={"deaths": -3}),
        base.model_copy(update={"rounds_played": 0}),
        base.model_copy(update={"kills": 500}),  # > 5 per round
        base.model_copy(update={"deaths": 40}),  # > rounds played
    ]
    inconsistent = base.model_copy(update={"team_rounds_won": 3, "opponent_rounds_won": 2})  # < rounds
    model = PlayerKillModel().fit([*bad, *good, inconsistent])
    assert model.n_records == len(good) + 1
    assert model.fit_stats == {"n_records": len(good) + 1, "n_skipped": 5}
    ref = PlayerKillModel().fit([*good, inconsistent])
    as_of = T0 + timedelta(days=60)
    assert model.rate(1, "mirage", 99, as_of=as_of) == ref.rate(1, "mirage", 99, as_of=as_of)


def test_save_load_keeps_scorelines_and_reads_old_files(tmp_path: Path) -> None:
    model = PlayerKillModel(KillModelConfig(score_skew_prior_sd=10.0)).fit(_skew_records(0.4, n_maps=300))
    as_of = T0 + timedelta(days=400)
    back = PlayerKillModel.load(model.save(tmp_path / "skew"))
    assert back.score_params(as_of) == model.score_params(as_of)
    assert back.config == model.config
    # a file written before the scoreline columns existed loads with unknown scorelines
    with np.load(tmp_path / "skew.npz", allow_pickle=False) as z:
        legacy = {k: z[k] for k in z.files if k not in ("own_rounds", "opp_rounds")}
    np.savez_compressed(tmp_path / "legacy.npz", **legacy)
    old = PlayerKillModel.load(tmp_path / "legacy.npz")
    assert old.n_records == model.n_records
    assert old.score_params(as_of) == (old.config.score_skew, old.config.hist_margin_var)


def test_kill_model_is_picklable_and_thread_safe() -> None:
    import copy
    import pickle
    from concurrent.futures import ThreadPoolExecutor

    model = PlayerKillModel(KillModelConfig(cache_size=2)).fit(_records({1: 0.7, 2: 0.6}, n_maps=40))
    clone = pickle.loads(pickle.dumps(model))
    assert copy.deepcopy(model).n_records == clone.n_records == model.n_records
    dates = [T0 + timedelta(days=d) for d in range(5, 45)]
    serial = [model.rate(1, "mirage", 99, as_of=d).kpr for d in dates]
    with ThreadPoolExecutor(max_workers=4) as pool:
        parallel = list(pool.map(lambda d: clone.rate(1, "mirage", 99, as_of=d).kpr, dates))
    assert parallel == serial
