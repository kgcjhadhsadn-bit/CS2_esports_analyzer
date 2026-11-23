"""Review queue for low-confidence Betclic event links (PLAN.md 4.4: never bet until confirmed).

Manual decisions set ``link_status`` to CONFIRMED / REJECTED; the odds collector must never
override those. When a decision changes the linked match or the team orientation, the event's
market/selection mappings are cleared (``is_mapped = false``) so no selection keyed for the old
orientation can be priced or bet; the collector maps them again on its next poll
(``extra["remap_required"] = true`` marks such events).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, null, or_, select, update
from sqlalchemy.orm import Session

from cs2bot.api.analytics import OPEN_STATUSES
from cs2bot.api.bankroll import paper_clause
from cs2bot.api.deps import DB, Auth, Ctx, Paging, require_auth
from cs2bot.api.params import IdPath, ListParam, parse_enum_list, validation_error
from cs2bot.api.queries import event_ref, load_matches, market_counts, match_brief, match_load_options
from cs2bot.api.schemas import ReviewActionIn, ReviewActionOut, ReviewCandidate, ReviewItem, ReviewPage
from cs2bot.db import state
from cs2bot.db.models import Bet, BookmakerEvent, BookmakerMarket, BookmakerSelection, Match, TeamAlias
from cs2bot.domain.enums import LinkStatus
from cs2bot.utils.logging import get_logger
from cs2bot.utils.names import team_name_similarity
from cs2bot.utils.time import ensure_utc

log = get_logger(__name__)
router = APIRouter(prefix="/api/review", tags=["review"], dependencies=[Depends(require_auth)])

_NO_START_LOOKBACK = timedelta(days=2)
_NO_START_LOOKAHEAD = timedelta(days=7)


def _names_by_team(session: Session, matches: Sequence[Match]) -> dict[int, list[str]]:
    """Display name + every registered alias per team (aliases improve matching across sources)."""
    names: dict[int, list[str]] = defaultdict(list)
    for m in matches:
        for team in (m.team1, m.team2):
            if team is not None and team.name not in names[team.id]:
                names[team.id].append(team.name)
    if names:
        for alias in session.scalars(select(TeamAlias).where(TeamAlias.team_id.in_(list(names)))):
            if alias.alias not in names[alias.team_id]:
                names[alias.team_id].append(alias.alias)
    return names


def _similarity(raw: str, candidates: Sequence[str]) -> float:
    return max((team_name_similarity(raw, c) for c in candidates), default=0.0)


def orientation(event: BookmakerEvent, match: Match, names: dict[int, list[str]]) -> tuple[float, bool]:
    """(name score 0..100, teams_swapped) - swapped when Betclic's first team is our team2."""
    t1 = names.get(match.team1_id or -1, [])
    t2 = names.get(match.team2_id or -1, [])
    direct = (_similarity(event.team1_raw, t1) + _similarity(event.team2_raw, t2)) / 2
    swapped = (_similarity(event.team1_raw, t2) + _similarity(event.team2_raw, t1)) / 2
    return (swapped, True) if swapped > direct else (direct, False)


def score_candidate(
    event: BookmakerEvent, match: Match, names: dict[int, list[str]], window_hours: float
) -> ReviewCandidate:
    """80% team-name similarity (best orientation) + 20% closeness of the start times."""
    name_score, swapped = orientation(event, match, names)
    diff_min: float | None = None
    time_score = 0.5
    if event.starts_at is not None and match.scheduled_at is not None:
        diff_min = abs((ensure_utc(match.scheduled_at) - ensure_utc(event.starts_at)).total_seconds()) / 60.0
        time_score = max(0.0, 1.0 - diff_min / (window_hours * 60.0))
    return ReviewCandidate(
        match=match_brief(match),
        score=round(0.8 * name_score / 100.0 + 0.2 * time_score, 4),
        name_score=round(name_score, 2),
        time_diff_minutes=round(diff_min, 1) if diff_min is not None else None,
        teams_swapped=swapped,
        is_current=event.match_id == match.id,
    )


def candidates_for(
    session: Session, event: BookmakerEvent, *, window_hours: float, limit: int, now: datetime
) -> list[ReviewCandidate]:
    if event.starts_at is not None:
        start = ensure_utc(event.starts_at)
        lo, hi = start - timedelta(hours=window_hours), start + timedelta(hours=window_hours)
    else:
        lo, hi = now - _NO_START_LOOKBACK, now + _NO_START_LOOKAHEAD
    matches = list(
        session.scalars(
            select(Match)
            .where(Match.scheduled_at >= lo, Match.scheduled_at <= hi)
            .options(*match_load_options())
        )
    )
    if event.match_id is not None and all(m.id != event.match_id for m in matches):
        current = load_matches(session, [event.match_id]).get(event.match_id)
        if current is not None:
            matches.append(current)
    names = _names_by_team(session, matches)
    scored = sorted(
        (score_candidate(event, m, names, window_hours) for m in matches),
        key=lambda c: (-c.score, c.match.id),
    )
    top = scored[:limit]
    if event.match_id is not None and not any(c.is_current for c in top):
        top.extend(c for c in scored if c.is_current)
    return top


@router.get("/links", response_model=ReviewPage)
def review_links(
    ctx: Ctx,
    db: DB,
    page: Paging,
    status: ListParam = None,
    window_hours: Annotated[float | None, Query(gt=0, le=24 * 14)] = None,
    max_candidates: Annotated[int | None, Query(ge=1, le=50)] = None,
) -> ReviewPage:
    """Events waiting for review (``status`` defaults to ``pending_review``) with candidate matches."""
    statuses = parse_enum_list(status, LinkStatus, "status") or [LinkStatus.PENDING_REVIEW]
    window = window_hours or ctx.options.review_window_hours
    limit = max_candidates or ctx.options.review_max_candidates
    stmt = select(BookmakerEvent).where(BookmakerEvent.link_status.in_(statuses))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    events = list(
        db.scalars(
            stmt.order_by(BookmakerEvent.starts_at.is_(None), BookmakerEvent.starts_at, BookmakerEvent.id)
            .limit(page.limit)
            .offset(page.offset)
        )
    )
    counts = market_counts(db, [e.id for e in events])
    current = load_matches(db, [e.match_id for e in events])
    now = ctx.now()
    items = [
        ReviewItem(
            event=event_ref(e),
            n_markets=counts.get(e.id, 0),
            current_match=match_brief(current[e.match_id]) if e.match_id in current else None,
            candidates=candidates_for(db, e, window_hours=window, limit=limit, now=now),
        )
        for e in events
    ]
    return ReviewPage(total=total, limit=page.limit, offset=page.offset, items=items)


def reset_event_mappings(session: Session, event_id: int) -> int:
    """Clear market/selection mappings of an event; returns how many markets were mapped."""
    mapped = int(
        session.scalar(
            select(func.count(BookmakerMarket.id)).where(
                BookmakerMarket.event_id == event_id, BookmakerMarket.is_mapped.is_(True)
            )
        )
        or 0
    )
    market_ids = select(BookmakerMarket.id).where(BookmakerMarket.event_id == event_id)
    session.execute(
        update(BookmakerSelection)
        .where(BookmakerSelection.market_id.in_(market_ids))
        .values(is_mapped=False, selection_key=None)
        .execution_options(synchronize_session=False)
    )
    session.execute(
        update(BookmakerMarket)
        .where(BookmakerMarket.event_id == event_id)
        .values(is_mapped=False, market_type=None, spec=null(), spec_key=None)
        .execution_options(synchronize_session=False)
    )
    return mapped


def _open_bets_on_event(session: Session, event_id: int) -> int:
    """Unsettled bets (real or paper) on the event's selections - shown as a warning to the reviewer."""
    selection_ids = (
        select(BookmakerSelection.id)
        .join(BookmakerMarket, BookmakerSelection.market_id == BookmakerMarket.id)
        .where(BookmakerMarket.event_id == event_id)
    )
    return int(
        session.scalar(
            select(func.count(Bet.id)).where(
                Bet.selection_id.in_(selection_ids),
                Bet.status.in_(list(OPEN_STATUSES)),
                # a settled paper bet keeps its open-looking status: it is no longer open
                or_(~paper_clause(), Bet.settled_at.is_(None)),
            )
        )
        or 0
    )


@router.post("/links/{event_id}", response_model=ReviewActionOut)
def review_action(event_id: IdPath, body: ReviewActionIn, ctx: Ctx, db: DB, auth: Auth) -> ReviewActionOut:
    """confirm (optionally with ``match_id``), reject, or relink to ``match_id``."""
    event = db.get(BookmakerEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="bookmaker event not found")
    previous_match, previous_status, previous_swapped = (
        event.match_id,
        event.link_status,
        bool(event.teams_swapped),
    )

    if body.action == "reject":
        if body.match_id is not None or body.teams_swapped is not None:
            raise validation_error(["body"], "reject takes no match_id / teams_swapped")
        event.link_status = LinkStatus.REJECTED
        event.match_id = None
        event.link_confidence = None
    else:
        if body.action == "relink" and body.match_id is None:
            raise validation_error(["body", "match_id"], "match_id is required for relink", "missing")
        target_id = body.match_id if body.match_id is not None else event.match_id
        if target_id is None:
            raise validation_error(
                ["body", "match_id"], "this event has no linked match: relink it with a match_id", "missing"
            )
        match = load_matches(db, [target_id]).get(target_id)
        if match is None:
            raise HTTPException(status_code=404, detail="match not found")
        if body.teams_swapped is not None:
            swapped = body.teams_swapped
        elif target_id == previous_match:
            swapped = previous_swapped
        else:
            swapped = orientation(event, match, _names_by_team(db, [match]))[1]
        event.match_id = target_id
        event.teams_swapped = swapped
        event.link_status = LinkStatus.CONFIRMED
        event.link_confidence = 1.0

    orientation_changed = event.match_id != previous_match or bool(event.teams_swapped) != previous_swapped
    markets_reset = reset_event_mappings(db, event.id) if orientation_changed else 0
    review_note: dict[str, Any] = {
        "action": body.action,
        "by": auth.actor,
        "at": ctx.now().isoformat(),
        "previous_match_id": previous_match,
        "previous_status": previous_status.value,
        "previous_teams_swapped": previous_swapped,
    }
    extra = dict(event.extra or {})
    extra["manual_review"] = review_note
    if orientation_changed:
        extra["remap_required"] = True
    event.extra = extra  # reassign so the JSON change is persisted
    open_bets = _open_bets_on_event(db, event.id)
    state.audit(
        db,
        actor=auth.actor,
        action="link_review",
        details={
            "event_id": event.id,
            "action": body.action,
            "match_id": event.match_id,
            "teams_swapped": bool(event.teams_swapped),
            "previous_match_id": previous_match,
            "previous_status": previous_status.value,
            "markets_reset": markets_reset,
        },
    )
    db.commit()  # the safe state (mappings cleared) is persisted before any re-mapping is attempted
    remapped: bool | None = None
    if orientation_changed and event.match_id is not None and ctx.event_remapper is not None:
        remapped = _remap(db, event, ctx)
    db.refresh(event)
    return ReviewActionOut(
        event=event_ref(event),
        action=body.action,
        previous_match_id=previous_match,
        previous_status=previous_status,
        previous_teams_swapped=previous_swapped,
        markets_reset=markets_reset,
        remapped=remapped,
        open_bets=open_bets,
    )


def _remap(session: Session, event: BookmakerEvent, ctx: Ctx) -> bool:
    """Run the injected re-mapper; on failure the cleared mappings stay (the collector re-maps later)."""
    remapper = ctx.event_remapper
    if remapper is None:  # pragma: no cover - checked by the caller
        return False
    try:
        remapper(session, event)
        extra = dict(event.extra or {})
        extra.pop("remap_required", None)
        event.extra = extra
        session.commit()
    except Exception as exc:  # the re-mapper belongs to the odds package; never fail the review
        session.rollback()
        log.warning("re-mapping bookmaker event %s failed: %s", event.id, type(exc).__name__)
        return False
    return True
