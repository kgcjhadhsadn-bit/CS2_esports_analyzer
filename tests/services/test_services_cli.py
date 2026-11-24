"""CLI smoke tests (typer CliRunner) against a SQLite file database; no network, no browser."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cs2bot.cli import app
from cs2bot.config import reset_settings_cache
from cs2bot.db.base import Base
from cs2bot.db.session import make_engine, reset_engine_cache
from cs2bot.services import doctor
from cs2bot.services.dbops import alembic_config

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    url = f"sqlite:///{tmp_path / 'bot.db'}"
    monkeypatch.setenv("CS2BOT_DATABASE__URL", url)
    monkeypatch.setenv("CS2BOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CS2BOT_MODELS__ARTIFACTS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("CS2BOT_MODELS__N_MAP_SIMULATIONS", "300")
    monkeypatch.setenv("CS2BOT_LOG_LEVEL", "WARNING")
    monkeypatch.setattr(doctor, "default_chromium_path", lambda: None)
    reset_settings_cache()
    reset_engine_cache()
    yield tmp_path
    reset_settings_cache()
    reset_engine_cache()


def _schema(url: str) -> None:
    """Tables via metadata (the SQLite test DB) + Alembic stamp, like a migrated PostgreSQL database."""
    from alembic import command

    engine = make_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    command.stamp(alembic_config(url), "head")


def invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.exit_code, result.output


def test_help_lists_every_documented_group() -> None:
    code, out = invoke("--help")
    assert code == 0
    for name in (
        "up",
        "doctor",
        "db",
        "ingest",
        "demos",
        "odds",
        "models",
        "predict",
        "bet",
        "settle",
        "backtest",
    ):
        assert name in out
    for group, commands in {
        "ingest": ["upcoming", "results", "backfill", "rankings", "news", "match", "hltv-browser"],
        "odds": ["collect", "unmapped"],
        "models": ["train", "tune"],
        "predict": ["upcoming", "match"],
        "executor": ["login", "health", "balance"],
        "control": ["kill-switch", "pause", "resume"],
        "db": ["upgrade", "seed-synthetic"],
        "demos": ["process"],
        "bet": ["run"],
    }.items():
        code, out = invoke(group, "--help")
        assert code == 0
        for command in commands:
            assert command in out, (group, command)


def test_db_upgrade_status_and_controls(env: Path) -> None:
    code, out = invoke("db", "status")
    assert code == 1 and "cs2bot db upgrade" in out
    assert invoke("db", "upgrade")[0] == 0
    assert invoke("db", "upgrade")[0] == 0  # idempotent
    code, out = invoke("db", "status")
    assert code == 0 and "at head" in out

    assert invoke("control", "kill-switch", "on", "--reason", "test")[0] == 0
    code, out = invoke("control", "status")
    assert "kill switch: ON test" in out
    assert invoke("control", "kill-switch", "off")[0] == 0
    assert invoke("control", "pause", "--reason", "holiday")[0] == 0
    code, out = invoke("control", "resume")
    assert code == 0 and "holiday" in out
    code, out = invoke("control", "kill-switch", "maybe")
    assert code == 1 and "on" in out


def test_missing_credentials_and_model_give_clear_errors(env: Path) -> None:
    _schema(os.environ["CS2BOT_DATABASE__URL"])
    code, out = invoke("ingest", "upcoming", "--source", "pandascore")
    assert code == 1 and "CS2BOT_PANDASCORE__API_TOKEN" in out
    code, out = invoke("predict", "upcoming")
    assert code == 1 and "models train" in out
    code, out = invoke("models", "train")
    assert code == 1 and "no in-scope map results" in out
    code, out = invoke("odds", "unmapped")
    assert code == 0 and "no unmapped markets" in out


def test_seed_train_predict_bet_settle_backtest(env: Path) -> None:
    _schema(os.environ["CS2BOT_DATABASE__URL"])
    code, out = invoke("db", "seed-synthetic", "--n-teams", "6", "--n-matches", "80")
    assert code == 0, out
    code, out = invoke("db", "seed-synthetic")
    assert code == 1 and "empty database" in out
    code, out = invoke("models", "train", "--no-ensemble")
    assert code == 0 and "trained m" in out, out
    code, out = invoke("models", "list")
    assert code == 0 and str(env / "models") in out
    code, out = invoke("predict", "upcoming")
    assert code == 0 and "predictions:" in out
    code, out = invoke("predict", "match", "1")
    assert code == 0 and "P(team1 wins)=" in out, out
    code, out = invoke("bet", "run", "--no-executor")
    assert code == 0 and "dry run" in out
    code, out = invoke("settle", "--no-executor")
    assert code == 0 and "settlement:" in out
    code, out = invoke("models", "tune", "--grid", '{"tau": [0.4, 0.6]}')
    assert code == 0 and "log-loss" in out, out
    code, out = invoke("backtest", "--from", "2099-01-01", "--to", "2099-01-02", "--sims", "200")
    assert code == 0, out
    code, out = invoke("backtest", "--from", "2099-01-02", "--to", "2099-01-01")
    assert code == 1 and "--to must be after" in out


def test_doctor_reports_problems_with_a_non_zero_exit(env: Path) -> None:
    code, out = invoke("doctor")
    assert code == 1
    assert "[FAIL] migrations" in out and "[FAIL] pandascore token" in out and "[FAIL] chromium" in out
    assert "anthropic key" in out and "disk space" in out and "rar tool" in out
    _schema(os.environ["CS2BOT_DATABASE__URL"])
    code, out = invoke("doctor")
    assert "[  OK] migrations" in out and "betting mode" in out and "DRY RUN" in out
