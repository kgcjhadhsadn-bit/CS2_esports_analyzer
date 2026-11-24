"""Betting cycle: fresh prediction + fresh quotes -> decisions -> stakes -> bets at the bookmaker.

One cycle (:func:`run_betting_cycle`):

1. **Recovery.** ``PENDING`` bets left by an earlier, interrupted cycle were never handed to the
   executor (a bet is moved to ``PLACING`` - committed - *before* ``executor.place`` is called), so
   they are cancelled; the outcome may be decided again. ``PLACING`` bets may exist at the bookmaker:
   they stay open, count as exposure and block their outcome until settlement reconciles them with
   the bet history. They are never sent again.
2. **Bankroll.** The ledger is opened from the bookmaker balance when it is empty (falls back to
   ``bankroll.starting_bankroll``); ``engine.bankroll.compute_state`` + ``check_loss_limits`` (a loss
   limit pauses the bot).
3. **Per match** with a fresh latest prediction (``match_analyses``, same model version as the active
   bundle, not older than ``max_prediction_age``) and fresh quotes: price every quote, ``ev.price``,
   ``decision.evaluate`` (all PLAN.md section 6 checks), then two extra rules: never two selections of
   the same market (existing bets included) and outcomes in descending EV order, each sized with
   ``bankroll.size_decision`` against the state that already contains the previous bets, so the
   per-bet, per-match exposure, daily and bets-per-day caps hold. Every decision is written to
   ``bet_decisions`` (rejections with their reasons; unchanged rejections are not repeated within an
   hour).
4. **Placement** of each accepted stake: ``bets`` row (``PENDING``, deterministic idempotency key from
   match + market + selection + mode + attempt) committed; ``db.state.is_trading_halted`` read fresh;
   ``PLACING`` committed; ``executor.place``; ``engine.ledger.apply_placement`` maps the result
   (``domain.betting.bet_status_after_placement``) and writes the ledger. ``LOGIN_REQUIRED`` /
   ``SELECTOR_ERROR`` pause trading with a clear reason (dashboard alert); ``UNCERTAIN`` keeps the bet
   ``PLACING`` and raises an audit alert; it is never retried.

Dry-run bets (``settings.executor.dry_run`` or ``dry_run=True``) are recorded with status ``DRY_RUN``
and never touch the ledger or the loss limits (``compute_state`` ignores them); within a cycle their
stakes are still reserved so the per-match cap applies to paper bets too.

Cycles in one process are serialised by a lock (``compute_state`` takes no DB locks); do not run
``cs2bot bet run`` while ``cs2bot up`` is betting.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from cs2bot import datasets
from cs2bot.config import Settings
from cs2bot.db import state as db_state
from cs2bot.db.models import Bet, BetDecisionRow, BookmakerEvent, Match, MatchAnalysis, Prediction
from cs2bot.domain.betting import (
    BankrollState,
    BetDecision,
    BetOrder,
    PlacementResult,
    PlacementStatus,
    PricedOutcome,
)
from cs2bot.domain.enums import BetStatus, MatchStatus
from cs2bot.domain.predictions import MatchPrediction
from cs2bot.engine import bankroll, decision, ledger
from cs2bot.engine.decision import NON_BLOCKING_BET_STATUSES, ReasonCode, make_reason
from cs2bot.engine.ev import price_all
from cs2bot.engine.pricing import price_quotes
from cs2bot.odds import latest_quotes
from cs2bot.services.prediction import write_outcome_predictions, write_rationale
from cs2bot.services.registry import ModelBundle
from cs2bot.utils.time import ensure_utc, utcnow

log = logging.getLogger(__name__)

ACTOR = "betting"
#: PENDING rows older than this (by the cycle clock) were left by an interrupted cycle.
STALE_PENDING_AFTER = timedelta(minutes=2)
DEFAULT_MAX_PREDICTION_AGE = timedelta(hours=8)
#: An unchanged rejection of the same outcome is written again only after this long.
DECISION_REPEAT_AFTER = timedelta(hours=1)
PAUSING_STATUSES = frozenset({PlacementStatus.LOGIN_REQUIRED, PlacementStatus.SELECTOR_ERROR})

_CYCLE_LOCK = threading.Lock()


@dataclass
class BetSummary:
    bet_id: int
    match_id: int
    spec_key: str
    selection_key: str
    stake: Decimal
    odds: Decimal
    status: str
    placement: str
    message: str = ""


@dataclass
class BettingReport:
    started_at: datetime
    dry_run: bool
    matches_considered: int = 0
    matches_with_quotes: int = 0
    decisions: int = 0
    decisions_written: int = 0
    accepted: int = 0
    bets_created: int = 0
    placed: int = 0
    dry_run_ok: int = 0
    failed: int = 0
    uncertain: int = 0
    cancelled: int = 0
    stale_pending_cancelled: int = 0
    halted_reason: str | None = None
    paused_reason: str | None = None
    loss_limit_hit: bool = False
    skipped: bool = False
    bets: list[BetSummary] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def items(self) -> int:
        return self.bets_created

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "dry_run": self.dry_run,
            "matches_considered": self.matches_considered,
            "matches_with_quotes": self.matches_with_quotes,
            "decisions": self.decisions,
            "decisions_written": self.decisions_written,
            "accepted": self.accepted,
            "bets_created": self.bets_created,
            "placed": self.placed,
            "dry_run_ok": self.dry_run_ok,
            "failed": self.failed,
            "uncertain": self.uncertain,
            "cancelled": self.cancelled,
            "stale_pending_cancelled": self.stale_pending_cancelled,
            "halted_reason": self.halted_reason,
            "paused_reason": self.paused_reason,
            "loss_limit_hit": self.loss_limit_hit,
            "skipped": self.skipped,
            "bets": [
                {
                    "bet_id": b.bet_id,
                    "match_id": b.match_id,
                    "spec_key": b.spec_key,
                    "selection": b.selection_key,
                    "stake": str(b.stake),
                    "odds": str(b.odds),
                    "status": b.status,
                    "placement": b.placement,
                }
                for b in self.bets
            ],
            "notes": self.notes[:20],
            "errors": self.errors[:20],
        }


class _StopPlacing(Exception):
    """Internal: stop placing for the rest of the cycle (halt, login required, selector error)."""


# ---------------------------------------------------------------------------------------- helpers


def outcome_hash(match_id: int, spec_key: str, selection_key: str) -> str:
    return hashlib.sha256(f"{match_id}|{spec_key}#{selection_key}".encode()).hexdigest()[:20]


def idempotency_key(
    session: Session, match_id: int, spec_key: str, selection_key: str, *, dry_run: bool
) -> str:
    """Deterministic key: match + market + selection + mode + attempt number.

    The attempt number is the count of earlier rows with the same prefix; only FAILED / CANCELLED
    rows (no money at risk) let an outcome be decided again, so a retry after a failure gets a new key
    while a crash can never produce a second key for a bet that may exist.
    """
    mode = "dry" if dry_run else "live"
    prefix = f"cs2b-{mode}-{match_id}-{outcome_hash(match_id, spec_key, selection_key)}-"
    count = session.scalar(select(func.count(Bet.id)).where(Bet.idempotency_key.like(f"{prefix}%"))) or 0
    return f"{prefix}{count + 1}"


def _blocking_bets(session: Session, match_id: int, *, include_dry_run: bool) -> list[Bet]:
    rows = session.scalars(select(Bet).where(Bet.match_id == match_id))
    return [
        b
        for b in rows
        if b.status not in NON_BLOCKING_BET_STATUSES
        and (include_dry_run or not (b.dry_run or b.status is BetStatus.DRY_RUN))
    ]


def _reject(dec: BetDecision, reason: str) -> BetDecision:
    return dec.model_copy(
        update={"accepted": False, "reasons": [*dec.reasons, reason], "stake": Decimal("0")}
    )


def enforce_one_selection_per_market(
    decisions: Sequence[BetDecision], *, bet_specs: dict[str, set[str]]
) -> list[BetDecision]:
    """Never back two selections of the same market (same ``spec_key``).

    ``bet_specs`` maps spec key -> selections that already have a blocking bet on this match. An accepted
    decision on a market with a bet on *another* selection is rejected; among accepted decisions of one
    market only the highest EV stays accepted.
    """
    out: list[BetDecision] = []
    for dec in decisions:
        spec_key = dec.priced.quote.spec.key()
        sel = dec.priced.quote.selection_key
        others = bet_specs.get(spec_key, set()) - {sel}
        if dec.accepted and others:
            dec = _reject(
                dec,
                make_reason(
                    ReasonCode.DUPLICATE,
                    f"another selection of this market already has a bet ({', '.join(sorted(others))})",
                ),
            )
        out.append(dec)
    best: dict[str, BetDecision] = {}
    for dec in out:
        if dec.accepted:
            key = dec.priced.quote.spec.key()
            if key not in best or dec.priced.ev > best[key].priced.ev:
                best[key] = dec
    final: list[BetDecision] = []
    for dec in out:
        key = dec.priced.quote.spec.key()
        if dec.accepted and best.get(key) is not dec:
            dec = _reject(
                dec,
                make_reason(
                    ReasonCode.DUPLICATE,
                    f"selection {best[key].priced.quote.selection_key} of the same market has a higher EV",
                ),
            )
        final.append(dec)
    return final


def _latest_prediction_id(session: Session, match_id: int, spec_key: str, selection_key: str) -> int | None:
    return session.scalar(
        select(Prediction.id)
        .where(
            Prediction.match_id == match_id,
            Prediction.spec_key == spec_key,
            Prediction.selection_key == selection_key,
            Prediction.is_latest.is_(True),
        )
        .order_by(Prediction.id.desc())
        .limit(1)
    )


def _reason_codes(reasons: Sequence[Any]) -> list[str]:
    return sorted({decision.reason_code(str(r)) for r in reasons})


def write_decision(
    session: Session,
    dec: BetDecision,
    *,
    now: datetime,
    extra: dict[str, Any] | None = None,
    force: bool = False,
) -> BetDecisionRow | None:
    """Persist one decision; an unchanged rejection seen within the last hour is not written again."""
    quote = dec.priced.quote
    spec_key = quote.spec.key()
    if not force and not dec.accepted:
        last = session.scalar(
            select(BetDecisionRow)
            .where(
                BetDecisionRow.match_id == dec.match_id,
                BetDecisionRow.spec_key == spec_key,
                BetDecisionRow.selection_key == quote.selection_key,
            )
            .order_by(BetDecisionRow.id.desc())
            .limit(1)
        )
        if (
            last is not None
            and not last.accepted
            and _reason_codes(last.reasons or []) == _reason_codes(dec.reasons)
            and last.odds == quote.odds
            and abs(last.p_win - dec.priced.outcome.p_win) < 1e-9
            and now - ensure_utc(last.decided_at) < DECISION_REPEAT_AFTER
        ):
            return None
    row = BetDecisionRow(
        match_id=dec.match_id,
        prediction_id=_latest_prediction_id(session, dec.match_id, spec_key, quote.selection_key),
        selection_id=quote.selection_id,
        spec_key=spec_key,
        selection_key=quote.selection_key,
        odds=quote.odds,
        odds_captured_at=quote.captured_at,
        decided_at=dec.decided_at,
        accepted=dec.accepted,
        reasons=list(dec.reasons),
        p_win=dec.priced.outcome.p_win,
        sigma=dec.priced.outcome.sigma,
        ev=dec.priced.ev,
        kelly_fraction=dec.kelly_fraction_used,
        proposed_stake=dec.stake if dec.stake > 0 else None,
        settings_snapshot={"notes": list(dec.notes), **(extra or {})},
    )
    session.add(row)
    session.flush()
    return row


def with_open_paper_bets(session: Session, state: BankrollState) -> BankrollState:
    """The state with every open paper bet reserved, so dry-run caps hold across cycles.

    ``compute_state`` ignores dry-run bets (they must never touch real limits), so without this a
    second dry-run cycle would see an empty per-match exposure and back the next outcome of a match
    whose paper exposure is already at the cap. Open = status ``DRY_RUN``, not settled on paper, and
    its match not finished / cancelled (a paper bet that can never be settled must not block paper
    betting forever). Only paper bets created since the local day start count towards the daily
    bet count.
    """
    rows = session.execute(
        select(Bet.match_id, Bet.stake, Bet.created_at)
        .outerjoin(Match, Match.id == Bet.match_id)
        .where(
            Bet.status == BetStatus.DRY_RUN,
            Bet.settled_at.is_(None),
            or_(Match.id.is_(None), Match.status.not_in([MatchStatus.FINISHED, MatchStatus.CANCELED])),
        )
    ).all()
    if not rows:
        return state
    exposure = dict(state.exposure_by_match)
    total = Decimal("0")
    today = 0
    for match_id, stake, created_at in rows:
        if stake is None or stake <= 0:
            continue
        total += stake
        if match_id is not None:
            exposure[match_id] = exposure.get(match_id, Decimal("0")) + stake
        if state.day_start is None or created_at is None or ensure_utc(created_at) >= state.day_start:
            today += 1
    return state.model_copy(
        update={
            "balance": state.balance - total,
            "open_stakes_today": state.open_stakes_today + total,
            "open_stakes_week": state.open_stakes_week + total,
            "exposure_by_match": exposure,
            "reserved_stakes": state.reserved_stakes + total,
            "bets_placed_today": state.bets_placed_today + today,
        }
    )


def _fresh_halt(session_factory: sessionmaker[Session]) -> tuple[bool, str | None]:
    """Kill switch / pause read with a NEW session (sessions do not expire cached rows on commit)."""
    try:
        with session_factory() as s:
            return db_state.is_trading_halted(s)
    except Exception as exc:  # cannot read the flags -> do not move money
        return True, f"could not read the trading state: {type(exc).__name__}: {exc}"


def cancel_stale_pending(
    session: Session, *, now: datetime, older_than: timedelta = STALE_PENDING_AFTER
) -> int:
    """Cancel PENDING bets an interrupted cycle left behind (they never reached the executor)."""
    n = 0
    for bet in session.scalars(select(Bet).where(Bet.status == BetStatus.PENDING)):
        created = (bet.extra or {}).get("cycle_at")
        try:
            created_at = ensure_utc(datetime.fromisoformat(created)) if created else None
        except ValueError:
            created_at = None
        if created_at is not None and now - created_at < older_than:
            continue
        bet.status = BetStatus.CANCELLED
        bet.error = "not sent to the bookmaker: the bot stopped before placement (stale PENDING row)"
        n += 1
    if n:
        db_state.audit(session, actor=ACTOR, action="stale_pending_cancelled", details={"count": n})
        session.flush()
    return n


def _ensure_ledger(session: Session, settings: Settings, executor: Any | None, *, now: datetime) -> None:
    """Open an empty ledger from the bookmaker balance (else ``starting_bankroll``)."""
    if ledger.current_balance(session) != Decimal("0.00") or _ledger_has_rows(session):
        return
    opening: Decimal | None = None
    if executor is not None:
        try:
            opening = executor.get_balance()
        except Exception as exc:
            log.warning("could not read the bookmaker balance to open the ledger: %s", exc)
    ledger.ensure_initialized(session, settings, at=now, opening_balance=opening)


def _ledger_has_rows(session: Session) -> bool:
    from cs2bot.db.models import BankrollLedger

    return session.scalar(select(BankrollLedger.id).limit(1)) is not None


def candidate_analyses(
    session: Session,
    settings: Settings,
    *,
    now: datetime,
    model_version: str | None,
    max_prediction_age: timedelta,
) -> list[MatchAnalysis]:
    """Latest analyses of scheduled matches inside the betting window that are fresh enough."""
    horizon = now + timedelta(hours=max(settings.decision.max_hours_before_start, 1))
    stmt = (
        select(MatchAnalysis)
        .join(Match, Match.id == MatchAnalysis.match_id)
        .where(
            MatchAnalysis.is_latest.is_(True),
            MatchAnalysis.created_at >= now - max_prediction_age,
            Match.status.in_([MatchStatus.SCHEDULED, MatchStatus.UNKNOWN]),
            Match.scheduled_at.is_not(None),
            Match.scheduled_at > now,
            Match.scheduled_at <= horizon,
        )
        .order_by(Match.scheduled_at, Match.id)
    )
    rows = list(session.scalars(stmt))
    if model_version is not None:
        rows = [r for r in rows if r.model_version == model_version]
    return rows


def _event_info(session: Session, event_id: int, match: Match | None) -> tuple[datetime | None, str]:
    event = session.get(BookmakerEvent, event_id)
    starts: datetime | None = match.scheduled_at if match is not None else None
    name = ""
    if event is not None:
        name = event.name or ""
        if event.starts_at is not None and (starts is None or event.starts_at < starts):
            starts = event.starts_at
    return (ensure_utc(starts) if starts is not None else None), name


# ------------------------------------------------------------------------------------- the cycle


def run_betting_cycle(
    session_factory: sessionmaker[Session],
    settings: Settings,
    executor: Any | None,
    bundle: ModelBundle | None,
    *,
    now: datetime | None = None,
    dry_run: bool | None = None,
    analyzer: Any | None = None,
    max_prediction_age: timedelta = DEFAULT_MAX_PREDICTION_AGE,
    match_ids: Sequence[int] | None = None,
) -> BettingReport:
    """Run one betting cycle (see the module docstring) and return what happened.

    ``executor`` is a :class:`~cs2bot.executor.BetExecutor`; with ``None`` (or the executor disabled in
    the settings) decisions are recorded but no bet is created. Real money moves only when neither
    ``dry_run`` nor ``settings.executor.dry_run`` is set. ``bundle`` (optional) requires predictions of
    the active model version.
    """
    now = ensure_utc(now or utcnow())
    dry = bool(settings.executor.dry_run or dry_run)
    report = BettingReport(started_at=now, dry_run=dry)
    if not _CYCLE_LOCK.acquire(blocking=False):
        report.skipped = True
        report.notes.append("another betting cycle is running in this process")
        return report
    try:
        _run_cycle(
            session_factory,
            settings,
            executor if settings.executor.enabled else None,
            bundle,
            report,
            now=now,
            dry=dry,
            analyzer=analyzer,
            max_prediction_age=max_prediction_age,
            match_ids=match_ids,
        )
    finally:
        _CYCLE_LOCK.release()
    return report


def _run_cycle(
    session_factory: sessionmaker[Session],
    settings: Settings,
    executor: Any | None,
    bundle: ModelBundle | None,
    report: BettingReport,
    *,
    now: datetime,
    dry: bool,
    analyzer: Any | None,
    max_prediction_age: timedelta,
    match_ids: Sequence[int] | None,
) -> None:
    if executor is None:
        report.notes.append("no executor: decisions are recorded, no bets are created")
    with session_factory() as session:
        report.stale_pending_cancelled = cancel_stale_pending(session, now=now)
        _ensure_ledger(session, settings, executor, now=now)
        state = bankroll.compute_state(session, settings, now=now)
        report.loss_limit_hit = bankroll.check_loss_limits(session, state, settings, actor=ACTOR, now=now)
        session.commit()
        halted, reason = db_state.is_trading_halted(session)
        if halted:
            report.halted_reason = reason
        analyses = candidate_analyses(
            session,
            settings,
            now=now,
            model_version=bundle.version if bundle is not None else None,
            max_prediction_age=max_prediction_age,
        )
        if match_ids is not None:
            wanted = set(match_ids)
            analyses = [a for a in analyses if a.match_id in wanted]
        targets = [(a.id, a.match_id) for a in analyses]
    stop_placing = False
    for analysis_id, match_id in targets:
        report.matches_considered += 1
        try:
            stop_placing = _bet_match(
                session_factory,
                settings,
                executor,
                report,
                analysis_id=analysis_id,
                match_id=match_id,
                now=now,
                dry=dry,
                analyzer=analyzer,
                stop_placing=stop_placing,
            )
        except Exception as exc:
            log.exception("betting failed for match %s", match_id)
            report.errors.append(f"match {match_id}: {type(exc).__name__}: {exc}")
    with session_factory() as session:
        paused, pause_reason = db_state.get_pause(session)
        if paused:
            report.paused_reason = pause_reason


def _bet_match(
    session_factory: sessionmaker[Session],
    settings: Settings,
    executor: Any | None,
    report: BettingReport,
    *,
    analysis_id: int,
    match_id: int,
    now: datetime,
    dry: bool,
    analyzer: Any | None,
    stop_placing: bool,
) -> bool:
    """Decide, size and place the outcomes of one match. Returns True when placing must stop."""
    with session_factory() as session:
        analysis = session.get(MatchAnalysis, analysis_id)
        match = session.get(Match, match_id)
        if analysis is None or match is None:
            return stop_placing
        prediction = MatchPrediction.model_validate(analysis.payload)
        quotes = latest_quotes(session, match_id, max_age_s=settings.betclic.odds_max_age_s, now=now)
        if not quotes:
            return stop_placing
        report.matches_with_quotes += 1
        # make sure every quoted outcome has a predictions row (decisions link to it)
        write_outcome_predictions(
            session,
            prediction,
            settings,
            analysis_id=analysis.id,
            model_run_id=None,
            now=now,
            replace=False,
        )
        dec_settings = db_state.effective_decision_settings(session, settings)
        bank_settings = db_state.effective_bankroll_settings(session, settings)
        pairs = price_quotes(
            prediction, quotes, sigma_floor=settings.models.sigma_floor, betclic=settings.betclic
        )
        priced = price_all(pairs, settings)
        by_event: dict[int, list[PricedOutcome]] = defaultdict(list)
        for p in priced:
            by_event[p.quote.bookmaker_event_id].append(p)
        decisions: list[BetDecision] = []
        for event_id, items in by_event.items():
            info = decision.load_match_decision_info(
                session,
                match_id=match_id,
                bookmaker_event_id=event_id,
                prediction=prediction,
                now=now,
                include_dry_run_bets=dry,
            )
            decisions.extend(
                decision.evaluate_all(
                    items, match=info, settings=dec_settings, betclic=settings.betclic, now=now
                )
            )
        bet_specs: dict[str, set[str]] = defaultdict(set)
        for b in _blocking_bets(session, match_id, include_dry_run=dry):
            bet_specs[b.spec_key].add(b.selection_key)
        decisions = enforce_one_selection_per_market(decisions, bet_specs=bet_specs)
        report.decisions += len(decisions)

        rejected = [d for d in decisions if not d.accepted]
        accepted = sorted((d for d in decisions if d.accepted), key=lambda d: d.priced.ev, reverse=True)
        for dec in rejected:
            if write_decision(session, dec, now=now, extra={"dry_run": dry}) is not None:
                report.decisions_written += 1
        session.commit()

        placed_decisions: list[BetDecision] = []
        session.expire_all()
        state = bankroll.compute_state(session, settings, now=now)
        if dry:
            state = with_open_paper_bets(session, state)
        for dec in accepted:
            sized = bankroll.size_decision(dec, state, bank_settings, settings.betclic)
            if not sized.accepted or sized.stake <= 0 or executor is None or stop_placing:
                if sized.accepted and executor is None:
                    sized = _reject(sized, "no_executor: no bet executor is configured")
                elif sized.accepted and stop_placing:
                    sized = _reject(sized, "halted: placing stopped earlier in this cycle")
                if write_decision(session, sized, now=now, extra={"dry_run": dry}) is not None:
                    report.decisions_written += 1
                session.commit()
                continue
            report.accepted += 1
            min_odds = decision.min_acceptable_odds(
                sized.priced,
                settings=dec_settings,
                betclic=settings.betclic,
                odds_tolerance=settings.executor.odds_tolerance,
            )
            row = write_decision(
                session,
                sized,
                now=now,
                extra={"dry_run": dry, "min_acceptable_odds": str(min_odds)},
                force=True,
            )
            report.decisions_written += 1
            placed_decisions.append(sized)
            try:
                state = _place_one(
                    session,
                    session_factory,
                    settings,
                    executor,
                    report,
                    dec=sized,
                    decision_row_id=row.id if row is not None else None,
                    min_odds=min_odds,
                    match=match,
                    state=state,
                    now=now,
                    dry=dry,
                )
            except _StopPlacing as stop:
                report.notes.append(str(stop))
                stop_placing = True
        if placed_decisions:
            _update_rationale(
                session, analysis, prediction, placed_decisions, settings, analyzer=analyzer, now=now
            )
            session.commit()
    return stop_placing


def _place_one(
    session: Session,
    session_factory: sessionmaker[Session],
    settings: Settings,
    executor: Any,
    report: BettingReport,
    *,
    dec: BetDecision,
    decision_row_id: int | None,
    min_odds: Decimal,
    match: Match,
    state: BankrollState,
    now: datetime,
    dry: bool,
) -> BankrollState:
    """Create the bet row, place it and apply the result; returns the bankroll state to size the next bet."""
    quote = dec.priced.quote
    spec_key = quote.spec.key()
    starts_at, event_name = _event_info(session, quote.bookmaker_event_id, match)
    key = idempotency_key(session, match.id, spec_key, quote.selection_key, dry_run=dry)
    bet = Bet(
        decision_id=decision_row_id,
        match_id=match.id,
        selection_id=quote.selection_id,
        external_event_id=quote.external_event_id,
        market_raw=quote.market_raw_name,
        selection_raw=quote.selection_raw_name,
        spec_key=spec_key,
        selection_key=quote.selection_key,
        requested_odds=quote.odds,
        min_acceptable_odds=min_odds,
        stake=dec.stake,
        status=BetStatus.PENDING,
        dry_run=dry,
        idempotency_key=key,
    )
    money = ledger.bet_money_fields(dec.stake, quote.odds, settings)
    bet.tax = money["tax"]  # type: ignore[assignment]
    bet.potential_payout = money["potential_payout"]  # type: ignore[assignment]
    bet.extra = {
        **dict(money["extra"]),  # type: ignore[call-overload]
        "cycle_at": now.isoformat(),
        "event_url": quote.event_url,
        "event_name": event_name,
        "event_starts_at": starts_at.isoformat() if starts_at else None,
        "market_external_id": quote.market_external_id,
        "selection_external_id": quote.selection_external_id,
        "p_win": dec.priced.outcome.p_win,
        "ev": dec.priced.ev,
        "model_odds_captured_at": quote.captured_at.isoformat(),
    }
    session.add(bet)
    session.commit()  # the row exists (PENDING) before anything is sent
    report.bets_created += 1

    halted, reason = _fresh_halt(session_factory)
    if halted:
        bet.status = BetStatus.CANCELLED
        bet.error = f"not placed: {reason}"
        session.commit()
        report.cancelled += 1
        report.halted_reason = reason
        report.bets.append(_summary(bet, "cancelled", reason or ""))
        raise _StopPlacing(f"trading halted before placement: {reason}")

    bet.status = BetStatus.PLACING
    bet.extra = {**dict(bet.extra or {}), ledger.PLACEMENT_ATTEMPTED_AT_KEY: utcnow().isoformat()}
    session.commit()  # from here on the bet may exist at the bookmaker: never re-sent

    order = BetOrder(
        bet_id=bet.id,
        idempotency_key=bet.idempotency_key,
        external_event_id=quote.external_event_id,
        event_url=quote.event_url,
        market_external_id=quote.market_external_id,
        market_raw_name=quote.market_raw_name,
        selection_external_id=quote.selection_external_id,
        selection_raw_name=quote.selection_raw_name,
        requested_odds=quote.odds,
        min_acceptable_odds=min_odds,
        stake=dec.stake,
        dry_run=dry,
        event_starts_at=starts_at,
        event_name=event_name,
    )
    try:
        result = executor.place(order)
    except Exception as exc:  # unknown whether anything was clicked: treat as unconfirmed
        log.exception("executor.place raised for bet %s", bet.id)
        result = PlacementResult(
            status=PlacementStatus.UNCERTAIN,
            message=f"executor raised {type(exc).__name__}: {exc}",
            at=utcnow(),
        )
    try:
        status = ledger.apply_placement(session, bet, result, settings)
    except ledger.LedgerConflictError as exc:
        log.error("placement result for bet %s conflicts with the ledger: %s", bet.id, exc)
        db_state.audit(
            session, actor=ACTOR, action="placement_conflict", details={"bet_id": bet.id, "error": str(exc)}
        )
        session.commit()
        report.errors.append(f"bet {bet.id}: {exc}")
        return state
    if result.screenshot_path:
        bet.screenshot_path = result.screenshot_path
    bet.extra = {**dict(bet.extra or {}), "placement_message": result.message[:1000]}
    if status is BetStatus.FAILED:
        bet.error = f"{result.status.value}: {result.message}"[:2000]
        report.failed += 1
    elif status is BetStatus.PLACING:
        report.uncertain += 1
        db_state.audit(
            session,
            actor=ACTOR,
            action="bet_uncertain",
            details={
                "bet_id": bet.id,
                "message": result.message[:500],
                "idempotency_key": bet.idempotency_key,
            },
        )
    elif status is BetStatus.DRY_RUN:
        report.dry_run_ok += 1
    elif status is BetStatus.PLACED:
        report.placed += 1
        if "WARNING:" in result.message:
            db_state.audit(
                session,
                actor=ACTOR,
                action="bet_warning",
                details={"bet_id": bet.id, "message": result.message[:500]},
            )
    report.bets.append(_summary(bet, result.status.value, result.message))
    stop_reason: str | None = None
    if result.status in PAUSING_STATUSES:
        what = (
            "Betclic login required"
            if result.status is PlacementStatus.LOGIN_REQUIRED
            else "Betclic page layout changed (selector error)"
        )
        stop_reason = f"{what}: {result.message}"[:500]
        ledger.pause_trading(session, stop_reason, actor=ACTOR)
    session.commit()
    if stop_reason is not None:
        raise _StopPlacing(f"trading paused: {stop_reason}")
    if bet.dry_run or status is BetStatus.DRY_RUN:
        # paper bets are invisible to compute_state; reserve them locally so caps hold in this cycle
        return bankroll.reserve_stake(state, match.id, dec.stake)
    session.expire_all()
    return bankroll.compute_state(session, settings, now=now)


def _summary(bet: Bet, placement: str, message: str) -> BetSummary:
    return BetSummary(
        bet_id=bet.id,
        match_id=bet.match_id or 0,
        spec_key=bet.spec_key,
        selection_key=bet.selection_key,
        stake=bet.stake,
        odds=bet.requested_odds,
        status=bet.status.value,
        placement=placement,
        message=message[:300],
    )


def _update_rationale(
    session: Session,
    analysis: MatchAnalysis,
    prediction: MatchPrediction,
    decisions: Sequence[BetDecision],
    settings: Settings,
    *,
    analyzer: Any | None,
    now: datetime,
) -> None:
    """Rewrite the analysis rationale so it explains the bets actually taken (best effort)."""
    try:
        ctx = datasets.load_match_context(session, prediction.match_id, settings=settings, now=now)
        text, model = write_rationale(prediction, decisions, ctx, analyzer=analyzer, now=now)
        if text:
            analysis.rationale = text
            analysis.rationale_model = model
            session.flush()
    except Exception as exc:
        log.warning("could not update the rationale of match %s: %s", prediction.match_id, exc)
