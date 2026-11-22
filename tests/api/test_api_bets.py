"""Bets list / detail / screenshot and the decision log."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.api.routes.bets import resolve_screenshot
from cs2bot.db.models import Bet
from tests.api.api_seed import Seed


def _set_screenshot(session_factory: sessionmaker[Session], bet_id: int, path: str | None) -> None:
    with session_factory() as s:
        bet = s.get(Bet, bet_id)
        assert bet is not None
        bet.screenshot_path = path
        s.commit()


def test_list_bets_all_with_summary(client: TestClient, seeded: Seed) -> None:
    body = client.get("/api/bets").json()
    assert body["total"] == 8 and len(body["items"]) == 8
    s = body["summary"]
    assert s == {"count": 8, "stake": "95.00", "pnl": "-8.96", "open": 2, "won": 2, "lost": 1, "void": 1}
    stamps = [b["placed_at"] or b["created_at"] for b in body["items"]]
    assert stamps == sorted(stamps, reverse=True)
    won = next(b for b in body["items"] if b["id"] == seeded.bets["won"])
    assert (
        won["stake"] == "20.00"
        and won["tax"] == "2.40"
        and won["pnl"] == "13.44"
        and won["payout"] == "33.44"
    )
    assert won["placed_odds"] == "1.90" and won["closing_odds"] == "1.84" and won["clv"] == 0.03
    assert won["p_win"] == 0.78 and won["ev"] == 0.30 and won["market_type"] == "match_winner"
    assert won["match"]["id"] == 1 and won["label"].startswith("Match winner: ")
    assert won["has_screenshot"] is False and won["screenshot_url"] is None
    opened = next(b for b in body["items"] if b["id"] == seeded.bets["open"])
    assert (
        opened["has_screenshot"] is True
        and opened["screenshot_url"] == f"/api/bets/{seeded.bets['open']}/screenshot"
    )


def test_list_bets_filters(client: TestClient, seeded: Seed) -> None:
    def ids(**params: object) -> set[int]:
        resp = client.get("/api/bets", params=params)
        assert resp.status_code == 200, resp.text
        return {b["id"] for b in resp.json()["items"]}

    b = seeded.bets
    assert ids(status="won") == {b["won"], b["paper_won"]}
    assert ids(status="won", dry_run="false") == {b["won"]}
    assert ids(status="won,lost", dry_run="false") == {b["won"], b["lost"]}
    assert ids(dry_run="true") == {b["paper_open"], b["paper_won"]}
    assert ids(**{"from": "2026-11-20T00:00:00Z", "to": "2026-11-20T10:00:00Z"}) == {
        b["lost"],
        b["paper_open"],
    }
    assert ids(match_id=101, status="placed") == {b["open"]}
    page = client.get("/api/bets", params={"limit": 2, "offset": 1}).json()
    assert page["total"] == 8 and len(page["items"]) == 2
    assert client.get("/api/bets", params={"status": "nope"}).status_code == 422


def test_bet_detail(client: TestClient, seeded: Seed) -> None:
    d = client.get(f"/api/bets/{seeded.bets['won']}").json()
    assert d["bet"]["status"] == "won"
    assert d["decision"]["id"] == seeded.decisions["won"] and d["decision"]["settings_snapshot"] == {
        "min_ev": 0.03
    }
    assert d["decision"]["bet_id"] == seeded.bets["won"]
    assert [(e["kind"], e["amount"], e["balance_after"]) for e in d["ledger"]] == [
        ("stake", "-20.00", "980.00"),
        ("payout", "33.44", "1003.44"),
    ]
    assert d["rationale"] is None
    opened = client.get(f"/api/bets/{seeded.bets['open']}").json()
    assert opened["rationale"] == "Team 1 is much stronger on Mirage." and opened["analysis_id"] is not None
    no_decision = client.get(f"/api/bets/{seeded.bets['void']}").json()
    assert no_decision["decision"] is None and no_decision["bet"]["label"] == "Total maps over 2.5"
    assert client.get("/api/bets/99999").status_code == 404


def test_screenshot_served_from_screenshot_dir(client: TestClient, seeded: Seed) -> None:
    resp = client.get(f"/api/bets/{seeded.bets['open']}/screenshot")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content.endswith(b"fake-image")
    assert resp.headers["cache-control"] == "no-store"


def test_screenshot_path_traversal_blocked(
    client: TestClient, seeded: Seed, session_factory: sessionmaker[Session]
) -> None:
    bet_id = seeded.bets["escape"]
    url = f"/api/bets/{bet_id}/screenshot"
    assert client.get(url).status_code == 404  # absolute path outside the directory
    for sneaky in (
        "../outside.png",
        "screenshots/../../outside.png",
        str(seeded.screenshot_dir / ".." / "outside.png"),
    ):
        _set_screenshot(session_factory, bet_id, sneaky)
        resp = client.get(url)
        assert resp.status_code == 404, sneaky
        assert b"secret" not in resp.content
    link = seeded.screenshot_dir / "link.png"
    os.symlink(seeded.outside_file, link)
    _set_screenshot(session_factory, bet_id, "link.png")
    assert client.get(url).status_code == 404
    (seeded.screenshot_dir / "notes.txt").write_text("not an image")
    _set_screenshot(session_factory, bet_id, "notes.txt")
    assert client.get(url).status_code == 404
    _set_screenshot(session_factory, bet_id, "missing.png")
    assert client.get(url).status_code == 404
    _set_screenshot(session_factory, bet_id, None)
    assert client.get(url).json()["detail"] == "this bet has no screenshot"
    assert client.get("/api/bets/99999/screenshot").status_code == 404


def test_resolve_screenshot_variants(tmp_path: Path) -> None:
    root = tmp_path / "shots"
    (root / "sub").mkdir(parents=True)
    img = root / "sub" / "a.jpg"
    img.write_bytes(b"x")
    assert resolve_screenshot("sub/a.jpg", root) == img.resolve()
    assert resolve_screenshot(str(img), root) == img.resolve()
    assert resolve_screenshot("sub/../sub/a.jpg", root) == img.resolve()
    assert resolve_screenshot("../shots/sub/a.jpg", root / "sub") is None
    assert resolve_screenshot("a\x00.png", root) is None


@pytest.mark.parametrize("accepted", [True, False])
def test_decisions_filters(client: TestClient, seeded: Seed, accepted: bool) -> None:
    body = client.get("/api/decisions", params={"accepted": str(accepted).lower()}).json()
    assert body["total"] == (3 if accepted else 2)
    assert all(d["accepted"] is accepted for d in body["items"])


def test_decisions_list(client: TestClient, seeded: Seed) -> None:
    body = client.get("/api/decisions").json()
    assert body["total"] == 5
    times = [d["decided_at"] for d in body["items"]]
    assert times == sorted(times, reverse=True)
    by_match = client.get("/api/decisions", params={"match_id": 101}).json()
    assert by_match["total"] == 3
    accepted = next(d for d in by_match["items"] if d["accepted"])
    assert accepted["id"] == seeded.decisions["accepted"] and accepted["bet_id"] == seeded.bets["open"]
    assert accepted["odds"] == "1.95" and accepted["proposed_stake"] == "15.00"
    assert accepted["label"] == "Match winner: Synth Team 1" and accepted["match"]["id"] == 101
    rejected = [d for d in by_match["items"] if not d["accepted"]]
    assert {tuple(d["reasons"]) for d in rejected} == {("sigma above threshold",), ("p_win 0.45 < 0.75",)}
    limited = client.get("/api/decisions", params={"limit": 1}).json()
    assert limited["total"] == 5 and len(limited["items"]) == 1
    window = client.get("/api/decisions", params={"from": "2026-11-20T11:00:00Z"}).json()
    assert window["total"] == 2
