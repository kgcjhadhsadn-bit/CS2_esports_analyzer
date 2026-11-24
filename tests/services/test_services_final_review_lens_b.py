"""Final review (Lens B: data and model correctness) regressions across package boundaries.

* the walk-forward backtest predicts a match exactly like the live prediction service (same models,
  same rules of the game - it used the built-in overtime money even when the owner configured another);
* stored Claude context flags never move the probability of a pairing they were not written for (a
  source replaced a participant; ``logit_shift`` is stored from the old team1's perspective).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

import cs2bot.db.models as M
from cs2bot import datasets
from cs2bot.backtest.pipeline import PipelineConfig, PipelineTrainer
from cs2bot.backtest.types import MatchQuery, TrainingSet
from cs2bot.domain.enums import ContextFlagType, MatchFormat, Tier
from cs2bot.ingest.pandascore.entities import ChangeTracker
from cs2bot.ingest.pandascore.sync import _replace_teams
from cs2bot.llm import MANUAL_FLAG_MODEL, combine_flags, deactivate_flags_after_team_change, load_active_flags
from cs2bot.services.prediction import predict_match
from tests.services.conftest import NOW, World


def _training_set(session: Any, cutoff: Any) -> TrainingSet:
    return TrainingSet(
        cutoff=cutoff,
        map_results=tuple(datasets.load_map_results(session, until=cutoff)),
        vetoes=tuple(datasets.load_veto_records(session, until=cutoff)),
        rounds=tuple(datasets.load_round_records(session, until=cutoff)),
        player_maps=tuple(datasets.load_player_map_records(session, until=cutoff)),
    )


def test_backtest_predicts_like_the_live_service(world: World) -> None:
    settings = world.settings
    settings.models.ot_start_money = 12_500  # an organiser's overtime money, configured by the owner
    match_id = world.match_ids[0]
    with world.factory() as session:
        live = predict_match(session, match_id, world.bundle, settings, now=world.now, persist=False)
        training = _training_set(session, world.now)
        ctx = datasets.load_match_context(
            session, match_id, settings=settings, now=world.now, include_context_flags=False
        )
        match = session.get(M.Match, match_id)
        assert match is not None and match.team1_id is not None and match.team2_id is not None
        query = MatchQuery(
            match_id=match_id,
            start_time=match.scheduled_at,
            format=MatchFormat.BO3,
            team1_id=match.team1_id,
            team2_id=match.team2_id,
            as_of=world.now,
            context=ctx,
            tier=Tier.S,
        )
    assert live is not None
    trainer = PipelineTrainer(
        PipelineConfig(
            n_sims=settings.models.n_map_simulations,
            scenario_n_sims=settings.models.scenario_n_simulations,
            seed=int(settings.models.random_seed or 0),
        ),
        settings=settings,
    )
    backtest = trainer(training).predict(query)

    assert backtest.central.p_team1_win == pytest.approx(live.central.p_team1_win, abs=1e-12)
    assert backtest.sigma_team1_win == pytest.approx(live.sigma_team1_win, abs=1e-12)
    assert backtest.central.map_score_pmf == pytest.approx(live.central.map_score_pmf, abs=1e-12)
    for b_slot, l_slot in zip(backtest.central.slots, live.central.slots, strict=True):
        assert b_slot.p_team1_win == pytest.approx(l_slot.p_team1_win, abs=1e-12)
        # round-level distributions depend on the economy (overtime money): same rules in both paths
        assert b_slot.score_pmf == pytest.approx(l_slot.score_pmf, abs=1e-12)
        assert b_slot.p_overtime == pytest.approx(l_slot.p_overtime, abs=1e-12)
    assert backtest.components["economy"] == live.components["economy"]


def _flag(
    match_id: int, team_id: int | None, shift: float, *, veto: bool = False, model: str = "claude"
) -> Any:
    return M.ContextFlagRow(
        match_id=match_id,
        team_id=team_id,
        flag_type=ContextFlagType.STAND_IN,
        severity=0.6,
        confidence=0.9,
        logit_shift=shift,
        veto=veto,
        rationale="stand-in announced",
        evidence=["news:1"],
        model=model,
        active=True,
        created_at=NOW,
    )


def test_flag_about_a_team_that_left_the_match_does_not_shift_the_probability(world: World) -> None:
    match_id = world.match_ids[0]
    with world.factory() as session:
        match = session.get(M.Match, match_id)
        assert match is not None
        old_team2 = match.team2_id
        session.add(_flag(match_id, old_team2, 0.2))  # "team2 plays with a stand-in" (old pairing)
        session.add(_flag(match_id, old_team2, 0.0, veto=True))
        match.team2_id = 7  # a source replaced the participant
        session.commit()
        flags = load_active_flags(session, match_id, now=world.now)
    assert len(flags) == 2
    assert all(f.team is None and f.logit_shift == 0.0 for f in flags)
    shift, veto = combine_flags(flags, world.settings.llm, now=world.now)
    assert shift == 0.0
    assert veto  # blocking is always safe: the veto bit survives


def test_pandascore_participant_replacement_switches_off_the_old_flags(world: World) -> None:
    """Old pairing A-B, new pairing C-A (PandaScore's order): A moves to team2, so a stored shift written
    from A's team1 perspective would now push the probability the wrong way."""
    match_id = world.match_ids[0]
    with world.factory() as session:
        match = session.get(M.Match, match_id)
        assert match is not None and match.team1_id is not None
        team_a = session.get(M.Team, match.team1_id)
        team_c = session.get(M.Team, 7)
        assert team_a is not None and team_c is not None
        session.add(_flag(match_id, team_a.id, -0.2))  # A (team1) weakened
        session.add(_flag(match_id, team_a.id, -0.15, veto=True))  # "do not bet": stays, without shift
        session.add(_flag(match_id, team_a.id, -0.1, model=MANUAL_FLAG_MODEL))  # the owner's own flag
        session.commit()
        _replace_teams(session, match, team_c, team_a, ChangeTracker())
        session.commit()
        assert (match.team1_id, match.team2_id) == (team_c.id, team_a.id)
        rows = session.scalars(
            select(M.ContextFlagRow).where(M.ContextFlagRow.match_id == match_id, M.ContextFlagRow.active)
        ).all()
        flags = load_active_flags(session, match_id, now=world.now)
    assert sorted((r.model, r.veto, r.logit_shift) for r in rows) == [
        ("claude", True, 0.0),
        (MANUAL_FLAG_MODEL, False, -0.1),
    ]
    assert len(flags) == 2
    veto_flag = next(f for f in flags if f.veto)
    assert veto_flag.team is not None and veto_flag.team.value == "team2" and veto_flag.logit_shift == 0.0


def test_deactivate_flags_after_team_change_is_idempotent(world: World) -> None:
    match_id = world.match_ids[1]
    with world.factory() as session:
        session.add(_flag(match_id, None, 0.1))
        session.commit()
        assert deactivate_flags_after_team_change(session, match_id) == 1
        assert deactivate_flags_after_team_change(session, match_id) == 0
        session.commit()
        assert load_active_flags(session, match_id, now=world.now) == []


def test_backtest_contexts_carry_no_post_match_start_sides(world: World) -> None:
    """Starting sides exist only for maps that were played: the live bot never has them before a match,
    and in a backtest they would reveal which maps were played (e.g. that a decider happened)."""
    from cs2bot.backtest.db import load_backtest_matches

    with world.factory() as session:
        matches = load_backtest_matches(
            session, settings=world.settings, since=world.now - timedelta(days=60), until=world.now
        )
        with_sides = session.scalar(
            select(func.count()).select_from(M.MapPlayed).where(M.MapPlayed.team1_start_side.is_not(None))
        )
    assert matches and with_sides
    assert all(m.context is not None and m.context.start_sides == {} for m in matches)
    assert any(m.context is not None and m.context.known_veto for m in matches)  # the veto stays


def test_cli_backtest_uses_the_live_model_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cs2bot backtest` used to run without the ensemble and with the default rating configuration,
    i.e. it measured a different model than the one that bets."""
    from typer.testing import CliRunner

    import cs2bot.backtest as backtest_pkg
    from cs2bot.backtest.report import BacktestReport
    from cs2bot.cli import app
    from cs2bot.config import Settings, reset_settings_cache
    from cs2bot.db.base import Base
    from cs2bot.db.session import make_engine, reset_engine_cache
    from cs2bot.models.ratings import RatingConfig
    from cs2bot.services.registry import save_rating_config

    url = f"sqlite:///{tmp_path / 'bot.db'}"
    monkeypatch.setenv("CS2BOT_DATABASE__URL", url)
    monkeypatch.setenv("CS2BOT_MODELS__ARTIFACTS_DIR", str(tmp_path / "models"))
    reset_settings_cache()
    reset_engine_cache()
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    settings = Settings()
    tuned = RatingConfig().model_copy(update={"initial_phi": RatingConfig().initial_phi * 1.1})
    save_rating_config(settings, tuned, {"note": "test"})
    seen: dict[str, Any] = {}

    def fake_run_db_backtest(session: Any, **kwargs: Any) -> BacktestReport:
        seen.update(kwargs)
        return BacktestReport(created_at=NOW)

    monkeypatch.setattr(backtest_pkg, "run_db_backtest", fake_run_db_backtest)
    try:
        result = CliRunner().invoke(app, ["backtest", "--from", "2026-01-01", "--to", "2026-02-01"])
    finally:
        reset_settings_cache()
        reset_engine_cache()
    assert result.exit_code == 0, result.output
    cfg = seen["pipeline_config"]
    assert cfg.use_ensemble is True
    assert cfg.rating_config == tuned
    assert "tuned" in result.output


def test_doctor_warns_when_the_active_probabilities_are_not_calibrated(world: World) -> None:
    """The decision rule assumes calibrated probabilities; a bundle without a working ensemble (the
    fixture's bundle is ratings-only) must not be reported as simply "ok"."""
    import json

    from cs2bot.services.doctor import check_models

    check = check_models(world.settings)
    assert check.level == "warn" and "NOT calibrated" in check.message

    assert world.bundle.path is not None
    manifest_path = world.bundle.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["metrics"]["ensemble"] = {"fallback": True, "fallback_reason": "lightgbm failed"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    check = check_models(world.settings)
    assert check.level == "warn" and "lightgbm failed" in check.message

    manifest["metrics"]["ensemble"] = {"fallback": False, "calibrator": "isotonic"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    check = check_models(world.settings)
    assert check.level == "ok"
