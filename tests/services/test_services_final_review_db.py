"""Final review (money path): the database itself refuses a second stake / payout / refund for one bet.

``engine.ledger`` keeps one entry per bet and kind with read-then-write checks; two writers racing on one
bet (``cs2bot settle`` next to ``cs2bot up``) could both pass them. Migration 0002 adds a unique index on
``bankroll_ledger(bet_id, kind)`` and refuses to run over a ledger that already holds duplicates.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cs2bot.config import Settings
from cs2bot.db.models import BankrollLedger, Bet
from cs2bot.domain.enums import BetStatus, LedgerKind
from cs2bot.engine import ledger
from cs2bot.services.dbops import alembic_config, migration_status

AT = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def _bet(session: Session) -> Bet:
    bet = Bet(
        external_event_id="ev",
        market_raw="Zwycięzca meczu",
        selection_raw="Natus Vincere",
        spec_key="match_winner",
        selection_key="team1",
        requested_odds=Decimal("1.90"),
        min_acceptable_odds=Decimal("1.85"),
        stake=Decimal("10.00"),
        status=BetStatus.PLACED,
        dry_run=False,
        idempotency_key="cs2b-live-1-x-1",
        placed_at=AT,
    )
    session.add(bet)
    session.flush()
    return bet


def test_second_stake_entry_for_one_bet_is_refused_by_the_database(
    session: Session, settings: Settings
) -> None:
    bet = _bet(session)
    ledger.ensure_initialized(session, settings, at=AT)
    ledger.record_stake(session, bet, at=AT)
    assert ledger.record_stake(session, bet, at=AT) is not None  # idempotent in code: no second row
    session.add(
        BankrollLedger(
            ts=AT,
            kind=LedgerKind.STAKE,
            amount=Decimal("-10.00"),
            balance_after=Decimal("980.00"),
            bet_id=bet.id,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_rows_without_a_bet_never_conflict(session: Session, settings: Settings) -> None:
    ledger.ensure_initialized(session, settings, at=AT)
    ledger.record_adjustment(session, Decimal("5.00"), "bonus", at=AT)
    ledger.record_adjustment(session, Decimal("-5.00"), "fee", at=AT)
    session.flush()


def test_migration_0002_adds_the_index_and_refuses_duplicates(tmp_path: Path) -> None:
    ok_url = f"sqlite:///{tmp_path / 'ok.db'}"
    command.upgrade(alembic_config(ok_url), "head")
    engine = create_engine(ok_url)
    try:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("bankroll_ledger")}
        assert indexes["uq_bankroll_ledger_bet_kind"]["unique"]
        assert migration_status(engine).at_head
    finally:
        engine.dispose()
    command.upgrade(alembic_config(ok_url), "head")  # idempotent

    dup_url = f"sqlite:///{tmp_path / 'dup.db'}"
    command.upgrade(alembic_config(dup_url), "0001")
    engine = create_engine(dup_url)
    try:
        with engine.begin() as conn:
            for _ in range(2):
                conn.execute(
                    text(
                        "INSERT INTO bankroll_ledger (ts, kind, amount, balance_after, bet_id) "
                        "VALUES (:ts, 'stake', -10, 990, 7)"
                    ),
                    {"ts": AT.isoformat()},
                )
        with pytest.raises(RuntimeError, match="bet 7 stake x2"):
            command.upgrade(alembic_config(dup_url), "head")
        assert not migration_status(engine).at_head
    finally:
        engine.dispose()
