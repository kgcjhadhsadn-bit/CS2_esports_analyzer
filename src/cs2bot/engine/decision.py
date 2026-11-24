"""Decision engine: bet only if ALL checks of PLAN.md section 6 pass.

:func:`evaluate` never stops at the first failure: every failing rule adds a reason, so the
dashboard can show the full "skipped because ..." list. A decision is ``accepted`` only when there
are no reasons. Each reason is ``"<code>: <human readable detail>"`` with ``<code>`` one of
:class:`ReasonCode` (use :func:`reason_code` to group/filter).

Rules (thresholds from :class:`cs2bot.config.DecisionSettings`; the probability / EV / sigma
thresholds can be overridden per market type through ``market_overrides[<MarketType value>]``):

* market type disabled (``disabled_markets``) or never bettable (``other``);
* selection unavailable at the bookmaker;
* odds outside ``[min_odds, max_odds]``;
* odds older than ``betclic.odds_max_age_s`` (or time-stamped in the future, a clock problem);
* outside the time window: less than ``min_minutes_before_start`` or more than
  ``max_hours_before_start`` before the scheduled start (unknown start time also fails);
* the match is no longer a plain scheduled match (live, finished, cancelled, postponed): this bot
  is pre-match only (``MatchDecisionInfo.match_status``, when known);
* ``p_win_given_settled < min_probability`` (the confidence check ignores refunded outcomes);
* ``EV < min_ev`` (per-unit EV after the stake tax, see :mod:`cs2bot.engine.ev`). A ``min_ev`` of -1
  or lower disables the check - the owner's rule since 2026-11-24 is "confidence only, odds ignored"
  (``DecisionSettings.min_ev = -1.0``); set 0.0 for a break-even guard;
* ``require_profitable_win`` (default on) and odds at which a WIN would pay back less than the gross
  stake after the stake tax (below 1.137 with 12 %; :func:`cs2bot.engine.ev.min_profitable_odds`):
  such a bet loses money whether it wins or loses. This is not an EV / value rule - a 1.28 favourite
  at 80 % still passes (final review, Lens C);
* ``require_trusted_calibration`` and a market type the calibration layer does not trust (only when
  the caller passes ``trusted_markets``; ``None`` = not known, no check);
* ``sigma > max_sigma``, where the pricing sigma (an uncertainty of the *unconditional* ``p_win``)
  is first put on the scale of the probability the confidence check uses:
  ``sigma / (1 - p_void - p_push)``. Without this, a market that is void half of the time (map 3
  of a BO3) would pass with twice the model uncertainty of a market that always settles;
* probabilities that are not a valid distribution (outside [0, 1], summing above 1, negative
  sigma, non-finite numbers) never pass;
* data completeness below ``min_data_completeness``;
* event link confidence below ``min_event_link_confidence`` or link status not AUTO / CONFIRMED;
* a context veto (Claude flag or manual) on the match;
* ``prefer_known_veto`` and a market tied to specific map slots (map-scoped markets, and player
  markets on a given map / maps) while the map veto is unknown -> "waiting for veto";
* duplicate: the same match + market spec + selection was already bet (see :func:`bet_key`).
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_UP, Decimal
from enum import StrEnum

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.config import BetclicSettings, DecisionSettings
from cs2bot.db.models import Bet, BookmakerEvent, ContextFlagRow, Match
from cs2bot.domain.betting import BetDecision, PricedOutcome
from cs2bot.domain.enums import BetStatus, LinkStatus, MatchStatus
from cs2bot.domain.markets import MarketSpec, MarketType
from cs2bot.domain.predictions import MatchPrediction, OutcomeProbability
from cs2bot.engine.ev import ODDS_STEP, min_odds_for_ev, min_profitable_odds
from cs2bot.utils.time import ensure_utc, utcnow

#: Link statuses that allow automatic betting.
BETTABLE_LINK_STATUSES: frozenset[LinkStatus] = frozenset({LinkStatus.AUTO, LinkStatus.CONFIRMED})
#: Odds time-stamped further than this in the future indicate a clock problem.
MAX_FUTURE_ODDS_SKEW_S = 300
#: Bet statuses that do NOT block betting the same outcome again (no money was ever at risk).
NON_BLOCKING_BET_STATUSES: frozenset[BetStatus] = frozenset({BetStatus.FAILED, BetStatus.CANCELLED})
#: Match statuses a pre-match bet may be placed in (UNKNOWN = the source did not say).
BETTABLE_MATCH_STATUSES: frozenset[MatchStatus] = frozenset({MatchStatus.SCHEDULED, MatchStatus.UNKNOWN})
_PROB_EPS = 1e-9
#: ``min_ev`` at or below this value disables the EV rule (EV per unit staked is never below -1).
EV_CHECK_DISABLED_AT = -1.0


def ev_check_enabled(min_ev: float) -> bool:
    """False when ``min_ev`` switches the EV rule off (confidence-only betting, odds ignored)."""
    return min_ev > EV_CHECK_DISABLED_AT


class ReasonCode(StrEnum):
    MARKET_DISABLED = "market_disabled"
    SELECTION_UNAVAILABLE = "selection_unavailable"
    ODDS_OUT_OF_RANGE = "odds_out_of_range"
    ODDS_STALE = "odds_stale"
    OUTSIDE_TIME_WINDOW = "outside_time_window"
    LOW_PROBABILITY = "low_probability"
    LOW_EV = "low_ev"
    HIGH_SIGMA = "high_sigma"
    LOW_DATA_COMPLETENESS = "low_data_completeness"
    WEAK_EVENT_LINK = "weak_event_link"
    CONTEXT_VETO = "context_veto"
    WAITING_FOR_VETO = "waiting_for_veto"
    DUPLICATE = "duplicate"
    PRICING_MISMATCH = "pricing_mismatch"
    INVALID_NUMBERS = "invalid_numbers"
    MATCH_NOT_SCHEDULED = "match_not_scheduled"
    UNTRUSTED_CALIBRATION = "untrusted_calibration"
    UNPROFITABLE_WIN = "unprofitable_win"


def make_reason(code: ReasonCode, detail: str) -> str:
    return f"{code.value}: {detail}"


def reason_code(reason: str) -> str:
    """The ``<code>`` part of a reason string (``"low_ev: ..."`` -> ``"low_ev"``)."""
    return reason.split(":", 1)[0].strip()


class MatchDecisionInfo(BaseModel):
    """Match-level facts the decision rules need (build it with :func:`load_match_decision_info`)."""

    match_id: int
    scheduled_at: datetime | None
    match_status: MatchStatus | None = None  # None = not known to the caller (no status check)
    data_completeness: float | None = None  # 0..1; None = unknown (fails the check)
    link_confidence: float | None = None  # bookmaker event <-> match link confidence, 0..1
    link_status: LinkStatus = LinkStatus.UNLINKED
    veto_known: bool = False
    context_veto: bool = False
    context_veto_reason: str | None = None
    already_bet_keys: set[str] = Field(default_factory=set)  # bet_key(...) of existing bets


@dataclass(frozen=True)
class Thresholds:
    min_probability: float
    min_ev: float
    max_sigma: float


def thresholds_for(market_type: MarketType, settings: DecisionSettings) -> Thresholds:
    """Global thresholds with the per-market override (``market_overrides[market_type.value]``)."""
    override = settings.market_overrides.get(market_type.value)

    def pick(name: str, default: float) -> float:
        value = getattr(override, name, None) if override is not None else None
        return default if value is None else float(value)

    return Thresholds(
        min_probability=pick("min_probability", settings.min_probability),
        min_ev=pick("min_ev", settings.min_ev),
        max_sigma=pick("max_sigma", settings.max_sigma),
    )


def bet_key(match_id: int, spec_key: str, selection_key: str) -> str:
    """Duplicate-detection key of one bet: match + canonical market spec + selection."""
    return f"{match_id}|{spec_key}#{selection_key}"


def is_slot_specific(spec: MarketSpec) -> bool:
    """Markets that depend on which map a slot turns out to be (they wait for the veto)."""
    return spec.is_map_scoped or spec.map_number is not None or spec.maps_scope is not None


def settled_sigma(outcome: OutcomeProbability) -> float:
    """The pricing sigma on the scale of ``p_win_given_settled``: ``sigma / (1 - p_void - p_push)``.

    ``OutcomeProbability.sigma`` is the uncertainty of the unconditional ``p_win``; the confidence
    check uses the probability given the bet settles, whose uncertainty is larger by the factor
    ``1 / P(settled)`` (first order, treating the refund probability as known). Infinite when the bet
    (almost) never settles; NaN stays NaN so the comparison fails.
    """
    settle_mass = 1.0 - outcome.p_void - outcome.p_push
    if not settle_mass > 1e-12:
        return math.inf
    return outcome.sigma / min(1.0, settle_mass)


def _fmt_odds(value: Decimal) -> str:
    return format(value.normalize(), "f")


def evaluate(
    priced: PricedOutcome,
    *,
    match: MatchDecisionInfo,
    settings: DecisionSettings,
    betclic: BetclicSettings,
    now: datetime,
    trusted_markets: Collection[str] | None = None,
) -> BetDecision:
    """Apply every rule to one priced outcome; ``accepted`` only if no rule fails.

    ``trusted_markets``: market type values (``MarketType.value``) whose calibration the calibration
    layer marks as trusted; enforced only with ``settings.require_trusted_calibration``.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (use cs2bot.utils.time.utcnow())")
    quote, outcome = priced.quote, priced.outcome
    spec = quote.spec
    reasons: list[str] = []

    if outcome.spec_key != spec.key() or outcome.selection != quote.selection_key:
        reasons.append(
            make_reason(
                ReasonCode.PRICING_MISMATCH,
                f"priced {outcome.spec_key}#{outcome.selection} but the quote is "
                f"{spec.key()}#{quote.selection_key}",
            )
        )

    # market type
    disabled = {m.strip().lower() for m in settings.disabled_markets}
    if spec.market_type is MarketType.OTHER:
        reasons.append(make_reason(ReasonCode.MARKET_DISABLED, "unmapped market type 'other' is never bet"))
    elif spec.market_type.value in disabled:
        reasons.append(
            make_reason(ReasonCode.MARKET_DISABLED, f"market type '{spec.market_type.value}' is disabled")
        )

    # availability and price
    if not quote.is_available:
        reasons.append(make_reason(ReasonCode.SELECTION_UNAVAILABLE, "selection is not available at Betclic"))
    if quote.odds < settings.min_odds or quote.odds > settings.max_odds:
        reasons.append(
            make_reason(
                ReasonCode.ODDS_OUT_OF_RANGE,
                f"odds {_fmt_odds(quote.odds)} outside [{_fmt_odds(settings.min_odds)}, "
                f"{_fmt_odds(settings.max_odds)}]",
            )
        )
    if quote.captured_at.tzinfo is None:
        reasons.append(make_reason(ReasonCode.ODDS_STALE, "odds timestamp has no timezone"))
        age_s = 0.0
    else:
        age_s = (now - quote.captured_at).total_seconds()
    if age_s > betclic.odds_max_age_s:
        reasons.append(
            make_reason(ReasonCode.ODDS_STALE, f"odds are {age_s:.0f}s old (max {betclic.odds_max_age_s}s)")
        )
    elif age_s < -MAX_FUTURE_ODDS_SKEW_S:
        reasons.append(
            make_reason(
                ReasonCode.ODDS_STALE, f"odds timestamp is {-age_s:.0f}s in the future (clock problem)"
            )
        )

    # timing
    if match.match_status is not None and match.match_status not in BETTABLE_MATCH_STATUSES:
        reasons.append(
            make_reason(
                ReasonCode.MATCH_NOT_SCHEDULED,
                f"match status is '{match.match_status.value}' (pre-match bets only)",
            )
        )
    if match.scheduled_at is None:
        reasons.append(make_reason(ReasonCode.OUTSIDE_TIME_WINDOW, "match start time is unknown"))
    elif match.scheduled_at.tzinfo is None:
        reasons.append(make_reason(ReasonCode.OUTSIDE_TIME_WINDOW, "match start time has no timezone"))
    else:
        to_start = match.scheduled_at - now
        min_gap = timedelta(minutes=settings.min_minutes_before_start)
        max_gap = timedelta(hours=settings.max_hours_before_start)
        minutes = to_start.total_seconds() / 60.0
        if to_start < min_gap:
            detail = (
                f"match started {-minutes:.0f} min ago"
                if minutes < 0
                else f"{minutes:.0f} min before start (min {settings.min_minutes_before_start} min)"
            )
            reasons.append(make_reason(ReasonCode.OUTSIDE_TIME_WINDOW, detail))
        elif to_start > max_gap:
            reasons.append(
                make_reason(
                    ReasonCode.OUTSIDE_TIME_WINDOW,
                    f"{minutes / 60.0:.1f} h before start (max {settings.max_hours_before_start} h)",
                )
            )

    # model quality (comparisons are written so that NaN fails them)
    numbers = {
        "p_win": outcome.p_win,
        "p_push": outcome.p_push,
        "p_void": outcome.p_void,
        "sigma": outcome.sigma,
        "ev": priced.ev,
        "kelly_full": priced.kelly_full,
    }
    broken = [name for name, value in numbers.items() if not math.isfinite(value)]
    if broken:
        reasons.append(make_reason(ReasonCode.INVALID_NUMBERS, f"non-finite {', '.join(broken)}"))
    else:
        probs = (outcome.p_win, outcome.p_push, outcome.p_void)
        if (
            any(p < -_PROB_EPS or p > 1.0 + _PROB_EPS for p in probs)
            or sum(probs) > 1.0 + 1e-6
            or outcome.sigma < 0
        ):
            reasons.append(
                make_reason(
                    ReasonCode.INVALID_NUMBERS,
                    f"p_win {outcome.p_win:.4f} / p_push {outcome.p_push:.4f} / p_void "
                    f"{outcome.p_void:.4f} / sigma {outcome.sigma:.4f} are not a valid distribution",
                )
            )
    th = thresholds_for(spec.market_type, settings)
    p_settled = outcome.p_win_given_settled
    if not p_settled >= th.min_probability:
        reasons.append(
            make_reason(
                ReasonCode.LOW_PROBABILITY,
                f"p(win | settled) {p_settled:.3f} < {th.min_probability:.3f}",
            )
        )
    if ev_check_enabled(th.min_ev) and not priced.ev >= th.min_ev:
        reasons.append(make_reason(ReasonCode.LOW_EV, f"EV {priced.ev:+.4f} < {th.min_ev:.4f}"))
    if settings.require_profitable_win:
        floor = min_profitable_odds(betclic)
        if quote.odds < floor:
            reasons.append(
                make_reason(
                    ReasonCode.UNPROFITABLE_WIN,
                    f"odds {_fmt_odds(quote.odds)} < {_fmt_odds(floor)}: after the stake tax a win would pay "
                    "back less than the stake",
                )
            )
    if (
        settings.require_trusted_calibration
        and trusted_markets is not None
        and spec.market_type.value not in trusted_markets
    ):
        reasons.append(
            make_reason(
                ReasonCode.UNTRUSTED_CALIBRATION,
                f"calibration of market type '{spec.market_type.value}' is not trusted yet",
            )
        )
    sigma_settled = settled_sigma(outcome)
    if not sigma_settled <= th.max_sigma:
        detail = f"sigma {sigma_settled:.4f} > {th.max_sigma:.4f}"
        if sigma_settled != outcome.sigma and math.isfinite(sigma_settled):
            detail += f" (sigma {outcome.sigma:.4f} of the unconditional p_win, scaled to p(win | settled))"
        reasons.append(make_reason(ReasonCode.HIGH_SIGMA, detail))

    # match-level data
    completeness = match.data_completeness
    if completeness is None or not completeness >= settings.min_data_completeness:
        shown = "unknown" if completeness is None else f"{completeness:.2f}"
        reasons.append(
            make_reason(
                ReasonCode.LOW_DATA_COMPLETENESS,
                f"data completeness {shown} < {settings.min_data_completeness:.2f}",
            )
        )
    if match.link_status not in BETTABLE_LINK_STATUSES:
        reasons.append(
            make_reason(ReasonCode.WEAK_EVENT_LINK, f"event link status is '{match.link_status.value}'")
        )
    conf = match.link_confidence
    if conf is None or not conf >= settings.min_event_link_confidence:
        shown = "unknown" if conf is None else f"{conf:.2f}"
        reasons.append(
            make_reason(
                ReasonCode.WEAK_EVENT_LINK,
                f"event link confidence {shown} < {settings.min_event_link_confidence:.2f}",
            )
        )
    if match.context_veto:
        reasons.append(
            make_reason(
                ReasonCode.CONTEXT_VETO, match.context_veto_reason or "context veto flag on this match"
            )
        )
    if settings.prefer_known_veto and not match.veto_known and is_slot_specific(spec):
        reasons.append(
            make_reason(
                ReasonCode.WAITING_FOR_VETO, "waiting for veto (market depends on which map is played)"
            )
        )
    if bet_key(match.match_id, spec.key(), quote.selection_key) in match.already_bet_keys:
        reasons.append(make_reason(ReasonCode.DUPLICATE, "this outcome was already bet on this match"))

    return BetDecision(
        priced=priced,
        match_id=match.match_id,
        accepted=not reasons,
        reasons=reasons,
        decided_at=now,
    )


def min_acceptable_odds(
    priced: PricedOutcome,
    *,
    settings: DecisionSettings,
    betclic: BetclicSettings,
    odds_tolerance: float,
) -> Decimal:
    """Lowest odds at which an accepted bet may still be placed (``BetOrder.min_acceptable_odds``).

    The executor re-reads the live price right before confirming. It may accept a small drop
    (``ExecutorSettings.odds_tolerance``, relative), but never a price at which the bet would no
    longer pass the decision rules: the result is the highest of

    * ``quote odds x (1 - odds_tolerance)``,
    * the odds at which the EV equals the market's ``min_ev`` (:func:`cs2bot.engine.ev.min_odds_for_ev`)
      - skipped when the EV rule is disabled (``min_ev <= -1``, odds ignored),
    * ``settings.min_odds``,
    * with ``require_profitable_win``: the lowest odds at which a win still pays back more than the
      stake (:func:`cs2bot.engine.ev.min_profitable_odds`),

    rounded UP to three decimals. For an outcome that should not be bet at all (no chance to win, or
    an EV already below the threshold) the result is not below the quoted odds, so nothing worse than
    the quote is ever accepted. Raises ``ValueError`` for a tolerance outside [0, 1).
    """
    if not 0.0 <= odds_tolerance < 1.0:
        raise ValueError(f"odds_tolerance must be in [0, 1), got {odds_tolerance!r}")
    quoted = priced.quote.odds
    floor = (quoted * (Decimal(1) - Decimal(repr(odds_tolerance)))).quantize(ODDS_STEP, rounding=ROUND_UP)
    th = thresholds_for(priced.quote.spec.market_type, settings)
    base = max(floor, settings.min_odds.quantize(ODDS_STEP, rounding=ROUND_UP))
    if settings.require_profitable_win:
        base = max(base, min_profitable_odds(betclic))
    if not ev_check_enabled(th.min_ev):
        return base
    edge_floor = min_odds_for_ev(priced.outcome, betclic, min_ev=th.min_ev)
    if edge_floor is None:
        return max(quoted, base)
    return max(base, edge_floor)


def evaluate_all(
    priced: Iterable[PricedOutcome],
    *,
    match: MatchDecisionInfo,
    settings: DecisionSettings,
    betclic: BetclicSettings,
    now: datetime,
    trusted_markets: Collection[str] | None = None,
) -> list[BetDecision]:
    """:func:`evaluate` every priced outcome of one match."""
    return [
        evaluate(p, match=match, settings=settings, betclic=betclic, now=now, trusted_markets=trusted_markets)
        for p in priced
    ]


# ----------------------------------------------------------------------------------------------
# DB helpers for the integration layer
# ----------------------------------------------------------------------------------------------


def load_already_bet_keys(session: Session, match_id: int, *, include_dry_run: bool = True) -> set[str]:
    """:func:`bet_key` of every bet on the match that put (or may have put) money at risk.

    FAILED / CANCELLED bets do not count (nothing was placed, the outcome may be retried). Dry-run
    bets count by default, so a dry run is not repeated every cycle; pass ``include_dry_run=False``
    when betting for real (``executor.dry_run`` off), otherwise the simulated bets of an earlier
    smoke test would block the real bets on the same outcomes.
    """
    rows = session.execute(
        select(Bet.spec_key, Bet.selection_key, Bet.status, Bet.dry_run).where(Bet.match_id == match_id)
    ).all()
    return {
        bet_key(match_id, spec_key, selection_key)
        for spec_key, selection_key, status, dry_run in rows
        if status not in NON_BLOCKING_BET_STATUSES
        and (include_dry_run or not (dry_run or status is BetStatus.DRY_RUN))
    }


def load_match_decision_info(
    session: Session,
    *,
    match_id: int,
    bookmaker_event_id: int | None = None,
    prediction: MatchPrediction | None = None,
    now: datetime | None = None,
    include_dry_run_bets: bool = True,
) -> MatchDecisionInfo:
    """Collect the match-level decision facts from the DB (and the prediction, when given).

    * start time: the earlier of ``matches.scheduled_at`` and the linked bookmaker event's
      ``starts_at`` (the bookmaker closes its pre-match market at its own kick-off time);
    * match status from ``matches.status`` - LIVE as soon as the linked bookmaker event is flagged
      in-play, even if the match row still says scheduled (pre-match bets only);
    * data completeness: ``prediction.data_completeness`` if a prediction is given, else
      ``matches.data_completeness``;
    * link confidence / status from the bookmaker event (it must be linked to this match);
    * veto known: ``prediction.veto_known``, else "the match has veto rows";
    * context veto: active, unexpired veto flags in ``context_flags`` or in the prediction;
    * duplicates: :func:`load_already_bet_keys` (``include_dry_run_bets`` as described there).
    """
    now = now or utcnow()
    match = session.get(Match, match_id)
    if match is None:
        raise ValueError(f"match {match_id} does not exist")
    if prediction is not None and prediction.match_id != match_id:
        raise ValueError(f"the prediction is for match {prediction.match_id}, not match {match_id}")
    link_conf: float | None = None
    link_status = LinkStatus.UNLINKED
    scheduled_at = match.scheduled_at
    match_status = match.status
    if bookmaker_event_id is not None:
        event = session.get(BookmakerEvent, bookmaker_event_id)
        if event is not None and event.match_id == match_id:
            link_conf, link_status = event.link_confidence, event.link_status
            if event.starts_at is not None and (scheduled_at is None or event.starts_at < scheduled_at):
                scheduled_at = event.starts_at
            if event.is_live and match_status in BETTABLE_MATCH_STATUSES:
                # the bookmaker already trades the event in-play: its prices are live odds, whatever
                # our (possibly stale) match row says
                match_status = MatchStatus.LIVE
    veto_reasons: list[str] = []
    flag_rows = session.scalars(
        select(ContextFlagRow).where(
            ContextFlagRow.match_id == match_id,
            ContextFlagRow.active.is_(True),
            ContextFlagRow.veto.is_(True),
        )
    )
    for row in flag_rows:
        if row.expires_at is None or row.expires_at > now:
            veto_reasons.append(f"{row.flag_type.value}: {row.rationale}")
    if prediction is not None:
        for flag in prediction.context_flags:
            if flag.veto and (flag.expires_at is None or _aware(flag.expires_at) > now):
                veto_reasons.append(f"{flag.flag_type.value}: {flag.rationale}")
    completeness = prediction.data_completeness if prediction is not None else match.data_completeness
    veto_known = prediction.veto_known if prediction is not None else bool(match.vetoes)
    return MatchDecisionInfo(
        match_id=match_id,
        scheduled_at=scheduled_at,
        match_status=match_status,
        data_completeness=completeness,
        link_confidence=link_conf,
        link_status=link_status,
        veto_known=veto_known,
        context_veto=bool(veto_reasons),
        context_veto_reason="; ".join(dict.fromkeys(veto_reasons)) or None,
        already_bet_keys=load_already_bet_keys(session, match_id, include_dry_run=include_dry_run_bets),
    )


def _aware(value: datetime) -> datetime:
    """Treat a naive timestamp from an LLM-produced flag as UTC instead of failing the comparison."""
    return ensure_utc(value)
