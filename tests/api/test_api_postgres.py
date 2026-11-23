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


def test_every_read_endpoint_on_postgres(pg_factory: sessionmaker[Session], tmp_path: Path) -> None:
    """Each GET endpoint (with its filters) runs on PostgreSQL: no SQLite-only SQL, no 5xx."""
    with pg_factory() as s:
        seed: Seed = seed_database(s, tmp_path)
        s.commit()
    status = [{"name": "odds_collect", "lane": "fast", "enabled": True, "next_due": NOW.isoformat()}]
    app = create_app(make_settings(tmp_path), pg_factory, scheduler_status_provider=lambda: status)
    app.state.cs2bot.clock = lambda: NOW
    urls = [
        "/api/health",
        "/api/matches?status=scheduled,finished&in_scope=true&linked=false&team_id=1&order=desc&limit=5",
        "/api/matches?from=2026-11-01&to=2026-12-01&offset=1",
        "/api/matches/101",
        "/api/matches/1",
        "/api/matches/101/odds-history",
        f"/api/matches/101/odds-history?selection_id={seed.selections['team1']}&since=2026-11-20T10:00:00Z",
        "/api/bets?status=won,dry_run&from=2026-11-01&match_id=101",
        "/api/bets?dry_run=true",
        f"/api/bets/{seed.bets['won']}",
        f"/api/bets/{seed.bets['open']}/screenshot",
        "/api/bankroll",
        "/api/bankroll?dry_run=true",
        "/api/performance/calibration?mode=dry_run&bins=5&from=2026-01-01",
        "/api/performance/markets?mode=all&to=2026-12-01",
        "/api/decisions?match_id=101&accepted=false&from=2026-11-01",
        "/api/control/state",
        "/api/control/kill-switch",
        "/api/control/config",
        "/api/control/betclic-login",
        "/api/review/links",
        "/api/review/links?status=rejected,auto&window_hours=48&max_candidates=3",
        "/api/jobs?name=settle_bets&status=failed",
        "/api/teams/1?window_days=365&limit=5",
    ]
    with TestClient(app, base_url=BASE_URL) as c:
        for url in urls:
            resp = c.get(url)
            assert resp.status_code == 200, f"{url}: {resp.status_code} {resp.text[:200]}"
        health = c.get("/api/health").json()
        assert health["scheduler"]["jobs"][0]["name"] == "odds_collect" and len(health["jobs"]) == 4
        paper = c.get("/api/bets", params={"dry_run": "true", "status": "won"}).json()
        assert [b["id"] for b in paper["items"]] == [seed.bets["paper_won"]]
        assert c.get("/api/matches/99999999999999999999").status_code == 422
        assert c.get("/api/matches/2147483647").status_code == 404
        # mutations: kill switch, pause/resume, reject (clears the mappings with a JSON null)
        on = c.post("/api/control/kill-switch", json={"on": True, "reason": "pg"}, headers=CSRF)
        assert on.status_code == 200 and on.json()["on"] is True
        assert c.post("/api/control/pause", headers=CSRF).json()["halted"] is True
        assert c.post("/api/control/resume", headers=CSRF).json()["paused"]["on"] is False
        rejected = c.post(
            f"/api/review/links/{seed.events['pending_swapped']}", json={"action": "reject"}, headers=CSRF
        )
        assert rejected.status_code == 200 and rejected.json()["markets_reset"] == 1
        bad = c.put(
            "/api/control/config",
            content='{"decision": {"min_ev": NaN}}',
            headers={**CSRF, "content-type": "application/json"},
        )
        assert bad.status_code == 422
