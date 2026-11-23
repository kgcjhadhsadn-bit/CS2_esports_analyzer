"""build_match_prediction: series math, scenarios, slots, player scopes, reproducibility, speed."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import numpy as np
import pytest

from cs2bot.domain.context import ContextFlag
from cs2bot.domain.enums import ContextFlagType, MatchFormat, Side, TeamSlot
from cs2bot.domain.predictions import MapSequenceProbability, MatchPrediction, PredictionCore
from cs2bot.models.map_sim import MapSimulator
from cs2bot.models.match_engine import build_match_prediction, scenario_probabilities
from cs2bot.models.player_props import PlayerKillModel
from cs2bot.models.round_model import RoundModel
from cs2bot.testing.synthetic import SynthUniverse
from tests.models.sim.sim_helpers import AS_OF, estimates, lineup, veto_sequences

POOL = ["ancient", "anubis", "dust2", "inferno", "mirage", "nuke", "train"]
PROBS = {
    "ancient": 0.62,
    "anubis": 0.48,
    "dust2": 0.70,
    "inferno": 0.55,
    "mirage": 0.66,
    "nuke": 0.58,
    "train": 0.4,
}
N = 3000


def _predict(fmt: MatchFormat, seqs: list[MapSequenceProbability], **kw: object) -> MatchPrediction:
    args: dict[str, object] = {
        "match_id": 77,
        "fmt": fmt,
        "team1_id": 1,
        "team2_id": 2,
        "veto_distribution": seqs,
        "map_estimates": estimates(PROBS),
        "round_model": RoundModel.default(),
        "n_sims": N,
        "seed": 5,
        "model_version": "test",
        "created_at": AS_OF,
        "simulator": MapSimulator(),
    }
    args.update(kw)
    return build_match_prediction(**args)  # type: ignore[arg-type]


def _check_core(core: PredictionCore, n_maps: int) -> None:
    assert sum(core.map_score_pmf.values()) == pytest.approx(1.0)
    assert len(core.slots) == n_maps
    for slot in core.slots:
        assert 0.0 <= slot.p_played <= 1.0 + 1e-12
        assert sum(slot.map_distribution.values()) == pytest.approx(1.0)
        assert sum(slot.score_pmf.values()) == pytest.approx(1.0)
        assert 0.0 < slot.p_team1_win < 1.0
        assert set(slot.p_team1_pistol) == {"1", "13"}
        for half in ("1", "2"):
            assert slot.p_team1_half[half] + slot.p_half_draw[half] <= 1.0 + 1e-12
        # map-level win probability equals the score distribution's team1 mass
        t1_mass = sum(p for k, p in slot.score_pmf.items() if int(k.split("-")[0]) > int(k.split("-")[1]))
        assert t1_mass == pytest.approx(slot.p_team1_win, abs=1e-9)


def test_bo3_series_math_and_scenarios() -> None:
    pred = _predict(MatchFormat.BO3, veto_sequences(POOL, 3, n_seq=8, seed=1))
    for core in (pred.central, pred.low, pred.high):
        assert core is not None
        _check_core(core, 3)
        p11 = core.map_score_pmf.get("2-1", 0.0) + core.map_score_pmf.get("1-2", 0.0)
        assert core.slots[2].p_played == pytest.approx(p11)
        assert core.slots[0].p_played == pytest.approx(1.0) and core.slots[1].p_played == pytest.approx(1.0)
        wins = core.map_score_pmf.get("2-0", 0.0) + core.map_score_pmf.get("2-1", 0.0)
        assert core.p_team1_win == pytest.approx(wins)
        assert set(core.map_score_pmf) <= {"2-0", "2-1", "1-2", "0-2"}
    assert pred.low is not None and pred.high is not None
    assert pred.low.p_team1_win < pred.central.p_team1_win < pred.high.p_team1_win
    assert pred.sigma_team1_win == pytest.approx(abs(pred.high.p_team1_win - pred.low.p_team1_win) / 2)
    assert pred.n_simulations == N and pred.format is MatchFormat.BO3
    comps = pred.components
    assert set(comps["map_strength_d"]) == {"central", "low", "high"}
    assert comps["cache"]["local_misses"] >= 1 and comps["timing_s"]["total"] > 0


def test_veto_known_single_sequence_matches_map_estimates() -> None:
    seq = [
        MapSequenceProbability(
            maps=["mirage", "nuke", "anubis"], pickers=[TeamSlot.TEAM1, TeamSlot.TEAM2, None], probability=1.0
        )
    ]
    pred = _predict(MatchFormat.BO3, seq, veto_known=True)
    c = pred.central
    for slot, m in zip(c.slots, ["mirage", "nuke", "anubis"], strict=True):
        assert slot.map_distribution == {m: 1.0}
        assert slot.p_team1_win == pytest.approx(PROBS[m])  # reweighted to the estimate exactly
    p1, p2, p3 = PROBS["mirage"], PROBS["nuke"], PROBS["anubis"]
    assert c.map_score_pmf["2-0"] == pytest.approx(p1 * p2)
    assert c.slots[2].p_played == pytest.approx(p1 * (1 - p2) + (1 - p1) * p2)
    assert c.p_team1_win == pytest.approx(p1 * p2 + (p1 * (1 - p2) + (1 - p1) * p2) * p3)
    assert pred.veto_known


def test_bo1_bo2_bo5_handling() -> None:
    bo1 = _predict(MatchFormat.BO1, veto_sequences(POOL, 1, n_seq=4, seed=2))
    _check_core(bo1.central, 1)
    assert bo1.central.slots[0].p_played == 1.0
    assert set(bo1.central.map_score_pmf) == {"1-0", "0-1"}
    assert bo1.central.p_team1_win == pytest.approx(bo1.central.slots[0].p_team1_win)

    bo2 = _predict(MatchFormat.BO2, veto_sequences(POOL, 2, n_seq=5, seed=3))
    c2 = bo2.central
    _check_core(c2, 2)
    assert set(c2.map_score_pmf) == {"2-0", "1-1", "0-2"}
    assert c2.p_draw == pytest.approx(c2.map_score_pmf["1-1"])
    assert c2.p_team1_win == pytest.approx(c2.map_score_pmf["2-0"])
    assert [s.p_played for s in c2.slots] == [pytest.approx(1.0), pytest.approx(1.0)]

    bo5 = _predict(MatchFormat.BO5, veto_sequences(POOL, 5, n_seq=4, seed=4))
    c5 = bo5.central
    _check_core(c5, 5)
    pmf = c5.map_score_pmf
    assert set(pmf) <= {"3-0", "3-1", "3-2", "2-3", "1-3", "0-3"}
    assert [s.p_played for s in c5.slots[:3]] == [pytest.approx(1.0)] * 3
    assert c5.slots[3].p_played == pytest.approx(1.0 - pmf["3-0"] - pmf["0-3"])
    assert c5.slots[4].p_played == pytest.approx(pmf["3-2"] + pmf["2-3"])


def test_sequences_are_truncated_merged_and_validated() -> None:
    seqs = [
        MapSequenceProbability(
            maps=["mirage", "nuke", "anubis"], pickers=[TeamSlot.TEAM1, TeamSlot.TEAM2, None], probability=0.3
        ),
        MapSequenceProbability(
            maps=["mirage", "nuke", "dust2"], pickers=[TeamSlot.TEAM1, TeamSlot.TEAM2, None], probability=0.3
        ),
        MapSequenceProbability(maps=["Dust II", "de_inferno"], pickers=[None], probability=0.2),
    ]
    pred = _predict(MatchFormat.BO2, seqs)  # the leftover decider is ignored in a BO2
    c = pred.central
    assert c.slots[0].map_distribution == pytest.approx({"mirage": 0.75, "dust2": 0.25})
    assert pred.components["veto"]["n_sequences"] == 2
    assert pred.components["veto"]["input_mass"] == pytest.approx(0.8)
    with pytest.raises(ValueError, match="fewer than"):
        _predict(
            MatchFormat.BO3, [MapSequenceProbability(maps=["mirage", "nuke"], pickers=[], probability=1.0)]
        )
    with pytest.raises(ValueError, match="repeats"):
        _predict(
            MatchFormat.BO3,
            [MapSequenceProbability(maps=["mirage", "nuke", "mirage"], pickers=[], probability=1.0)],
        )
    with pytest.raises(ValueError, match="no MapWinEstimate"):
        _predict(MatchFormat.BO1, [MapSequenceProbability(maps=["vertigo"], pickers=[None], probability=1.0)])
    with pytest.raises(ValueError, match="positive"):
        _predict(MatchFormat.BO1, [MapSequenceProbability(maps=["mirage"], pickers=[None], probability=0.0)])
    with pytest.raises(ValueError, match="timezone"):
        _predict(MatchFormat.BO1, veto_sequences(POOL, 1, n_seq=2), created_at=datetime(2025, 1, 1))


def test_logit_shift_and_context_flags() -> None:
    seqs = veto_sequences(POOL, 3, n_seq=3, seed=6)
    flag = ContextFlag(
        flag_type=ContextFlagType.STAND_IN,
        team=TeamSlot.TEAM2,
        severity=0.5,
        confidence=0.9,
        logit_shift=0.2,
        rationale="stand-in",
    )
    base = _predict(MatchFormat.BO3, seqs)
    shifted = _predict(MatchFormat.BO3, seqs, logit_shift=0.2, context_flags=[flag])
    assert shifted.central.p_team1_win > base.central.p_team1_win
    assert shifted.logit_shift_applied == 0.2 and shifted.context_flags == [flag]
    c, lo, hi, sl = scenario_probabilities(estimates({"x": 0.6}, sigma=0.05)["x"], 0.0)
    assert lo < c < hi and sl == pytest.approx(0.05 / 0.24)
    c0, lo0, hi0, sl0 = scenario_probabilities(estimates({"x": 0.6}, sigma=0.0)["x"], 0.0)
    assert lo0 == c0 == hi0 and sl0 == 0.0


def test_start_side_affects_pistol_markets() -> None:
    seq = [MapSequenceProbability(maps=["nuke"], pickers=[None], probability=1.0)]
    est = estimates({**PROBS, "nuke": 0.5})
    ct = _predict(MatchFormat.BO1, seq, map_estimates=est, start_sides={"de_nuke": Side.CT})
    unk = _predict(MatchFormat.BO1, seq, map_estimates=est)
    assert ct.central.slots[0].p_team1_pistol["1"] > unk.central.slots[0].p_team1_pistol["1"] > 0.45
    assert ct.central.slots[0].p_team1_half["1"] > ct.central.slots[0].p_team1_half["2"]
    assert ct.components["start_sides"] == {"nuke": "ct"}


def test_reproducible_with_seed_and_json_roundtrip() -> None:
    seqs = veto_sequences(POOL, 3, n_seq=4, seed=8)
    a = _predict(MatchFormat.BO3, seqs, seed=99)
    b = _predict(MatchFormat.BO3, seqs, seed=99)
    da, db = a.model_dump(mode="json"), b.model_dump(mode="json")
    for d in (da, db):
        d["components"].pop("timing_s")
        d["components"].pop("cache")
    assert da == db
    c = _predict(MatchFormat.BO3, seqs, seed=100)
    assert c.central.slots[0].score_pmf != a.central.slots[0].score_pmf
    back = MatchPrediction.model_validate_json(a.model_dump_json())
    assert back.central.p_team1_win == pytest.approx(a.central.p_team1_win)


def test_player_props_scopes(
    fitted_kill_model: PlayerKillModel, synthetic_universe: SynthUniverse, fitted_round_model: RoundModel
) -> None:
    uni = synthetic_universe
    players = lineup(uni, 1, 2)
    pool = uni.map_pool
    probs = dict.fromkeys(pool, 0.6)
    pred = _predict(
        MatchFormat.BO3,
        veto_sequences(pool, 3, n_seq=5, seed=9),
        map_estimates=estimates(probs),
        round_model=fitted_round_model,
        players=players,
        kill_model=fitted_kill_model,
        as_of=AS_OF,
    )
    for core in (pred.central, pred.low, pred.high):
        assert core is not None
        scopes = {"map:1", "map:2", "map:3", "maps:1,2", "maps:1,2,3", "match"}
        assert set(core.player_kills_by_scope) == scopes
        for scope, projs in core.player_kills_by_scope.items():
            assert set(projs) == {str(p.player_id) for p in players}
            for proj in projs.values():
                assert sum(proj.kills_pmf.values()) == pytest.approx(1.0), scope
                assert proj.kills_var > 0
        for scope in scopes:
            assert sum(core.top_fragger[scope].values()) == pytest.approx(1.0)
            for team in ("team1", "team2"):
                assert sum(core.top_fragger[f"{scope}|{team}"].values()) == pytest.approx(1.0)
        # linearity of expectations across scopes
        for p in players:
            pid = str(p.player_id)
            k = core.player_kills_by_scope
            assert k["maps:1,2"][pid].kills_mean == pytest.approx(
                k["map:1"][pid].kills_mean + k["map:2"][pid].kills_mean, rel=1e-6
            )
            exp_match = sum(s.p_played * k[f"map:{s.map_number}"][pid].kills_mean for s in core.slots)
            assert k["match"][pid].kills_mean == pytest.approx(exp_match, rel=1e-6)
            assert core.slots[0].players[pid] == k["map:1"][pid]
            # maps 1-3 given that map 3 is played (1-1 after two maps): close to the sum of the slot
            # means (only the score skew moves it), and above the two-map total
            three = k["maps:1,2,3"][pid].kills_mean
            assert three == pytest.approx(
                k["maps:1,2"][pid].kills_mean + k["map:3"][pid].kills_mean, rel=0.03
            )
            assert three > k["maps:1,2"][pid].kills_mean


def test_player_kill_mean_is_kpr_times_expected_rounds(
    fitted_kill_model: PlayerKillModel, synthetic_universe: SynthUniverse
) -> None:
    uni = synthetic_universe
    players = lineup(uni, 3, 4)
    seq = [MapSequenceProbability(maps=["inferno"], pickers=[None], probability=1.0)]
    pred = _predict(
        MatchFormat.BO1,
        seq,
        team1_id=3,
        team2_id=4,
        players=players,
        kill_model=fitted_kill_model,
        as_of=AS_OF,
    )
    slot = pred.central.slots[0]
    exp_rounds = sum(int(a) * p + int(b) * p for k, p in slot.score_pmf.items() for a, b in [k.split("-")])
    rates = fitted_kill_model.lineup_rates(players, map_name="inferno", team1_id=3, team2_id=4, as_of=AS_OF)
    for p in players:
        proj = slot.players[str(p.player_id)]
        assert proj.kills_mean == pytest.approx(rates[p.player_id].kpr * exp_rounds, rel=0.02)
        assert proj.deaths_mean == pytest.approx(rates[p.player_id].dpr * exp_rounds, rel=1e-6)
    by_scope = pred.central.player_kills_by_scope
    for pid, proj in by_scope["match"].items():  # BO1: the match is map 1
        one = by_scope["map:1"][pid]
        assert proj.kills_mean == pytest.approx(one.kills_mean) and proj.kills_var == pytest.approx(
            one.kills_var
        )
        assert proj.kills_pmf.keys() == one.kills_pmf.keys()
    assert "maps:1,2" not in pred.central.player_kills_by_scope


def test_players_without_kill_model_are_skipped() -> None:
    seq = [MapSequenceProbability(maps=["mirage"], pickers=[None], probability=1.0)]
    pred = _predict(MatchFormat.BO1, seq, players=[])
    assert pred.central.player_kills_by_scope == {} and pred.central.top_fragger == {}


def test_performance_bo3_three_scenarios_20k(
    fitted_kill_model: PlayerKillModel, synthetic_universe: SynthUniverse
) -> None:
    uni = synthetic_universe
    t0 = time.perf_counter()
    pred = build_match_prediction(
        match_id=1,
        fmt=MatchFormat.BO3,
        team1_id=1,
        team2_id=2,
        veto_distribution=veto_sequences(uni.map_pool, 3, n_seq=30, seed=10),
        map_estimates=estimates(
            {m: float(np.clip(0.5 + 0.05 * i, 0.1, 0.9)) for i, m in enumerate(uni.map_pool)}
        ),
        round_model=RoundModel.default(),
        players=lineup(uni, 1, 2),
        kill_model=fitted_kill_model,
        n_sims=20_000,
        seed=1,
        model_version="perf",
        created_at=datetime(2025, 3, 1, tzinfo=UTC),
        simulator=MapSimulator(),
    )
    elapsed = time.perf_counter() - t0
    assert pred.central.p_team1_win > 0.5
    assert elapsed < 20.0, f"BO3 prediction took {elapsed:.1f}s"


def test_zero_sigma_scenarios_reuse_central_run() -> None:
    seqs = veto_sequences(POOL, 3, n_seq=3, seed=12)
    pred = _predict(MatchFormat.BO3, seqs, map_estimates=estimates(PROBS, sigma=0.0))
    assert pred.low is not None and pred.high is not None
    assert pred.sigma_team1_win == 0.0
    for k in range(3):
        assert pred.low.slots[k].score_pmf == pred.central.slots[k].score_pmf == pred.high.slots[k].score_pmf


# ------------------------------------------------------------------------------------------- review round 1


def test_race_to_n_published_for_every_target_with_void_mass() -> None:
    seq = [MapSequenceProbability(maps=["nuke"], pickers=[None], probability=1.0)]
    slot = _predict(MatchFormat.BO1, seq).central.slots[0]
    assert set(slot.p_team1_race_to) == {str(n) for n in range(1, 17)}
    assert set(slot.p_neither_race_to) == set(slot.p_team1_race_to)
    scores = {tuple(int(x) for x in k.split("-")): p for k, p in slot.score_pmf.items()}
    for key, p1 in slot.p_team1_race_to.items():
        n = int(key)
        neither = slot.p_neither_race_to[key]
        expected_neither = sum(p for (a, b), p in scores.items() if max(a, b) < n)
        assert neither == pytest.approx(expected_neither, abs=1e-9)
        assert 0.0 <= p1 <= 1.0 - neither + 1e-9
        if n <= 13:
            assert neither == 0.0
    assert slot.p_neither_race_to["16"] == pytest.approx(1.0 - slot.p_overtime, abs=1e-9)


def test_bo5_prefix_scopes_and_conditional_top_fragger(
    fitted_kill_model: PlayerKillModel, synthetic_universe: SynthUniverse
) -> None:
    uni = synthetic_universe
    players = lineup(uni, 1, 2)
    pred = _predict(
        MatchFormat.BO5,
        veto_sequences(uni.map_pool, 5, n_seq=3, seed=21),
        map_estimates=estimates(dict.fromkeys(uni.map_pool, 0.55)),
        players=players,
        kill_model=fitted_kill_model,
        as_of=AS_OF,
        top_fragger_samples=3000,
    )
    core = pred.central
    prefixes = {"maps:1,2", "maps:1,2,3", "maps:1,2,3,4", "maps:1,2,3,4,5"}
    assert prefixes <= set(core.player_kills_by_scope)
    for scope in prefixes:
        assert sum(core.top_fragger[scope].values()) == pytest.approx(1.0)
        for proj in core.player_kills_by_scope[scope].values():
            assert sum(proj.kills_pmf.values()) == pytest.approx(1.0)
    pid = str(players[0].player_id)
    means = [core.player_kills_by_scope[s][pid].kills_mean for s in sorted(prefixes, key=len)]
    assert means == sorted(means)  # every extra map adds kills
    assert core.player_kills_by_scope["maps:1,2,3,4,5"][pid].kills_mean > (
        core.player_kills_by_scope["match"][pid].kills_mean
    )  # all five maps played vs the unconditional match total


@pytest.mark.parametrize(
    "bad",
    [
        {"p_team1_win": float("nan")},
        {"sigma": float("nan")},
        {"sigma": -0.1},
        {"p_team1_win": 1.5},
    ],
)
def test_invalid_map_estimates_are_rejected(bad: dict[str, float]) -> None:
    est = estimates(PROBS)
    est["mirage"] = est["mirage"].model_copy(update=bad)
    seq = [MapSequenceProbability(maps=["mirage"], pickers=[None], probability=1.0)]
    with pytest.raises(ValueError, match="MapWinEstimate"):
        _predict(MatchFormat.BO1, seq, map_estimates=est)
    with pytest.raises(ValueError, match="logit_shift"):
        _predict(MatchFormat.BO1, seq, logit_shift=float("inf"))


def test_settings_and_context_helpers() -> None:
    from cs2bot.config import ModelSettings
    from cs2bot.domain.match import LineupPlayer, MatchContext
    from cs2bot.models.match_engine import context_kwargs, economy_from_settings, engine_options

    ms = ModelSettings(
        n_map_simulations=1200, scenario_n_simulations=600, ot_start_money=12_500, random_seed=3
    )
    opts = engine_options(ms, match_id=42)
    assert opts["n_sims"] == 1200 and opts["scenario_n_sims"] == 600
    assert opts["economy"].ot_start_money == 12_500 and opts["top_fragger_samples"] == ms.top_fragger_samples
    assert opts["seed"] == engine_options(ms, match_id=42)["seed"] != engine_options(ms, match_id=43)["seed"]
    assert engine_options(ModelSettings(random_seed=None))["seed"] is None
    assert economy_from_settings(ModelSettings()).ot_start_money == 10_000

    from cs2bot.domain.enums import PlayerRole

    ctx = MatchContext(
        match_id=42,
        scheduled_at=AS_OF,
        format=MatchFormat.BO1,
        team1_id=1,
        team2_id=2,
        team1_lineup=[LineupPlayer(player_id=10, nickname="a", role=PlayerRole.AWPER)],
        team2_lineup=[LineupPlayer(player_id=20, nickname="b"), LineupPlayer(player_id=10, nickname="dup")],
        start_sides={"de_mirage": Side.T},
    )
    kw = context_kwargs(ctx)
    assert [(p.player_id, p.team, p.role) for p in kw["players"]] == [
        (10, TeamSlot.TEAM1, PlayerRole.AWPER),
        (20, TeamSlot.TEAM2, None),
    ]
    seq = [MapSequenceProbability(maps=["mirage"], pickers=[None], probability=1.0)]
    pred = build_match_prediction(
        **kw,
        **opts,
        veto_distribution=seq,
        map_estimates=estimates(PROBS),
        round_model=RoundModel.default(),
        kill_model=PlayerKillModel(),
        model_version="ctx",
        created_at=AS_OF,
        simulator=MapSimulator(),
    )
    assert pred.match_id == 42 and pred.components["start_sides"] == {"mirage": "t"}
    assert pred.components["economy"]["ot_start_money"] == 12_500
    assert pred.components["n_sims"] == {"central": 1200, "low": 600, "high": 600}
    assert set(pred.central.slots[0].players) == {"10", "20"}


def test_shared_simulator_is_thread_safe() -> None:
    from concurrent.futures import ThreadPoolExecutor

    seqs = veto_sequences(POOL, 3, n_seq=3, seed=31)
    sim = MapSimulator(cache_size=4, calibration_cache_size=8)  # small caches -> constant eviction

    def run(seed: int) -> float:
        return _predict(MatchFormat.BO3, seqs, seed=seed, simulator=sim).central.slots[0].p_overtime

    serial = [run(s) for s in (1, 2, 3, 4)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        parallel = list(pool.map(run, (1, 2, 3, 4)))
    assert parallel == serial


def test_top_fragger_samples_validated() -> None:
    seq = [MapSequenceProbability(maps=["mirage"], pickers=[None], probability=1.0)]
    with pytest.raises(ValueError, match="top_fragger_samples"):
        _predict(MatchFormat.BO1, seq, top_fragger_samples=0)
