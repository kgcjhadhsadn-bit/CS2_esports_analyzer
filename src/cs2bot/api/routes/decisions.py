"""Decision log (every accepted / rejected outcome with its reasons)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from cs2bot.api.deps import DB, Paging, require_auth
from cs2bot.api.formatting import parse_spec_key
from cs2bot.api.params import FromParam, ToParam, as_utc
from cs2bot.api.queries import decision_out, load_matches, player_ids_in, player_names
from cs2bot.api.schemas import DecisionPage
from cs2bot.db.models import Bet, BetDecisionRow

router = APIRouter(prefix="/api/decisions", tags=["decisions"], dependencies=[Depends(require_auth)])


@router.get("", response_model=DecisionPage)
def list_decisions(
    db: DB,
    page: Paging,
    match_id: int | None = None,
    accepted: bool | None = None,
    from_: FromParam = None,
    to: ToParam = None,
) -> DecisionPage:
    stmt = select(BetDecisionRow)
    if match_id is not None:
        stmt = stmt.where(BetDecisionRow.match_id == match_id)
    if accepted is not None:
        stmt = stmt.where(BetDecisionRow.accepted.is_(accepted))
    lower, upper = as_utc(from_), as_utc(to)
    if lower is not None:
        stmt = stmt.where(BetDecisionRow.decided_at >= lower)
    if upper is not None:
        stmt = stmt.where(BetDecisionRow.decided_at <= upper)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(
        db.scalars(
            stmt.order_by(BetDecisionRow.decided_at.desc(), BetDecisionRow.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
    )
    matches = load_matches(db, [r.match_id for r in rows])
    bet_by_decision: dict[int, int] = {}
    if rows:
        for bet_id, decision_id in db.execute(
            select(Bet.id, Bet.decision_id).where(Bet.decision_id.in_([r.id for r in rows])).order_by(Bet.id)
        ):
            if decision_id is not None:
                bet_by_decision[decision_id] = bet_id  # ordered by id: the newest bet wins
    names = player_names(
        db, [pid for r in rows for pid in player_ids_in(parse_spec_key(r.spec_key), r.selection_key)]
    )
    items = [
        decision_out(r, match=matches.get(r.match_id), players=names, bet_id=bet_by_decision.get(r.id))
        for r in rows
    ]
    return DecisionPage(total=total, limit=page.limit, offset=page.offset, items=items)
