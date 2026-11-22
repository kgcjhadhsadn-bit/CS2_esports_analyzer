"""DB read helpers shared by several routes (bulk loading, no N+1 queries) and row -> schema builders."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from cs2bot.api.ev import breakeven_odds, effective_odds, ev_after_tax, fair_odds
from cs2bot.api.formatting import (
    age_seconds,
    describe_outcome,
    market_sort_key,
    market_type_of,
    money_str,
    money_str0,
    odds_str,
    parse_spec_key,
    round_or_none,
    to_decimal,
    utc,
)
from cs2bot.api.schemas import (
    BetBrief,
    BetOut,
    BookmakerEventRef,
    DecisionBrief,
    DecisionDetail,
    DecisionOut,
    MarketRow,
    MatchBrief,
    MatchSummary,
    TeamRef,
    TournamentRef,
)
from cs2bot.db.models import (
    Bet,
    BetDecisionRow,
    BookmakerEvent,
    BookmakerMarket,
    BookmakerSelection,
    Match,
    MatchAnalysis,
    Player,
    Prediction,
    Team,
    Tournament,
)
from cs2bot.domain.enums import BetStatus, LinkStatus
from cs2bot.domain.markets import parse_player_selection
from cs2bot.utils.time import ensure_utc

ACTIVE_LINK_STATUSES: tuple[LinkStatus, ...] = (LinkStatus.AUTO, LinkStatus.CONFIRMED)
_EVENT_RANK: dict[LinkStatus, int] = {
    LinkStatus.CONFIRMED: 0,
    LinkStatus.AUTO: 1,
    LinkStatus.PENDING_REVIEW: 2,
    LinkStatus.UNLINKED: 3,
    LinkStatus.REJECTED: 4,
}

# --------------------------------------------------------------------------------------------
# references
# --------------------------------------------------------------------------------------------


def team_ref(team: Team | None) -> TeamRef | None:
    if team is None:
        return None
    return TeamRef(
        id=team.id, name=team.name, country=team.country, logo_url=team.logo_url, hltv_rank=team.hltv_rank
    )


def tournament_ref(tour: Tournament | None) -> TournamentRef | None:
    if tour is None:
        return None
    return TournamentRef(id=tour.id, name=tour.name, tier=tour.tier, is_lan=tour.is_lan)


def event_ref(event: BookmakerEvent) -> BookmakerEventRef:
    return BookmakerEventRef(
        id=event.id,
        bookmaker=event.bookmaker.value,
        external_id=event.external_id,
        name=event.name,
        team1_raw=event.team1_raw,
        team2_raw=event.team2_raw,
        competition_raw=event.competition_raw,
        starts_at=utc(event.starts_at),
        url=event.url,
        is_live=bool(event.is_live),
        match_id=event.match_id,
        link_status=event.link_status,
        link_confidence=event.link_confidence,
        teams_swapped=bool(event.teams_swapped),
        first_seen_at=utc(event.first_seen_at),
        last_seen_at=utc(event.last_seen_at),
    )


def match_brief(match: Match) -> MatchBrief:
    return MatchBrief(
        id=match.id,
        scheduled_at=utc(match.scheduled_at),
        status=match.status,
        format=match.format,
        team1=team_ref(match.team1),
        team2=team_ref(match.team2),
        tournament=match.tournament.name if match.tournament is not None else None,
    )


def match_load_options() -> list[Any]:
    """Eager-load options so building summaries does not lazy-load per match."""
    return [selectinload(Match.team1), selectinload(Match.team2), selectinload(Match.tournament)]


def load_matches(session: Session, ids: Iterable[int | None]) -> dict[int, Match]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = session.scalars(select(Match).where(Match.id.in_(wanted)).options(*match_load_options()))
    return {m.id: m for m in rows}


def team_names(match: Match | None) -> tuple[str | None, str | None]:
    if match is None:
        return None, None
    return (
        match.team1.name if match.team1 is not None else None,
        match.team2.name if match.team2 is not None else None,
    )


def player_names(session: Session, ids: Iterable[int | None]) -> dict[int, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    return {p.id: p.nickname for p in session.scalars(select(Player).where(Player.id.in_(wanted)))}


def player_ids_in(spec: Mapping[str, Any], selection_key: str) -> list[int]:
    out: list[int] = []
    for key in ("player_id", "opponent_player_id"):
        value = spec.get(key)
        if isinstance(value, int):
            out.append(value)
    pid = parse_player_selection(selection_key)
    if pid is not None:
        out.append(pid)
    return out


# --------------------------------------------------------------------------------------------
# bookmaker events
# --------------------------------------------------------------------------------------------


def primary_event(events: Sequence[BookmakerEvent]) -> BookmakerEvent | None:
    """Most relevant non-rejected event of a match: confirmed > auto > pending review, newest first."""
    usable = [e for e in events if e.link_status is not LinkStatus.REJECTED]
    if not usable:
        return None
    return min(
        usable,
        key=lambda e: (
            _EVENT_RANK.get(e.link_status, 9),
            -(e.last_seen_at.timestamp() if e.last_seen_at else 0),
        ),
    )


def events_by_match(session: Session, match_ids: Collection[int]) -> dict[int, list[BookmakerEvent]]:
    out: dict[int, list[BookmakerEvent]] = defaultdict(list)
    if not match_ids:
        return out
    for event in session.scalars(select(BookmakerEvent).where(BookmakerEvent.match_id.in_(match_ids))):
        if event.match_id is not None:
            out[event.match_id].append(event)
    return out


def market_counts(session: Session, event_ids: Collection[int]) -> dict[int, int]:
    if not event_ids:
        return {}
    rows = session.execute(
        select(BookmakerMarket.event_id, func.count(BookmakerMarket.id))
        .where(BookmakerMarket.event_id.in_(event_ids))
        .group_by(BookmakerMarket.event_id)
    )
    return {int(event_id): int(n) for event_id, n in rows}


# --------------------------------------------------------------------------------------------
# predictions x selections
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PredRow:
    id: int
    match_id: int
    spec_key: str
    selection_key: str
    market_type: str
    spec: dict[str, Any]
    p_win: float
    p_push: float
    p_void: float
    sigma: float
    model_version: str
    created_at: datetime


def latest_predictions(
    session: Session,
    match_ids: Collection[int],
    *,
    with_spec: bool = False,
    market_types: Collection[str] | None = None,
) -> dict[int, dict[tuple[str, str], PredRow]]:
    """Latest prediction per (match, spec_key, selection_key) among rows flagged ``is_latest``."""
    out: dict[int, dict[tuple[str, str], PredRow]] = defaultdict(dict)
    if not match_ids:
        return out
    cols: list[Any] = [
        Prediction.id,
        Prediction.match_id,
        Prediction.spec_key,
        Prediction.selection_key,
        Prediction.market_type,
        Prediction.p_win,
        Prediction.p_push,
        Prediction.p_void,
        Prediction.sigma,
        Prediction.model_version,
        Prediction.created_at,
    ]
    if with_spec:
        cols.append(Prediction.spec)
    stmt = select(*cols).where(Prediction.match_id.in_(match_ids), Prediction.is_latest.is_(True))
    if market_types is not None:
        stmt = stmt.where(Prediction.market_type.in_(list(market_types)))
    for row in session.execute(stmt):
        key = (row.spec_key, row.selection_key)
        current = out[row.match_id].get(key)
        created = ensure_utc(row.created_at)
        if current is not None and (current.created_at, current.id) >= (created, row.id):
            continue
        spec = row.spec if with_spec and isinstance(row.spec, dict) else parse_spec_key(row.spec_key)
        out[row.match_id][key] = PredRow(
            id=int(row.id),
            match_id=int(row.match_id),
            spec_key=row.spec_key,
            selection_key=row.selection_key,
            market_type=row.market_type,
            spec=dict(spec),
            p_win=float(row.p_win),
            p_push=float(row.p_push or 0.0),
            p_void=float(row.p_void or 0.0),
            sigma=float(row.sigma),
            model_version=row.model_version,
            created_at=created,
        )
    return out


@dataclass(frozen=True)
class SelRow:
    selection_id: int
    event_id: int
    match_id: int
    event_is_live: bool
    market_raw: str
    market_type: str | None
    market_spec: dict[str, Any] | None
    spec_key: str
    selection_raw: str
    selection_key: str
    odds: Decimal | None
    odds_at: datetime | None
    is_available: bool


def mapped_selections(
    session: Session, match_ids: Collection[int], *, statuses: Collection[LinkStatus] = ACTIVE_LINK_STATUSES
) -> dict[int, list[SelRow]]:
    """Mapped bookmaker selections of events linked (auto/confirmed by default) to the matches."""
    out: dict[int, list[SelRow]] = defaultdict(list)
    if not match_ids:
        return out
    stmt = (
        select(
            BookmakerSelection.id.label("selection_id"),
            BookmakerSelection.raw_name.label("selection_raw"),
            BookmakerSelection.selection_key,
            BookmakerSelection.last_odds,
            BookmakerSelection.last_odds_at,
            BookmakerSelection.is_available,
            BookmakerMarket.raw_name.label("market_raw"),
            BookmakerMarket.market_type,
            BookmakerMarket.spec.label("market_spec"),
            BookmakerMarket.spec_key,
            BookmakerEvent.id.label("event_id"),
            BookmakerEvent.match_id,
            BookmakerEvent.is_live,
        )
        .join(BookmakerMarket, BookmakerSelection.market_id == BookmakerMarket.id)
        .join(BookmakerEvent, BookmakerMarket.event_id == BookmakerEvent.id)
        .where(
            BookmakerEvent.match_id.in_(match_ids),
            BookmakerEvent.link_status.in_(list(statuses)),
            BookmakerMarket.is_mapped.is_(True),
            BookmakerSelection.is_mapped.is_(True),
            BookmakerMarket.spec_key.is_not(None),
            BookmakerSelection.selection_key.is_not(None),
        )
    )
    for row in session.execute(stmt):
        out[int(row.match_id)].append(
            SelRow(
                selection_id=int(row.selection_id),
                event_id=int(row.event_id),
                match_id=int(row.match_id),
                event_is_live=bool(row.is_live),
                market_raw=row.market_raw,
                market_type=row.market_type,
                market_spec=row.market_spec if isinstance(row.market_spec, dict) else None,
                spec_key=row.spec_key,
                selection_raw=row.selection_raw,
                selection_key=row.selection_key,
                odds=to_decimal(row.last_odds),
                odds_at=utc(row.last_odds_at),
                is_available=bool(row.is_available),
            )
        )
    return out


def unmapped_selection_count(session: Session, match_id: int) -> int:
    stmt = (
        select(func.count(BookmakerSelection.id))
        .join(BookmakerMarket, BookmakerSelection.market_id == BookmakerMarket.id)
        .join(BookmakerEvent, BookmakerMarket.event_id == BookmakerEvent.id)
        .where(
            BookmakerEvent.match_id == match_id,
            BookmakerEvent.link_status.in_(list(ACTIVE_LINK_STATUSES)),
            or_(
                BookmakerMarket.is_mapped.is_(False),
                BookmakerSelection.is_mapped.is_(False),
                BookmakerMarket.spec_key.is_(None),
                BookmakerSelection.selection_key.is_(None),
            ),
        )
    )
    return int(session.scalar(stmt) or 0)


def _ev_for(pred: PredRow, sel: SelRow, tax_rate: Decimal) -> float | None:
    if sel.odds is None or sel.odds <= 0:
        return None
    return ev_after_tax(pred.p_win, sel.odds, tax_rate, p_push=pred.p_push, p_void=pred.p_void)


def best_ev_by_match(session: Session, match_ids: Collection[int], tax_rate: Decimal) -> dict[int, float]:
    """Highest after-tax EV among available, pre-match bookmaker prices that have a model price."""
    preds = latest_predictions(session, match_ids)
    sels = mapped_selections(session, match_ids)
    out: dict[int, float] = {}
    for match_id, rows in sels.items():
        by_key = preds.get(match_id, {})
        for sel in rows:
            if sel.event_is_live or not sel.is_available:
                continue
            pred = by_key.get((sel.spec_key, sel.selection_key))
            if pred is None:
                continue
            ev = _ev_for(pred, sel, tax_rate)
            if ev is not None and (match_id not in out or ev > out[match_id]):
                out[match_id] = ev
    return out


def accepted_outcome_counts(session: Session, match_ids: Collection[int]) -> dict[int, int]:
    """Distinct outcomes (spec_key + selection_key) with at least one accepted decision, per match."""
    if not match_ids:
        return {}
    sub = (
        select(BetDecisionRow.match_id, BetDecisionRow.spec_key, BetDecisionRow.selection_key)
        .where(BetDecisionRow.match_id.in_(match_ids), BetDecisionRow.accepted.is_(True))
        .distinct()
        .subquery()
    )
    rows = session.execute(select(sub.c.match_id, func.count()).group_by(sub.c.match_id))
    return {int(mid): int(n) for mid, n in rows}


def analysed_match_ids(session: Session, match_ids: Collection[int]) -> set[int]:
    if not match_ids:
        return set()
    rows = session.scalars(
        select(MatchAnalysis.match_id)
        .where(MatchAnalysis.match_id.in_(match_ids), MatchAnalysis.is_latest.is_(True))
        .distinct()
    )
    return {int(r) for r in rows}


def match_summaries(session: Session, matches: Sequence[Match], tax_rate: Decimal) -> list[MatchSummary]:
    ids = [m.id for m in matches]
    events = events_by_match(session, ids)
    counts = accepted_outcome_counts(session, ids)
    analysed = analysed_match_ids(session, ids)
    best = best_ev_by_match(session, ids, tax_rate)
    out: list[MatchSummary] = []
    for m in matches:
        primary = primary_event(events.get(m.id, []))
        out.append(
            MatchSummary(
                id=m.id,
                scheduled_at=utc(m.scheduled_at),
                status=m.status,
                format=m.format,
                tier=m.tier,
                stage=m.stage,
                is_lan=m.is_lan,
                in_scope=bool(m.in_scope),
                data_completeness=m.data_completeness,
                team1=team_ref(m.team1),
                team2=team_ref(m.team2),
                tournament=tournament_ref(m.tournament),
                team1_score=m.team1_score,
                team2_score=m.team2_score,
                winner_team_id=m.winner_team_id,
                betclic=event_ref(primary) if primary is not None else None,
                accepted_decisions=counts.get(m.id, 0),
                best_ev=round_or_none(best.get(m.id)),
                has_analysis=m.id in analysed,
            )
        )
    return out


# --------------------------------------------------------------------------------------------
# decisions / bets
# --------------------------------------------------------------------------------------------


def latest_decisions(session: Session, match_id: int) -> dict[tuple[str, str], BetDecisionRow]:
    """Most recent decision per outcome (decision ids grow with time)."""
    newest = (
        select(func.max(BetDecisionRow.id))
        .where(BetDecisionRow.match_id == match_id)
        .group_by(BetDecisionRow.spec_key, BetDecisionRow.selection_key)
    )
    rows = session.scalars(select(BetDecisionRow).where(BetDecisionRow.id.in_(newest)))
    return {(r.spec_key, r.selection_key): r for r in rows}


_NO_MONEY_STATUSES = frozenset({BetStatus.FAILED, BetStatus.CANCELLED})


def latest_bets(session: Session, match_id: int) -> dict[tuple[str, str], Bet]:
    """Most relevant bet per outcome: the newest one with money (or paper) at stake, else the newest."""
    out: dict[tuple[str, str], Bet] = {}
    for bet in session.scalars(select(Bet).where(Bet.match_id == match_id).order_by(Bet.id)):
        key = (bet.spec_key, bet.selection_key)
        current = out.get(key)
        if current is None or bet.status not in _NO_MONEY_STATUSES or current.status in _NO_MONEY_STATUSES:
            out[key] = bet
    return out


def decision_brief(row: BetDecisionRow) -> DecisionBrief:
    decided = ensure_utc(row.decided_at)
    return DecisionBrief(
        id=row.id,
        accepted=bool(row.accepted),
        reasons=list(row.reasons or []),
        decided_at=decided,
        odds=odds_str(row.odds),
        p_win=float(row.p_win),
        sigma=float(row.sigma),
        ev=float(row.ev),
        kelly_fraction=float(row.kelly_fraction or 0.0),
        proposed_stake=money_str(row.proposed_stake),
    )


def bet_brief(bet: Bet) -> BetBrief:
    return BetBrief(
        id=bet.id,
        status=bet.status,
        dry_run=bool(bet.dry_run),
        stake=money_str0(bet.stake),
        requested_odds=odds_str(bet.requested_odds),
        placed_odds=odds_str(bet.placed_odds),
        placed_at=utc(bet.placed_at),
        pnl=money_str(bet.pnl),
    )


def _decision_fields(
    row: BetDecisionRow, *, match: Match | None, players: Mapping[int, str], bet_id: int | None
) -> dict[str, Any]:
    t1, t2 = team_names(match)
    spec = parse_spec_key(row.spec_key)
    return {
        "id": row.id,
        "match_id": row.match_id,
        "match": match_brief(match) if match is not None else None,
        "prediction_id": row.prediction_id,
        "selection_id": row.selection_id,
        "market_type": market_type_of(row.spec_key),
        "spec_key": row.spec_key,
        "selection_key": row.selection_key,
        "label": describe_outcome(spec, row.selection_key, team1=t1, team2=t2, players=players),
        "odds": odds_str(row.odds) or "",
        "odds_captured_at": utc(row.odds_captured_at),
        "decided_at": ensure_utc(row.decided_at),
        "accepted": bool(row.accepted),
        "reasons": list(row.reasons or []),
        "p_win": float(row.p_win),
        "sigma": float(row.sigma),
        "ev": float(row.ev),
        "kelly_fraction": float(row.kelly_fraction or 0.0),
        "proposed_stake": money_str(row.proposed_stake),
        "bet_id": bet_id,
    }


def decision_out(
    row: BetDecisionRow, *, match: Match | None, players: Mapping[int, str], bet_id: int | None
) -> DecisionOut:
    return DecisionOut(**_decision_fields(row, match=match, players=players, bet_id=bet_id))


def decision_detail(
    row: BetDecisionRow, *, match: Match | None, players: Mapping[int, str], bet_id: int | None
) -> DecisionDetail:
    return DecisionDetail(
        **_decision_fields(row, match=match, players=players, bet_id=bet_id),
        settings_snapshot=dict(row.settings_snapshot or {}),
    )


def bet_out(
    bet: Bet,
    *,
    match: Match | None,
    decision: BetDecisionRow | None,
    players: Mapping[int, str],
) -> BetOut:
    t1, t2 = team_names(match)
    spec = parse_spec_key(bet.spec_key)
    has_screenshot = bool(bet.screenshot_path)
    return BetOut(
        id=bet.id,
        match_id=bet.match_id,
        match=match_brief(match) if match is not None else None,
        decision_id=bet.decision_id,
        selection_id=bet.selection_id,
        bookmaker=bet.bookmaker.value,
        external_event_id=bet.external_event_id,
        market_raw=bet.market_raw,
        selection_raw=bet.selection_raw,
        market_type=market_type_of(bet.spec_key),
        spec_key=bet.spec_key,
        selection_key=bet.selection_key,
        label=describe_outcome(spec, bet.selection_key, team1=t1, team2=t2, players=players),
        requested_odds=odds_str(bet.requested_odds) or "",
        min_acceptable_odds=odds_str(bet.min_acceptable_odds) or "",
        placed_odds=odds_str(bet.placed_odds),
        stake=money_str0(bet.stake),
        tax=money_str0(bet.tax),
        potential_payout=money_str(bet.potential_payout),
        status=bet.status,
        dry_run=bool(bet.dry_run),
        external_bet_id=bet.external_bet_id,
        has_screenshot=has_screenshot,
        screenshot_url=f"/api/bets/{bet.id}/screenshot" if has_screenshot else None,
        error=bet.error,
        created_at=utc(bet.created_at),
        placed_at=utc(bet.placed_at),
        settled_at=utc(bet.settled_at),
        payout=money_str(bet.payout),
        pnl=money_str(bet.pnl),
        closing_odds=odds_str(bet.closing_odds),
        clv=round_or_none(bet.clv),
        p_win=float(decision.p_win) if decision is not None else None,
        ev=float(decision.ev) if decision is not None else None,
    )


# --------------------------------------------------------------------------------------------
# market table of one match
# --------------------------------------------------------------------------------------------


def build_market_rows(
    session: Session,
    match: Match,
    *,
    tax_rate: Decimal,
    now: datetime,
    odds_max_age_s: float,
    extra_players: Mapping[int, str] | None = None,
) -> list[MarketRow]:
    """Latest model prices joined with the current bookmaker prices of one match.

    Each latest prediction yields one row per matching bookmaker selection (or one row without
    odds); mapped selections that the model does not price yet get rows without probabilities.
    """
    preds = latest_predictions(session, [match.id], with_spec=True).get(match.id, {})
    sels = mapped_selections(session, [match.id]).get(match.id, [])
    decisions = latest_decisions(session, match.id)
    bets = latest_bets(session, match.id)
    sels_by_key: dict[tuple[str, str], list[SelRow]] = defaultdict(list)
    for sel in sels:
        sels_by_key[(sel.spec_key, sel.selection_key)].append(sel)

    wanted_players: set[int] = set()
    for pred in preds.values():
        wanted_players.update(player_ids_in(pred.spec, pred.selection_key))
    for sel in sels:
        wanted_players.update(
            player_ids_in(sel.market_spec or parse_spec_key(sel.spec_key), sel.selection_key)
        )
    names = dict(extra_players or {})
    names.update(player_names(session, wanted_players - set(names)))
    t1, t2 = team_names(match)

    def make_row(key: tuple[str, str], pred: PredRow | None, sel: SelRow | None) -> MarketRow:
        spec_key, selection_key = key
        spec: dict[str, Any]
        if pred is not None:
            spec = pred.spec
        elif sel is not None and sel.market_spec:
            spec = dict(sel.market_spec)
        else:
            spec = parse_spec_key(spec_key)
        market_type = str(spec.get("market_type") or market_type_of(spec_key))
        row: dict[str, Any] = {
            "market_type": market_type,
            "spec_key": spec_key,
            "spec": spec,
            "selection_key": selection_key,
            "label": describe_outcome(spec, selection_key, team1=t1, team2=t2, players=names),
        }
        if pred is not None:
            row.update(
                prediction_id=pred.id,
                model_version=pred.model_version,
                predicted_at=pred.created_at,
                p_win=round_or_none(pred.p_win),
                p_push=round_or_none(pred.p_push),
                p_void=round_or_none(pred.p_void),
                p_lose=round_or_none(max(0.0, 1.0 - pred.p_win - pred.p_push - pred.p_void)),
                sigma=round_or_none(pred.sigma),
                fair_odds=odds_str(fair_odds(pred.p_win, p_push=pred.p_push, p_void=pred.p_void)),
                breakeven_odds=odds_str(
                    breakeven_odds(pred.p_win, tax_rate, p_push=pred.p_push, p_void=pred.p_void)
                ),
            )
        if sel is not None:
            age = age_seconds(sel.odds_at, now)
            row.update(
                selection_id=sel.selection_id,
                bookmaker_event_id=sel.event_id,
                market_raw=sel.market_raw,
                selection_raw=sel.selection_raw,
                odds=odds_str(sel.odds),
                effective_odds=odds_str(effective_odds(sel.odds, tax_rate)) if sel.odds is not None else None,
                odds_captured_at=sel.odds_at,
                odds_age_s=age,
                odds_stale=(age is None or age > odds_max_age_s),
                is_available=sel.is_available,
                event_is_live=sel.event_is_live,
            )
        if pred is not None and sel is not None:
            row["ev"] = round_or_none(_ev_for(pred, sel, tax_rate))
        decision = decisions.get(key)
        if decision is not None:
            row["decision"] = decision_brief(decision)
        bet = bets.get(key)
        if bet is not None:
            row["bet"] = bet_brief(bet)
        return MarketRow(**row)

    rows: list[MarketRow] = []
    for key, pred in preds.items():
        matching = sels_by_key.pop(key, [])
        if not matching:
            rows.append(make_row(key, pred, None))
        rows.extend(make_row(key, pred, sel) for sel in matching)
    for key, remaining in sels_by_key.items():
        rows.extend(make_row(key, None, sel) for sel in remaining)
    rows.sort(key=lambda r: (*market_sort_key(r.spec, r.selection_key), r.selection_id or 0))
    return rows
