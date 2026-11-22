"""Bets: list with filters + summary, detail (decision, ledger, rationale) and the bet screenshot."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from cs2bot.api.analytics import EXCLUDED_STATUSES, OPEN_STATUSES, ZERO
from cs2bot.api.deps import DB, Ctx, Paging, require_auth
from cs2bot.api.formatting import money_str0, parse_spec_key, to_decimal
from cs2bot.api.params import FromParam, ListParam, ToParam, as_utc, parse_enum_list
from cs2bot.api.queries import bet_out, decision_detail, load_matches, player_ids_in, player_names
from cs2bot.api.schemas import BetDetail, BetPage, BetsSummary, LedgerEntryOut
from cs2bot.db.models import BankrollLedger, Bet, BetDecisionRow, MatchAnalysis
from cs2bot.domain.enums import BetStatus
from cs2bot.utils.paths import resolve_path
from cs2bot.utils.time import ensure_utc

router = APIRouter(prefix="/api/bets", tags=["bets"], dependencies=[Depends(require_auth)])

IMAGE_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def resolve_screenshot(stored: str, screenshot_dir: Path) -> Path | None:
    """The screenshot file for a stored path, only if it really lies inside ``screenshot_dir``.

    Relative paths are tried against the working directory / project root (``data/screenshots/x.png``)
    and against the screenshot directory (``x.png``). Symlinks are resolved before the containment
    check, so neither ``..`` segments, absolute paths nor links can escape the directory.
    """
    try:
        root = resolve_path(screenshot_dir).resolve()
        raw = Path(stored)
    except (OSError, ValueError, RuntimeError):
        return None
    candidates = [raw] if raw.is_absolute() else [resolve_path(raw), root / raw]
    for candidate in candidates:
        try:
            real = candidate.resolve(strict=True)
        except (OSError, ValueError, RuntimeError):
            continue
        if real.is_relative_to(root) and real.is_file() and real.suffix.lower() in IMAGE_TYPES:
            return real
    return None


@router.get("", response_model=BetPage)
def list_bets(
    db: DB,
    page: Paging,
    status: ListParam = None,
    dry_run: bool | None = None,
    from_: FromParam = None,
    to: ToParam = None,
    match_id: int | None = None,
) -> BetPage:
    when = func.coalesce(Bet.placed_at, Bet.created_at)
    stmt = select(Bet)
    statuses = parse_enum_list(status, BetStatus, "status")
    if statuses:
        stmt = stmt.where(Bet.status.in_(statuses))
    if dry_run is not None:
        stmt = stmt.where(Bet.dry_run.is_(dry_run))
    lower, upper = as_utc(from_), as_utc(to)
    if lower is not None:
        stmt = stmt.where(when >= lower)
    if upper is not None:
        stmt = stmt.where(when <= upper)
    if match_id is not None:
        stmt = stmt.where(Bet.match_id == match_id)

    totals = list(db.execute(stmt.with_only_columns(Bet.status, Bet.stake, Bet.pnl)))
    counted = [(s, to_decimal(stake) or ZERO, to_decimal(pnl)) for s, stake, pnl in totals]
    summary = BetsSummary(
        count=len(counted),
        stake=money_str0(sum((stake for s, stake, _ in counted if s not in EXCLUDED_STATUSES), ZERO)),
        pnl=money_str0(sum((pnl for s, _, pnl in counted if s.is_settled and pnl is not None), ZERO)),
        open=sum(1 for s, _, _ in counted if s in OPEN_STATUSES),
        won=sum(1 for s, _, _ in counted if s is BetStatus.WON),
        lost=sum(1 for s, _, _ in counted if s is BetStatus.LOST),
        void=sum(1 for s, _, _ in counted if s is BetStatus.VOID),
    )
    bets = list(db.scalars(stmt.order_by(when.desc(), Bet.id.desc()).limit(page.limit).offset(page.offset)))
    matches = load_matches(db, [b.match_id for b in bets])
    decision_ids = [b.decision_id for b in bets if b.decision_id is not None]
    decisions = (
        {d.id: d for d in db.scalars(select(BetDecisionRow).where(BetDecisionRow.id.in_(decision_ids)))}
        if decision_ids
        else {}
    )
    names = player_names(
        db, [pid for b in bets for pid in player_ids_in(parse_spec_key(b.spec_key), b.selection_key)]
    )
    items = [
        bet_out(
            b,
            match=matches.get(b.match_id) if b.match_id is not None else None,
            decision=decisions.get(b.decision_id) if b.decision_id is not None else None,
            players=names,
        )
        for b in bets
    ]
    return BetPage(total=len(counted), limit=page.limit, offset=page.offset, items=items, summary=summary)


@router.get("/{bet_id}", response_model=BetDetail)
def get_bet(bet_id: int, db: DB) -> BetDetail:
    bet = db.get(Bet, bet_id)
    if bet is None:
        raise HTTPException(status_code=404, detail="bet not found")
    match = load_matches(db, [bet.match_id]).get(bet.match_id) if bet.match_id is not None else None
    decision = db.get(BetDecisionRow, bet.decision_id) if bet.decision_id is not None else None
    wanted = player_ids_in(parse_spec_key(bet.spec_key), bet.selection_key)
    if decision is not None:
        wanted += player_ids_in(parse_spec_key(decision.spec_key), decision.selection_key)
    names = player_names(db, wanted)
    ledger = [
        LedgerEntryOut(
            id=row.id,
            ts=ensure_utc(row.ts),
            kind=row.kind,
            amount=money_str0(row.amount),
            balance_after=money_str0(row.balance_after),
            bet_id=row.bet_id,
            note=row.note,
        )
        for row in db.scalars(
            select(BankrollLedger)
            .where(BankrollLedger.bet_id == bet.id)
            .order_by(BankrollLedger.ts, BankrollLedger.id)
        )
    ]
    analysis = None
    if bet.match_id is not None:
        analysis = db.scalar(
            select(MatchAnalysis)
            .where(MatchAnalysis.match_id == bet.match_id)
            .order_by(
                MatchAnalysis.is_latest.desc(), MatchAnalysis.created_at.desc(), MatchAnalysis.id.desc()
            )
            .limit(1)
        )
    return BetDetail(
        bet=bet_out(bet, match=match, decision=decision, players=names),
        decision=(
            decision_detail(decision, match=match, players=names, bet_id=bet.id)
            if decision is not None
            else None
        ),
        ledger=ledger,
        analysis_id=analysis.id if analysis is not None else None,
        rationale=analysis.rationale if analysis is not None else None,
    )


@router.get(
    "/{bet_id}/screenshot",
    response_class=FileResponse,
    responses={200: {"content": {"image/png": {}, "image/jpeg": {}}}, 404: {"description": "No screenshot"}},
)
def get_screenshot(bet_id: int, ctx: Ctx, db: DB) -> FileResponse:
    bet = db.get(Bet, bet_id)
    if bet is None:
        raise HTTPException(status_code=404, detail="bet not found")
    if not bet.screenshot_path:
        raise HTTPException(status_code=404, detail="this bet has no screenshot")
    path = resolve_screenshot(bet.screenshot_path, ctx.settings.executor.screenshot_dir)
    if path is None:
        raise HTTPException(status_code=404, detail="screenshot not found")
    return FileResponse(path, media_type=IMAGE_TYPES[path.suffix.lower()], content_disposition_type="inline")
