"""Seed data for the dashboard API tests (a handcrafted dataset on top of a small synthetic universe).

Dataset (``NOW`` = Friday 2026-11-20 12:00 UTC = 13:00 Warsaw):

* synthetic universe: teams 1-6 ("Synth Team N"), players 1-30, finished matches 1-24
* upcoming match 101 (team 1 vs 2, BO3, tier S, linked AUTO to a Betclic event with mapped
  match-winner / map-handicap / total-maps markets, one unmapped market, odds snapshots,
  latest predictions, an analysis, decisions, a stand-in flag, lineups and a partial veto)
* upcoming match 102 (team 3 vs 4, out of scope, not linked) and 103 (team 5 vs 6)
* Betclic events pending review: one tentatively linked to 103 with swapped team order, one unlinked
  that should be matched to 102
* real bets: won / lost (today) / void / open (today, with screenshot) / failed / cancelled
  (screenshot path escaping the directory); paper bets: open + won
* ledger: deposit 1000, stakes, payout, refund -> balance 968.44
* job runs: fresh odds job, stale upcoming sync, failing settle job, stuck predict job
* executor health in ``system_state``: logged out, a failing selector
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from cs2bot.config import Settings
from cs2bot.db import state
from cs2bot.db.models import (
    BankrollLedger,
    Bet,
    BetDecisionRow,
    BookmakerEvent,
    BookmakerMarket,
    BookmakerSelection,
    ContextFlagRow,
    JobRun,
    Match,
    MatchAnalysis,
    MatchLineup,
    OddsSnapshot,
    Prediction,
    Tournament,
    Veto,
)
from cs2bot.domain.enums import (
    BetStatus,
    Bookmaker,
    ContextFlagType,
    DataSource,
    JobStatus,
    LedgerKind,
    LinkStatus,
    MatchFormat,
    MatchStatus,
    Tier,
    VetoAction,
)
from cs2bot.domain.markets import MarketSpec, MarketType
from cs2bot.domain.predictions import MapSlotPrediction, MatchPrediction, PredictionCore
from cs2bot.testing.synthetic import SynthUniverse, generate_universe, populate_db

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
CSRF = {"X-Requested-With": "cs2bot"}
BASE_URL = "http://127.0.0.1:8000"

MW = MarketSpec(market_type=MarketType.MATCH_WINNER)
HCP = MarketSpec(market_type=MarketType.MAP_HANDICAP, line=-1.5)
TOTAL = MarketSpec(market_type=MarketType.TOTAL_MAPS, line=2.5)
CMS = MarketSpec(market_type=MarketType.CORRECT_MAP_SCORE)
MAP1 = MarketSpec(market_type=MarketType.MAP_WINNER, map_number=1)

_UNIVERSE: list[SynthUniverse] = []


def small_universe() -> SynthUniverse:
    if not _UNIVERSE:
        _UNIVERSE.append(
            generate_universe(n_teams=6, n_matches=24, seed=11, start=NOW - timedelta(days=150), days=120)
        )
    return _UNIVERSE[0]


@dataclass
class Seed:
    """Ids of the handcrafted rows (synthetic ids are known: teams 1-6, matches 1-24)."""

    tmp: Path
    screenshot_dir: Path
    outside_file: Path
    events: dict[str, int] = field(default_factory=dict)
    selections: dict[str, int] = field(default_factory=dict)
    bets: dict[str, int] = field(default_factory=dict)
    decisions: dict[str, int] = field(default_factory=dict)
    predictions: dict[str, int] = field(default_factory=dict)


def _ts(days: float = 0, hours: float = 0, minutes: float = 0) -> datetime:
    return NOW + timedelta(days=days, hours=hours, minutes=minutes)


def _add(session: Session, obj: Any) -> Any:
    session.add(obj)
    session.flush()
    return obj


def _event(session: Session, external_id: str, **kw: Any) -> BookmakerEvent:
    kw.setdefault("first_seen_at", _ts(hours=-6))
    kw.setdefault("last_seen_at", _ts(minutes=-2))
    return _add(session, BookmakerEvent(bookmaker=Bookmaker.BETCLIC, external_id=external_id, **kw))


def _market(session: Session, event: BookmakerEvent, raw: str, spec: MarketSpec | None) -> BookmakerMarket:
    return _add(
        session,
        BookmakerMarket(
            event_id=event.id,
            external_id=raw[:10],
            raw_name=raw,
            market_type=spec.market_type.value if spec else None,
            spec=spec.model_dump(mode="json") if spec else None,
            spec_key=spec.key() if spec else None,
            is_mapped=spec is not None,
            first_seen_at=_ts(hours=-6),
            last_seen_at=_ts(minutes=-2),
        ),
    )


def _selection(
    session: Session, market: BookmakerMarket, raw: str, key: str | None, odds: str, *, age_s: float = 60
) -> BookmakerSelection:
    return _add(
        session,
        BookmakerSelection(
            market_id=market.id,
            external_id=raw[:10],
            raw_name=raw,
            selection_key=key,
            is_mapped=key is not None,
            last_odds=Decimal(odds),
            last_odds_at=NOW - timedelta(seconds=age_s),
            is_available=True,
        ),
    )


def _prediction(
    session: Session,
    match_id: int,
    spec: MarketSpec,
    selection: str,
    p_win: float,
    *,
    sigma: float = 0.03,
    latest: bool = True,
    created: datetime | None = None,
    p_void: float = 0.0,
) -> Prediction:
    return _add(
        session,
        Prediction(
            match_id=match_id,
            model_version="test-1",
            created_at=created or _ts(minutes=-20),
            market_type=spec.market_type.value,
            spec=spec.model_dump(mode="json"),
            spec_key=spec.key(),
            selection_key=selection,
            p_win=p_win,
            p_void=p_void,
            sigma=sigma,
            data_completeness=0.97,
            is_latest=latest,
        ),
    )


def _decision(
    session: Session,
    match_id: int,
    spec: MarketSpec,
    selection: str,
    *,
    accepted: bool,
    decided: datetime,
    odds: str,
    p_win: float,
    ev: float,
    reasons: list[str] | None = None,
    selection_id: int | None = None,
    stake: str | None = None,
) -> BetDecisionRow:
    return _add(
        session,
        BetDecisionRow(
            match_id=match_id,
            selection_id=selection_id,
            spec_key=spec.key(),
            selection_key=selection,
            odds=Decimal(odds),
            odds_captured_at=decided - timedelta(minutes=1),
            decided_at=decided,
            accepted=accepted,
            reasons=reasons or (["ok"] if accepted else ["rejected"]),
            p_win=p_win,
            sigma=0.03,
            ev=ev,
            kelly_fraction=0.05 if accepted else 0.0,
            proposed_stake=Decimal(stake) if stake else None,
            settings_snapshot={"min_ev": 0.03},
        ),
    )


def _bet(session: Session, key: str, **kw: Any) -> Bet:
    kw.setdefault("created_at", kw.get("placed_at") or _ts(hours=-1))
    kw.setdefault("bookmaker", Bookmaker.BETCLIC)
    kw.setdefault("external_event_id", "ev-x")
    kw.setdefault("market_raw", "Zwycięzca meczu")
    kw.setdefault("selection_raw", "Synth Team 1")
    kw.setdefault("spec_key", MW.key())
    kw.setdefault("selection_key", "team1")
    kw.setdefault("requested_odds", Decimal("1.90"))
    kw.setdefault("min_acceptable_odds", Decimal("1.85"))
    kw.setdefault("tax", kw["stake"] * Decimal("0.12"))
    return _add(session, Bet(idempotency_key=f"idem-{key}", **kw))


def seed_database(session: Session, tmp: Path) -> Seed:
    uni = small_universe()
    populate_db(session, uni, with_rounds=False)
    screenshot_dir = tmp / "screenshots"
    screenshot_dir.mkdir()
    (screenshot_dir / "bet_open.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-image")
    outside = tmp / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\nsecret")
    seed = Seed(tmp=tmp, screenshot_dir=screenshot_dir, outside_file=outside)

    tour_s = _add(session, Tournament(name="IEM Test Masters", tier=Tier.S, is_lan=True))
    _add(
        session,
        Match(
            id=101,
            tournament_id=tour_s.id,
            team1_id=1,
            team2_id=2,
            scheduled_at=_ts(days=1),
            status=MatchStatus.SCHEDULED,
            format=MatchFormat.BO3,
            tier=Tier.S,
            is_lan=True,
            in_scope=True,
            data_completeness=0.97,
            last_synced_at=_ts(minutes=-10),
        ),
    )
    _add(
        session,
        Match(
            id=102,
            team1_id=3,
            team2_id=4,
            scheduled_at=_ts(days=2),
            status=MatchStatus.SCHEDULED,
            format=MatchFormat.BO1,
            tier=Tier.B,
            in_scope=False,
        ),
    )
    _add(
        session,
        Match(
            id=103,
            team1_id=5,
            team2_id=6,
            scheduled_at=_ts(hours=3),
            status=MatchStatus.SCHEDULED,
            format=MatchFormat.BO3,
            tier=Tier.A,
            in_scope=True,
            data_completeness=0.5,
        ),
    )
    # lineups (team 2 plays with a stand-in), partial veto, context flag
    for pid in range(1, 6):
        session.add(MatchLineup(match_id=101, team_id=1, player_id=pid, source=DataSource.HLTV))
    for pid in range(6, 10):
        session.add(MatchLineup(match_id=101, team_id=2, player_id=pid, source=DataSource.HLTV))
    session.add(MatchLineup(match_id=101, team_id=2, player_id=30, is_standin=True, source=DataSource.HLTV))
    session.add(Veto(match_id=101, step=1, team_id=1, action=VetoAction.BAN, map_name="nuke"))
    session.add(Veto(match_id=101, step=2, team_id=2, action=VetoAction.BAN, map_name="train"))
    session.add(
        ContextFlagRow(
            match_id=101,
            team_id=2,
            flag_type=ContextFlagType.STAND_IN,
            severity=0.6,
            confidence=0.9,
            logit_shift=0.12,
            veto=False,
            rationale="Team 2 plays with a stand-in",
            evidence=["https://example.invalid/news/1"],
            model="claude-opus-5-5",
            active=True,
            created_at=_ts(hours=-2),
        )
    )

    # ---------------------------------------------------------------- Betclic event for 101
    ev = _event(
        session,
        "ev-101",
        name="Synth Team 1 - Synth Team 2",
        team1_raw="Synth Team 1",
        team2_raw="Synth Team 2",
        competition_raw="IEM",
        starts_at=_ts(days=1),
        url="https://www.betclic.pl/e-sport/ev-101",
        match_id=101,
        link_status=LinkStatus.AUTO,
        link_confidence=0.97,
    )
    seed.events["main"] = ev.id
    mw = _market(session, ev, "Zwycięzca meczu", MW)
    s_t1 = _selection(session, mw, "Synth Team 1", "team1", "1.95")
    s_t2 = _selection(session, mw, "Synth Team 2", "team2", "1.80")
    hcp = _market(session, ev, "Handicap map", HCP)
    h_t1 = _selection(session, hcp, "Synth Team 1 (-1.5)", "team1", "3.10")
    _selection(session, hcp, "Synth Team 2 (+1.5)", "team2", "1.35")
    tot = _market(session, ev, "Liczba map", TOTAL)
    _selection(session, tot, "Powyżej 2.5", "over", "2.20", age_s=3600)
    _selection(session, tot, "Poniżej 2.5", "under", "1.62", age_s=3600)
    odd = _market(session, ev, "Dziwny rynek", None)
    _selection(session, odd, "Coś", None, "5.00")
    seed.selections.update(team1=s_t1.id, team2=s_t2.id, hcp_team1=h_t1.id)
    for hours, odds in ((-3, "2.05"), (-2, "2.00"), (-1, "1.95")):
        session.add(OddsSnapshot(selection_id=s_t1.id, odds=Decimal(odds), captured_at=_ts(hours=hours)))
    session.add(OddsSnapshot(selection_id=s_t2.id, odds=Decimal("1.80"), captured_at=_ts(hours=-3)))

    # predictions for 101 (+ an old non-latest one that must be ignored)
    _prediction(session, 101, MW, "team1", 0.70, latest=False, created=_ts(hours=-5))
    seed.predictions["team1"] = _prediction(session, 101, MW, "team1", 0.80).id
    _prediction(session, 101, MW, "team2", 0.20)
    _prediction(session, 101, HCP, "team1", 0.45)
    _prediction(session, 101, HCP, "team2", 0.55)
    _prediction(session, 101, CMS, "2-0", 0.35, sigma=0.04)

    core = PredictionCore(
        p_team1_win=0.8,
        map_score_pmf={"2-0": 0.35, "2-1": 0.3, "1-2": 0.2, "0-2": 0.15},
        slots=[
            MapSlotPrediction(
                map_number=1,
                p_played=1.0,
                map_distribution={"mirage": 1.0},
                p_team1_win=0.7,
                score_pmf={"13-9": 1.0},
                p_overtime=0.1,
            )
        ],
    )
    payload = MatchPrediction(
        match_id=101,
        created_at=_ts(minutes=-20),
        model_version="test-1",
        format=MatchFormat.BO3,
        team1_id=1,
        team2_id=2,
        central=core,
        sigma_team1_win=0.03,
    ).model_dump(mode="json")
    session.add(
        MatchAnalysis(
            match_id=101,
            created_at=_ts(hours=-3),
            model_version="test-0",
            payload={"old": True},
            rationale="old",
            is_latest=False,
        )
    )
    session.add(
        MatchAnalysis(
            match_id=101,
            created_at=_ts(minutes=-20),
            model_version="test-1",
            payload=payload,
            rationale="Team 1 is much stronger on Mirage.",
            rationale_model="claude-opus-5-5",
            is_latest=True,
        )
    )

    # decisions for 101: an older rejection then an acceptance for team1; a rejection for the handicap
    _decision(
        session,
        101,
        MW,
        "team1",
        accepted=False,
        decided=_ts(hours=-2),
        odds="2.00",
        p_win=0.7,
        ev=0.2,
        reasons=["sigma above threshold"],
        selection_id=s_t1.id,
    )
    d_ok = _decision(
        session,
        101,
        MW,
        "team1",
        accepted=True,
        decided=_ts(minutes=-30),
        odds="1.95",
        p_win=0.8,
        ev=0.3728,
        selection_id=s_t1.id,
        stake="15.00",
    )
    _decision(
        session,
        101,
        HCP,
        "team1",
        accepted=False,
        decided=_ts(minutes=-30),
        odds="3.10",
        p_win=0.45,
        ev=0.2276,
        reasons=["p_win 0.45 < 0.75"],
        selection_id=h_t1.id,
    )
    seed.decisions["accepted"] = d_ok.id

    # ---------------------------------------------------------------- review queue events
    pending = _event(
        session,
        "ev-103",
        name="Synth Team 6 - Synth Team 5",
        team1_raw="Synth Team 6",
        team2_raw="Synth Team 5",
        starts_at=_ts(hours=3, minutes=15),
        match_id=103,
        link_status=LinkStatus.PENDING_REVIEW,
        link_confidence=0.7,
        teams_swapped=False,
    )
    pm = _market(session, pending, "Zwycięzca meczu", MW)
    _selection(session, pm, "Synth Team 6", "team1", "1.70")
    _selection(session, pm, "Synth Team 5", "team2", "2.10")
    seed.events["pending_swapped"] = pending.id
    unlinked = _event(
        session,
        "ev-102",
        name="Synth Team 3 vs Synth Team 4",
        team1_raw="SYNTH TEAM 3",
        team2_raw="Synth Team 4 Esports",
        starts_at=_ts(days=2, minutes=10),
        link_status=LinkStatus.PENDING_REVIEW,
        link_confidence=0.55,
    )
    seed.events["pending_unlinked"] = unlinked.id
    rejected = _event(
        session,
        "ev-old",
        name="Old event",
        team1_raw="A",
        team2_raw="B",
        starts_at=_ts(days=-1),
        link_status=LinkStatus.REJECTED,
    )
    seed.events["rejected"] = rejected.id

    # ---------------------------------------------------------------- decisions/bets on finished match 1
    d_won = _decision(
        session,
        1,
        MW,
        "team1",
        accepted=True,
        decided=_ts(days=-3, hours=-3),
        odds="1.90",
        p_win=0.78,
        ev=0.30,
        stake="20.00",
    )
    d_lost = _decision(
        session,
        2,
        MAP1,
        "team2",
        accepted=True,
        decided=_ts(hours=-5),
        odds="2.10",
        p_win=0.76,
        ev=0.40,
        stake="30.00",
    )
    seed.decisions.update(won=d_won.id, lost=d_lost.id)

    b_won = _bet(
        session,
        "won",
        decision_id=d_won.id,
        match_id=1,
        stake=Decimal("20.00"),
        placed_odds=Decimal("1.90"),
        potential_payout=Decimal("33.44"),
        status=BetStatus.WON,
        dry_run=False,
        external_bet_id="BC-1",
        placed_at=datetime(2026, 11, 17, 10, 0, tzinfo=UTC),
        settled_at=datetime(2026, 11, 17, 14, 0, tzinfo=UTC),
        payout=Decimal("33.44"),
        pnl=Decimal("13.44"),
        closing_odds=Decimal("1.84"),
        clv=0.03,
    )
    b_lost = _bet(
        session,
        "lost",
        decision_id=d_lost.id,
        match_id=2,
        spec_key=MAP1.key(),
        selection_key="team2",
        market_raw="Zwycięzca mapy 1",
        selection_raw="Synth Team X",
        stake=Decimal("30.00"),
        requested_odds=Decimal("2.10"),
        placed_odds=Decimal("2.10"),
        status=BetStatus.LOST,
        dry_run=False,
        placed_at=datetime(2026, 11, 20, 8, 0, tzinfo=UTC),
        settled_at=datetime(2026, 11, 20, 11, 0, tzinfo=UTC),
        payout=Decimal("0"),
        pnl=Decimal("-30.00"),
        clv=-0.01,
    )
    b_open = _bet(
        session,
        "open",
        decision_id=d_ok.id,
        match_id=101,
        selection_id=s_t1.id,
        external_event_id="ev-101",
        stake=Decimal("15.00"),
        requested_odds=Decimal("1.95"),
        placed_odds=Decimal("1.95"),
        status=BetStatus.PLACED,
        dry_run=False,
        placed_at=datetime(2026, 11, 20, 11, 30, tzinfo=UTC),
        screenshot_path="bet_open.png",
    )
    b_void = _bet(
        session,
        "void",
        match_id=3,
        spec_key=TOTAL.key(),
        selection_key="over",
        stake=Decimal("10.00"),
        status=BetStatus.VOID,
        dry_run=False,
        placed_at=datetime(2026, 11, 17, 12, 0, tzinfo=UTC),
        settled_at=datetime(2026, 11, 18, 9, 0, tzinfo=UTC),
        payout=Decimal("10.00"),
        pnl=Decimal("0.00"),
    )
    b_failed = _bet(
        session,
        "failed",
        match_id=101,
        stake=Decimal("5.00"),
        status=BetStatus.FAILED,
        dry_run=False,
        error="odds moved",
    )
    b_escape = _bet(
        session,
        "escape",
        match_id=101,
        stake=Decimal("5.00"),
        status=BetStatus.CANCELLED,
        dry_run=False,
        screenshot_path=str(outside),
    )
    b_paper_open = _bet(
        session,
        "paper-open",
        match_id=101,
        spec_key=HCP.key(),
        selection_key="team2",
        stake=Decimal("10.00"),
        status=BetStatus.DRY_RUN,
        dry_run=True,
        placed_at=datetime(2026, 11, 20, 9, 0, tzinfo=UTC),
    )
    b_paper_won = _bet(
        session,
        "paper-won",
        match_id=4,
        stake=Decimal("10.00"),
        placed_odds=Decimal("2.00"),
        status=BetStatus.WON,
        dry_run=True,
        placed_at=datetime(2026, 11, 19, 8, 0, tzinfo=UTC),
        settled_at=datetime(2026, 11, 19, 10, 0, tzinfo=UTC),
        payout=Decimal("17.60"),
        pnl=Decimal("7.60"),
    )
    for key, bet in (
        ("won", b_won),
        ("lost", b_lost),
        ("open", b_open),
        ("void", b_void),
        ("failed", b_failed),
        ("escape", b_escape),
        ("paper_open", b_paper_open),
        ("paper_won", b_paper_won),
    ):
        seed.bets[key] = bet.id

    ledger: list[tuple[datetime, LedgerKind, str, str, int | None]] = [
        (datetime(2026, 11, 10, 9, 0, tzinfo=UTC), LedgerKind.DEPOSIT, "1000.00", "1000.00", None),
        (datetime(2026, 11, 17, 10, 0, tzinfo=UTC), LedgerKind.STAKE, "-20.00", "980.00", b_won.id),
        (datetime(2026, 11, 17, 12, 0, tzinfo=UTC), LedgerKind.STAKE, "-10.00", "970.00", b_void.id),
        (datetime(2026, 11, 17, 14, 0, tzinfo=UTC), LedgerKind.PAYOUT, "33.44", "1003.44", b_won.id),
        (datetime(2026, 11, 18, 9, 0, tzinfo=UTC), LedgerKind.REFUND, "10.00", "1013.44", b_void.id),
        (datetime(2026, 11, 20, 8, 0, tzinfo=UTC), LedgerKind.STAKE, "-30.00", "983.44", b_lost.id),
        (datetime(2026, 11, 20, 11, 30, tzinfo=UTC), LedgerKind.STAKE, "-15.00", "968.44", b_open.id),
    ]
    for ts, kind, amount, balance, bet_id in ledger:
        session.add(
            BankrollLedger(
                ts=ts, kind=kind, amount=Decimal(amount), balance_after=Decimal(balance), bet_id=bet_id
            )
        )

    # ---------------------------------------------------------------- calibration predictions (finished 1-10)
    # written one hour before each match started (later predictions are look-ahead and are ignored)
    for mid in range(1, 11):
        finished = session.get(Match, mid)
        assert finished is not None and finished.scheduled_at is not None
        before_start = finished.scheduled_at - timedelta(hours=1)
        _prediction(session, mid, MW, "team1", 0.6, created=before_start)
        _prediction(session, mid, MW, "team2", 0.4, created=before_start)
        _prediction(session, mid, MAP1, "team1", 0.55, created=before_start)

    # ---------------------------------------------------------------- job runs
    def run(
        name: str,
        status: JobStatus,
        started: datetime,
        *,
        minutes: float | None = 1,
        error: str | None = None,
    ) -> None:
        finished = started + timedelta(minutes=minutes) if minutes is not None else None
        session.add(
            JobRun(
                job_name=name, status=status, started_at=started, finished_at=finished, items=3, error=error
            )
        )

    run("odds_collect", JobStatus.SUCCESS, _ts(minutes=-8))
    run("odds_collect", JobStatus.SUCCESS, _ts(minutes=-2))
    run("ingest_upcoming", JobStatus.SUCCESS, _ts(hours=-5))
    run("settle_bets", JobStatus.SUCCESS, _ts(hours=-2))
    for minutes in (-90, -60, -30):
        run("settle_bets", JobStatus.FAILED, _ts(minutes=minutes), error="RuntimeError: boom")
    run("predict_upcoming", JobStatus.SUCCESS, _ts(hours=-3, minutes=-30))
    run("predict_upcoming", JobStatus.RUNNING, _ts(hours=-3), minutes=None)

    state.set_state(
        session,
        state.EXECUTOR_HEALTH,
        {
            "ok": False,
            "logged_in": False,
            "balance": "968.44",
            "selectors_ok": {"stake_input": True, "confirm_button": False},
            "message": "session expired",
            "checked_at": _ts(minutes=-5).isoformat(),
        },
    )
    session.flush()
    return seed


def make_settings(tmp: Path, *, api: dict[str, Any] | None = None, **sections: Any) -> Settings:
    """Settings for tests: no dashboard dir (unless given) and screenshots under ``tmp``."""
    api_cfg: dict[str, Any] = {"dashboard_dist_dir": str(tmp / "no-dashboard")}
    api_cfg.update(api or {})
    executor = {"screenshot_dir": str(tmp / "screenshots")}
    executor.update(sections.pop("executor", {}))
    return Settings(api=api_cfg, executor=executor, **sections)
