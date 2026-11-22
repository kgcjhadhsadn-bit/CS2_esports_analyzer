"""The main API queries against PostgreSQL (runs only when CS2BOT_TEST_DATABASE_URL is set)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.api import create_app
from cs2bot.db.base import Base
from cs2bot.db.session import make_engine
from tests.api.api_seed import BASE_URL, CSRF, NOW, Seed, make_settings, seed_database

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_factory() -> Iterator[sessionmaker[Session]]:
    url = os.environ.get("CS2BOT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("CS2BOT_TEST_DATABASE_URL not set")
    engine = make_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_endpoints_on_postgres(pg_factory: sessionmaker[Session], tmp_path: Path) -> None:
    with pg_factory() as s:
        seed: Seed = seed_database(s, tmp_path)
        s.commit()
    app = create_app(make_settings(tmp_path), pg_factory)
    app.state.cs2bot.clock = lambda: NOW
    with TestClient(app, base_url=BASE_URL) as c:
        assert c.get("/api/health").json()["db"]["ok"] is True
        listed = c.get("/api/matches", params={"status": "scheduled", "linked": "true"}).json()
        assert [m["id"] for m in listed["items"]] == [101]
        detail = c.get("/api/matches/101").json()
        assert detail["match"]["best_ev"] == pytest.approx(0.3728, abs=1e-6) and detail["markets"]
        assert c.get("/api/bankroll").json()["balance"] == "968.44"
        assert c.get("/api/performance/calibration").json()["predictions"]["n"] == 20
        assert c.get("/api/bets", params={"status": "won,lost", "dry_run": "false"}).json()["total"] == 2
        assert c.get("/api/teams/1").status_code == 200
        assert c.get("/api/decisions", params={"accepted": "true"}).json()["total"] == 3
        put = c.put("/api/control/config", json={"decision": {"max_odds": "9.5"}}, headers=CSRF)
        assert put.status_code == 200 and put.json()["effective"]["decision"]["max_odds"] == "9.5"
        review = c.post(
            f"/api/review/links/{seed.events['pending_swapped']}",
            json={"action": "confirm", "teams_swapped": True},
            headers=CSRF,
        ).json()
        assert review["markets_reset"] == 1
