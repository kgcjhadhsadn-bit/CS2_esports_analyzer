"""Final review (Lens B) regressions in the model plane: the ensemble must not depend on team order.

Mirrored training rows do not make LightGBM antisymmetric, and which team is "team1" is an accident of
the source (PandaScore's opponent order). Before the fix the same map between the same two teams got
0.44 for "A vs B" and 1 - 0.61 = 0.39 for "B vs A" on a synthetic data set - a spurious edge either way.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from cs2bot.domain.context import ContextFlag
from cs2bot.domain.enums import ContextFlagType, MatchFormat, Side, TeamSlot, VetoAction
from cs2bot.domain.match import LineupPlayer, MatchContext
from cs2bot.domain.predictions import MapSequenceProbability
from cs2bot.domain.records import VetoStep
from cs2bot.models.ensemble import EnsembleConfig, MapWinEnsemble, mirror_context
from tests.learning.ensemble_helpers import fitted_ensemble


def _ctx(match_id: int, t1: int, t2: int, when: object, **extra: object) -> MatchContext:
    return MatchContext(
        match_id=match_id,
        scheduled_at=when,
        format=MatchFormat.BO3,
        team1_id=t1,
        team2_id=t2,
        **extra,
    )


PAIRS = [(1, 2), (3, 9), (5, 6), (7, 8), (4, 12), (10, 11)]
MAPS = ("mirage", "inferno", "nuke", "ancient")


def test_ensemble_context_prediction_is_orientation_free() -> None:
    fx = fitted_ensemble()
    as_of = fx.report.data_end + timedelta(hours=3)
    kw = {"as_of": as_of, "store": fx.store, "rating_provider": fx.ratings}
    worst_raw_gap = 0.0
    for i, (a, b) in enumerate(PAIRS):
        ctx = _ctx(95_000 + i, a, b, as_of)
        for map_name in MAPS:
            for picker in (None, TeamSlot.TEAM1, TeamSlot.TEAM2):
                ab = fx.ensemble.predict(ctx, map_name, picked_by=picker, **kw)
                ba = fx.ensemble.predict(
                    mirror_context(ctx), map_name, picked_by=picker.other if picker else None, **kw
                )
                assert ab.p_team1_win + ba.p_team1_win == pytest.approx(1.0, abs=1e-12)
                assert ab.sigma == pytest.approx(ba.sigma, abs=1e-12)
                assert ab.components["orientation_gap"] == pytest.approx(ba.components["orientation_gap"])
                worst_raw_gap = max(worst_raw_gap, abs(ab.components["orientation_gap"]))
    # the raw model really is asymmetric (this is what the averaging removes)
    assert worst_raw_gap > 1e-4


def test_symmetric_prediction_can_be_switched_off_and_differs_from_the_raw_one() -> None:
    fx = fitted_ensemble()
    as_of = fx.report.data_end + timedelta(hours=3)
    raw = MapWinEnsemble.from_dict(
        {**fx.ensemble.to_dict(), "config": {**_config_dump(), "symmetric": False}}
    )
    ctx = _ctx(95_100, 7, 8, as_of)
    kw = {"as_of": as_of, "store": fx.store, "rating_provider": fx.ratings}
    sym = fx.ensemble.predict(ctx, "nuke", **kw)
    one_sided = raw.predict(ctx, "nuke", **kw)
    assert one_sided.p_team1_win == pytest.approx(sym.components["p_forward"])
    assert "orientation_gap" not in one_sided.components
    # half the disagreement between the two orientations is model uncertainty
    assert sym.components["sigma_model"] >= abs(sym.components["orientation_gap"]) / 2 - 1e-12


def _config_dump() -> dict[str, object]:
    return fitted_ensemble().ensemble.config.model_dump(mode="json")


def test_sequence_estimates_are_orientation_free() -> None:
    fx = fitted_ensemble()
    as_of = fx.report.data_end + timedelta(hours=3)
    ctx = _ctx(95_200, 3, 9, as_of)
    seqs = [
        MapSequenceProbability(
            maps=["mirage", "nuke", "ancient"],
            pickers=[TeamSlot.TEAM1, TeamSlot.TEAM2, None],
            probability=0.6,
        ),
        MapSequenceProbability(
            maps=["nuke", "mirage", "inferno"],
            pickers=[TeamSlot.TEAM1, TeamSlot.TEAM2, None],
            probability=0.4,
        ),
    ]
    mirrored_seqs = [
        s.model_copy(update={"pickers": [p.other if p else None for p in s.pickers]}) for s in seqs
    ]
    kw = {"as_of": as_of, "store": fx.store, "rating_provider": fx.ratings}
    ab = fx.ensemble.predict_for_sequences(ctx, seqs, **kw)
    ba = fx.ensemble.predict_for_sequences(mirror_context(ctx), mirrored_seqs, **kw)
    assert set(ab) == set(ba)
    for name in ab:
        assert ab[name].p_team1_win + ba[name].p_team1_win == pytest.approx(1.0, abs=1e-12)
        assert ab[name].sigma == pytest.approx(ba[name].sigma, abs=1e-12)


def test_mirror_context_swaps_every_oriented_field() -> None:
    when = fitted_ensemble().report.data_end
    ctx = _ctx(
        95_300,
        3,
        9,
        when,
        team1_name="A",
        team2_name="B",
        team1_lineup=[LineupPlayer(player_id=1)],
        team2_lineup=[LineupPlayer(player_id=2, is_standin=True)],
        known_veto=[
            VetoStep(step=1, action=VetoAction.BAN, team=TeamSlot.TEAM1, map_name="nuke"),
            VetoStep(step=7, action=VetoAction.LEFTOVER, team=None, map_name="mirage"),
        ],
        first_veto_team=TeamSlot.TEAM1,
        start_sides={"mirage": Side.CT, "nuke": None},
        context_flags=[
            ContextFlag(
                flag_type=ContextFlagType.STAND_IN,
                team=TeamSlot.TEAM2,
                severity=0.5,
                confidence=0.9,
                logit_shift=0.15,
                rationale="x",
            )
        ],
    )
    m = mirror_context(ctx)
    assert (m.team1_id, m.team2_id, m.team1_name, m.team2_name) == (9, 3, "B", "A")
    assert [p.player_id for p in m.team1_lineup] == [2] and m.team1_lineup[0].is_standin
    assert [s.team for s in m.known_veto] == [TeamSlot.TEAM2, None]
    assert m.first_veto_team is TeamSlot.TEAM2
    assert m.start_sides == {"mirage": Side.T, "nuke": None}
    assert m.context_flags[0].team is TeamSlot.TEAM1 and m.context_flags[0].logit_shift == -0.15
    assert mirror_context(m) == ctx
    assert EnsembleConfig().symmetric is True


def test_sequence_without_pickers_never_drops_a_map() -> None:
    """A short pickers list means "picker unknown"; the ensemble used to skip those maps (zip), so the
    match engine later failed with "no map estimate" (the rating model already handled it)."""
    fx = fitted_ensemble()
    as_of = fx.report.data_end + timedelta(hours=3)
    ctx = _ctx(95_400, 5, 6, as_of)
    seqs = [MapSequenceProbability(maps=["mirage", "nuke", "ancient"], pickers=[], probability=1.0)]
    out = fx.ensemble.predict_for_sequences(
        ctx, seqs, as_of=as_of, store=fx.store, rating_provider=fx.ratings
    )
    assert set(out) == {"mirage", "nuke", "ancient"}
    assert all(est.picked_by is None for est in out.values())
    ratings_out = fx.ratings.model.predict_for_sequences(5, 6, seqs, as_of)
    assert set(ratings_out) == set(out)
