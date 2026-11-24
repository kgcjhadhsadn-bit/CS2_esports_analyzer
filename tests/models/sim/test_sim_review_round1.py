"""Regression tests for the issues fixed in review round 1 (second pass) of the sim package."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from scipy import stats

from cs2bot.domain.enums import BuyType, MatchFormat, Side, TeamSlot
from cs2bot.domain.predictions import MapSequenceProbability
from cs2bot.domain.records import RoundRecord
from cs2bot.models.map_sim import MapSimulator
from cs2bot.models.match_engine import build_match_prediction
from cs2bot.models.player_props import MIN_CV2_FOR_QUADRATURE, PlayerInput, PlayerKillModel
from cs2bot.models.round_model import RoundModel
from cs2bot.models.sim_series import series_prefix_sum_pmf, series_prefix_sum_pmfs
from cs2bot.models.sim_stats import gamma_quadrature
from cs2bot.testing.synthetic import SynthUniverse
from tests.models.sim.sim_helpers import AS_OF, estimates, lineup

UNKNOWN_ID = 987_654_321


@pytest.mark.parametrize("cv2", [1e-5, 0.004, 0.064, 0.5, 3.0])
def test_gamma_quadrature_reproduces_gamma_moments(cv2: float) -> None:
    x, w = gamma_quadrature(cv2, 7)
    assert np.all(np.isfinite(x)) and np.all(np.isfinite(w)) and np.all(x > 0)
    assert w.sum() == pytest.approx(1.0)
    dist = stats.gamma(a=1.0 / cv2, scale=cv2)
    for order in (1, 2, 3, 4):
        assert np.dot(w, x**order) == pytest.approx(dist.moment(order), rel=1e-8)
    x1, w1 = gamma_quadrature(0.0, 7)
    assert x1.tolist() == [1.0] and w1.tolist() == [1.0]


def test_multimap_kills_share_the_rate_uncertainty(
    fitted_kill_model: PlayerKillModel, synthetic_universe: SynthUniverse
) -> None:
    """A player's true KPR is one number for all maps: Cov(K1, K2) = cv2 * E[K1] * E[K2].

    Before the fix the rate uncertainty was folded into each map's NB size independently, which
    understated the variance of multi-map totals (about 25 % for an unknown player in a BO3).
    """
    uni = synthetic_universe
    players = lineup(uni, 1, 2)
    players[0] = PlayerInput(player_id=UNKNOWN_ID, team=TeamSlot.TEAM1, nickname="standin")
    maps = list(uni.map_pool[:3])
    seqs = [
        MapSequenceProbability(maps=maps, pickers=[TeamSlot.TEAM1, TeamSlot.TEAM2, None], probability=1.0)
    ]
    pred = build_match_prediction(
        match_id=5,
        fmt=MatchFormat.BO3,
        team1_id=1,
        team2_id=2,
        veto_distribution=seqs,
        map_estimates=estimates(dict.fromkeys(maps, 0.55)),
        round_model=RoundModel.default(),
        players=players,
        kill_model=fitted_kill_model,
        n_sims=3000,
        seed=4,
        model_version="review",
        created_at=AS_OF,
        simulator=MapSimulator(),
    )
    scopes = pred.central.player_kills_by_scope
    for pl in (players[0], players[1]):
        rate = fitted_kill_model.lineup_rates(players, map_name=maps[0], team1_id=1, team2_id=2, as_of=AS_OF)[
            pl.player_id
        ]
        cv2 = rate.cv2 if rate.cv2 >= MIN_CV2_FOR_QUADRATURE else 0.0
        if pl.player_id == UNKNOWN_ID:
            assert cv2 > 0.02  # role/global prior: a wide rate uncertainty
        m1, m2 = (scopes[f"map:{k}"][str(pl.player_id)] for k in (1, 2))
        both = scopes["maps:1,2"][str(pl.player_id)]
        assert both.kills_mean == pytest.approx(m1.kills_mean + m2.kills_mean, rel=1e-6)
        expected_var = m1.kills_var + m2.kills_var + 2.0 * cv2 * m1.kills_mean * m2.kills_mean
        assert both.kills_var == pytest.approx(expected_var, rel=2e-3)


def _round(
    *,
    played_at: datetime,
    map_name: str,
    rnd: int,
    team1_ct: bool,
    team1_won: bool,
    buys: tuple[BuyType, BuyType],
) -> RoundRecord:
    return RoundRecord(
        match_id=int(played_at.timestamp()) // 86400,
        map_number=1,
        map_name=map_name,
        played_at=played_at,
        round_number=rnd,
        team1_id=1,
        team2_id=2,
        team1_side=Side.CT if team1_ct else Side.T,
        winner=TeamSlot.TEAM1 if team1_won else TeamSlot.TEAM2,
        team1_buy=buys[0],
        team2_buy=buys[1],
    )


def _ct_heavy_rounds(
    start: datetime, map_name: str, n_days: int, p_ct: float, seed: int
) -> list[RoundRecord]:
    rng = np.random.default_rng(seed)
    out = []
    for day in range(n_days):
        for rnd in range(2, 24):
            team1_ct = rnd <= 12
            ct_won = bool(rng.random() < p_ct)
            out.append(
                _round(
                    played_at=start + timedelta(days=day, minutes=rnd),
                    map_name=map_name,
                    rnd=rnd,
                    team1_ct=team1_ct,
                    team1_won=ct_won == team1_ct,
                    buys=(BuyType.FULL, BuyType.FULL),
                )
            )
    return out


def test_round_model_refit_starts_from_priors() -> None:
    """Re-fitting an instance with an earlier ``as_of`` must not keep estimates from later data."""
    early = datetime(2024, 1, 1, tzinfo=UTC)
    late = datetime(2024, 6, 1, tzinfo=UTC)
    rounds = _ct_heavy_rounds(early, "mirage", 20, 0.52, seed=1) + _ct_heavy_rounds(
        late, "vertigo", 40, 0.80, seed=2
    )
    cutoff = datetime(2024, 3, 1, tzinfo=UTC)
    model = RoundModel()
    model.fit(rounds)  # all data: vertigo strongly CT-sided
    assert model.ct_bias_for("vertigo") > RoundModel.default().ct_bias_for("vertigo") + 0.1
    model.fit(rounds, as_of=cutoff)  # vertigo rounds are all after the cut-off
    fresh = RoundModel().fit(rounds, as_of=cutoff)
    assert model.ct_bias_for("vertigo") == pytest.approx(RoundModel.default().ct_bias_for("vertigo"))
    assert model.to_dict()["ct_bias"] == fresh.to_dict()["ct_bias"]
    assert model.fingerprint() == fresh.fingerprint()
    # no rounds at all before the cut-off -> exactly the priors (not the previous fit)
    model.fit(rounds, as_of=datetime(2023, 1, 1, tzinfo=UTC))
    assert model.fingerprint() == RoundModel.default().fingerprint()


def test_round_model_skips_unusable_records() -> None:
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    good = _ct_heavy_rounds(t0, "nuke", 5, 0.6, seed=3)
    bad_map = _round(
        played_at=t0, map_name="???", rnd=5, team1_ct=True, team1_won=True, buys=(BuyType.FULL, BuyType.ECO)
    )
    bad_round = _round(
        played_at=t0, map_name="nuke", rnd=0, team1_ct=True, team1_won=True, buys=(BuyType.FULL, BuyType.ECO)
    )
    model = RoundModel().fit([bad_map, *good, bad_round])
    assert model.fit_stats["n_rounds"] == len(good)
    assert model.fit_stats["n_skipped"] == 2
    assert "" not in model.ct_bias


@pytest.mark.parametrize("fmt", list(MatchFormat))
def test_all_prefix_sums_in_one_pass_match_single_prefix(fmt: MatchFormat) -> None:
    rng = np.random.default_rng(int(fmt.n_maps))
    n = fmt.n_maps
    p_maps = list(rng.uniform(0.2, 0.8, n))
    pmf1 = [rng.dirichlet(np.ones(int(rng.integers(3, 8)))) for _ in range(n)]
    pmf2 = [rng.dirichlet(np.ones(int(rng.integers(3, 8)))) for _ in range(n)]
    all_prefixes = series_prefix_sum_pmfs(fmt, p_maps, pmf1, pmf2)
    assert len(all_prefixes) == n
    for upto in range(1, n + 1):
        mass, pmf = series_prefix_sum_pmf(fmt, p_maps, pmf1, pmf2, upto)
        mass_all, pmf_all = all_prefixes[upto - 1]
        assert mass_all == pytest.approx(mass, abs=1e-12)
        np.testing.assert_allclose(pmf_all, pmf, atol=1e-12)
    # a map that can never be played: certain 2-0 in a BO3
    certain = series_prefix_sum_pmfs(MatchFormat.BO3, [1.0, 1.0, 0.5], pmf1[:1] * 3, pmf2[:1] * 3)
    assert certain[2][0] == 0.0 and certain[2][1].tolist() == [1.0]
