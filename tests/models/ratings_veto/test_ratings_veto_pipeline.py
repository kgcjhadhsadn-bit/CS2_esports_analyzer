"""Ratings + veto wired together the way ``services.prediction`` will use them (incl. the match engine)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from cs2bot.domain.enums import MatchFormat, TeamSlot
from cs2bot.domain.predictions import MapSequenceProbability, MapWinEstimate
from cs2bot.models.map_sim import MapSimulator
from cs2bot.models.match_engine import build_match_prediction
from cs2bot.models.ratings import MapRatingModel
from cs2bot.models.round_model import RoundModel
from cs2bot.models.veto import VetoModel, map_slot_distribution
from cs2bot.testing.synthetic import SynthUniverse

from .rv_helpers import universe


@pytest.fixture(scope="module")
def trained() -> tuple[SynthUniverse, MapRatingModel, VetoModel]:
    uni = universe(seed=3)
    ratings = MapRatingModel().fit(uni.map_results(), record_history=True)
    veto = VetoModel().fit(uni.veto_records(), map_advantage=ratings.historical_map_advantage)
    return uni, ratings, veto


@pytest.mark.parametrize("fmt", list(MatchFormat))
def test_veto_distribution_and_map_estimates_feed_the_match_engine(
    trained: tuple[SynthUniverse, MapRatingModel, VetoModel], fmt: MatchFormat
) -> None:
    uni, ratings, veto = trained
    as_of = max(r.played_at for r in uni.map_results()) + timedelta(days=1)
    team1, team2 = 5, 9
    sequences = veto.predict_sequences(
        team1,
        team2,
        fmt,
        uni.map_pool,
        first_veto=None,
        as_of=as_of,
        map_advantage=ratings.map_advantage,
        top_k=12,
    )
    assert all(isinstance(s, MapSequenceProbability) for s in sequences)
    assert sum(s.probability for s in sequences) == pytest.approx(1.0)
    json.dumps([s.model_dump(mode="json") for s in sequences])
    estimates = ratings.predict_for_sequences(team1, team2, sequences, as_of, is_lan=True)
    assert set(estimates) == {m for s in sequences for m in s.maps}
    assert all(isinstance(e, MapWinEstimate) for e in estimates.values())
    prediction = build_match_prediction(
        match_id=1,
        fmt=fmt,
        team1_id=team1,
        team2_id=team2,
        veto_distribution=sequences,
        map_estimates=estimates,
        round_model=RoundModel.default(),
        n_sims=300,
        seed=11,
        model_version="ratings_veto-test",
        created_at=as_of,
        simulator=MapSimulator(),
    )
    assert prediction.format is fmt
    assert len(prediction.central.slots) == fmt.n_maps
    assert prediction.veto_distribution == sequences
    # for the slots that are always played the engine's map distribution is exactly the veto
    # model's slot distribution (later slots are conditional on the series reaching them)
    slots = map_slot_distribution(sequences)
    for slot in prediction.central.slots:
        assert set(slot.map_distribution) == set(slots[slot.map_number])
        if slot.map_number <= fmt.min_maps:
            assert slot.p_played == pytest.approx(1.0)
            assert slot.map_distribution == pytest.approx(slots[slot.map_number])
    assert 0.0 < prediction.central.p_team1_win < 1.0
    if fmt is MatchFormat.BO2:
        assert prediction.central.p_draw > 0.0
    assert prediction.low is not None and prediction.high is not None
    assert prediction.low.p_team1_win <= prediction.central.p_team1_win <= prediction.high.p_team1_win


def test_known_veto_gives_the_engine_a_single_sequence(
    trained: tuple[SynthUniverse, MapRatingModel, VetoModel],
) -> None:
    uni, ratings, veto = trained
    rec = next(r for r in reversed(uni.veto_records()) if r.format is MatchFormat.BO3)
    as_of = max(r.played_at for r in uni.map_results()) + timedelta(days=1)
    sequences = veto.predict_sequences(
        rec.team1_id, rec.team2_id, rec.format, uni.map_pool, known_steps=rec.steps, as_of=as_of
    )
    assert len(sequences) == 1 and sequences[0].probability == 1.0
    picked = dict(zip(sequences[0].maps, sequences[0].pickers, strict=True))
    estimates = {
        m: ratings.predict(rec.team1_id, rec.team2_id, m, as_of, picked_by=picker)
        for m, picker in picked.items()
    }
    assert [e.picked_by for e in estimates.values()] == [TeamSlot.TEAM1, TeamSlot.TEAM2, None]
    prediction = build_match_prediction(
        match_id=rec.match_id,
        fmt=rec.format,
        team1_id=rec.team1_id,
        team2_id=rec.team2_id,
        veto_distribution=sequences,
        map_estimates=estimates,
        round_model=RoundModel.default(),
        n_sims=300,
        seed=3,
        model_version="ratings_veto-test",
        created_at=as_of,
        veto_known=True,
        simulator=MapSimulator(),
    )
    for slot, map_name in zip(prediction.central.slots, sequences[0].maps, strict=True):
        assert slot.map_distribution == {map_name: 1.0}


def test_walk_forward_use_never_looks_ahead(trained: tuple[SynthUniverse, MapRatingModel, VetoModel]) -> None:
    """Backtest pattern: one history-recording rating fit + ``fit_until`` veto models per cut-off."""
    uni, ratings, _ = trained
    vetoes = sorted(uni.veto_records(), key=lambda r: r.played_at)
    rec = vetoes[int(len(vetoes) * 0.8)]
    cut = rec.played_at
    snapshot = ratings.snapshot_before(cut)
    assert snapshot.last_processed is not None and snapshot.last_processed < cut
    veto = VetoModel().fit_until(vetoes, cut, map_advantage=ratings.historical_map_advantage)
    assert veto.last_veto_time is not None and veto.last_veto_time < cut
    sequences = veto.predict_sequences(
        rec.team1_id, rec.team2_id, rec.format, uni.map_pool, as_of=cut, map_advantage=snapshot.map_advantage
    )
    assert sum(s.probability for s in sequences) == pytest.approx(1.0)
    # the same call against the full-history model must be refused (its last game is after the cut)
    with pytest.raises(ValueError):
        veto.predict_sequences(
            rec.team1_id,
            rec.team2_id,
            rec.format,
            uni.map_pool,
            as_of=cut,
            map_advantage=ratings.map_advantage,
        )
