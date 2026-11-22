"""Model calibration and per-market performance."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.api.analytics import (
    EXCLUDED_STATUSES,
    BetFact,
    bet_stats,
    brier_by_month,
    calibration_set,
    group_by_market,
)
from cs2bot.api.bankroll import load_bet_facts
from cs2bot.api.deps import DB, Ctx, require_auth
from cs2bot.api.formatting import money_str0, odds_str, parse_spec_key, round_or_none, utc
from cs2bot.api.params import FromParam, ToParam, as_utc
from cs2bot.api.schemas import CalibrationOut, MarketPerfOut, MarketPerfRow
from cs2bot.db.models import Bet, BetDecisionRow, MapPlayed, Match, Prediction
from cs2bot.domain.enums import BetStatus, MatchStatus
from cs2bot.domain.markets import SELECTION_DRAW, SELECTION_TEAM1, SELECTION_TEAM2, MarketType
from cs2bot.utils.time import ensure_utc

router = APIRouter(prefix="/api/performance", tags=["performance"], dependencies=[Depends(require_auth)])

Mode = Literal["real", "dry_run", "all"]
WINNER_MARKETS: tuple[str, ...] = (MarketType.MATCH_WINNER.value, MarketType.MAP_WINNER.value)
_SELECTION_PREFERENCE = {SELECTION_TEAM1: 0, SELECTION_TEAM2: 1}


def _dry_run_filter(mode: Mode) -> bool | None:
    return None if mode == "all" else mode == "dry_run"


def _winner_outcome(row: Any, maps: dict[tuple[int, int], int | None]) -> int | None:
    """1/0 outcome of a finished match/map winner prediction; ``None`` if unknown or void."""
    team_id: int | None
    if row.selection_key == SELECTION_TEAM1:
        team_id = row.team1_id
    elif row.selection_key == SELECTION_TEAM2:
        team_id = row.team2_id
    else:
        team_id = None
    if row.market_type == MarketType.MATCH_WINNER.value:
        winner = row.winner_team_id
        is_draw = winner is None and row.team1_score is not None and row.team1_score == row.team2_score
        if row.selection_key == SELECTION_DRAW:
            return 1 if is_draw else (0 if winner is not None else None)
        if team_id is None:
            return None
        if winner is None:
            return 0 if is_draw else None
        return 1 if winner == team_id else 0
    map_number = parse_spec_key(row.spec_key).get("map_number")
    if team_id is None or not isinstance(map_number, int):
        return None
    map_winner = maps.get((row.match_id, map_number))
    if map_winner is None:  # map not played (void) or no result
        return None
    return 1 if map_winner == team_id else 0


def prediction_samples(
    session: Session, lower: datetime | None, upper: datetime | None
) -> dict[str, list[tuple[float, int, datetime | None]]]:
    """(p_win, outcome, match time) of the latest pre-match winner predictions of finished matches.

    Only one selection per binary market is used (team1 when present) so complementary rows do
    not double-count the same event.
    """
    stmt = (
        select(
            Prediction.id,
            Prediction.match_id,
            Prediction.market_type,
            Prediction.spec_key,
            Prediction.selection_key,
            Prediction.p_win,
            Prediction.created_at,
            Match.team1_id,
            Match.team2_id,
            Match.winner_team_id,
            Match.team1_score,
            Match.team2_score,
            Match.scheduled_at,
        )
        .join(Match, Match.id == Prediction.match_id)
        .where(
            Match.status == MatchStatus.FINISHED,
            Prediction.is_latest.is_(True),
            Prediction.market_type.in_(WINNER_MARKETS),
        )
    )
    map_stmt = (
        select(MapPlayed.match_id, MapPlayed.map_number, MapPlayed.winner_team_id)
        .join(Match, Match.id == MapPlayed.match_id)
        .where(Match.status == MatchStatus.FINISHED)
    )
    if lower is not None:
        stmt = stmt.where(Match.scheduled_at >= lower)
        map_stmt = map_stmt.where(Match.scheduled_at >= lower)
    if upper is not None:
        stmt = stmt.where(Match.scheduled_at <= upper)
        map_stmt = map_stmt.where(Match.scheduled_at <= upper)
    maps = {(int(mid), int(num)): winner for mid, num, winner in session.execute(map_stmt)}

    def rank(row: Any) -> tuple[int, float, int]:
        # team1 row first, then the newest prediction
        return (
            _SELECTION_PREFERENCE.get(row.selection_key, 2),
            -ensure_utc(row.created_at).timestamp(),
            -int(row.id),
        )

    chosen: dict[tuple[int, str], Any] = {}  # (match, spec_key) -> result row
    for row in session.execute(stmt):
        key = (int(row.match_id), str(row.spec_key))
        current = chosen.get(key)
        if current is None or rank(row) < rank(current):
            chosen[key] = row
    out: dict[str, list[tuple[float, int, datetime | None]]] = defaultdict(list)
    for row in chosen.values():
        outcome = _winner_outcome(row, maps)
        if outcome is not None:
            out[str(row.market_type)].append((float(row.p_win), outcome, utc(row.scheduled_at)))
    return out


@router.get("/calibration", response_model=CalibrationOut)
def calibration(
    ctx: Ctx,
    db: DB,
    bins: Annotated[int, Query(ge=2, le=50, description="Number of equal-width probability bins")] = 10,
    mode: Annotated[Mode, Query(description="Bets used: real, dry_run or all")] = "all",
    from_: FromParam = None,
    to: ToParam = None,
) -> CalibrationOut:
    tz = ctx.settings.timezone
    lower, upper = as_utc(from_), as_utc(to)
    stmt = (
        select(Bet.status, Bet.settled_at, BetDecisionRow.p_win)
        .join(BetDecisionRow, Bet.decision_id == BetDecisionRow.id)
        .where(Bet.status.in_([BetStatus.WON, BetStatus.LOST]))
    )
    dry = _dry_run_filter(mode)
    if dry is not None:
        stmt = stmt.where(Bet.dry_run.is_(dry))
    if lower is not None:
        stmt = stmt.where(Bet.settled_at >= lower)
    if upper is not None:
        stmt = stmt.where(Bet.settled_at <= upper)
    bet_rows = [
        (float(p), 1 if status is BetStatus.WON else 0, utc(settled))
        for status, settled, p in db.execute(stmt)
    ]
    by_market = prediction_samples(db, lower, upper)
    all_preds = [s for samples in by_market.values() for s in samples]

    def build(samples: list[tuple[float, int, datetime | None]]) -> Any:
        return calibration_set([(p, y) for p, y, _ in samples], bins, by_month=brier_by_month(samples, tz))

    return CalibrationOut(
        n_bins=bins,
        bets=build(bet_rows),
        predictions=build(all_preds),
        predictions_by_market={market: build(samples) for market, samples in sorted(by_market.items())},
    )


def _perf_row(market_type: str, facts: list[BetFact]) -> MarketPerfRow:
    stats = bet_stats(facts)
    return MarketPerfRow(
        market_type=market_type,
        bets=stats.bets,
        open=stats.open_bets,
        settled=stats.settled_bets,
        won=stats.won,
        lost=stats.lost,
        void=stats.void,
        hit_rate=round_or_none(stats.hit_rate),
        staked=money_str0(stats.turnover),
        pnl=money_str0(stats.realized_pnl),
        roi=round_or_none(stats.yield_rate),
        avg_odds=odds_str(stats.avg_odds),
        avg_clv=round_or_none(stats.avg_clv),
        avg_ev=round_or_none(stats.avg_ev),
    )


@router.get("/markets", response_model=MarketPerfOut)
def markets(
    db: DB,
    mode: Annotated[Mode, Query(description="real, dry_run or all bets")] = "real",
    from_: FromParam = None,
    to: ToParam = None,
) -> MarketPerfOut:
    lower, upper = as_utc(from_), as_utc(to)
    facts = [
        f for f in load_bet_facts(db, dry_run=_dry_run_filter(mode)) if f.status not in EXCLUDED_STATUSES
    ]
    if lower is not None:
        facts = [f for f in facts if f.opened_at is not None and f.opened_at >= lower]
    if upper is not None:
        facts = [f for f in facts if f.opened_at is not None and f.opened_at <= upper]
    return MarketPerfOut(
        mode=mode,
        items=[_perf_row(market, rows) for market, rows in group_by_market(facts)],
        total=_perf_row("all", facts),
    )
