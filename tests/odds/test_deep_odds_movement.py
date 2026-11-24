"""deep_data: Betclic line movement, match-level market features, sharp benchmark, CLV reference."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from cs2bot.datasets.deep import MarketPoint
from cs2bot.db.models import BookmakerEvent, BookmakerMarket, BookmakerSelection, Match, OddsSnapshot, Team
from cs2bot.domain.enums import Bookmaker, LinkStatus, MatchStatus
from cs2bot.odds.movement import (
    MARKET_FEATURE_NAMES,
    MovementConfig,
    PricePoint,
    devig_two_way,
    load_match_winner_history,
    load_selection_history,
    market_features,
    selection_movement,
)
from cs2bot.odds.sharp import (
    DisabledSharpOddsSource,
    JsonFileSharpOddsSource,
    SharpOddsSource,
    closing_line_reference,
)

T0 = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)


def test_selection_movement_uses_only_points_before_as_of() -> None:
    pts = [
        PricePoint(T0 - timedelta(hours=10), 2.00),
        PricePoint(T0 - timedelta(hours=6), 1.90),
        PricePoint(T0 - timedelta(hours=6) + timedelta(minutes=10), 1.70),  # steam: 0.526 -> 0.588
        PricePoint(T0 - timedelta(hours=2), 1.70, is_available=False),  # suspension: ignored
        PricePoint(T0 - timedelta(hours=1), 1.72),
        PricePoint(T0 + timedelta(minutes=5), 1.20),  # after as_of: invisible
    ]
    mv = selection_movement(pts, T0)
    assert mv.n_points == 4 and mv.opening_odds == 2.0 and mv.current_odds == 1.72
    assert mv.n_moves == 3 and mv.steam_up == 1 and mv.steam_down == 0
    assert mv.prob_change == pytest.approx(1 / 1.72 - 0.5)
    assert mv.hours_since_last_move == pytest.approx(1.0)
    assert mv.hours_quoted == pytest.approx(10.0)
    assert (mv.min_odds, mv.max_odds) == (1.70, 2.00)
    # nothing visible yet
    early = selection_movement(pts, T0 - timedelta(hours=11))
    assert not early.quoted and math.isnan(early.opening_odds)
    # a bigger steam threshold sees no steam
    assert selection_movement(pts, T0, config=MovementConfig(steam_min_prob=0.2)).steam_up == 0


def test_devig_and_market_features_flip() -> None:
    p, over = devig_two_way(1.80, 2.00)
    assert p == pytest.approx((1 / 1.8) / (1 / 1.8 + 1 / 2.0)) and over == pytest.approx(1 / 1.8 + 0.5 - 1)
    assert math.isnan(devig_two_way(None, 2.0)[0]) and math.isnan(devig_two_way(1.0, 2.0)[0])
    pts = [
        MarketPoint(T0 - timedelta(hours=20), 2.10, 1.75),
        MarketPoint(T0 - timedelta(hours=5), 1.80, 2.00),
        MarketPoint(T0 - timedelta(hours=5, minutes=-15), 1.60, 2.30),
        MarketPoint(T0 - timedelta(hours=3), None, 2.30),  # suspended side: skipped
        MarketPoint(T0 + timedelta(hours=1), 1.10, 6.0),  # future
    ]
    f = market_features(pts, T0)
    assert set(f) == set(MARKET_FEATURE_NAMES)
    p_open = devig_two_way(2.10, 1.75)[0]
    p_last = devig_two_way(1.60, 2.30)[0]
    assert f["mkt_p_open"] == pytest.approx(p_open) and f["mkt_p_last"] == pytest.approx(p_last)
    assert f["mkt_logit_move"] > 0 and f["mkt_n_moves"] == 2 and f["mkt_steam"] >= 1
    assert f["mkt_hours_quoted"] == pytest.approx(20.0)
    assert f["mkt_hours_since_move"] == pytest.approx(4.75)
    g = market_features(pts, T0, flip=True)
    assert g["mkt_p_last"] == pytest.approx(1 - p_last) and g["mkt_steam"] == -f["mkt_steam"]
    assert g["mkt_logit_move"] == pytest.approx(-f["mkt_logit_move"])
    empty = market_features([], T0)
    assert math.isnan(empty["mkt_p_last"]) and empty["mkt_n_moves"] == 0.0
    sharp = market_features(pts, T0, sharp=[(T0 - timedelta(hours=1), 0.70), (T0 + timedelta(hours=1), 0.9)])
    assert sharp["sharp_p_last"] == pytest.approx(0.70)
    assert sharp["sharp_minus_mkt"] == pytest.approx(0.70 - p_last)


def _event(
    session: Session, match_id: int, status: LinkStatus, prices: list[tuple[timedelta, str, str, bool]]
) -> tuple[int, int]:
    ev = BookmakerEvent(
        bookmaker=Bookmaker.BETCLIC,
        external_id=f"e{match_id}{status.value}",
        name="A - B",
        team1_raw="A",
        team2_raw="B",
        starts_at=T0,
        match_id=match_id,
        link_status=status,
        first_seen_at=T0 - timedelta(days=2),
        last_seen_at=T0,
    )
    session.add(ev)
    session.flush()
    mk = BookmakerMarket(
        event_id=ev.id,
        raw_name="Zwyciezca",
        market_type="match_winner",
        spec={"market_type": "match_winner"},
        spec_key="match_winner",
        is_mapped=True,
        first_seen_at=T0 - timedelta(days=2),
        last_seen_at=T0,
    )
    session.add(mk)
    session.flush()
    sels = {}
    for key in ("team1", "team2"):
        sel = BookmakerSelection(market_id=mk.id, raw_name=key, selection_key=key, is_mapped=True)
        session.add(sel)
        session.flush()
        sels[key] = sel.id
    for lead, key, odds, avail in prices:
        session.add(
            OddsSnapshot(
                selection_id=sels[key], odds=Decimal(odds), is_available=avail, captured_at=T0 - lead
            )
        )
    session.flush()
    return sels["team1"], sels["team2"]


@pytest.fixture
def match_id(session: Session) -> int:
    session.add_all([Team(id=1, name="A"), Team(id=2, name="B")])
    session.flush()
    m = Match(team1_id=1, team2_id=2, scheduled_at=T0, status=MatchStatus.SCHEDULED)
    session.add(m)
    session.flush()
    return m.id


def test_load_match_winner_history_from_linked_events_only(session: Session, match_id: int) -> None:
    s1, s2 = _event(
        session,
        match_id,
        LinkStatus.AUTO,
        [
            (timedelta(hours=10), "team1", "1.80", True),
            (timedelta(hours=10), "team2", "2.00", True),
            (timedelta(hours=2), "team1", "1.60", True),
            (timedelta(hours=1), "team2", "2.30", False),
        ],
    )
    _event(session, match_id, LinkStatus.PENDING_REVIEW, [(timedelta(hours=9), "team1", "5.00", True)])
    hist = load_match_winner_history(session)
    pts = hist[match_id]
    assert [(p.team1_odds, p.team2_odds) for p in pts] == [(1.8, None), (1.8, 2.0), (1.6, 2.0), (1.6, None)]
    cut = load_match_winner_history(session, until=T0 - timedelta(hours=3))
    assert len(cut[match_id]) == 2
    f = market_features(pts, T0)
    assert f["mkt_p_last"] == pytest.approx(devig_two_way(1.6, 2.0)[0])
    sel = load_selection_history(session, [s1, s2])
    assert [p.odds for p in sel[s1]] == [1.8, 1.6] and sel[s2][-1].is_available is False


def test_sharp_sources_and_closing_reference(session: Session, match_id: int, tmp_path: Path) -> None:
    s1, s2 = _event(
        session,
        match_id,
        LinkStatus.CONFIRMED,
        [(timedelta(hours=1), "team1", "1.50", True), (timedelta(hours=1), "team2", "2.60", True)],
    )
    disabled = DisabledSharpOddsSource()
    assert isinstance(disabled, SharpOddsSource) and not disabled.enabled and disabled.quotes(match_id) == []
    ref = closing_line_reference(session, s1, T0, sharp=disabled, match_id=match_id, selection_key="team1")
    assert ref is not None and ref.source == "betclic" and ref.odds == Decimal("1.50")
    assert ref.probability == pytest.approx(1 / 1.5)
    path = tmp_path / "sharp.json"
    path.write_text(
        json.dumps(
            {
                "quotes": [
                    {
                        "match_id": match_id,
                        "captured_at": "2026-11-20T17:30:00Z",
                        "team1_odds": 1.40,
                        "team2_odds": 3.0,
                    },
                    {
                        "match_id": match_id,
                        "captured_at": "2026-11-20T18:30:00Z",
                        "team1_odds": 1.1,
                        "team2_odds": 8,
                    },
                    {"match_id": "bad"},
                    {
                        "match_id": match_id,
                        "captured_at": "2026-11-20T17:00:00Z",
                        "team1_odds": 0.5,
                        "team2_odds": 3,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    src = JsonFileSharpOddsSource(path)
    assert isinstance(src, SharpOddsSource) and src.enabled
    assert len(src.quotes(match_id)) == 2 and len(src.quotes(match_id, until=T0)) == 1
    p1 = devig_two_way(1.40, 3.0)[0]
    for key, sid, expected in (("team1", s1, p1), ("team2", s2, 1 - p1)):
        r = closing_line_reference(
            session, sid, T0, sharp=src, match_id=match_id, selection_key=key, spec_key="match_winner"
        )
        assert r is not None and r.source == "sharp" and r.probability == pytest.approx(expected)
    # not a match-winner selection -> Betclic's own close
    other = closing_line_reference(session, s1, T0, sharp=src, match_id=match_id, selection_key="team1")
    assert other is not None and other.source == "betclic"
    assert closing_line_reference(session, s1, T0 - timedelta(hours=2)) is None
    assert not JsonFileSharpOddsSource(tmp_path / "missing.json").enabled


def test_sharp_source_from_settings(tmp_path: Path) -> None:
    from cs2bot.config import Settings
    from cs2bot.odds.sharp import sharp_source_from_settings

    assert not sharp_source_from_settings(Settings()).enabled  # disabled by default
    path = tmp_path / "sharp.json"
    path.write_text('{"quotes": []}', encoding="utf-8")
    settings = Settings(models={"sharp_odds_file": str(path)})  # type: ignore[arg-type]
    src = sharp_source_from_settings(settings)
    assert isinstance(src, JsonFileSharpOddsSource) and src.enabled and src.quotes(1) == []
