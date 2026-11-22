"""MapSimulator: MR12 / MR3 rules, side swaps, economy behaviour, calibration, outputs, speed."""

from __future__ import annotations

import itertools
import time
from dataclasses import replace

import numpy as np
import pytest

from cs2bot.domain.enums import Side
from cs2bot.models.economy import ECO_CODE, FORCE_CODE, FULL_CODE, PISTOL_CODE, EconomyParams
from cs2bot.models.map_sim import (
    MapSimInput,
    MapSimulator,
    calibrate_strength,
    iid_map_win_prob,
    iid_round_prob_for_map,
)
from cs2bot.models.round_model import RoundModel
from cs2bot.testing.synthetic import SynthUniverse


def _valid_final_score(a: int, b: int) -> bool:
    hi, lo = max(a, b), min(a, b)
    if hi == 13:
        return lo <= 11
    k, rem = divmod(hi - 16, 3)
    return hi >= 16 and rem == 0 and k >= 0 and hi - lo in (2, 3, 4)


@pytest.mark.parametrize("d", [-0.6, 0.0, 0.35])
def test_mr12_and_overtime_termination(simulator: MapSimulator, d: float) -> None:
    out = simulator.simulate(MapSimInput("mirage", d, None, round_model=RoundModel.default()), 6000, 1)
    for key, p in out.score_pmf.items():
        a, b = (int(x) for x in key.split("-"))
        assert _valid_final_score(a, b), key
        assert p > 0
    assert sum(out.score_pmf.values()) == pytest.approx(1.0)
    assert sum(out.total_rounds_pmf.values()) == pytest.approx(1.0)
    totals = out.total_rounds
    assert totals.min() >= 13
    assert out.p_overtime == pytest.approx(float(np.mean(totals > 24)))
    assert out.p_team1_win == pytest.approx(float(np.mean(out.team1_rounds > out.team2_rounds)))
    assert not np.any(out.team1_rounds == out.team2_rounds)  # no draws on a map
    ot_mass = sum(p for k, p in out.score_pmf.items() if max(int(x) for x in k.split("-")) > 13)
    assert ot_mass == pytest.approx(out.p_overtime)
    for half in ("1", "2"):
        assert 0.0 <= out.p_team1_half[half] + out.p_half_draw[half] <= 1.0
    for tg in out.race_targets:
        assert out.p_neither_race_to[str(tg)] == 0.0  # someone always reaches N <= 13


def _ct_dominant_model(bias: float) -> RoundModel:
    return RoundModel(ct_bias={"testmap": bias})


def test_sides_swap_after_round_12(simulator: MapSimulator) -> None:
    inp = MapSimInput("testmap", 0.0, Side.CT, round_model=_ct_dominant_model(3.0))
    out = simulator.simulate(inp, 4000, 2, record_rounds=True)
    assert out.p_team1_pistol["1"] > 0.9  # team1 is CT in round 1
    assert out.p_team1_pistol["13"] < 0.1  # ... and T in round 13
    assert out.p_team1_half["1"] > 0.9 and out.p_team1_half["2"] < 0.1
    log = out.round_log
    assert log is not None
    won = log["team1_won"]
    assert won[:12].mean() > 0.85 and won[12:24][log["active"][12:24]].mean() < 0.15
    # pistol rounds are rounds 1 and 13 only
    buys = log["buys"]
    assert np.all(buys[0] == PISTOL_CODE) and np.all(buys[12] == PISTOL_CODE)
    assert not np.any(buys[1:12] == PISTOL_CODE) and not np.any(buys[13:] == PISTOL_CODE)


@pytest.mark.parametrize(("switch", "team1_first"), [(False, 0.0), (True, 1.0)])
def test_overtime_sides_and_repeat_at_3_3(simulator: MapSimulator, switch: bool, team1_first: float) -> None:
    """CT wins every round: 12-12, then the CT side of the 2nd half keeps CT into overtime."""
    econ = EconomyParams(max_overtimes=2, ot_switch_sides_at_start=switch)
    inp = MapSimInput(
        "testmap", 0.0, Side.CT, economy=econ, round_model=_ct_dominant_model(14.0), race_targets=(15, 16)
    )
    out = simulator.simulate(inp, 2000, 3)
    assert out.p_overtime == 1.0
    assert out.p_half_draw == {"1": 0.0, "2": 0.0}  # 12-0 and 0-12 halves
    assert out.p_team1_half["1"] == 1.0
    assert out.p_team1_race_to["15"] == pytest.approx(team1_first)
    # every overtime goes 3-3 (15-15, 18-18): only the safety coin flip ends it
    assert set(out.score_pmf) == {"19-18", "18-19"}


def test_unknown_side_is_exact_50_50_mixture(simulator: MapSimulator) -> None:
    rm = RoundModel.default()
    n = 4000
    ct = simulator.simulate(MapSimInput("nuke", 0.1, Side.CT, round_model=rm), n, 7)
    t = simulator.simulate(MapSimInput("nuke", 0.1, Side.T, round_model=rm), n, 7)
    mix = simulator.simulate(MapSimInput("nuke", 0.1, None, round_model=rm), n, 7)
    half = n // 2
    expected = (np.sum(ct.team1_wins[:half]) + np.sum(t.team1_wins[half:])) / n
    assert mix.p_team1_win == pytest.approx(expected)
    assert mix.records.team1_start_ct.mean() == 0.5
    assert ct.p_team1_pistol["1"] > mix.p_team1_pistol["1"] > t.p_team1_pistol["1"]


def test_economy_pistol_loser_usually_ecos_round_two(simulator: MapSimulator) -> None:
    out = simulator.simulate(
        MapSimInput("inferno", 0.0, Side.T, round_model=RoundModel.default()), 6000, 4, record_rounds=True
    )
    assert out.diagnostics["r2_pistol_loser_eco"] > 0.6
    assert out.diagnostics["r2_pistol_winner_wins"] > 0.6
    log = out.round_log
    assert log is not None
    r1 = log["team1_won"][0]
    buys_r2 = log["buys"][1]
    loser_buy = np.where(r1, buys_r2[1], buys_r2[0])
    winner_buy = np.where(r1, buys_r2[0], buys_r2[1])
    assert np.mean(loser_buy == ECO_CODE) > 0.6
    assert np.mean(winner_buy == ECO_CODE) < 0.05  # the pistol winner buys (SMGs / anti-eco)
    assert np.mean(np.isin(winner_buy, (FORCE_CODE, FULL_CODE))) > 0.95
    shares = {k: v for k, v in out.diagnostics.items() if k.startswith("buy_share_")}
    assert sum(shares.values()) == pytest.approx(1.0)
    assert shares["buy_share_full"] > 0.45  # most professional rounds are full buys


def test_calibrate_strength_accuracy_and_monotonicity(simulator: MapSimulator) -> None:
    inp = MapSimInput("ancient", 0.0, None, round_model=RoundModel.default())
    targets = [0.12, 0.35, 0.5, 0.65, 0.88]
    ds = []
    for tgt in targets:
        d = simulator.calibrate_strength(tgt, inp, n_sims=6000, rng_seed=11, tol=0.003)
        out = simulator.simulate(inp.with_strength(d), 6000, 11)
        assert abs(out.p_team1_win - tgt) <= 0.003 + 1e-12
        ds.append(d)
    assert all(b > a for a, b in itertools.pairwise(ds))
    assert abs(ds[2]) < 0.1  # 50 % <-> d ~ 0


def test_calibration_is_pure_and_cached() -> None:
    inp = MapSimInput("nuke", 0.0, Side.CT, round_model=RoundModel.default())
    sim_a, sim_b = MapSimulator(), MapSimulator()
    sim_a.calibrate_strength(0.3, inp, n_sims=3000, rng_seed=5)  # different history in sim_a
    d1 = sim_a.calibrate_strength(0.7, inp, n_sims=3000, rng_seed=5)
    d2 = sim_b.calibrate_strength(0.7, inp, n_sims=3000, rng_seed=5)
    assert d1 == d2
    hits = sim_a.stats["calibration_cache_hits"]
    assert sim_a.calibrate_strength(0.7, inp, n_sims=3000, rng_seed=5) == d1
    assert sim_a.stats["calibration_cache_hits"] == hits + 1
    # module-level helper uses the shared simulator and gives the same answer
    assert calibrate_strength(0.7, inp, n_sims=3000, rng_seed=5) == d1


def test_extreme_targets_are_clipped(simulator: MapSimulator) -> None:
    inp = MapSimInput("dust2", 0.0, None, round_model=RoundModel.default())
    d_hi = simulator.calibrate_strength(0.9999, inp, n_sims=2000, rng_seed=1)
    d_lo = simulator.calibrate_strength(0.0001, inp, n_sims=2000, rng_seed=1)
    assert d_hi > 0.8 and d_lo < -0.8


def test_reweighted_hits_target_and_keeps_conditionals(simulator: MapSimulator) -> None:
    out = simulator.simulate(MapSimInput("mirage", 0.2, None, round_model=RoundModel.default()), 5000, 9)
    rw = out.reweighted(0.75)
    assert rw.p_team1_win == pytest.approx(0.75)
    assert sum(rw.score_pmf.values()) == pytest.approx(1.0)
    assert rw.weights is not None and rw.weights.sum() == pytest.approx(1.0)

    def cond(o: object, team1: bool) -> dict[str, float]:
        pmf = o.score_pmf  # type: ignore[attr-defined]
        sel = {k: v for k, v in pmf.items() if (int(k.split("-")[0]) > int(k.split("-")[1])) == team1}
        z = sum(sel.values())
        return {k: v / z for k, v in sel.items()}

    for team1 in (True, False):
        a, b = cond(out, team1), cond(rw, team1)
        assert a.keys() == b.keys()
        for k in a:
            assert a[k] == pytest.approx(b[k])
    assert out.reweighted(out.p_team1_win) is out


def test_reproducible_with_seed() -> None:
    inp = MapSimInput("train", 0.15, None, round_model=RoundModel.default())
    a = MapSimulator().simulate(inp, 3000, 123)
    b = MapSimulator().simulate(inp, 3000, 123)
    c = MapSimulator().simulate(inp, 3000, 124)
    np.testing.assert_array_equal(a.team1_rounds, b.team1_rounds)
    np.testing.assert_array_equal(a.team2_rounds, b.team2_rounds)
    assert a.score_pmf == b.score_pmf
    assert not np.array_equal(a.team1_rounds, c.team1_rounds)


def test_race_beyond_13_can_be_unreached(simulator: MapSimulator) -> None:
    inp = MapSimInput("nuke", 0.4, None, round_model=RoundModel.default(), race_targets=(5, 13, 16))
    out = simulator.simulate(inp, 4000, 2)
    assert out.p_neither_race_to["16"] == pytest.approx(1.0 - out.p_overtime, abs=1e-9)
    assert out.p_team1_race_to["5"] + (1.0 - out.p_team1_race_to["5"]) == 1.0
    # race to 13 == map winner unless the map went to overtime
    assert abs(out.p_team1_race_to["13"] - out.p_team1_win) <= out.p_overtime


def test_iid_reference_formula() -> None:
    assert iid_map_win_prob(0.5) == pytest.approx(0.5)
    xs = np.linspace(0.3, 0.7, 9)
    ys = [iid_map_win_prob(float(x)) for x in xs]
    assert all(b > a for a, b in itertools.pairwise(ys))
    assert iid_round_prob_for_map(iid_map_win_prob(0.56)) == pytest.approx(0.56, abs=1e-9)


def test_simulator_matches_synthetic_ground_truth(
    simulator: MapSimulator, synthetic_universe: SynthUniverse, fitted_round_model: RoundModel
) -> None:
    """At the TRUE strength gap the simulator (with the fitted round model) reproduces the generator."""
    uni = synthetic_universe
    for a, b, m in ((1, 2, "nuke"), (3, 5, "mirage"), (4, 8, "anubis")):
        d = uni.teams[a].map_strength(m) - uni.teams[b].map_strength(m)
        truth = uni.true_map_win_prob(a, b, m, n_sims=1500, seed=3)
        out = simulator.simulate(MapSimInput(m, d, None, round_model=fitted_round_model), 6000, 4)
        assert abs(out.p_team1_win - truth) < 0.05, (m, d, truth, out.p_team1_win)


def test_total_rounds_vs_synthetic_universe(
    simulator: MapSimulator, synthetic_universe: SynthUniverse, fitted_round_model: RoundModel
) -> None:
    """Loose check of the total-rounds distribution against the generator's empirical one.

    The synthetic generator uses a simpler economy (less momentum), so only coarse agreement is
    expected: mean within 2 rounds, spread within 1.2 rounds, overtime rate within 9 points.
    """
    uni = synthetic_universe
    rows = []
    for mt in uni.matches:
        t1, t2 = uni.teams[mt.team1_id], uni.teams[mt.team2_id]
        for mp in mt.maps:
            d = t1.map_strength(mp.map_name) - t2.map_strength(mp.map_name)
            rows.append((abs(d), d, mp.map_name, mp.team1_start_side, mp.team1_rounds + mp.team2_rounds))
    rng = np.random.default_rng(0)
    for lo, hi in ((0.0, 0.25), (0.25, 1.5)):
        sel = [r for r in rows if lo <= r[0] < hi]
        assert len(sel) > 50
        emp = np.array([r[4] for r in sel])
        pick = rng.choice(len(sel), size=min(40, len(sel)), replace=False)
        sims = [
            simulator.simulate(
                MapSimInput(sel[i][2], sel[i][1], sel[i][3], round_model=fitted_round_model), 400, int(i)
            ).total_rounds
            for i in pick
        ]
        sim_tot = np.concatenate(sims)
        assert abs(sim_tot.mean() - emp.mean()) < 2.0
        assert abs(sim_tot.std() - emp.std()) < 1.2
        assert abs(np.mean(sim_tot > 24) - np.mean(emp > 24)) < 0.09


def test_input_validation() -> None:
    with pytest.raises(ValueError):
        MapSimInput("mirage", race_targets=(0,))
    with pytest.raises(ValueError):
        MapSimInput("mirage", strength_diff=float("nan"))
    with pytest.raises(ValueError):
        MapSimulator().simulate(MapSimInput("mirage"), 1)
    assert MapSimInput("de_dust2").map_name == "dust2"
    assert replace(MapSimInput("mirage"), strength_diff=0.5).with_strength(0.1).strength_diff == 0.1


def test_performance_20k_sims_one_map() -> None:
    sim = MapSimulator()
    inp = MapSimInput("mirage", 0.1, None, round_model=RoundModel.default())
    sim.simulate(inp, 2000, 0)  # warm-up
    t0 = time.perf_counter()
    out = sim.simulate(inp, 20_000, 1)
    elapsed = time.perf_counter() - t0
    assert out.n_sims == 20_000
    assert elapsed < 1.0, f"20k simulations took {elapsed:.2f}s"
