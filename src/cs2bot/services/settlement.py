"""Settlement: confirm unconfirmed placements, settle finished bets, closing odds / CLV, reconcile.

:func:`settle_bets` runs, in this order (the order matters, see ``engine.ledger.reconcile``):

1. **Executor health** -> ``system_state["executor_health"]`` (+ the in-memory cache for the API).
2. **Bet history** from the bookmaker (``executor.fetch_bet_history``) when there are open real bets.
3. **PLACING bets** (``UNCERTAIN`` placements, or a crash around the click) are looked up in the
   history: by bet id, else by stake + selection (+ market) name among rows that may have been placed
   at or after the attempt (the history shows minutes or only the day). Found -> ``PLACED`` with the
   bookmaker's id and odds (``ledger.apply_placement``). Still absent after ``grace`` -> ``FAILED``
   (exposure released), flagged ``extra["unconfirmed_not_found"]`` (the betting cycle never bets that
   outcome again) and an audit alert asks the owner to verify on Betclic. Ambiguous matches and an
   unavailable history leave the bet open.
4. **PLACED bets**: the bookmaker's own history is the source of truth for money when it shows the
   bet settled (WON / LOST / VOID): its outcome is used and, when the row shows a plausible payout
   that differs from the local tax rules, an ADJUSTMENT entry makes the ledger and the bet's
   ``payout`` / ``pnl`` follow it. Otherwise, once the match is finished or cancelled, the local
   resolver decides (``engine.settlement.resolve_bet`` on ``datasets.load_match_actuals``); UNKNOWN
   stays open and is flagged (once per bet, not on every pass). When both exist and disagree, an
   alert is recorded and the bookmaker wins. A bet the history still shows open is never settled
   locally (its payout is not credited yet).
5. **Paper bets** (dry run) of finished matches are settled "on paper" by the local resolver
   (``ledger.settle_bet`` stores payout / pnl on the row, no ledger entry).
6. **Closing odds and CLV** for placed and paper bets once the event has started
   (``odds.closing_odds``; CLV = our odds / closing odds - 1).
7. **Balance reconciliation** (live mode only): ``executor.get_balance`` + ``ledger.reconcile`` (a large
   mismatch pauses the bot). Skipped while the bet history could not be read and real bets are open:
   a payout Betclic already credited would otherwise be booked as a reconciliation now and again by
   the local settlement later.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from cs2bot import datasets
from cs2bot.config import Settings
from cs2bot.db import state as db_state
from cs2bot.db.models import (
    BankrollLedger,
    Bet,
    BookmakerEvent,
    BookmakerMarket,
    BookmakerSelection,
    Match,
    MatchLineup,
)
from cs2bot.domain.betting import BookmakerBetRecord, BookmakerBetStatus, PlacementResult, PlacementStatus
from cs2bot.domain.enums import BetStatus, MatchStatus, TeamSlot
from cs2bot.domain.results import MatchActuals, SettlementOutcome
from cs2bot.engine import ledger, settlement
from cs2bot.odds import closing_odds
from cs2bot.services.health import HealthCache, refresh_executor_health
from cs2bot.utils.money import CENT, quantize_money
from cs2bot.utils.time import WARSAW, ensure_utc, utcnow

log = logging.getLogger(__name__)

ACTOR = "settlement"
DEFAULT_GRACE = timedelta(minutes=30)
#: A bet the bookmaker still shows open this long after its match finished is flagged.
OPEN_AT_BOOKMAKER_FLAG_AFTER = timedelta(hours=12)
#: History rows show minutes; a row may carry a time up to this much before the attempt.
HISTORY_TIME_SLACK = timedelta(minutes=2)
#: ``bets.extra`` flag set on a PLACING bet marked FAILED because the bookmaker's bet history did not
#: show it after the grace period: the bet may still exist (the executor skips unreadable history
#: rows), so ``services.betting`` never decides this outcome again automatically.
UNCONFIRMED_NOT_FOUND_KEY = "unconfirmed_not_found"
#: ``bets.extra`` key holding ``[team1_id, team2_id]`` of the match when the bet was created (written by
#: ``services.betting``). The selection key ("team1", a handicap line ...) only means something in that
#: orientation: if a source later replaces a participant, the local resolver must not settle the bet
#: against the new teams (final review) - it stays UNKNOWN (alert) unless Betclic's history settles it.
TEAMS_AT_PLACEMENT_KEY = "teams_at_placement"

_BOOKMAKER_OUTCOME = {
    BookmakerBetStatus.WON: SettlementOutcome.WON,
    BookmakerBetStatus.LOST: SettlementOutcome.LOST,
    BookmakerBetStatus.VOID: SettlementOutcome.VOID,
}
FINISHED_STATUSES = (MatchStatus.FINISHED, MatchStatus.CANCELED)


@dataclass
class SettlementReport:
    started_at: datetime
    history_rows: int | None = None
    #: True when the bet history was needed (open real bets) but could not be read.
    history_failed: bool = False
    placing_confirmed: int = 0
    placing_failed: int = 0
    placing_open: int = 0
    settled_real: int = 0
    settled_paper: int = 0
    won: int = 0
    lost: int = 0
    void: int = 0
    unknown: int = 0
    disagreements: int = 0
    clv_updated: int = 0
    health_ok: bool | None = None
    reconcile: dict[str, Any] | None = None
    alerts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def items(self) -> int:
        return self.settled_real + self.settled_paper + self.placing_confirmed + self.placing_failed

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "history_rows": self.history_rows,
            "history_failed": self.history_failed,
            "placing_confirmed": self.placing_confirmed,
            "placing_failed": self.placing_failed,
            "placing_open": self.placing_open,
            "settled_real": self.settled_real,
            "settled_paper": self.settled_paper,
            "won": self.won,
            "lost": self.lost,
            "void": self.void,
            "unknown": self.unknown,
            "disagreements": self.disagreements,
            "clv_updated": self.clv_updated,
            "health_ok": self.health_ok,
            "reconcile": self.reconcile,
            "alerts": self.alerts[:20],
            "notes": self.notes[:20],
            "errors": self.errors[:20],
        }


# ---------------------------------------------------------------------------------------- helpers


def _norm(text: str | None) -> str:
    from cs2bot.executor.textutils import normalise_text

    return normalise_text(text or "")


def _names_fit(shown: str | None, wanted: str | None) -> bool:
    from cs2bot.executor.textutils import names_compatible

    return names_compatible(_norm(shown), _norm(wanted))


def _attempt_time(bet: Bet) -> datetime | None:
    raw = (bet.extra or {}).get(ledger.PLACEMENT_ATTEMPTED_AT_KEY)
    if isinstance(raw, str):
        try:
            return ensure_utc(datetime.fromisoformat(raw))
        except ValueError:
            pass
    if bet.placed_at is not None:
        return ensure_utc(bet.placed_at)
    cycle = (bet.extra or {}).get("cycle_at")
    if isinstance(cycle, str):
        try:
            return ensure_utc(datetime.fromisoformat(cycle))
        except ValueError:
            pass
    return None


def history_row_latest_time(placed_at: datetime) -> datetime:
    """The latest instant a history row shown with ``placed_at`` may really have been placed.

    Betclic's history shows minutes or only the day; the executor parses the START of that minute /
    day (``cs2bot.executor.betclic``): local (Warsaw) midnight stands for the whole day, zero seconds
    for the whole minute. Without this a bet shown with its date only would never match the
    unconfirmed placement it belongs to, be marked FAILED after the grace period, and its exposure
    released although the money is at risk.
    """
    local = ensure_utc(placed_at).astimezone(WARSAW)
    if (local.hour, local.minute, local.second) == (0, 0, 0):
        return (local + timedelta(days=1)).astimezone(UTC)  # wall-clock day (DST days have 23 / 25 h)
    if local.second == 0:
        return ensure_utc(placed_at) + timedelta(minutes=1)
    return ensure_utc(placed_at)


def match_history_record(
    bet: Bet, records: Sequence[BookmakerBetRecord], *, claimed: set[str]
) -> tuple[BookmakerBetRecord | None, int]:
    """(record, number of candidates) for a bet: by bet id, else stake + selection (+ market) + time.

    ``claimed`` holds bookmaker bet ids that already belong to other bets. Returns ``(None, n)`` when
    nothing or more than one row fits (``n`` = number of fitting rows). A row fits in time when it may
    have been placed at or after the attempt (minute / day resolution of the history, see
    :func:`history_row_latest_time`, plus ``HISTORY_TIME_SLACK`` for clock differences).
    """
    if bet.external_bet_id:
        hits = [r for r in records if r.external_bet_id == bet.external_bet_id]
        return (hits[0], 1) if hits else (None, 0)
    attempt = _attempt_time(bet)
    fits: list[BookmakerBetRecord] = []
    for r in records:
        if r.external_bet_id and r.external_bet_id in claimed:
            continue
        if r.stake is None or r.stake != bet.stake:
            continue
        if not _names_fit(r.selection_name, bet.selection_raw):
            continue
        if r.market_name and not _names_fit(r.market_name, bet.market_raw):
            continue
        if (
            attempt is not None
            and r.placed_at is not None
            and history_row_latest_time(r.placed_at) + HISTORY_TIME_SLACK < attempt
        ):
            continue
        fits.append(r)
    if len(fits) == 1:
        return fits[0], 1
    return None, len(fits)


def _player_teams(session: Session, match: Match) -> dict[int, TeamSlot]:
    out: dict[int, TeamSlot] = {}
    for player_id, team_id in session.execute(
        select(MatchLineup.player_id, MatchLineup.team_id).where(MatchLineup.match_id == match.id)
    ):
        if team_id == match.team1_id:
            out[player_id] = TeamSlot.TEAM1
        elif team_id == match.team2_id:
            out[player_id] = TeamSlot.TEAM2
    return out


def _kickoff(session: Session, bet: Bet, match: Match | None) -> datetime | None:
    start = match.scheduled_at if match is not None else None
    if bet.selection_id is not None:
        ev_start = session.scalar(
            select(BookmakerEvent.starts_at)
            .join(BookmakerMarket, BookmakerMarket.event_id == BookmakerEvent.id)
            .join(BookmakerSelection, BookmakerSelection.market_id == BookmakerMarket.id)
            .where(BookmakerSelection.id == bet.selection_id)
        )
        if ev_start is not None and (start is None or ev_start < start):
            start = ev_start
    return ensure_utc(start) if start is not None else None


def _same_outcome(a: SettlementOutcome, b: SettlementOutcome) -> bool:
    return a == b or (a.is_refund and b.is_refund)


def _alert(
    session: Session, report: SettlementReport, action: str, message: str, details: dict[str, Any]
) -> None:
    report.alerts.append(message)
    log.warning("settlement alert: %s", message)
    db_state.audit(session, actor=ACTOR, action=action, details={"message": message, **details})


#: ``bets.extra`` key listing the alert actions already raised for a bet (see :func:`_alert_once`).
ALERTS_KEY = "settlement_alerts"


def _alert_once(
    session: Session,
    report: SettlementReport,
    bet: Bet,
    action: str,
    message: str,
    details: dict[str, Any],
) -> None:
    """Like :func:`_alert`, but once per bet and action: a bet that stays UNKNOWN (or ambiguous, or
    cashed out) would otherwise write the same audit row on every settlement pass, i.e. every few
    minutes for as long as it stays open."""
    sent = list((bet.extra or {}).get(ALERTS_KEY) or [])
    if action in sent:
        report.notes.append(message)
        return
    _alert(session, report, action, message, details)
    bet.extra = {**dict(bet.extra or {}), ALERTS_KEY: [*sent, action]}


class _Actuals:
    """Per-run cache of match actuals and lineups."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._cache: dict[int, tuple[MatchActuals | None, dict[int, TeamSlot]]] = {}

    def get(self, match: Match) -> tuple[MatchActuals | None, dict[int, TeamSlot]]:
        if match.id not in self._cache:
            try:
                actuals = datasets.load_match_actuals(self.session, match.id)
            except Exception as exc:
                log.warning("could not load the result of match %s: %s", match.id, exc)
                actuals = None
            self._cache[match.id] = (actuals, _player_teams(self.session, match))
        return self._cache[match.id]


def _local_resolution(
    session: Session, bet: Bet, match: Match | None, actuals_cache: _Actuals, settings: Settings
) -> settlement.Resolution | None:
    """The local resolver's verdict, or None when the match is not finished (nothing to resolve yet)."""
    if match is None or match.status not in FINISHED_STATUSES:
        return None
    teams = (bet.extra or {}).get(TEAMS_AT_PLACEMENT_KEY)
    if isinstance(teams, list) and len(teams) == 2 and teams != [match.team1_id, match.team2_id]:
        return settlement.Resolution(
            SettlementOutcome.UNKNOWN,
            f"the match teams changed since the bet was placed (then {teams[0]} / {teams[1]}, now "
            f"{match.team1_id} / {match.team2_id}) - settle it from Betclic's history or by hand",
        )
    actuals, player_teams = actuals_cache.get(match)
    if actuals is None:
        return settlement.Resolution(SettlementOutcome.UNKNOWN, "no result data for the match")
    return settlement.resolve_bet(bet, actuals, betclic=settings.betclic, player_teams=player_teams)


# ------------------------------------------------------------------------------------ the service


def settle_bets(
    session_factory: sessionmaker[Session],
    settings: Settings,
    executor: Any | None = None,
    *,
    now: datetime | None = None,
    grace: timedelta = DEFAULT_GRACE,
    health_cache: HealthCache | None = None,
    check_health: bool = True,
) -> SettlementReport:
    """Run one settlement pass (see the module docstring). One broken bet never aborts the pass."""
    now = ensure_utc(now or utcnow())
    report = SettlementReport(started_at=now)
    if executor is not None and check_health:
        health = refresh_executor_health(session_factory, executor, health_cache)
        report.health_ok = health.ok

    records = _fetch_history(session_factory, executor, report)

    with session_factory() as session:
        claimed = {
            ext
            for ext in session.scalars(select(Bet.external_bet_id).where(Bet.external_bet_id.is_not(None)))
            if ext
        }
        placing_ids = list(
            session.scalars(select(Bet.id).where(Bet.status == BetStatus.PLACING).order_by(Bet.id))
        )
    for bet_id in placing_ids:
        _guarded(
            session_factory,
            report,
            bet_id,
            {BetStatus.PLACING},
            _reconcile_placing,
            records,
            claimed,
            now,
            grace,
            settings,
        )

    with session_factory() as session:
        open_ids = list(
            session.scalars(select(Bet.id).where(Bet.status == BetStatus.PLACED).order_by(Bet.id))
        )
        paper_ids = list(
            session.scalars(
                select(Bet.id)
                .where(Bet.status == BetStatus.DRY_RUN, Bet.settled_at.is_(None))
                .order_by(Bet.id)
            )
        )
    for bet_id in open_ids:
        _guarded(
            session_factory,
            report,
            bet_id,
            {BetStatus.PLACED},
            _settle_placed,
            records,
            claimed,
            now,
            grace,
            settings,
        )
    for bet_id in paper_ids:
        _guarded(
            session_factory,
            report,
            bet_id,
            {BetStatus.DRY_RUN},
            _settle_paper,
            records,
            claimed,
            now,
            grace,
            settings,
        )

    try:
        _update_clv(session_factory, report, now=now)
    except Exception as exc:  # closing odds are analytics: never let them block the reconciliation
        log.exception("closing odds / CLV update failed")
        report.errors.append(f"clv: {type(exc).__name__}: {exc}")
    _reconcile_balance(session_factory, settings, executor, report, now=now)
    return report


def _guarded(
    session_factory: sessionmaker[Session],
    report: SettlementReport,
    bet_id: int,
    expected: set[BetStatus],
    fn: Any,
    *args: Any,
) -> None:
    """Run one step for one bet in its own transaction; an error is recorded, never raised.

    The row is read ``FOR UPDATE`` (PostgreSQL; ignored by SQLite) and skipped when its status is no
    longer ``expected``: a second settlement running at the same time (``cs2bot settle`` next to
    ``cs2bot up``) waits for the first and then sees the bet already settled, so a payout can never
    be booked twice (the ledger's one-entry-per-bet rule is enforced in code, not by the database).
    """
    try:
        with session_factory() as session:
            bet = session.get(Bet, bet_id, with_for_update=True)
            if bet is None or bet.status not in expected:
                return
            fn(session, bet, report, *args)
            session.commit()
    except Exception as exc:
        log.exception("settlement step failed for bet %s", bet_id)
        report.errors.append(f"bet {bet_id}: {type(exc).__name__}: {exc}")


def _fetch_history(
    session_factory: sessionmaker[Session], executor: Any | None, report: SettlementReport
) -> list[BookmakerBetRecord] | None:
    """The bookmaker's bet history since the oldest open real bet (None = unavailable / not needed)."""
    if executor is None or not hasattr(executor, "fetch_bet_history"):
        return None
    with session_factory() as session:
        open_bets = list(
            session.scalars(
                select(Bet).where(
                    Bet.status.in_([BetStatus.PLACING, BetStatus.PLACED]),
                    or_(Bet.dry_run.is_(False), Bet.status == BetStatus.PLACING),
                )
            )
        )
    times = [t for t in (_attempt_time(b) for b in open_bets) if t is not None]
    if not open_bets:
        return None
    since = min(times) - timedelta(minutes=5) if times else utcnow() - timedelta(days=14)
    try:
        records = list(executor.fetch_bet_history(since))
    except Exception as exc:
        report.history_failed = True
        report.notes.append(f"bet history unavailable: {type(exc).__name__}: {exc}")
        log.warning("bet history unavailable: %s", exc)
        return None
    report.history_rows = len(records)
    return records


def _reconcile_placing(
    session: Session,
    bet: Bet,
    report: SettlementReport,
    records: list[BookmakerBetRecord] | None,
    claimed: set[str],
    now: datetime,
    grace: timedelta,
    settings: Settings,
) -> None:
    if records is None:
        report.placing_open += 1
        return
    record, n = match_history_record(bet, records, claimed=claimed)
    if record is not None:
        result = PlacementResult(
            status=PlacementStatus.PLACED,
            placed_odds=record.odds,
            external_bet_id=record.external_bet_id,
            message="confirmed from the bookmaker's bet history",
            at=record.placed_at or _attempt_time(bet) or now,
        )
        ledger.apply_placement(session, bet, result, settings)
        if record.external_bet_id:
            claimed.add(record.external_bet_id)
        report.placing_confirmed += 1
        db_state.audit(
            session,
            actor=ACTOR,
            action="placing_confirmed",
            details={"bet_id": bet.id, "external_bet_id": record.external_bet_id},
        )
        return
    if n > 1:
        report.placing_open += 1
        _alert_once(
            session,
            report,
            bet,
            "placing_ambiguous",
            f"bet {bet.id}: {n} bet-history rows fit this unconfirmed placement - check Betclic by hand",
            {"bet_id": bet.id},
        )
        return
    attempt = _attempt_time(bet)
    if attempt is not None and now - attempt < grace:
        report.placing_open += 1
        return
    result = PlacementResult(
        status=PlacementStatus.FAILED,
        message=f"not found in the bookmaker's bet history {grace} after the attempt",
        at=now,
    )
    ledger.apply_placement(session, bet, result, settings)
    # The bet may still exist (an unreadable history row is skipped by the executor): never decide
    # this outcome again automatically (services.betting blocks it). A real bet that does exist
    # shows up as a balance shortfall in the reconciliation of this same pass (which pauses).
    bet.extra = {**dict(bet.extra or {}), UNCONFIRMED_NOT_FOUND_KEY: True}
    report.placing_failed += 1
    _alert(
        session,
        report,
        "placing_not_found",
        f"bet {bet.id} ({bet.selection_raw}, {bet.stake} PLN) was not found in the Betclic history and is "
        "marked FAILED - please verify on Betclic that no such bet exists (the bot will not bet this "
        "outcome again)",
        {"bet_id": bet.id, "idempotency_key": bet.idempotency_key},
    )


def _settle_placed(
    session: Session,
    bet: Bet,
    report: SettlementReport,
    records: list[BookmakerBetRecord] | None,
    claimed: set[str],
    now: datetime,
    grace: timedelta,
    settings: Settings,
) -> None:
    match = session.get(Match, bet.match_id) if bet.match_id is not None else None
    cache = _Actuals(session)
    record: BookmakerBetRecord | None = None
    if records is not None:
        record, _ = match_history_record(bet, records, claimed=claimed)
        if record is not None and not bet.external_bet_id and record.external_bet_id:
            bet.external_bet_id = record.external_bet_id
            claimed.add(record.external_bet_id)
    local = _local_resolution(session, bet, match, cache, settings)

    if record is not None and record.status in _BOOKMAKER_OUTCOME:
        outcome = _BOOKMAKER_OUTCOME[record.status]
        if (
            local is not None
            and local.outcome is not SettlementOutcome.UNKNOWN
            and not _same_outcome(local.outcome, outcome)
        ):
            report.disagreements += 1
            _alert(
                session,
                report,
                "settlement_disagreement",
                f"bet {bet.id}: Betclic settled it {outcome.value}, the local result says "
                f"{local.outcome.value} - the bookmaker's settlement is used",
                {
                    "bet_id": bet.id,
                    "bookmaker": outcome.value,
                    "local": local.outcome.value,
                    "reason": local.reason,
                },
            )
        payout, _ = ledger.settle_bet(session, bet, outcome, settings, at=now)
        bet.extra = {**dict(bet.extra or {}), "settled_by": "bookmaker"}
        if record.payout is not None:
            _follow_bookmaker_payout(session, report, bet, outcome, record.payout, payout, settings, now=now)
        _count(report, outcome, paper=False)
        return
    if record is not None and record.status is BookmakerBetStatus.CASHOUT:
        report.unknown += 1
        _alert_once(
            session,
            report,
            bet,
            "cashout_seen",
            f"bet {bet.id} shows a cash-out at Betclic (the bot never cashes out) - settle it by hand",
            {"bet_id": bet.id},
        )
        return
    if record is not None and record.status is BookmakerBetStatus.OPEN:
        if (
            match is not None
            and match.status in FINISHED_STATUSES
            and match.scheduled_at is not None
            and now - ensure_utc(match.scheduled_at) > OPEN_AT_BOOKMAKER_FLAG_AFTER
        ):
            report.notes.append(f"bet {bet.id}: match finished but Betclic still shows the bet open")
        return
    if local is None:
        return
    if local.outcome is SettlementOutcome.UNKNOWN:
        report.unknown += 1
        _alert_once(
            session,
            report,
            bet,
            "settlement_unknown",
            f"bet {bet.id} cannot be settled from the match data ({local.reason or 'unknown'}); "
            "it stays open",
            {"bet_id": bet.id, "reason": local.reason},
        )
        return
    ledger.settle_bet(session, bet, local.outcome, settings, at=now)
    bet.extra = {**dict(bet.extra or {}), "settled_by": "local", "settle_reason": local.reason}
    _count(report, local.outcome, paper=False)


def plausible_bookmaker_payout(bet: Bet, outcome: SettlementOutcome, amount: Decimal) -> bool:
    """Whether a payout read off the bet history can be true for this bet and outcome.

    A bookmaker never returns more than ``stake x odds`` on a win (that is before any tax), more than
    the stake on a void, or anything on a loss. A number outside that range is a misread row.
    """
    if not amount.is_finite() or amount < 0:
        return False
    stake = Decimal(bet.stake)
    if outcome is SettlementOutcome.LOST:
        return amount == 0
    if outcome.is_refund:
        return amount <= stake
    odds = Decimal(bet.placed_odds if bet.placed_odds is not None else bet.requested_odds)
    return Decimal(0) < amount <= quantize_money(stake * odds) + CENT


def _follow_bookmaker_payout(
    session: Session,
    report: SettlementReport,
    bet: Bet,
    outcome: SettlementOutcome,
    bookmaker_payout: Decimal,
    ledger_payout: Decimal,
    settings: Settings,
    *,
    now: datetime,
) -> None:
    """The bookmaker's payout is the truth for money: book the difference to the local amount.

    ``ledger.settle_bet`` books the locally computed payout (tax rules from the settings). When the
    history shows another plausible amount (e.g. Betclic keeps the stake tax on a void, or rounds the
    winnings tax differently) an ADJUSTMENT entry brings the ledger to what was really credited and
    the bet's ``payout`` / ``pnl`` follow, so P&L and loss limits use real money and the balance
    reconciliation does not pause the bot for it. An implausible amount (a misread row) is not used;
    the alert asks the owner to check, and the reconciliation compares with the real balance anyway.
    """
    paid = quantize_money(Decimal(bookmaker_payout))
    diff = paid - quantize_money(ledger_payout)
    if diff == 0:
        return
    details = {"bet_id": bet.id, "bookmaker_payout": str(paid), "ledger_payout": str(ledger_payout)}
    if not plausible_bookmaker_payout(bet, outcome, paid):
        _alert(
            session,
            report,
            "payout_mismatch",
            f"bet {bet.id}: the Betclic history shows an implausible payout {paid} for a {outcome.value} "
            f"bet (expected {ledger_payout}); the ledger keeps its own amount - please check",
            details,
        )
        return
    ledger.record_adjustment(
        session,
        diff,
        f"bet {bet.id}: Betclic paid {paid}, settlement computed {ledger_payout}",
        at=now,
    )
    bet.payout = paid
    bet.pnl = paid - quantize_money(Decimal(bet.stake))
    bet.extra = {**dict(bet.extra or {}), "local_payout": str(ledger_payout)}
    if abs(diff) > settings.executor.reconcile_tolerance:
        _alert(
            session,
            report,
            "payout_mismatch",
            f"bet {bet.id}: Betclic paid {paid}, the local tax rules gave {ledger_payout}; the ledger "
            "follows Betclic (check the tax settings if this repeats)",
            details,
        )


def _settle_paper(
    session: Session,
    bet: Bet,
    report: SettlementReport,
    records: list[BookmakerBetRecord] | None,
    claimed: set[str],
    now: datetime,
    grace: timedelta,
    settings: Settings,
) -> None:
    match = session.get(Match, bet.match_id) if bet.match_id is not None else None
    local = _local_resolution(session, bet, match, _Actuals(session), settings)
    if local is None:
        return
    if local.outcome is SettlementOutcome.UNKNOWN:
        report.unknown += 1
        return
    ledger.settle_bet(session, bet, local.outcome, settings, at=now)
    bet.extra = {**dict(bet.extra or {}), "settled_by": "local", "settle_reason": local.reason}
    _count(report, local.outcome, paper=True)


def _count(report: SettlementReport, outcome: SettlementOutcome, *, paper: bool) -> None:
    if paper:
        report.settled_paper += 1
    else:
        report.settled_real += 1
    if outcome is SettlementOutcome.WON:
        report.won += 1
    elif outcome is SettlementOutcome.LOST:
        report.lost += 1
    else:
        report.void += 1


def _update_clv(session_factory: sessionmaker[Session], report: SettlementReport, *, now: datetime) -> None:
    """Closing odds (last available price before kick-off) and CLV for bets whose event has started."""
    with session_factory() as session:
        bets = list(
            session.scalars(
                select(Bet).where(
                    Bet.closing_odds.is_(None),
                    Bet.selection_id.is_not(None),
                    Bet.status.in_(
                        [BetStatus.PLACED, BetStatus.WON, BetStatus.LOST, BetStatus.VOID, BetStatus.DRY_RUN]
                    ),
                )
            )
        )
        for bet in bets:
            try:
                match = session.get(Match, bet.match_id) if bet.match_id is not None else None
                kickoff = _kickoff(session, bet, match)
                if kickoff is None or kickoff > now or bet.selection_id is None:
                    continue
                closing = closing_odds(session, bet.selection_id, kickoff)
                if closing is None or closing <= 0:
                    continue
                ours = bet.placed_odds or bet.requested_odds
                bet.closing_odds = closing
                bet.clv = float(Decimal(ours) / Decimal(closing) - 1)
                report.clv_updated += 1
            except Exception as exc:
                report.errors.append(f"clv bet {bet.id}: {type(exc).__name__}: {exc}")
        session.commit()


def _open_real_bets(session: Session) -> bool:
    """Whether real bets are still open (placed or unconfirmed) after this pass."""
    return (
        session.scalar(
            select(Bet.id)
            .where(Bet.status.in_([BetStatus.PLACED, BetStatus.PLACING]), Bet.dry_run.is_(False))
            .limit(1)
        )
        is not None
    )


def _reconcile_balance(
    session_factory: sessionmaker[Session],
    settings: Settings,
    executor: Any | None,
    report: SettlementReport,
    *,
    now: datetime,
) -> None:
    """Compare the ledger with the bookmaker balance (live mode only; paper mode never touches money)."""
    if executor is None:
        return
    if settings.executor.dry_run:
        report.notes.append("dry-run mode: balance reconciliation skipped")
        return
    try:
        balance = executor.get_balance()
    except Exception as exc:
        report.notes.append(f"balance unavailable: {type(exc).__name__}: {exc}")
        return
    if balance is None:
        report.notes.append("balance unavailable (not logged in?)")
        return
    try:
        with session_factory() as session:
            if report.history_failed and _open_real_bets(session):
                # Betclic may already have credited bets the history would have settled; booking that
                # as a reconciliation now and the local settlement later would count it twice.
                report.notes.append("balance reconciliation skipped: the bet history was unavailable")
                return
            if session.scalar(select(BankrollLedger.id).limit(1)) is None:
                ledger.ensure_initialized(session, settings, at=now, opening_balance=balance)
            result = ledger.reconcile(
                session, balance, tolerance=settings.executor.reconcile_tolerance, actor=ACTOR, at=now
            )
            session.commit()
        report.reconcile = {
            "observed": str(result.observed_balance),
            "ledger": str(result.ledger_balance),
            "difference": str(result.difference),
            "flagged": result.flagged,
            "deferred": result.deferred,
            "paused": result.paused,
        }
        if result.flagged:
            report.alerts.append(f"balance mismatch {result.difference} PLN (paused={result.paused})")
    except Exception as exc:
        log.exception("balance reconciliation failed")
        report.errors.append(f"reconcile: {type(exc).__name__}: {exc}")
