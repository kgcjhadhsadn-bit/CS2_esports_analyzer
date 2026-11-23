"""Second reviewer pass of round 1 (see docs/dev/reports/api.md, "Review round 1").

* a row flagged ``dry_run`` that ``engine.ledger.settle_bet`` settled while it was still
  ``pending`` (its simulated placement never ran) is a settled paper bet, not an open one;
* a won bet without ``pnl`` (but with ``payout``) counts ``payout - stake`` like the engine;
* forged / undecodable session cookies are rejected without errors.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.api.analytics import effective_status
from cs2bot.api.auth import SessionAuth
from cs2bot.domain.betting import PlacementResult, PlacementStatus
from cs2bot.domain.enums import BetStatus
from cs2bot.engine import bankroll as engine_bankroll
from cs2bot.engine import ledger as engine_ledger
from tests.api.api_seed import BASE_URL, CSRF, MW, NOW, Seed, _bet, _decision, make_settings


def test_effective_status_of_flagged_paper_rows() -> None:
    won = {"settled_outcome": "won"}
    for status in (BetStatus.PENDING, BetStatus.PLACING, BetStatus.PLACED):
        assert effective_status(status, extra=won, dry_run=True) is BetStatus.WON
        assert effective_status(status, extra={}, dry_run=True) is status  # unsettled: unchanged
        assert effective_status(status, extra=won) is status  # a real bet is never reinterpreted
    assert effective_status(BetStatus.PENDING, pnl=Decimal("-5"), settled_at=NOW, dry_run=True) is (
        BetStatus.LOST
    )
    # failed / cancelled rows never had anything at stake, flagged or not
    assert effective_status(BetStatus.FAILED, extra=won, dry_run=True) is BetStatus.FAILED
    assert effective_status(BetStatus.CANCELLED, extra=won, dry_run=True) is BetStatus.CANCELLED


def test_paper_bet_settled_while_still_pending(
    client: TestClient, seeded: Seed, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path)
    before_bets = client.get("/api/bets", params={"dry_run": "true"}).json()["summary"]
    before_bank = client.get("/api/bankroll", params={"dry_run": "true"}).json()
    before_perf = client.get("/api/performance/markets", params={"mode": "dry_run"}).json()["total"]
    before_cal = client.get("/api/performance/calibration", params={"mode": "dry_run"}).json()["bets"]["n"]
    with session_factory() as s:
        decision = _decision(
            s, 5, MW, "team1", accepted=True, decided=NOW - timedelta(hours=6), odds="2.00", p_win=0.7, ev=0.2
        )
        bet = _bet(
            s,
            "paper-pending",
            decision_id=decision.id,
            match_id=5,
            selection_id=seeded.selections["team2"],
            stake=Decimal("10.00"),
            requested_odds=Decimal("2.00"),
            status=BetStatus.PENDING,
            dry_run=True,
        )
        engine_ledger.settle_bet(s, bet, "won", settings, at=NOW - timedelta(hours=1))
        assert bet.status is BetStatus.PENDING  # the engine settles it on paper, status unchanged
        bet_id = bet.id
        s.commit()

    listed = client.get("/api/bets", params={"dry_run": "true"}).json()
    row = next(b for b in listed["items"] if b["id"] == bet_id)
    assert row["status"] == "pending" and row["result"] == "won" and row["is_open"] is False
    # counted as a won paper bet, not as an open one
    assert listed["summary"]["open"] == before_bets["open"]
    assert listed["summary"]["won"] == before_bets["won"] + 1
    won_ids = [b["id"] for b in client.get("/api/bets", params={"status": "won"}).json()["items"]]
    assert bet_id in won_ids
    assert client.get(f"/api/bets/{bet_id}").json()["bet"]["result"] == "won"

    paper = client.get("/api/bankroll", params={"dry_run": "true"}).json()
    assert paper["open_bets"] == before_bank["open_bets"]
    assert paper["open_exposure"] == before_bank["open_exposure"]
    assert paper["stats"]["won"] == before_bank["stats"]["won"] + 1
    gained = Decimal(paper["stats"]["realized_pnl"]) - Decimal(before_bank["stats"]["realized_pnl"])
    assert gained == Decimal("7.60")  # 10 * 0.88 * 2.00 - 10

    perf = client.get("/api/performance/markets", params={"mode": "dry_run"}).json()["total"]
    assert (perf["open"], perf["won"]) == (before_perf["open"], before_perf["won"] + 1)
    cal = client.get("/api/performance/calibration", params={"mode": "dry_run"}).json()["bets"]
    assert cal["n"] == before_cal + 1

    # the review warning counts only the real PLACED bet of the seed, not the settled paper row
    body = client.post(
        f"/api/review/links/{seeded.events['main']}", json={"action": "confirm"}, headers=CSRF
    ).json()
    assert body["open_bets"] == 1


def test_won_bet_without_pnl_counts_payout_minus_stake(
    app_factory, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path)
    with session_factory() as s:
        engine_ledger.ensure_initialized(s, settings, at=NOW - timedelta(hours=3))
        bet = _bet(s, "won-no-pnl", stake=Decimal("20.00"), status=BetStatus.PENDING, dry_run=False)
        result = PlacementResult(
            status=PlacementStatus.PLACED, placed_odds=Decimal("2.00"), at=NOW - timedelta(hours=2)
        )
        engine_ledger.apply_placement(s, bet, result, settings)
        engine_ledger.settle_bet(s, bet, "won", settings, at=NOW - timedelta(hours=1))
        bet.pnl = None  # an integration bug: the payout is known, the pnl was lost
        s.flush()
        st = engine_bankroll.compute_state(s, settings, now=NOW)
        s.commit()
    assert st.realized_pnl_today == Decimal("15.20")  # 20 * 0.88 * 2 - 20
    with TestClient(app_factory(settings=settings), base_url=BASE_URL) as c:
        b = c.get("/api/bankroll").json()
    assert b["stats"]["realized_pnl"] == "15.20" and b["daily"]["realized_pnl"] == "15.20"
    assert b["daily"]["start_bankroll"] == f"{st.day_start_bankroll:.2f}"


def test_undecodable_session_tokens_are_rejected() -> None:
    from pydantic import SecretStr

    auth = SessionAuth(username="admin", password=SecretStr("pw"), secret=SecretStr("k" * 32), max_age_s=600)
    good = auth.issue()
    assert auth.verify(good) is not None
    for token in ("", "x", "a.b.c", good + "x", "é" * 40, good.replace(".", "..", 1)):
        assert auth.verify(token) is None
    other = SessionAuth(username="admin", password=SecretStr("pw"), secret=SecretStr("z" * 32), max_age_s=600)
    assert other.verify(good) is None
