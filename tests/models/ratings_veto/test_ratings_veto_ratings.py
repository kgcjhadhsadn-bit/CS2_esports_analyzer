"""MapRatingModel: hierarchical online Glicko-2 on the synthetic universe and on hand-made records."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

import pytest
from pydantic import ValidationError
from scipy.stats import spearmanr

from cs2bot.domain.enums import TeamSlot, Tier
from cs2bot.domain.predictions import MapSequenceProbability, MapWinEstimate
from cs2bot.domain.records import MapResultRecord
from cs2bot.models.ratings import (
    MapRatingModel,
    RatingConfig,
    RatingSnapshot,
    chronological_split,
    evaluate_walk_forward,
    tune,
)
from cs2bot.models.ratings_glicko import g, logit, sigmoid
from cs2bot.testing.synthetic import SynthUniverse

from .rv_helpers import T0, map_result, universe

LINEUP_A = (1, 2, 3, 4, 5)
LINEUP_B = (11, 12, 13, 14, 15)


def _after(records: list[MapResultRecord], days: float = 1.0) -> datetime:
    return max(r.played_at for r in records) + timedelta(days=days)


def _season(n_rounds: int = 12, *, lineup_a: tuple[int, ...] = LINEUP_A) -> list[MapResultRecord]:
    """Team 1 (strong) beats teams 2-4 most of the time, one map every 3 days."""
    out: list[MapResultRecord] = []
    mid = 0
    maps = ["mirage", "nuke", "inferno"]
    for i in range(n_rounds):
        for opp in (2, 3, 4):
            mid += 1
            win = (mid % 5) != 0  # team 1 loses every fifth map
            out.append(
                map_result(
                    mid,
                    1,
                    opp,
                    day=3.0 * mid,
                    score=(13, 7) if win else (9, 13),
                    map_name=maps[(i + opp) % 3],
                    lineup1=lineup_a,
                    lineup2=tuple(100 * opp + k for k in range(5)),
                )
            )
    return out


# ------------------------------------------------------------------------- synthetic universe


@pytest.mark.parametrize("seed", [3, 4])
def test_overall_ratings_recover_true_strength_order(seed: int) -> None:
    uni = universe(seed=seed)
    model = MapRatingModel().fit(uni.map_results())
    ids = sorted(uni.teams)
    rho = spearmanr([uni.teams[t].strength for t in ids], [model.rating(t).mu for t in ids]).statistic
    assert rho > 0.8


def test_ratings_recover_order_on_the_shared_small_universe(synthetic_universe: SynthUniverse) -> None:
    model = MapRatingModel().fit(synthetic_universe.map_results())
    ids = sorted(synthetic_universe.teams)
    truth = [synthetic_universe.teams[t].strength for t in ids]
    rho = spearmanr(truth, [model.rating(t).mu for t in ids]).statistic
    assert rho > 0.8
    table = model.ratings_table()
    assert [s.mu for s in table] == sorted((s.mu for s in table), reverse=True)
    assert {s.team_id for s in table} == set(ids)


@pytest.mark.parametrize("seed", [3, 4])
def test_out_of_sample_scores_beat_coin_flip_and_are_calibrated(seed: int) -> None:
    recs = universe(seed=seed).map_results()
    train, hold = chronological_split(recs, 0.4)
    wf = evaluate_walk_forward(MapRatingModel().fit(train), hold)
    assert wf.n > 400
    assert wf.log_loss < math.log(2.0) - 0.05
    assert wf.brier < 0.25 - 0.02
    assert wf.accuracy > 0.6
    # reliability: observed frequency tracks the predicted probability in every populated bin
    bins = wf.reliability(5)
    assert sum(b.n for b in bins) == wf.n
    for b in bins:
        if b.n >= 40:
            assert abs(b.observed_rate - b.mean_predicted) < 0.12, b
    assert wf.ece(5) < 0.07
    # monotone: higher predicted bins win more often
    rates = [b.observed_rate for b in bins if b.n >= 40]
    assert rates == sorted(rates)


def test_map_offsets_help_when_map_pools_differ_a_lot() -> None:
    uni = universe(seed=3, map_offset_sd=0.4)
    recs = uni.map_results()
    train, hold = chronological_split(recs, 0.4)
    wf_map = evaluate_walk_forward(MapRatingModel().fit(train), hold)
    wf_overall = evaluate_walk_forward(MapRatingModel(RatingConfig(map_offset_sd=0.0)).fit(train), hold)
    assert wf_map.log_loss < wf_overall.log_loss - 0.03
    assert wf_map.brier < wf_overall.brier - 0.01
    # the "overall" component of the hierarchical model is also worse than its map-aware estimate
    assert wf_map.log_loss < wf_map.log_loss_of(overall=True) - 0.03

    model = MapRatingModel().fit(recs)
    truth: list[float] = []
    fitted: list[float] = []
    for team_id, team in uni.teams.items():
        for map_name in uni.map_pool:
            snap = model.rating(team_id, map_name)
            if snap.n_games >= 5:
                truth.append(team.map_offsets[map_name])
                fitted.append(snap.map_offset)
    assert len(truth) > 60
    assert spearmanr(truth, fitted).statistic > 0.5


def test_map_advantage_tracks_true_map_edges() -> None:
    uni = universe(seed=3, map_offset_sd=0.4)
    recs = uni.map_results()
    model = MapRatingModel().fit(recs)
    as_of = _after(recs)
    truth: list[float] = []
    fitted: list[float] = []
    for a, b in [(1, 2), (3, 7), (5, 12), (9, 16), (4, 10)]:
        for map_name in uni.map_pool:
            truth.append(uni.teams[a].map_strength(map_name) - uni.teams[b].map_strength(map_name))
            adv = model.map_advantage(a, b, map_name, as_of)
            assert adv == pytest.approx(-model.map_advantage(b, a, map_name, as_of), abs=1e-9)
            assert adv == pytest.approx(logit(model.predict(a, b, map_name, as_of).p_team1_win))
            fitted.append(adv)
    assert spearmanr(truth, fitted).statistic > 0.8


def test_walk_forward_predictions_use_only_earlier_games() -> None:
    recs = universe(seed=3).map_results()
    train, hold = chronological_split(recs, 0.1)
    wf = evaluate_walk_forward(MapRatingModel().fit(train), hold)
    by_key = {(r.match_id, r.map_number): r for r in recs}
    for pred in wf.predictions[5::37]:
        rec = by_key[(pred.match_id, pred.map_number)]
        assert pred.as_of <= rec.played_at
        reference = MapRatingModel().fit_until(recs, pred.as_of)
        est = reference.predict(
            rec.team1_id,
            rec.team2_id,
            rec.map_name,
            pred.as_of,
            is_lan=rec.is_lan,
            team1_lineup=rec.team1_lineup,
            team2_lineup=rec.team2_lineup,
        )
        assert pred.p_team1_win == pytest.approx(est.p_team1_win, abs=1e-12)
        assert pred.team1_won == (rec.team1_rounds > rec.team2_rounds)


# ---------------------------------------------------------------------------- Glicko-2 core


def test_single_game_update_is_exactly_glicko2() -> None:
    """Without map offsets one game moves the rating by the Glicko-2 formulas (Steps 3, 4, 7)."""
    cfg = RatingConfig(map_offset_sd=0.0, mov_weight=0.0, initial_phi=1.2)
    model = MapRatingModel(cfg)
    assert model.update(map_result(1, 1, 2, score=(13, 9)))
    phi0 = cfg.initial_phi
    g_opp = g(phi0)
    e = sigmoid(g_opp * 0.0)
    v = 1.0 / (g_opp**2 * e * (1 - e))
    phi_new = 1.0 / math.sqrt(1.0 / phi0**2 + 1.0 / v)
    mu_new = phi_new**2 * g_opp * (1.0 - e)
    winner, loser = model.rating(1), model.rating(2)
    assert winner.phi == pytest.approx(phi_new)
    assert winner.mu == pytest.approx(mu_new)
    assert loser.mu == pytest.approx(-mu_new)
    assert loser.phi == pytest.approx(phi_new)
    assert winner.r == pytest.approx(1500.0 + 173.7178 * mu_new)
    assert winner.rd == pytest.approx(173.7178 * phi_new)
    assert winner.n_games == 1
    assert winner.last_played == T0
    assert winner.sigma == cfg.initial_sigma


def test_deviation_inflates_with_time_and_is_capped() -> None:
    cfg = RatingConfig(map_offset_sd=0.0, initial_phi=2.0, initial_sigma=0.3, max_sigma=0.5)
    recs = [map_result(i, 1, 2, day=0.1 * i, score=(13, 10) if i % 2 else (10, 13)) for i in range(1, 9)]
    model = MapRatingModel(cfg).fit(recs)
    last = recs[-1].played_at
    base = model.rating(1)
    assert base.phi < 1.0
    soon = model.rating(1, None, last + timedelta(days=7))
    later = model.rating(1, None, last + timedelta(days=70))
    never = model.rating(1, None, last + timedelta(days=36500))
    assert base.phi < soon.phi < later.phi < never.phi
    # phi_new = sqrt(phi^2 + sigma^2 * periods) with 7-day periods ...
    assert later.phi == pytest.approx(math.sqrt(base.phi**2 + later.sigma**2 * 10.0))
    # ... capped at the initial deviation
    assert never.phi == pytest.approx(cfg.initial_phi)
    assert soon.mu == base.mu
    # a longer lay-off makes the prediction less certain
    p_soon = model.predict(1, 2, "mirage", last + timedelta(days=7))
    p_later = model.predict(1, 2, "mirage", last + timedelta(days=200))
    assert p_later.sigma > p_soon.sigma


def test_volatility_stays_bounded_and_reacts_to_erratic_results() -> None:
    cfg = RatingConfig(map_offset_sd=0.0, tau=1.2, initial_sigma=0.06)
    steady: list[MapResultRecord] = []
    mid = 0
    for week in range(30):
        for k in range(4):
            mid += 1
            steady.append(map_result(mid, 1, 2, day=7 * week + k, score=(13, 11) if mid % 2 else (11, 13)))
    model = MapRatingModel(cfg).fit(steady)
    sigma_steady = model.rating(1).sigma
    assert cfg.min_sigma <= sigma_steady <= cfg.initial_sigma + 1e-9
    # team 1 suddenly wins everything: results far outside expectation raise its volatility
    streak = [map_result(1000 + i, 1, 2, day=210 + 0.5 * i, score=(13, 2)) for i in range(40)]
    model2 = MapRatingModel(cfg).fit(steady + streak)
    snap = model2.rating(1, None, _after(streak, 10))
    assert snap.sigma > sigma_steady
    assert snap.sigma <= cfg.max_sigma
    assert snap.mu > model.rating(1).mu + 0.5


# ---------------------------------------------------------------------------- map hierarchy


def test_first_game_on_a_map_starts_from_overall_rating_with_inflated_deviation() -> None:
    recs = [r for r in _season(8) if r.map_name == "mirage" or r.team2_id != 2]
    model = MapRatingModel().fit(recs)
    overall = model.rating(1)
    unseen = model.rating(1, "overpass")
    assert unseen.n_games == 0
    assert unseen.last_played is None
    assert unseen.mu == pytest.approx(overall.mu)
    assert unseen.map_offset == 0.0
    assert unseen.map_weight == 0.0
    assert unseen.phi == pytest.approx(math.sqrt(overall.phi**2 + model.config.map_offset_sd**2))
    assert unseen.phi > overall.phi
    seen = model.rating(1, "mirage")
    assert seen.n_games > 0
    assert 0.0 < seen.map_weight < 1.0
    assert seen.phi < unseen.phi
    assert seen.mu == pytest.approx(overall.mu + seen.map_offset)
    assert seen.r == pytest.approx(1500.0 + 173.7178 * seen.mu)


def test_map_shrinkage_weight_follows_n_over_n_plus_k() -> None:
    """Documented choice: w = 1 - psi^2/psi_0^2 ~= n / (n + k), k = 4 / map_offset_sd^2."""
    cfg = RatingConfig(initial_phi=0.25, map_offset_sd=0.5, map_offset_drift=0.0, mov_weight=0.0)
    assert cfg.equivalent_k_shrink == pytest.approx(16.0)
    assert RatingConfig(map_offset_sd=0.0).equivalent_k_shrink == math.inf
    model = MapRatingModel(cfg)
    weights: list[float] = []
    for n in range(1, 41):
        model.update(map_result(n, 1, 2, day=0.01 * n, score=(13, 11) if n % 2 else (11, 13)))
        weights.append(model.rating(1, "mirage").map_weight)
    assert weights == sorted(weights)
    for n in (4, 16, 40):
        assert weights[n - 1] == pytest.approx(n / (n + cfg.equivalent_k_shrink), abs=0.06)
    # a different map is still at the prior
    assert model.rating(1, "nuke").map_weight == 0.0


def test_map_specialist_is_rated_higher_on_its_map() -> None:
    recs: list[MapResultRecord] = []
    for i in range(1, 31):
        on_nuke = i % 2 == 0
        recs.append(
            map_result(
                i,
                1,
                2,
                day=i,
                score=(13, 6) if on_nuke else (7, 13),
                map_name="nuke" if on_nuke else "mirage",
            )
        )
    model = MapRatingModel().fit(recs)
    as_of = _after(recs)
    nuke = model.predict(1, 2, "nuke", as_of)
    mirage = model.predict(1, 2, "mirage", as_of)
    neutral = model.predict(1, 2, "inferno", as_of)
    assert nuke.p_team1_win > 0.7 > 0.3 > mirage.p_team1_win
    assert mirage.p_team1_win < neutral.p_team1_win < nuke.p_team1_win
    assert nuke.components["overall"] == pytest.approx(mirage.components["overall"])
    assert nuke.components["map"] == pytest.approx(nuke.p_team1_win)
    assert neutral.sigma > nuke.sigma  # an unplayed map is more uncertain
    assert model.rating(1, "nuke").map_offset > 0.3
    assert model.rating(1, "mirage").map_offset < -0.3
    assert model.map_advantage(1, 2, "nuke", as_of) > 0 > model.map_advantage(1, 2, "mirage", as_of)


def test_overall_only_config_has_no_map_component() -> None:
    recs = _season(6)
    model = MapRatingModel(RatingConfig(map_offset_sd=0.0)).fit(recs)
    as_of = _after(recs)
    a = model.predict(1, 2, "mirage", as_of)
    b = model.predict(1, 2, "nuke", as_of)
    assert a.p_team1_win == pytest.approx(b.p_team1_win)
    assert a.components["overall"] == pytest.approx(a.components["map"])
    snap = model.rating(1, "mirage")
    assert snap.mu == pytest.approx(model.rating(1).mu)
    assert snap.map_weight == 0.0


def test_map_names_are_normalised() -> None:
    recs = [map_result(i, 1, 2, day=i, map_name="de_dust2" if i % 2 else "Dust II") for i in range(1, 9)]
    model = MapRatingModel().fit(recs)
    assert model.rating(1, "DUST2").n_games == 8
    as_of = _after(recs)
    est = model.predict(1, 2, "de_dust2", as_of)
    assert est.map_name == "dust2"
    assert est.p_team1_win == pytest.approx(model.predict(1, 2, "Dust 2", as_of).p_team1_win)


# ------------------------------------------------------------------------------ predictions


def test_predict_returns_map_win_estimate_with_components() -> None:
    recs = _season(8)
    model = MapRatingModel().fit(recs)
    est = model.predict(1, 2, "mirage", _after(recs), picked_by=TeamSlot.TEAM2)
    assert isinstance(est, MapWinEstimate)
    assert est.map_name == "mirage"
    assert est.picked_by is TeamSlot.TEAM2
    assert 0.5 < est.p_team1_win < 1.0
    assert 0.0 < est.sigma < 0.5
    assert {"overall", "map", "logit_mean", "logit_sd"} <= set(est.components)
    assert all(isinstance(v, float) for v in est.components.values())
    json.dumps(est.model_dump(mode="json"))  # JSON friendly


def test_predictions_are_antisymmetric() -> None:
    recs = _season(8)
    model = MapRatingModel().fit(recs)
    as_of = _after(recs)
    for a, b, map_name, lan in [(1, 2, "mirage", False), (3, 4, "nuke", True), (2, 999, "train", False)]:
        ab = model.predict(a, b, map_name, as_of, is_lan=lan)
        ba = model.predict(b, a, map_name, as_of, is_lan=lan)
        assert ab.p_team1_win + ba.p_team1_win == pytest.approx(1.0)
        assert ab.sigma == pytest.approx(ba.sigma)


def test_unknown_teams_get_the_prior() -> None:
    recs = _season(8)
    model = MapRatingModel().fit(recs)
    as_of = _after(recs)
    both_unknown = model.predict(777, 888, "mirage", as_of)
    assert both_unknown.p_team1_win == pytest.approx(0.5)
    assert both_unknown.sigma > 0.3
    known = model.predict(1, 2, "mirage", as_of)
    assert both_unknown.sigma > 2 * known.sigma
    vs_strong = model.predict(777, 1, "mirage", as_of)
    assert vs_strong.p_team1_win < 0.5
    assert vs_strong.sigma > known.sigma
    snap = model.rating(777)
    assert isinstance(snap, RatingSnapshot)
    assert snap.is_prior
    assert snap.n_games == 0
    assert snap.last_played is None
    assert snap.r == pytest.approx(1500.0)
    assert snap.rd == pytest.approx(173.7178 * model.config.initial_phi)
    assert not model.rating(1).is_prior
    # an empty model predicts the prior for everybody and accepts any as_of
    empty = MapRatingModel()
    assert empty.predict(1, 2, "mirage", T0).p_team1_win == pytest.approx(0.5)
    assert empty.last_processed is None


@runtime_checkable
class _RatingProvider(Protocol):
    """Mirror of ``cs2bot.features.RatingProvider`` (see the brief addendum)."""

    def predict(
        self, team1_id: int, team2_id: int, map_name: str, as_of: datetime, *, is_lan: bool = False
    ) -> MapWinEstimate: ...


def test_model_satisfies_the_rating_provider_protocol() -> None:
    recs = _season(4)
    provider: _RatingProvider = MapRatingModel().fit(recs)
    assert isinstance(provider, _RatingProvider)
    est = provider.predict(1, 2, "mirage", _after(recs), is_lan=True)
    assert isinstance(est, MapWinEstimate)


# ------------------------------------------------------------------------------ no look-ahead


def test_predict_refuses_as_of_not_after_last_processed_game() -> None:
    recs = _season(6)
    model = MapRatingModel().fit(recs)
    last = recs[-1].played_at
    assert model.last_processed == last
    with pytest.raises(ValueError, match="strictly before as_of"):
        model.predict(1, 2, "mirage", last)
    with pytest.raises(ValueError):
        model.predict(1, 2, "mirage", last - timedelta(days=30))
    with pytest.raises(ValueError):
        model.rating(1, None, last - timedelta(seconds=1))
    with pytest.raises(ValueError):
        model.map_advantage(1, 2, "mirage", last)
    with pytest.raises(ValueError):
        model.ratings_table(as_of=last)
    assert 0.0 < model.predict(1, 2, "mirage", last + timedelta(seconds=1)).p_team1_win < 1.0


def test_fit_until_ignores_games_at_or_after_the_cut_off() -> None:
    recs = universe(seed=3).map_results()
    cut = recs[len(recs) // 2].played_at
    before = [r for r in recs if r.played_at < cut]
    assert 0 < len(before) < len(recs)
    truncated = MapRatingModel().fit(before)
    until = MapRatingModel().fit_until(recs, cut)
    assert until.n_processed == len(before)
    assert until.last_processed is not None and until.last_processed < cut
    assert until.to_dict() == truncated.to_dict()
    # changing the future does not change a prediction made at the cut-off
    future = [r for r in recs if r.played_at >= cut]
    flipped = [
        r.model_copy(update={"team1_rounds": r.team2_rounds, "team2_rounds": r.team1_rounds}) for r in future
    ]
    other = MapRatingModel().fit_until(before + flipped, cut)
    for a, b in [(1, 2), (5, 9), (3, 14)]:
        assert other.predict(a, b, "nuke", cut).p_team1_win == until.predict(a, b, "nuke", cut).p_team1_win


def test_history_snapshots_equal_refitting_up_to_the_cut_off() -> None:
    recs = universe(seed=3).map_results()
    full = MapRatingModel().fit(recs, record_history=True)
    assert full.records_history
    for frac in (0.2, 0.55, 0.9):
        cut = recs[int(len(recs) * frac)].played_at
        reference = MapRatingModel().fit_until(recs, cut)
        snap = full.snapshot_before(cut)
        assert snap.to_dict() == reference.to_dict()
        for a, b, map_name in [(1, 2, "mirage"), (7, 3, "nuke"), (16, 11, "train")]:
            want = reference.predict(a, b, map_name, cut, is_lan=True)
            got = full.historical_predict(a, b, map_name, cut, is_lan=True)
            assert got.p_team1_win == pytest.approx(want.p_team1_win, abs=1e-12)
            assert got.sigma == pytest.approx(want.sigma, abs=1e-12)
            assert full.historical_map_advantage(a, b, map_name, cut) == pytest.approx(
                reference.map_advantage(a, b, map_name, cut), abs=1e-12
            )
    # before the first game everybody is at the prior
    first = recs[0].played_at
    assert full.historical_predict(1, 2, "mirage", first).p_team1_win == pytest.approx(0.5)
    with pytest.raises(RuntimeError):
        MapRatingModel().fit(recs[:50]).historical_map_advantage(1, 2, "mirage", first)


def test_updates_must_be_chronological_and_are_idempotent() -> None:
    model = MapRatingModel()
    first = map_result(1, 1, 2, day=5)
    assert model.update(first) is True
    state = model.to_dict()
    assert model.update(first) is False  # same (match_id, map_number): ignored
    assert model.to_dict() == state
    with pytest.raises(ValueError, match="chronologically"):
        model.update(map_result(2, 1, 2, day=4))
    # records that carry no information are skipped without touching the state
    assert model.update(map_result(3, 1, 1, day=6)) is False
    assert model.update(map_result(4, 1, 2, day=6, score=(12, 12))) is False
    assert model.to_dict() == state
    assert model.n_processed == 1
    assert model.has_processed_match(1)
    assert not model.has_processed_match(4)
    # fit sorts its input and ignores duplicates
    recs = _season(4)
    shuffled = list(reversed(recs)) + recs[:5]
    assert MapRatingModel().fit(shuffled).to_dict() == MapRatingModel().fit(recs).to_dict()


def test_copy_is_independent() -> None:
    recs = _season(6)
    model = MapRatingModel().fit(recs[:-6])
    clone = model.copy()
    for r in recs[-6:]:
        model.update(r)
    assert clone.n_processed == len(recs) - 6
    assert clone.last_processed == recs[-7].played_at
    assert clone.to_dict() == MapRatingModel().fit(recs[:-6]).to_dict()
    assert model.to_dict() == MapRatingModel().fit(recs).to_dict()


# ----------------------------------------------------------------------------------- roster


def test_roster_change_increases_uncertainty_progressively() -> None:
    recs = _season(12)
    model = MapRatingModel().fit(recs)
    as_of = _after(recs)
    same = model.predict(1, 2, "mirage", as_of, team1_lineup=LINEUP_A)
    assert same.sigma == pytest.approx(model.predict(1, 2, "mirage", as_of).sigma)
    assert same.components["team1_roster_changes"] == 0.0
    sigmas = [same.sigma]
    phis = [same.components["team1_phi"]]
    probs = [same.p_team1_win]
    for n_new in range(1, 6):
        lineup = LINEUP_A[: 5 - n_new] + tuple(900 + k for k in range(n_new))
        est = model.predict(1, 2, "mirage", as_of, team1_lineup=lineup)
        assert est.components["team1_roster_changes"] == float(n_new)
        sigmas.append(est.sigma)
        phis.append(est.components["team1_phi"])
        probs.append(est.p_team1_win)
    assert sigmas == sorted(sigmas)
    assert phis == sorted(phis)
    assert sigmas[1] > sigmas[0]
    # one change is mild, three or more is close to a reset
    full_reset = phis[5] - phis[0]
    assert (phis[1] - phis[0]) < 0.35 * full_reset
    assert (phis[3] - phis[0]) > 0.7 * full_reset
    # the favourite's edge regresses toward the population mean
    assert probs[0] > probs[1] > probs[3] > probs[5] > 0.5
    # hypothetical lineups never change the stored state
    assert model.to_dict() == MapRatingModel().fit(recs).to_dict()


def test_full_roster_replacement_resets_deviation_and_regresses_rating() -> None:
    cfg = RatingConfig(map_offset_sd=0.0, roster_mu_regression=0.5)
    recs = _season(12)
    model = MapRatingModel(cfg).fit(recs)
    before = model.rating(1)
    as_of = _after(recs, 0.001)
    new_five = model.predict(1, 777, "mirage", as_of, team1_lineup=(901, 902, 903, 904, 905))
    assert new_five.components["team1_phi"] == pytest.approx(cfg.initial_phi, rel=1e-6)
    # mu regressed halfway to the mean: the logit mean halves (opponent is an unknown team at 0)
    assert new_five.components["logit_mean"] == pytest.approx(0.5 * before.mu, rel=1e-6)


def test_roster_change_in_a_played_game_is_applied_to_the_state() -> None:
    recs = _season(12)
    nxt_same = map_result(500, 1, 2, day=200, lineup1=LINEUP_A, lineup2=(200, 201, 202, 203, 204))
    nxt_new = map_result(500, 1, 2, day=200, lineup1=(1, 2, 901, 902, 903), lineup2=(200, 201, 202, 203, 204))
    same = MapRatingModel().fit([*recs, nxt_same])
    changed = MapRatingModel().fit([*recs, nxt_new])
    assert changed.rating(1).phi > same.rating(1).phi
    # the opponent's own state is unaffected by team 1's roster move apart from the result itself
    assert changed.rating(3).mu == same.rating(3).mu
    # the new lineup is now the reference: predicting with it shows no further change
    as_of = nxt_new.played_at + timedelta(days=1)
    est = changed.predict(1, 2, "mirage", as_of, team1_lineup=(903, 902, 901, 2, 1))
    assert est.components["team1_roster_changes"] == 0.0


def test_incomplete_lineups_are_not_roster_changes() -> None:
    recs = _season(6)
    model = MapRatingModel().fit(recs)
    as_of = _after(recs)
    base = model.predict(1, 2, "mirage", as_of)
    partial = model.predict(1, 2, "mirage", as_of, team1_lineup=(1, 2, 3))
    assert partial.components["team1_roster_changes"] == 0.0
    assert partial.sigma == pytest.approx(base.sigma)
    # a record listing only three known players keeps the full reference lineup
    extra = map_result(900, 1, 2, day=150, lineup1=(1, 2, 3))
    model.update(extra)
    after = model.predict(1, 2, "mirage", extra.played_at + timedelta(days=1), team1_lineup=LINEUP_A)
    assert after.components["team1_roster_changes"] == 0.0
    one_new = model.predict(
        1, 2, "mirage", extra.played_at + timedelta(days=1), team1_lineup=(1, 2, 3, 4, 99)
    )
    assert one_new.components["team1_roster_changes"] == 1.0
    # teams without lineup information are never adjusted
    no_lineups = MapRatingModel().fit([r.model_copy(update={"team1_lineup": ()}) for r in recs])
    est = no_lineups.predict(1, 2, "mirage", as_of, team1_lineup=(7, 8, 9, 10, 11))
    assert est.components["team1_roster_changes"] == 0.0


# ------------------------------------------------------------------ margin, LAN, tier weights


def test_margin_of_victory_scales_the_update_but_a_win_always_gains() -> None:
    def gain(score: tuple[int, int], weight: float) -> float:
        model = MapRatingModel(RatingConfig(mov_weight=weight))
        model.update(map_result(1, 1, 2, score=score))
        return model.rating(1).mu

    for weight in (0.0, 0.1, 0.5, 1.0):
        blowout, close, overtime = gain((13, 1), weight), gain((13, 11), weight), gain((19, 17), weight)
        assert blowout > 0 and close > 0 and overtime > 0
        assert gain((11, 13), weight) < 0
        if weight == 0.0:
            assert blowout == pytest.approx(close)
        else:
            assert blowout > close > overtime
    # default: mild (a close win keeps at least 90% of a blowout's gain)
    default = RatingConfig().mov_weight
    assert 0.0 < default <= 0.25
    assert gain((13, 11), default) > 0.9 * gain((13, 1), default)


def test_lan_adjustment_default_zero_and_configurable() -> None:
    recs = _season(8)
    as_of = _after(recs)
    default = MapRatingModel().fit(recs)
    assert RatingConfig().lan_scale == 0.0
    online = default.predict(1, 2, "mirage", as_of)
    lan = default.predict(1, 2, "mirage", as_of, is_lan=True)
    assert lan.p_team1_win == pytest.approx(online.p_team1_win)
    assert lan.components["lan"] == 1.0 and online.components["lan"] == 0.0
    scaled = MapRatingModel(RatingConfig(lan_scale=0.3)).fit(recs)
    s_online = scaled.predict(1, 2, "mirage", as_of)
    s_lan = scaled.predict(1, 2, "mirage", as_of, is_lan=True)
    assert s_lan.components["logit_mean"] == pytest.approx(1.3 * s_online.components["logit_mean"])
    assert s_lan.p_team1_win > s_online.p_team1_win > 0.5


def test_per_team_lan_offsets_learn_lan_specialists() -> None:
    recs: list[MapResultRecord] = []
    for i in range(1, 41):
        lan = i % 2 == 0
        recs.append(map_result(i, 1, 2, day=i, score=(13, 7) if lan else (7, 13), is_lan=lan))
    model = MapRatingModel(RatingConfig(lan_offset_sd=0.6)).fit(recs)
    as_of = _after(recs)
    assert (
        model.predict(1, 2, "mirage", as_of, is_lan=True).p_team1_win
        > 0.6
        > 0.4
        > (model.predict(1, 2, "mirage", as_of, is_lan=False).p_team1_win)
    )
    restored = MapRatingModel.from_dict(json.loads(json.dumps(model.to_dict())))
    assert restored.predict(1, 2, "mirage", as_of, is_lan=True).p_team1_win == pytest.approx(
        model.predict(1, 2, "mirage", as_of, is_lan=True).p_team1_win
    )


def test_tier_weights_scale_the_information_of_a_game() -> None:
    def gain(tier: Tier, weights: dict[str, float]) -> float:
        model = MapRatingModel(RatingConfig(tier_weights=weights))
        model.update(map_result(1, 1, 2, tier=tier))
        return model.rating(1).mu

    base = gain(Tier.S, {})
    assert gain(Tier.B, {}) == pytest.approx(base)
    assert gain(Tier.S, {"s": 2.0}) > base
    assert gain(Tier.B, {"b": 0.5}) < base
    assert gain(Tier.A, {"s": 2.0}) == pytest.approx(base)
    with pytest.raises(ValidationError):
        RatingConfig(tier_weights={"platinum": 1.0})
    with pytest.raises(ValidationError):
        RatingConfig(tier_weights={"s": 0.0})


def test_config_validation() -> None:
    with pytest.raises(ValidationError):
        RatingConfig(initial_phi=0.0)
    with pytest.raises(ValidationError):
        RatingConfig(roster_reset_fractions=(0.0, 0.5, 0.2))
    with pytest.raises(ValidationError):
        RatingConfig(roster_reset_fractions=())
    with pytest.raises(ValidationError):
        RatingConfig(initial_sigma=0.9)
    with pytest.raises(ValidationError):
        RatingConfig(mov_weight=1.5)
    with pytest.raises(ValidationError):
        RatingConfig.model_validate({"not_a_field": 1})
    cfg = RatingConfig(roster_reset_fractions=(0.0, 0.5))
    assert cfg.roster_fraction(0) == 0.0
    assert cfg.roster_fraction(1) == 0.5
    assert cfg.roster_fraction(4) == 0.5  # beyond the table: last value


# ------------------------------------------------------------------------------ persistence


def test_save_load_round_trip(tmp_path: Path) -> None:
    recs = sorted(universe(seed=3).map_results(), key=lambda r: r.played_at)[:500]
    cfg = RatingConfig(tier_weights={"s": 1.5}, lan_scale=0.1, lan_offset_sd=0.2, mov_weight=0.2)
    model = MapRatingModel(cfg).fit(recs[:400])
    path = model.save(tmp_path / "models" / "ratings.json")
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["kind"] == "cs2bot.MapRatingModel"
    loaded = MapRatingModel.load(path)
    assert loaded.config == cfg
    assert loaded.to_dict() == model.to_dict()
    assert loaded.last_processed == model.last_processed
    as_of = recs[399].played_at + timedelta(hours=1)
    for a, b, map_name in [(1, 2, "mirage"), (4, 9, "nuke"), (12, 99, "ancient")]:
        want = model.predict(a, b, map_name, as_of, is_lan=True)
        got = loaded.predict(a, b, map_name, as_of, is_lan=True)
        assert got == want
    assert loaded.rating(3, "inferno") == model.rating(3, "inferno")
    # the loaded model keeps learning exactly like the original (and skips what it already saw)
    assert loaded.update(recs[10]) is False
    for r in recs[400:]:
        assert loaded.update(r) == model.update(r)
    assert loaded.to_dict() == model.to_dict()


def test_load_rejects_foreign_payloads(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        MapRatingModel.from_dict({"kind": "something else"})
    payload = MapRatingModel().to_dict()
    payload["version"] = 999
    with pytest.raises(ValueError):
        MapRatingModel.from_dict(payload)
    empty = MapRatingModel.load(MapRatingModel().save(tmp_path / "empty.json"))
    assert empty.n_processed == 0
    assert empty.team_ids == []


# ----------------------------------------------------------------------------------- tuning


def test_chronological_split_respects_match_boundaries() -> None:
    recs = universe(seed=3).map_results()
    train, hold = chronological_split(recs, 0.25)
    assert len(train) + len(hold) == len(recs)
    split = min(r.played_at for r in hold)
    assert max(r.played_at for r in train) < split
    assert not {r.match_id for r in train} & {r.match_id for r in hold}
    n_matches = len({r.match_id for r in recs})
    assert len({r.match_id for r in hold}) == pytest.approx(0.25 * n_matches, abs=2)
    with pytest.raises(ValueError):
        chronological_split(recs, 1.0)
    with pytest.raises(ValueError):
        chronological_split(recs[:1], 0.5)


def test_tune_picks_the_config_with_the_lowest_holdout_log_loss() -> None:
    recs = universe(seed=3, map_offset_sd=0.4).map_results()
    grid = {"map_offset_sd": [0.0, 0.6], "mov_weight": [0.0, 0.1]}
    best, report = tune(recs, grid, holdout_fraction=0.25)
    assert isinstance(best, RatingConfig)
    assert len(report["results"]) == 4
    losses = [row["log_loss"] for row in report["results"]]
    assert losses == sorted(losses)
    assert report["best_log_loss"] == losses[0]
    assert report["best_log_loss"] < report["baseline_log_loss"] == pytest.approx(math.log(2))
    assert report["n_train"] + report["n_holdout"] == len(recs)
    # map offsets matter on this universe
    assert best.map_offset_sd == 0.6
    assert report["best_params"] == {"map_offset_sd": 0.6, "mov_weight": best.mov_weight}
    # the reported loss is reproducible by hand
    train, hold = chronological_split(recs, 0.25)
    manual = evaluate_walk_forward(MapRatingModel(best).fit(train), hold).log_loss
    assert report["best_log_loss"] == pytest.approx(manual)
    json.dumps(report)
    # the class-level alias and the base config are honoured
    base = RatingConfig(initial_phi=1.5)
    best2, _ = MapRatingModel.tune(recs, {"mov_weight": [0.1]}, base_config=base)
    assert best2.initial_phi == 1.5
    with pytest.raises(ValueError, match="unknown RatingConfig fields"):
        tune(recs, {"nope": [1]})


def test_default_config_is_near_the_optimum_of_a_coarse_grid() -> None:
    """Defaults were tuned on synthetic universes: no coarse alternative is clearly better."""
    recs = universe(seed=4).map_results()
    _, report = tune(
        recs, {"map_offset_sd": [0.3, 0.6, 1.0], "initial_phi": [1.2, 2.0]}, holdout_fraction=0.4
    )
    default_row = next(
        row for row in report["results"] if row["params"] == {"map_offset_sd": 0.6, "initial_phi": 2.0}
    )
    assert default_row["log_loss"] - report["best_log_loss"] < 0.01


# ----------------------------------------------------------------------- map pick advantage


def test_pick_advantage_is_off_by_default_and_shifts_the_picker_when_enabled() -> None:
    recs = _season(8)
    as_of = _after(recs)
    assert RatingConfig().pick_advantage == 0.0
    plain = MapRatingModel().fit(recs)
    base = plain.predict(1, 2, "mirage", as_of)
    assert plain.predict(1, 2, "mirage", as_of, picked_by=TeamSlot.TEAM2).p_team1_win == base.p_team1_win
    assert base.components["pick_shift"] == 0.0

    model = MapRatingModel(RatingConfig(pick_advantage=0.3)).fit(recs)  # records carry no picker
    assert model.to_dict()["teams"] == plain.to_dict()["teams"]
    neutral = model.predict(1, 2, "mirage", as_of)
    own = model.predict(1, 2, "mirage", as_of, picked_by=TeamSlot.TEAM1)
    theirs = model.predict(1, 2, "mirage", as_of, picked_by=TeamSlot.TEAM2)
    assert neutral.p_team1_win == pytest.approx(base.p_team1_win)
    assert own.components["logit_mean"] == pytest.approx(neutral.components["logit_mean"] + 0.3)
    assert theirs.components["logit_mean"] == pytest.approx(neutral.components["logit_mean"] - 0.3)
    assert theirs.p_team1_win < neutral.p_team1_win < own.p_team1_win
    assert own.picked_by is TeamSlot.TEAM1
    # orientation: "team 1 on its own pick" is the mirror image of "team 2 on its opponent's pick"
    mirrored = model.predict(2, 1, "mirage", as_of, picked_by=TeamSlot.TEAM2)
    assert own.p_team1_win + mirrored.p_team1_win == pytest.approx(1.0)
    # map_advantage (used by the veto model) never includes the pick shift
    assert model.map_advantage(1, 2, "mirage", as_of) == pytest.approx(logit(neutral.p_team1_win))


def test_pick_advantage_discounts_wins_on_the_own_pick() -> None:
    def gain(picked_by: TeamSlot | None) -> float:
        model = MapRatingModel(RatingConfig(pick_advantage=0.4))
        model.update(map_result(1, 1, 2).model_copy(update={"picked_by": picked_by}))
        return model.rating(1).mu

    assert gain(TeamSlot.TEAM2) > gain(None) > gain(TeamSlot.TEAM1) > 0.0


def test_pick_advantage_helps_on_the_synthetic_universe() -> None:
    """Synthetic teams pick on their true relative map strength, so the picker beats its rating."""
    recs = universe(seed=4).map_results()
    best, report = tune(recs, {"pick_advantage": [0.0, 0.3]}, holdout_fraction=0.4)
    assert best.pick_advantage == 0.3
    by_value = {row["params"]["pick_advantage"]: row["log_loss"] for row in report["results"]}
    assert by_value[0.3] < by_value[0.0] - 0.002


def test_predict_for_sequences_gives_one_estimate_per_map() -> None:
    recs = _season(8)
    as_of = _after(recs)
    t1, t2 = TeamSlot.TEAM1, TeamSlot.TEAM2
    sequences = [
        MapSequenceProbability(
            maps=["mirage", "de_nuke", "inferno"], pickers=[t1, t2, None], probability=0.6
        ),
        MapSequenceProbability(maps=["mirage", "inferno", "nuke"], pickers=[t1, t2, None], probability=0.3),
        MapSequenceProbability(maps=["train", "mirage", "nuke"], pickers=[t2, t1, None], probability=0.1),
        MapSequenceProbability(maps=["dust2", "mirage", "nuke"], pickers=[t2, t1, None], probability=0.0),
    ]
    plain = MapRatingModel().fit(recs)
    estimates = plain.predict_for_sequences(1, 2, sequences, as_of, is_lan=True)
    assert set(estimates) == {"mirage", "nuke", "inferno", "train"}  # zero-probability paths ignored
    for map_name, est in estimates.items():
        assert est.map_name == map_name
        assert est.p_team1_win == pytest.approx(plain.predict(1, 2, map_name, as_of, is_lan=True).p_team1_win)
    assert estimates["mirage"].picked_by is t1  # the same picker in every sequence
    assert estimates["train"].picked_by is t2
    assert estimates["nuke"].picked_by is None  # team 2's pick in one sequence, decider in others
    assert estimates["inferno"].picked_by is None

    model = MapRatingModel(RatingConfig(pick_advantage=0.4)).fit(recs)
    mixed = model.predict_for_sequences(1, 2, sequences, as_of)
    as_pick = model.predict(1, 2, "nuke", as_of, picked_by=t2)
    as_decider = model.predict(1, 2, "nuke", as_of)
    want = 0.6 * as_pick.p_team1_win + 0.4 * as_decider.p_team1_win
    assert mixed["nuke"].p_team1_win == pytest.approx(want)
    assert as_pick.p_team1_win < mixed["nuke"].p_team1_win < as_decider.p_team1_win
    assert mixed["nuke"].sigma == pytest.approx(0.6 * as_pick.sigma + 0.4 * as_decider.sigma)
    assert mixed["nuke"].components["p_picked_by_team2"] == pytest.approx(0.6)
    assert mixed["nuke"].components["pick_shift"] == pytest.approx(-0.4 * 0.6)
    assert mixed["mirage"].p_team1_win == pytest.approx(
        model.predict(1, 2, "mirage", as_of, picked_by=t1).p_team1_win
    )
    with pytest.raises(ValueError):
        model.predict_for_sequences(1, 2, sequences, recs[-1].played_at)
    assert model.predict_for_sequences(1, 2, [], as_of) == {}


def test_sigma_matches_the_real_error_against_ground_truth() -> None:
    """``sigma`` is an honest error bar: errors vs the true probability are about one sigma wide."""
    uni = universe(seed=3)
    recs = uni.map_results()
    train, hold = chronological_split(recs, 0.4)
    wf = evaluate_walk_forward(MapRatingModel().fit(train), hold)
    z_scores: list[float] = []
    errors: list[float] = []
    truth: dict[tuple[int, int, str], float] = {}
    for pred in wf.predictions[3::10][:45]:
        key = (pred.team1_id, pred.team2_id, pred.map_name)
        if key not in truth:
            truth[key] = uni.true_map_win_prob(*key, n_sims=500, seed=1)
        errors.append(truth[key] - pred.p_team1_win)
        z_scores.append(errors[-1] / pred.sigma)
    n = len(z_scores)
    rms_z = math.sqrt(sum(z * z for z in z_scores) / n)
    assert 0.5 < rms_z < 1.5  # neither wildly over- nor under-confident
    assert sum(abs(z) < 2.0 for z in z_scores) / n > 0.85
    assert math.sqrt(sum(e * e for e in errors) / n) < 0.2
    assert abs(sum(errors) / n) < 0.06  # no systematic bias toward team1 / team2
