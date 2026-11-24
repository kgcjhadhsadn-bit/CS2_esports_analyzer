"""Safety controls (kill switch, pause/resume, config overrides, Betclic login) and the link review queue."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.db import state
from cs2bot.db.models import AuditLog, BookmakerEvent, BookmakerMarket, BookmakerSelection
from cs2bot.domain.enums import LinkStatus
from tests.api.api_seed import BASE_URL, CSRF, Seed


def _audit(session_factory: sessionmaker[Session], action: str | None = None) -> list[AuditLog]:
    with session_factory() as s:
        stmt = select(AuditLog).order_by(AuditLog.id)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        return list(s.scalars(stmt))


# ------------------------------------------------------------------ kill switch / pause


def test_kill_switch_roundtrip_persists_and_audits(client: TestClient, session_factory) -> None:
    assert client.get("/api/control/kill-switch").json()["on"] is False
    resp = client.post("/api/control/kill-switch", json={"on": True, "reason": "  odd odds  "}, headers=CSRF)
    assert resp.status_code == 200
    body = resp.json()
    assert body["on"] is True and body["reason"] == "odd odds" and body["updated_by"] == "dashboard"
    assert body["updated_at"] is not None
    with session_factory() as s:
        assert state.is_kill_switch_on(s) is True
        assert state.is_trading_halted(s) == (True, "kill switch is on")
    (row,) = _audit(session_factory, "kill_switch_on")
    assert row.actor == "dashboard" and row.details == {"reason": "odd odds"}
    assert client.get("/api/control/state").json()["halted"] is True
    off = client.post("/api/control/kill-switch", json={"on": False}, headers=CSRF).json()
    assert off["on"] is False and off["reason"] is None
    assert len(_audit(session_factory, "kill_switch_off")) == 1
    assert client.post("/api/control/kill-switch", json={"on": "maybe"}, headers=CSRF).status_code == 422
    assert client.post("/api/control/kill-switch", json={"on": True, "x": 1}, headers=CSRF).status_code == 422


def test_mutations_require_csrf_header(client: TestClient, session_factory) -> None:
    for headers in ({}, {"X-Requested-With": "XMLHttpRequest"}):
        resp = client.post("/api/control/kill-switch", json={"on": True}, headers=headers)
        assert resp.status_code == 403 and "CSRF" in resp.json()["detail"]
    for method, path, payload in (
        ("post", "/api/control/pause", {}),
        ("post", "/api/control/resume", {}),
        ("put", "/api/control/config", {"decision": {"min_ev": 0.05}}),
        ("post", "/api/control/betclic-login", None),
        ("post", "/api/review/links/1", {"action": "reject"}),
        ("post", "/api/auth/logout", None),
        ("delete", "/api/control/kill-switch", None),
        ("patch", "/api/control/config", {}),
    ):
        assert client.request(method, path, json=payload).status_code == 403, path
    with session_factory() as s:
        assert state.is_trading_halted(s) == (False, None)
    assert _audit(session_factory) == []
    evil = client.post(
        "/api/control/kill-switch", json={"on": True}, headers={**CSRF, "Origin": "https://evil.example"}
    )
    assert evil.status_code == 403 and "origin" in evil.json()["detail"]
    ok = client.post(
        "/api/control/kill-switch", json={"on": True}, headers={**CSRF, "Origin": "http://localhost:5173"}
    )
    assert ok.status_code == 200
    same_origin = client.post(
        "/api/control/kill-switch", json={"on": False}, headers={**CSRF, "Origin": BASE_URL}
    )
    assert same_origin.status_code == 200


def test_pause_and_resume(client: TestClient, session_factory) -> None:
    paused = client.post("/api/control/pause", json={"reason": "taking a break"}, headers=CSRF).json()
    assert paused["halted"] is True and paused["reason"] == "paused: taking a break"
    assert paused["paused"]["on"] is True and paused["kill_switch"]["on"] is False
    with session_factory() as s:
        assert state.get_pause(s) == (True, "taking a break")
    default_reason = client.post("/api/control/pause", headers=CSRF).json()
    assert default_reason["paused"]["reason"] == "paused from the dashboard"
    resumed = client.post("/api/control/resume", headers=CSRF).json()
    assert resumed["halted"] is False and resumed["paused"]["on"] is False
    assert [a.action for a in _audit(session_factory)] == ["pause", "pause", "resume"]


def test_resume_does_not_clear_kill_switch(client: TestClient) -> None:
    client.post("/api/control/kill-switch", json={"on": True, "reason": "x"}, headers=CSRF)
    body = client.post("/api/control/resume", json={"reason": "go"}, headers=CSRF).json()
    assert body["halted"] is True and body["kill_switch"]["on"] is True


# ------------------------------------------------------------------ config


def test_config_view_defaults(client: TestClient) -> None:
    body = client.get("/api/control/config").json()
    assert body["effective"] == body["defaults"]
    assert body["overrides"] == {"decision": {}, "bankroll": {}} and body["updated_at"] is None
    assert body["effective"]["decision"]["min_probability"] == 0.75
    assert body["effective"]["bankroll"]["starting_bankroll"] == "1000.00"


def test_config_put_overrides(client: TestClient, session_factory) -> None:
    resp = client.put(
        "/api/control/config",
        json={
            "decision": {"min_ev": 0.05, "min_probability": 0.75, "disabled_markets": ["player_kills"]},
            "bankroll": {"kelly_fraction": 0.2, "max_bets_per_day": 4, "stake_step": "0.50"},
        },
        headers=CSRF,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # min_probability equals the default, so it is not stored as an override
    assert body["overrides"]["decision"] == {"min_ev": 0.05, "disabled_markets": ["player_kills"]}
    assert body["overrides"]["bankroll"] == {
        "kelly_fraction": 0.2,
        "max_bets_per_day": 4,
        "stake_step": "0.50",
    }
    assert body["effective"]["decision"]["min_ev"] == 0.05
    assert body["defaults"]["decision"]["min_ev"] == -1.0  # odds ignored by default
    assert body["effective"]["bankroll"]["stake_step"] == "0.50" and body["updated_by"] == "dashboard"
    with session_factory() as s:
        from cs2bot.config import Settings

        assert state.effective_decision_settings(s, Settings()).min_ev == 0.05
    assert len(_audit(session_factory, "config_overrides")) == 1

    # omitted section keeps its overrides; {} clears a section
    kept = client.put("/api/control/config", json={"decision": {"max_sigma": 0.04}}, headers=CSRF).json()
    assert kept["overrides"]["decision"] == {"max_sigma": 0.04}
    assert kept["overrides"]["bankroll"]["kelly_fraction"] == 0.2
    cleared = client.put("/api/control/config", json={"bankroll": {}}, headers=CSRF).json()
    assert cleared["overrides"] == {"decision": {"max_sigma": 0.04}, "bankroll": {}}
    assert cleared["effective"]["bankroll"]["kelly_fraction"] == 0.25
    market = client.put(
        "/api/control/config",
        json={"decision": {"market_overrides": {"map_winner": {"min_probability": 0.8}}}},
        headers=CSRF,
    ).json()
    assert market["effective"]["decision"]["market_overrides"]["map_winner"]["min_probability"] == 0.8


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"decision": {"min_evv": 0.05}}, "min_evv"),
        ({"decision": {"min_ev": "lots"}}, "min_ev"),
        ({"decision": {"min_ev": -1.5}}, "min_ev"),
        ({"decision": {"min_probability": 1.0}}, "min_probability"),
        ({"decision": {"max_odds": "1.005"}}, "max_odds"),
        ({"decision": {"min_odds": "0.9"}}, "min_odds"),
        ({"decision": {"disabled_markets": ["darts"]}}, "disabled_markets"),
        ({"decision": {"market_overrides": {"map_winner": {"min_ev": 3}}}}, "market_overrides"),
        ({"decision": {"market_overrides": {"nope": {"min_ev": 0.1}}}}, "market_overrides"),
        ({"bankroll": {"kelly_fraction": 1.5}}, "kelly_fraction"),
        ({"bankroll": {"kelly_fraction": 0}}, "kelly_fraction"),
        ({"bankroll": {"daily_loss_limit_fraction": 2}}, "daily_loss_limit_fraction"),
        ({"bankroll": {"currency": "pln"}}, "currency"),
        ({"bankroll": {"stake_step": "0"}}, "stake_step"),
        ({"bankroll": {"max_bets_per_day": -1}}, "max_bets_per_day"),
        ({"executor": {"dry_run": False}}, "executor"),
    ],
)
def test_config_put_validation(
    client: TestClient, session_factory, payload: dict[str, Any], field: str
) -> None:
    before = client.get("/api/control/config").json()
    resp = client.put("/api/control/config", json=payload, headers=CSRF)
    assert resp.status_code == 422, resp.text
    assert field in str(resp.json()["detail"])
    assert client.get("/api/control/config").json() == before
    assert _audit(session_factory, "config_overrides") == []


# ------------------------------------------------------------------ Betclic login window


def test_betclic_login_not_configured(client: TestClient) -> None:
    resp = client.post("/api/control/betclic-login", headers=CSRF)
    assert resp.status_code == 501
    assert client.get("/api/control/betclic-login").json()["configured"] is False


def test_betclic_login_launches_in_background(app_factory, seeded: Seed, session_factory) -> None:
    release = threading.Event()
    called = threading.Event()

    def launcher() -> None:
        called.set()
        release.wait(5)

    app = app_factory(login_launcher=launcher)
    with TestClient(app, base_url=BASE_URL) as c:
        resp = c.post("/api/control/betclic-login", headers=CSRF)
        assert resp.status_code == 202 and resp.json()["started"] is True
        assert called.wait(5)
        status = c.get("/api/control/betclic-login").json()
        assert status["configured"] is True and status["running"] is True
        assert c.post("/api/control/betclic-login", headers=CSRF).status_code == 409
        release.set()
        app.state.cs2bot.login_runner.join(5)
        done = c.get("/api/control/betclic-login").json()
        assert (
            done["running"] is False and done["last_finished_at"] is not None and done["last_error"] is None
        )
    assert len(_audit(session_factory, "betclic_login_requested")) == 1


def test_betclic_login_reports_launcher_result(app_factory, seeded: Seed) -> None:
    app = app_factory(login_launcher=lambda: False)  # e.g. open_login_window() timed out
    with TestClient(app, base_url=BASE_URL) as c:
        assert c.post("/api/control/betclic-login", headers=CSRF).status_code == 202
        app.state.cs2bot.login_runner.join(5)
        status = c.get("/api/control/betclic-login").json()
    assert status["last_result"] is False and status["last_error"] is None


def test_betclic_login_failure_is_reported(app_factory, seeded: Seed) -> None:
    def launcher() -> None:
        raise RuntimeError("Chromium not installed")

    app = app_factory(login_launcher=launcher)
    with TestClient(app, base_url=BASE_URL) as c:
        assert c.post("/api/control/betclic-login", headers=CSRF).status_code == 202
        app.state.cs2bot.login_runner.join(5)
        status = c.get("/api/control/betclic-login").json()
    assert status["last_error"] == "RuntimeError: Chromium not installed" and status["running"] is False


# ------------------------------------------------------------------ review queue


def _event(session_factory: sessionmaker[Session], event_id: int) -> BookmakerEvent:
    with session_factory() as s:
        ev = s.get(BookmakerEvent, event_id)
        assert ev is not None
        s.expunge(ev)
        return ev


def _mapped(session_factory: sessionmaker[Session], event_id: int) -> tuple[int, int]:
    with session_factory() as s:
        markets = list(s.scalars(select(BookmakerMarket).where(BookmakerMarket.event_id == event_id)))
        sels = list(
            s.scalars(
                select(BookmakerSelection).where(BookmakerSelection.market_id.in_([m.id for m in markets]))
            )
        )
        return sum(m.is_mapped for m in markets), sum(x.is_mapped for x in sels)


def test_review_queue_candidates(client: TestClient, seeded: Seed) -> None:
    body = client.get("/api/review/links").json()
    assert body["total"] == 2
    items = {i["event"]["id"]: i for i in body["items"]}
    swapped = items[seeded.events["pending_swapped"]]
    assert swapped["n_markets"] == 1 and swapped["current_match"]["id"] == 103
    best = swapped["candidates"][0]
    assert best["match"]["id"] == 103 and best["is_current"] is True and best["teams_swapped"] is True
    assert best["name_score"] == 100.0 and best["time_diff_minutes"] == 15.0
    assert best["score"] == pytest.approx(0.8 + 0.2 * (1 - 15 / (24 * 60)), abs=1e-4)
    unlinked = items[seeded.events["pending_unlinked"]]
    assert unlinked["current_match"] is None
    top = unlinked["candidates"][0]
    assert top["match"]["id"] == 102 and top["teams_swapped"] is False and top["is_current"] is False
    assert len(unlinked["candidates"]) <= 5
    narrow = client.get("/api/review/links", params={"window_hours": 0.1, "max_candidates": 1}).json()
    assert all(len(i["candidates"]) <= 1 for i in narrow["items"])
    rejected = client.get("/api/review/links", params={"status": "rejected"}).json()
    assert [i["event"]["id"] for i in rejected["items"]] == [seeded.events["rejected"]]


def test_review_confirm_with_swap_resets_mappings(client: TestClient, seeded: Seed, session_factory) -> None:
    event_id = seeded.events["pending_swapped"]
    assert _mapped(session_factory, event_id) == (1, 2)
    resp = client.post(
        f"/api/review/links/{event_id}", json={"action": "confirm", "teams_swapped": True}, headers=CSRF
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["event"]["link_status"] == "confirmed" and body["event"]["teams_swapped"] is True
    assert body["event"]["match_id"] == 103 and body["event"]["link_confidence"] == 1.0
    assert body["previous_status"] == "pending_review" and body["previous_teams_swapped"] is False
    assert body["markets_reset"] == 1 and body["open_bets"] == 0
    assert _mapped(session_factory, event_id) == (0, 0)  # stale orientation can never be priced
    ev = _event(session_factory, event_id)
    assert ev.extra["remap_required"] is True and ev.extra["manual_review"]["action"] == "confirm"
    (row,) = _audit(session_factory, "link_review")
    assert row.details["event_id"] == event_id and row.details["teams_swapped"] is True
    assert client.get("/api/review/links").json()["total"] == 1
    # now linked & confirmed: match 103 counts as linked
    assert [m["id"] for m in client.get("/api/matches", params={"linked": "true"}).json()["items"]] == [
        103,
        101,
    ]


def test_review_confirm_as_is_keeps_mappings(client: TestClient, seeded: Seed, session_factory) -> None:
    event_id = seeded.events["pending_swapped"]
    body = client.post(f"/api/review/links/{event_id}", json={"action": "confirm"}, headers=CSRF).json()
    assert body["event"]["teams_swapped"] is False and body["markets_reset"] == 0
    assert _mapped(session_factory, event_id) == (1, 2)
    assert "remap_required" not in _event(session_factory, event_id).extra


def test_review_reject(client: TestClient, seeded: Seed, session_factory) -> None:
    event_id = seeded.events["pending_swapped"]
    body = client.post(f"/api/review/links/{event_id}", json={"action": "reject"}, headers=CSRF).json()
    assert body["event"]["link_status"] == "rejected" and body["event"]["match_id"] is None
    assert body["previous_match_id"] == 103 and body["markets_reset"] == 1
    with session_factory() as s:
        ev = s.get(BookmakerEvent, event_id)
        assert ev is not None and ev.link_status is LinkStatus.REJECTED
    bad = client.post(f"/api/review/links/{event_id}", json={"action": "reject", "match_id": 1}, headers=CSRF)
    assert bad.status_code == 422


def test_review_relink(client: TestClient, seeded: Seed) -> None:
    event_id = seeded.events["pending_unlinked"]
    missing = client.post(f"/api/review/links/{event_id}", json={"action": "relink"}, headers=CSRF)
    assert missing.status_code == 422
    assert (
        client.post(f"/api/review/links/{event_id}", json={"action": "confirm"}, headers=CSRF).status_code
        == 422
    )
    unknown = client.post(
        f"/api/review/links/{event_id}", json={"action": "relink", "match_id": 9999}, headers=CSRF
    )
    assert unknown.status_code == 404
    body = client.post(
        f"/api/review/links/{event_id}", json={"action": "relink", "match_id": 102}, headers=CSRF
    ).json()
    assert body["event"]["match_id"] == 102 and body["event"]["link_status"] == "confirmed"
    assert body["event"]["teams_swapped"] is False and body["previous_match_id"] is None
    flipped = client.post(
        f"/api/review/links/{event_id}",
        json={"action": "relink", "match_id": 102, "teams_swapped": True},
        headers=CSRF,
    ).json()
    assert flipped["event"]["teams_swapped"] is True and flipped["previous_teams_swapped"] is False
    assert client.post("/api/review/links/99999", json={"action": "reject"}, headers=CSRF).status_code == 404
    assert (
        client.post(f"/api/review/links/{event_id}", json={"action": "approve"}, headers=CSRF).status_code
        == 422
    )


def test_review_reports_open_bets(client: TestClient, seeded: Seed) -> None:
    body = client.post(
        f"/api/review/links/{seeded.events['main']}",
        json={"action": "confirm", "teams_swapped": True},
        headers=CSRF,
    ).json()
    assert body["open_bets"] == 1 and body["markets_reset"] == 3


def test_review_runs_injected_remapper(app_factory, seeded: Seed, session_factory) -> None:
    calls: list[tuple[int, bool]] = []

    def remapper(session: Session, event: BookmakerEvent) -> int:
        # stand-in for cs2bot.odds.collector.remap_event: re-key the selections for the new orientation
        calls.append((event.id, bool(event.teams_swapped)))
        for market in event.markets:
            market.is_mapped = True
            market.spec_key = "match_winner"
            for sel in market.selections:
                sel.selection_key = "team2" if sel.raw_name == "Synth Team 6" else "team1"
                sel.is_mapped = True
        return 2

    event_id = seeded.events["pending_swapped"]
    with TestClient(app_factory(event_remapper=remapper), base_url=BASE_URL) as c:
        body = c.post(
            f"/api/review/links/{event_id}", json={"action": "confirm", "teams_swapped": True}, headers=CSRF
        ).json()
        assert body["markets_reset"] == 1 and body["remapped"] is True
        assert calls == [(event_id, True)]
        assert _mapped(session_factory, event_id) == (1, 2)
        assert "remap_required" not in _event(session_factory, event_id).extra
        # no orientation change -> no remap
        again = c.post(f"/api/review/links/{event_id}", json={"action": "confirm"}, headers=CSRF).json()
        assert again["remapped"] is None and len(calls) == 1


def test_review_remapper_failure_keeps_mappings_cleared(app_factory, seeded: Seed, session_factory) -> None:
    def remapper(session: Session, event: BookmakerEvent) -> None:
        for market in event.markets:
            market.is_mapped = True  # partial work that must be rolled back
        raise RuntimeError("mapper exploded")

    event_id = seeded.events["pending_swapped"]
    with TestClient(app_factory(event_remapper=remapper), base_url=BASE_URL) as c:
        body = c.post(
            f"/api/review/links/{event_id}", json={"action": "confirm", "teams_swapped": True}, headers=CSRF
        ).json()
    assert body["remapped"] is False and body["event"]["link_status"] == "confirmed"
    assert _mapped(session_factory, event_id) == (0, 0)
    assert _event(session_factory, event_id).extra["remap_required"] is True
