"""``cs2bot up`` start/stop smoke test with fakes (file SQLite, FakeExecutor, no browser, no network)."""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

import cs2bot.db.models as M
from cs2bot.config import Settings
from cs2bot.db.base import Base
from cs2bot.db.session import make_engine, make_session_factory
from cs2bot.executor import FakeExecutor
from cs2bot.flows.supervisor import EXIT_NOT_READY, EXIT_OK, UpHandles, dashboard_url, run_up
from cs2bot.odds import StaticOddsSource
from cs2bot.services.dbops import alembic_config


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _settings(root: Path) -> Settings:
    s = Settings()
    s.database.url = f"sqlite:///{root / 'bot.db'}"
    s.data_dir = root / "data"
    s.models.artifacts_dir = root / "models"
    s.api.port = _free_port()  # the Host-header check expects the configured port
    s.api.serve_dashboard = False
    s.scheduler.tick_s = 0.05
    s.llm.enabled = False
    return s


def test_run_up_starts_everything_and_stops_cleanly(tmp_path: Path) -> None:
    from alembic import command

    settings = _settings(tmp_path)
    engine = make_engine(settings.database.url)
    Base.metadata.create_all(engine)
    command.stamp(alembic_config(settings.database.url), "head")
    factory = make_session_factory(engine)
    executors: list[FakeExecutor] = []
    seen: dict[str, Any] = {}
    lines: list[str] = []

    def make_executor(s: Settings, f: Any) -> FakeExecutor:
        ex = FakeExecutor()
        executors.append(ex)
        return ex

    def started(h: UpHandles) -> None:
        r = httpx.get(f"{h.url}api/ping", timeout=10)
        seen["ping"] = r.json()
        health = httpx.get(f"{h.url}api/health", timeout=10).json()
        seen["health"] = health
        # let the fast lane run once before stopping
        for _ in range(200):
            with factory() as s:
                if s.scalar(select(M.JobRun).where(M.JobRun.job_name == "settle_bets")) is not None:
                    break
            threading.Event().wait(0.05)
        h.stop.set()

    code = run_up(
        settings,
        session_factory=factory,
        executor_factory=make_executor,
        runtime_overrides={"odds_source_factory": lambda: StaticOddsSource([]), "hltv_enabled": False},
        on_started=started,
        echo=lines.append,
    )
    assert code == EXIT_OK, lines
    assert seen["ping"] == {"status": "ok"}
    assert "scheduler" in str(seen["health"])
    assert any("Dashboard: http://127.0.0.1:" in line for line in lines)
    assert executors and executors[0].closed
    with factory() as s:
        names = {r.job_name for r in s.scalars(select(M.JobRun))}
    assert {"odds_collect", "predict_upcoming", "bet_run", "settle_bets"} <= names
    engine.dispose()


def test_run_up_refuses_a_database_behind_the_migrations(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = make_engine(settings.database.url)
    Base.metadata.create_all(engine)  # tables but no alembic stamp
    lines: list[str] = []
    code = run_up(settings, session_factory=make_session_factory(engine), echo=lines.append)
    assert code == EXIT_NOT_READY and "cs2bot db upgrade" in lines[0]
    engine.dispose()


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "http://127.0.0.1:8000/"),
        ("0.0.0.0", "http://127.0.0.1:8000/"),
        ("::1", "http://[::1]:8000/"),
    ],
)
def test_dashboard_url(host: str, expected: str) -> None:
    s = Settings()
    s.api.host = host
    assert dashboard_url(s) == expected
