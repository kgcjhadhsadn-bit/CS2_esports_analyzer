"""Backtests over the database: stored Betclic price history, finished matches, run_db_backtest."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from cs2bot.backtest import (
    BacktestConfig,
    DbOddsHistory,
    PipelineConfig,
    load_backtest_matches,
    load_historical_data,
    outcome_key,
    run_db_backtest,
)
from cs2bot.config import DecisionSettings, Settings
from cs2bot.db.models import BookmakerEvent, BookmakerMarket, BookmakerSelection, Match, OddsSnapshot
from cs2bot.domain.enums import Bookmaker, LinkStatus
from cs2bot.domain.markets import MarketSpec, MarketType
from cs2bot.testing.synthetic import SynthUniverse, generate_universe, populate_db

MW = MarketSpec(market_type=MarketType.MATCH_WINNER)


def _universe() -> SynthUniverse:
    return generate_universe(n_teams=6, n_matches=60, seed=8, days=120, with_kills=False)


def _event(
    session: Session, match: Match, *, ext: str, status: LinkStatus = LinkStatus.AUTO
) -> tuple[BookmakerSelection, BookmakerSelection]:
    assert match.scheduled_at is not None
    t = match.scheduled_at
    ev = BookmakerEvent(
        bookmaker=Bookmaker.BETCLIC,
        external_id=ext,
        name="A - B",
        team1_raw="A",
        team2_raw="B",
        starts_at=t,
        match_id=match.id,
        link_status=status,
        link_confidence=0.97,
        first_seen_at=t - timedelta(days=2),
        last_seen_at=t,
    )
    session.add(ev)
    session.flush()
    mk = BookmakerMarket(
        event_id=ev.id,
        external_id=f"m{ext}",
        raw_name="Zwycięzca meczu",
        market_type=MW.market_type.value,
        spec=MW.model_dump(mode="json"),
        spec_key=MW.key(),
        is_mapped=True,
        first_seen_at=t - timedelta(days=2),
        last_seen_at=t,
    )
    session.add(mk)
    session.flush()
    sels = []
    for key in ("team1", "team2"):
        sel = BookmakerSelection(
            market_id=mk.id, external_id=f"{ext}{key}", raw_name=key, selection_key=key, is_mapped=True
        )
        session.add(sel)
        sels.append(sel)
    session.flush()
    return sels[0], sels[1]


def _snap(session: Session, sel: BookmakerSelection, odds: str, at: datetime, available: bool = True) -> None:
    session.add(OddsSnapshot(selection_id=sel.id, odds=Decimal(odds), is_available=available, captured_at=at))
    session.flush()


@pytest.fixture
def db(session: Session) -> tuple[Session, SynthUniverse]:
    uni = _universe()
    populate_db(session, uni, with_rounds=True, with_kills=False)
    session.flush()
    return session, uni


def test_db_odds_history_replays_what_was_on_offer(db: tuple[Session, SynthUniverse]) -> None:
    session, uni = db
    match = session.get(Match, uni.matches[-1].id)
    assert match is not None and match.scheduled_at is not None
    k = match.scheduled_at
    s1, s2 = _event(session, match, ext="901")
    _snap(session, s1, "1.90", k - timedelta(hours=20))
    _snap(session, s2, "1.95", k - timedelta(hours=20))
    _snap(session, s1, "1.70", k - timedelta(hours=3))
    _snap(session, s1, "1.70", k - timedelta(hours=2), available=False)  # suspended
    _snap(session, s1, "1.65", k - timedelta(minutes=20))
    _snap(session, s1, "1.20", k + timedelta(minutes=5))  # in-play, after kick-off
    history = DbOddsHistory(session)
    at = lambda t: {(q.selection_key, q.odds) for q in history.quotes_before(match.id, t)}  # noqa: E731
    assert at(k - timedelta(hours=20)) == set()
    assert at(k - timedelta(hours=10)) == {("team1", Decimal("1.90")), ("team2", Decimal("1.95"))}
    assert at(k - timedelta(hours=1)) == {("team2", Decimal("1.95"))}
    assert at(k) == {("team1", Decimal("1.65")), ("team2", Decimal("1.95"))}
    quote = history.quotes_before(match.id, k - timedelta(hours=10))[0]
    assert quote.spec == MW and quote.captured_at == k - timedelta(hours=20)
    assert quote.external_event_id == "901" and quote.market_raw_name == "Zwycięzca meczu"
    closing = history.closing_quote(match.id, outcome_key(MW, "team1"))
    assert closing is not None and closing.odds == Decimal("1.65")
    assert history.closing_quote(match.id, outcome_key(MW, "draw")) is None
    assert history.kickoff(match.id) == k


def test_db_odds_history_ignores_unsafe_links(db: tuple[Session, SynthUniverse]) -> None:
    session, uni = db
    match = session.get(Match, uni.matches[-2].id)
    assert match is not None and match.scheduled_at is not None
    s1, _ = _event(session, match, ext="902", status=LinkStatus.PENDING_REVIEW)
    _snap(session, s1, "2.10", match.scheduled_at - timedelta(hours=5))
    assert DbOddsHistory(session).quotes_before(match.id, match.scheduled_at) == []


def test_load_backtest_matches_and_history(db: tuple[Session, SynthUniverse], settings: Settings) -> None:
    session, uni = db
    matches = load_backtest_matches(session, settings=settings)
    assert [m.match_id for m in matches] == [m.id for m in uni.matches]
    first = matches[0]
    assert first.actuals is not None and first.actuals.complete
    assert first.context is not None and first.context.known_veto  # the published veto
    assert first.start_time == uni.matches[0].played_at
    later = load_backtest_matches(
        session, settings=settings, since=uni.matches[30].played_at, with_context=False
    )
    assert len(later) == 30 and later[0].context is None
    data = load_historical_data(session, matches=matches, with_rounds=False)
    assert len(data.map_results) == len(uni.map_results()) and len(data.rounds) == 0
    assert len(data.vetoes) == len(uni.matches)


def test_run_db_backtest_end_to_end(db: tuple[Session, SynthUniverse]) -> None:
    session, uni = db
    for i, m in enumerate(uni.matches[-12:]):
        match = session.get(Match, m.id)
        assert match is not None and match.scheduled_at is not None
        s1, s2 = _event(session, match, ext=f"9{i:02d}")
        _snap(session, s1, "2.60", match.scheduled_at - timedelta(hours=6))
        _snap(session, s2, "2.60", match.scheduled_at - timedelta(hours=6))
        _snap(session, s1, "2.40", match.scheduled_at - timedelta(minutes=10))
    settings = Settings(decision=DecisionSettings(min_probability=0.3, min_ev=0.0, max_sigma=0.5))
    start = uni.matches[-12].played_at
    report = run_db_backtest(
        session,
        settings=settings,
        since=start,
        until=uni.matches[-1].played_at + timedelta(minutes=1),
        history_since=uni.matches[0].played_at - timedelta(days=1),
        config=BacktestConfig(retrain_every=timedelta(days=60)),
        pipeline_config=PipelineConfig(simulate=False),
    )
    assert report.n_matches == 12 and report.n_matches_predicted == 12
    assert report.n_prediction_errors == 0 and report.n_quotes == 24 and report.n_priced == 24
    # 2.60 on both sides is an overround below 1 (a mispriced market): the model bets one side
    assert report.n_bets >= 6
    assert all(b.closing_odds is not None for b in report.bets if b.selection == "team1")
    assert report.n_predictions == 24
    assert "## Betting" in report.to_markdown()
