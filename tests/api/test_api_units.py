"""Unit tests of the API's pure helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr

from cs2bot.api.analytics import (
    BetFact,
    bet_stats,
    brier_by_month,
    calibration_set,
    downsample,
    max_drawdown,
    pnl_by_period,
    wilson_interval,
)
from cs2bot.api.auth import LoginRateLimiter, SessionAuth
from cs2bot.api.configedit import ConfigValidationError, normalize_overrides
from cs2bot.api.ev import breakeven_odds, effective_odds, ev_after_tax, fair_odds
from cs2bot.api.formatting import (
    describe_outcome,
    market_type_of,
    money_str,
    odds_str,
    parse_spec_key,
    round_or_none,
)
from cs2bot.api.health import expected_interval_s, job_kind, stale_after_s
from cs2bot.api.schemas import ConfigIn
from cs2bot.api.security import AllowedHosts, build_allowed_hosts, build_allowed_origins, split_host_header
from cs2bot.config import APISettings, SchedulerSettings, Settings
from cs2bot.domain.enums import BetStatus
from cs2bot.domain.markets import MarketSpec, MarketType

# ------------------------------------------------------------------ formatting


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("12.5"), "12.50"),
        (Decimal("0.005"), "0.01"),
        (Decimal("-0.004"), "0.00"),
        (Decimal("-3.333"), "-3.33"),
        (7, "7.00"),
        (0.1 + 0.2, "0.30"),
        ("not money", None),
        (None, None),
        (Decimal("NaN"), None),
    ],
)
def test_money_str(value: object, expected: str | None) -> None:
    assert money_str(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1.850"), "1.85"),
        (Decimal("2"), "2.00"),
        (Decimal("1.875"), "1.875"),
        (1.9, "1.90"),
        (None, None),
    ],
)
def test_odds_str(value: object, expected: str | None) -> None:
    assert odds_str(value) == expected


@pytest.mark.parametrize(
    "spec",
    [
        MarketSpec(market_type=MarketType.MATCH_WINNER),
        MarketSpec(market_type=MarketType.TOTAL_ROUNDS, map_number=2, line=26.5),
        MarketSpec(market_type=MarketType.MAP_HANDICAP, line=-1.5),
        MarketSpec(market_type=MarketType.TEAM_TOTAL_ROUNDS, map_number=1, team="team2", line=12.5),
        MarketSpec(market_type=MarketType.PLAYER_KILLS, player_id=12, maps_scope=(1, 2), line=33.5),
        MarketSpec(market_type=MarketType.PLAYER_KILLS_H2H, player_id=3, opponent_player_id=8, map_number=1),
        MarketSpec(market_type=MarketType.HALF_WINNER, map_number=1, half=2),
        MarketSpec(market_type=MarketType.PISTOL_ROUND_WINNER, map_number=3, round_number=13),
    ],
)
def test_parse_spec_key_roundtrip(spec: MarketSpec) -> None:
    parsed = parse_spec_key(spec.key())
    if "maps_scope" in parsed:
        parsed["maps_scope"] = tuple(parsed["maps_scope"])
    assert MarketSpec(**parsed) == spec
    assert market_type_of(spec.key()) == spec.market_type.value


def test_parse_spec_key_ignores_garbage() -> None:
    assert parse_spec_key("map_winner|map=x|weird|foo=1|line=2") == {"market_type": "map_winner", "line": 2.0}


@pytest.mark.parametrize(
    ("spec", "selection", "expected"),
    [
        ({"market_type": "match_winner"}, "team2", "Match winner: FaZe"),
        ({"market_type": "match_winner"}, "draw", "Match winner: draw"),
        ({"market_type": "map_winner", "map_number": 2}, "team1", "Map 2 winner: NAVI"),
        ({"market_type": "map_handicap", "line": -1.5}, "team1", "Map handicap: NAVI -1.5"),
        ({"market_type": "map_handicap", "line": -1.5}, "team2", "Map handicap: FaZe +1.5"),
        (
            {"market_type": "round_handicap", "map_number": 1, "line": 3.5},
            "team2",
            "Map 1 round handicap: FaZe -3.5",
        ),
        ({"market_type": "total_maps", "line": 2.5}, "over", "Total maps over 2.5"),
        (
            {"market_type": "total_rounds", "map_number": 1, "line": 26.5},
            "under",
            "Map 1 total rounds under 26.5",
        ),
        (
            {"market_type": "team_total_rounds", "map_number": 1, "team": "team2", "line": 12.5},
            "over",
            "Map 1 FaZe rounds over 12.5",
        ),
        ({"market_type": "total_rounds_odd_even", "map_number": 3}, "odd", "Map 3 total rounds: odd"),
        ({"market_type": "correct_map_score"}, "2-1", "Correct map score: NAVI 2-1 FaZe"),
        (
            {"market_type": "map_correct_score", "map_number": 1},
            "13-9",
            "Map 1 correct score: NAVI 13-9 FaZe",
        ),
        ({"market_type": "map_overtime", "map_number": 1}, "yes", "Map 1 overtime: yes"),
        (
            {"market_type": "pistol_round_winner", "map_number": 1, "round_number": 13},
            "team1",
            "Map 1 pistol round 13: NAVI",
        ),
        (
            {"market_type": "race_to_rounds", "map_number": 1, "line": 5},
            "team2",
            "Map 1 race to 5 rounds: FaZe",
        ),
        ({"market_type": "half_winner", "map_number": 2, "half": 1}, "draw", "Map 2 half 1: draw"),
        ({"market_type": "both_teams_win_a_map"}, "no", "Both teams win a map: no"),
        (
            {"market_type": "player_kills", "player_id": 7, "maps_scope": [1, 2], "line": 33.5},
            "over",
            "s1mple kills (maps 1-2) over 33.5",
        ),
        (
            {"market_type": "player_kills_h2h", "player_id": 7, "opponent_player_id": 9, "map_number": 1},
            "player:9",
            "Kills duel (map 1) s1mple vs player #9: player #9",
        ),
        (
            {"market_type": "top_fragger", "map_number": 1, "team": "team1"},
            "player:7",
            "Top fragger of NAVI (map 1): s1mple",
        ),
        ({"market_type": "other"}, "x", "Other: x"),
    ],
)
def test_describe_outcome(spec: dict, selection: str, expected: str) -> None:
    assert describe_outcome(spec, selection, team1="NAVI", team2="FaZe", players={7: "s1mple"}) == expected


def test_describe_outcome_without_team_names() -> None:
    assert (
        describe_outcome({"market_type": "match_winner"}, "team1", team1=None, team2=None)
        == "Match winner: Team 1"
    )


def test_round_or_none() -> None:
    assert round_or_none(float("nan")) is None and round_or_none(float("inf")) is None
    assert round_or_none(0.1234567891) == 0.123457 and round_or_none(None) is None


# ------------------------------------------------------------------ EV


def test_ev_after_tax() -> None:
    assert effective_odds(Decimal("2.00"), Decimal("0.12")) == Decimal("1.7600")
    assert ev_after_tax(0.6, Decimal("2.00"), Decimal("0.12")) == pytest.approx(0.6 * 1.76 - 1)
    assert ev_after_tax(0.6, Decimal("2.00"), Decimal("0")) == pytest.approx(0.2)
    # pushes and voids refund the full gross stake
    assert ev_after_tax(0.5, Decimal("2.00"), Decimal("0.12"), p_push=0.1, p_void=0.2) == pytest.approx(
        0.5 * 1.76 + 0.3 - 1
    )
    # PLAN.md: at p = 0.75 the break-even odds after tax are ~1.52
    assert float(breakeven_odds(0.75, Decimal("0.12")) or 0) == pytest.approx(1 / (0.75 * 0.88))
    assert float(fair_odds(0.5, p_void=0.2) or 0) == pytest.approx(1.6)
    assert fair_odds(0.0) is None and breakeven_odds(0.0, Decimal("0.12")) is None


# ------------------------------------------------------------------ analytics


def _fact(
    i: int, status: BetStatus, stake: str, pnl: str | None, *, settled: datetime | None = None, **kw: object
) -> BetFact:
    base: dict = {
        "id": i,
        "match_id": 1,
        "market_type": "match_winner",
        "status": status,
        "dry_run": False,
        "stake": Decimal(stake),
        "pnl": Decimal(pnl) if pnl is not None else None,
        "odds": Decimal("2.0"),
        "clv": None,
        "ev": None,
        "p_win": None,
        "placed_at": None,
        "settled_at": settled,
        "created_at": None,
    }
    base.update(kw)
    return BetFact(**base)


def test_bet_stats_and_drawdown() -> None:
    t = datetime(2026, 1, 5, 12, tzinfo=UTC)
    facts = [
        _fact(1, BetStatus.WON, "10", "8.00", settled=t, clv=0.05),
        _fact(2, BetStatus.LOST, "10", None, settled=t + timedelta(hours=1)),  # missing pnl -> -stake
        _fact(3, BetStatus.VOID, "10", "0", settled=t + timedelta(hours=2)),
        _fact(4, BetStatus.PLACED, "5", None),
        _fact(5, BetStatus.FAILED, "50", None),
        _fact(6, BetStatus.CANCELLED, "50", None),
    ]
    s = bet_stats(facts)
    assert (s.bets, s.open_bets, s.settled_bets, s.won, s.lost, s.void) == (4, 1, 3, 1, 1, 1)
    assert (
        s.turnover == Decimal("20") and s.realized_pnl == Decimal("-2.00") and s.open_stakes == Decimal("5")
    )
    assert s.hit_rate == 0.5 and s.yield_rate == pytest.approx(-0.1) and s.avg_clv == 0.05
    assert max_drawdown([Decimal(x) for x in ("8", "-10", "0", "5", "-20")], Decimal("100")) == (
        Decimal("25"),
        pytest.approx(25 / 108),
    )
    assert max_drawdown([], Decimal("100")) == (Decimal("0"), None)
    assert max_drawdown([Decimal("-5")], Decimal("0")) == (Decimal("5"), None)


def test_pnl_by_period_uses_local_dates() -> None:
    # 23:30 UTC on Sunday 2026-01-04 is already Monday 00:30 in Warsaw
    late = datetime(2026, 1, 4, 23, 30, tzinfo=UTC)
    facts = [
        _fact(1, BetStatus.WON, "10", "5", settled=late),
        _fact(2, BetStatus.LOST, "10", "-10", settled=late - timedelta(days=1)),
    ]
    days = pnl_by_period(facts, "Europe/Warsaw", "day")
    assert [(r.period, r.stats.realized_pnl) for r in days] == [
        ("2026-01-04", Decimal("-10")),
        ("2026-01-05", Decimal("5")),
    ]
    weeks = pnl_by_period(facts, "Europe/Warsaw", "week")
    assert [r.period for r in weeks] == ["2026-W01", "2026-W02"]
    assert [r.period for r in pnl_by_period(facts, "UTC", "week")] == ["2026-W01"]
    assert [r.period for r in pnl_by_period(facts, "Europe/Warsaw", "month")] == ["2026-01"]
    with pytest.raises(ValueError):
        pnl_by_period(facts, "UTC", "year")


def test_calibration_set() -> None:
    samples = [(0.05, 0), (0.15, 0), (0.95, 1), (0.95, 1), (0.92, 0), (1.0, 1), (0.0, 0)]
    cal = calibration_set(samples, 10)
    assert cal.n == 7 and len(cal.bins) == 10
    top = cal.bins[9]
    assert top.count == 4 and top.mean_predicted == pytest.approx((0.95 + 0.95 + 0.92 + 1.0) / 4)
    assert top.observed_rate == 0.75
    assert cal.bins[0].count == 2 and cal.bins[5].count == 0 and cal.bins[5].observed_rate is None
    expected_brier = sum((p - y) ** 2 for p, y in samples) / 7
    assert cal.brier == pytest.approx(expected_brier, abs=1e-6)
    assert cal.ece is not None and 0 <= cal.ece <= 1 and cal.log_loss is not None
    assert calibration_set([], 5).n == 0
    assert wilson_interval(0, 0) is None
    lo, hi = wilson_interval(5, 10) or (0, 0)
    assert lo < 0.5 < hi
    months = brier_by_month(
        [(0.6, 1, datetime(2026, 1, 31, 23, 30, tzinfo=UTC)), (0.6, 0, None)], "Europe/Warsaw"
    )
    assert [(m.period, m.n) for m in months] == [("2026-02", 1)]


def test_downsample() -> None:
    pts = list(range(100))
    out = downsample(pts, 10)
    assert len(out) <= 11 and out[0] == 0 and out[-1] == 99 and out == sorted(out)
    assert downsample(pts[:5], 10) == pts[:5] and downsample(pts, 1) == [99]


# ------------------------------------------------------------------ security helpers


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("localhost", ("localhost", None)),
        ("LocalHost:8000", ("localhost", 8000)),
        ("[::1]:8000", ("::1", 8000)),
        ("[::1]", ("::1", None)),
        ("::1", None),
        ("[evil.com]:80", None),
        ("host:99999", None),
        ("host:abc", None),
        ("a b", None),
        ("user@host", None),
        ("", None),
        ("[]", None),
        ("[::1]x", None),
    ],
)
def test_split_host_header(header: str, expected: tuple[str, int | None] | None) -> None:
    assert split_host_header(header) == expected


def test_allowed_hosts_and_origins() -> None:
    api = APISettings(port=8000, cors_origins=["http://localhost:5173", "garbage"])
    hosts = build_allowed_hosts(api, ["Example.LAN"])
    assert hosts.is_allowed("example.lan:8000") and hosts.is_allowed("localhost:5173")
    assert not hosts.is_allowed(None) and not hosts.is_allowed("localhost:1")
    origins = build_allowed_origins(api, hosts)
    assert (
        "http://127.0.0.1:8000" in origins
        and "http://[::1]:8000" in origins
        and "http://localhost:5173" in origins
    )
    wildcard = build_allowed_hosts(APISettings.model_construct(host="0.0.0.0", port=8000, cors_origins=[]))
    assert "127.0.0.1" in wildcard.hostnames and "0.0.0.0" not in wildcard.hostnames
    assert AllowedHosts(frozenset({"x"}), frozenset({None})).origins() == frozenset({"http://x"})


# ------------------------------------------------------------------ auth


def test_session_auth_tokens() -> None:
    auth = SessionAuth(username="admin", password=SecretStr("pw"), secret=SecretStr("k" * 32), max_age_s=3600)
    assert auth.enabled and not auth.ephemeral_secret
    assert auth.check_credentials("admin", "pw")
    assert not auth.check_credentials("admin", "pw ") and not auth.check_credentials("Admin", "pw")
    token = auth.issue()
    info = auth.verify(token)
    assert info is not None and info.username == "admin"
    assert auth.verify(token + "x") is None and auth.verify("") is None and auth.verify(None) is None
    other = SessionAuth(
        username="admin", password=SecretStr("pw"), secret=SecretStr("q" * 32), max_age_s=3600
    )
    assert other.verify(token) is None  # different secret
    renamed = SessionAuth(
        username="root", password=SecretStr("pw"), secret=SecretStr("k" * 32), max_age_s=3600
    )
    assert renamed.verify(token) is None
    auth.revoke(info.sid)
    assert auth.verify(token) is None and auth.verify(auth.issue()) is not None
    disabled = SessionAuth(username="admin", password=None, secret=None, max_age_s=10)
    assert not disabled.enabled and disabled.check_credentials("x", "y") and disabled.verify(token) is None
    assert disabled.ephemeral_secret and disabled.max_age_s == 60


def test_login_rate_limiter() -> None:
    now = [1000.0]
    limiter = LoginRateLimiter(max_failures=2, global_max_failures=3, window_s=60, clock=lambda: now[0])
    assert limiter.retry_after("a") is None
    limiter.record_failure("a")
    limiter.record_failure("a")
    wait = limiter.retry_after("a")
    assert wait is not None and wait == pytest.approx(60)
    assert limiter.retry_after("b") is None
    limiter.record_failure("b")  # third failure overall -> global limit
    assert limiter.retry_after("c") is not None
    now[0] += 61
    assert limiter.retry_after("a") is None and limiter.retry_after("c") is None
    limiter.record_failure("a")
    limiter.reset("a")
    assert limiter.retry_after("a") is None


# ------------------------------------------------------------------ config validation


def test_normalize_overrides_semantics() -> None:
    settings = Settings()
    current = {"decision": {"min_ev": 0.06}, "bankroll": {"kelly_fraction": 0.2}}
    out = normalize_overrides(
        ConfigIn(bankroll={"max_bets_per_day": 3, "currency": "PLN"}), settings, current
    )
    assert out == {"decision": {"min_ev": 0.06}, "bankroll": {"max_bets_per_day": 3}}
    assert normalize_overrides(ConfigIn(decision={}, bankroll={}), settings, current) == {}
    decimal = normalize_overrides(ConfigIn(decision={"max_odds": 9}), settings, {})
    assert decimal == {"decision": {"max_odds": "9"}}  # JSON-safe (Decimal stored as string)
    with pytest.raises(ConfigValidationError) as err:
        normalize_overrides(ConfigIn(decision={"min_odds": "5", "max_odds": "4"}), settings, {})
    assert len(err.value.errors) == 1  # the shared odds check runs once
    with pytest.raises(ConfigValidationError) as err2:
        normalize_overrides(ConfigIn(decision={"foo": 1}, bankroll={"kelly_fraction": 2}), settings, {})
    assert {e["loc"][-1] for e in err2.value.errors} == {"foo", "kelly_fraction"}


# ------------------------------------------------------------------ health helpers


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("odds_collect", "odds"),
        ("betclic.odds", "odds"),
        ("ingest_upcoming", "upcoming"),
        ("ingest.results", "results"),
        ("news_poll", "news"),
        ("predict_upcoming", "predict"),
        ("predict_and_bet", "predict"),
        ("bet_run", "bet"),
        ("settle_bets", "settle"),
        ("demos_process", "demos"),
        ("hltv_rankings", "rankings"),
        ("models_retrain", "retrain"),
        ("db_backup", "backup"),
        ("balance_reconcile", "reconcile"),
        ("betclic_login", None),
        ("misc", None),
    ],
)
def test_job_kind(name: str, kind: str | None) -> None:
    assert job_kind(name) == kind


def test_expected_intervals() -> None:
    sched = SchedulerSettings()
    assert expected_interval_s("odds", sched) == 180 and expected_interval_s("upcoming", sched) == 1800
    assert expected_interval_s(None, sched) is None and expected_interval_s("retrain", sched) == 86400
    assert stale_after_s(180) == 960
