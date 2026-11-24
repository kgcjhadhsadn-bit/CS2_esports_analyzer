"""one stake / payout / refund ledger entry per bet

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24 12:00:00

Final review (money path): ``engine.ledger`` books every STAKE / PAYOUT / REFUND of a bet at most once,
but with read-then-write checks; a unique index on ``(bet_id, kind)`` makes the database refuse a second
booking when two writers race on one bet. Rows without a bet (deposits, withdrawals, adjustments,
reconciliations) are unaffected (NULL ``bet_id`` values never conflict).

The upgrade refuses to run while the ledger already holds such duplicates: they mean money was booked
twice and must be corrected by hand first (the error lists them).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "uq_bankroll_ledger_bet_kind"


def upgrade() -> None:
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT bet_id, kind, COUNT(*) AS n FROM bankroll_ledger WHERE bet_id IS NOT NULL "
                "GROUP BY bet_id, kind HAVING COUNT(*) > 1"
            )
        )
        .fetchall()
    )
    if duplicates:
        listed = ", ".join(f"bet {row[0]} {row[1]} x{row[2]}" for row in duplicates[:20])
        raise RuntimeError(
            "bankroll_ledger books the same bet more than once (" + listed + "). Correct these entries by "
            "hand (keep one entry per bet and kind, fix balance_after) before running cs2bot db upgrade."
        )
    with op.batch_alter_table("bankroll_ledger", schema=None) as batch_op:
        batch_op.create_index(INDEX, ["bet_id", "kind"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("bankroll_ledger", schema=None) as batch_op:
        batch_op.drop_index(INDEX)
