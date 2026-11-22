"""Tests for the shared foundation: config, domain contracts, utils, DB schema, synthetic data."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from cs2bot.config import Settings
from cs2bot.db import state
from cs2bot.db.jobs import track_job
from cs2bot.db.models import MapPlayed, Match, Player, PlayerMapStats, Round, Team, Veto
from cs2bot.db.repo import resolve_player, resolve_team, resolve_tournament
from cs2bot.domain.enums import DataSource, JobStatus, MatchFormat, Side, TeamSlot, Tier, VetoAction
from cs2bot.domain.markets import (
    MarketSpec,
    MarketType,
    parse_player_selection,
    parse_score_selection,
    player_selection,
)
from cs2bot.domain.predictions import OutcomeProbability
from cs2bot.testing.synthetic import generate_universe, populate_db, synthetic_two_way_odds
from cs2bot.utils.money import D, round_down_to_step
from cs2bot.utils.names import normalize_map_name, normalize_team_name, team_name_similarity
from cs2bot.utils.time import ensure_utc, local_day_start, local_week_start, parse_datetime

# ------------------------------------------------------------------ config


def test_settings_defaults(settings: Settings) -> None:
    assert settings.environment == "test"
    assert settings.decision.min_probability == 0.75
    assert settings.bankroll.kelly_fraction == 0.25
    assert settings.effective_tax_rate == Decimal("0.12")
    assert settings.executor.dry_run is True


def test_settings_env_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CS2BOT_BANKROLL__KELLY_FRACTION", "0.5")
    monkeypatch.setenv("CS2BOT_BETCLIC__TAX_FREE_PROMO", "true")
    s = Settings()
    assert s.bankroll.kelly_fraction == 0.5
    assert s.effective_tax_rate == Decimal("0")


def test_blank_env_values_are_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CS2BOT_API__PASSWORD", "")
    monkeypatch.setenv("CS2BOT_HLTV__PROXY_URL", "  ")
    monkeypatch.setenv("CS2BOT_PANDASCORE__API_TOKEN", "abc")
    s = Settings()
    assert s.api.password is None and s.hltv.proxy_url is None
    assert s.pandascore.api_token is not None and s.pandascore.api_token.get_secret_value() == "abc"


def test_non_loopback_api_requires_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CS2BOT_ENVIRONMENT", "prod")
    assert Settings().api.is_loopback  # local-only default needs no password
    monkeypatch.setenv("CS2BOT_API__HOST", "0.0.0.0")
    with pytest.raises(ValueError, match="non-loopback"):
        Settings()


# ------------------------------------------------------------------ domain


def test_market_spec_key_stable_and_hashable() -> None:
    a = MarketSpec(market_type=MarketType.TOTAL_ROUNDS, map_number=1, line=26.5)
    b = MarketSpec(market_type=MarketType.TOTAL_ROUNDS, map_number=1, line=26.5)
    assert a.key() == "total_rounds|map=1|line=26.5"
    assert a == b and hash(a) == hash(b)
    k = MarketSpec(market_type=MarketType.PLAYER_KILLS, player_id=12, maps_scope=(1, 2), line=33.5).key()
    assert k == "player_kills|maps=1,2|player=12|line=33.5"
    with pytest.raises(ValueError):
        MarketSpec(market_type=MarketType.TEAM_TOTAL_ROUNDS, team="home")


def test_selection_helpers() -> None:
    assert parse_player_selection(player_selection(42)) == 42
    assert parse_player_selection("team1") is None
    assert parse_score_selection("13-9") == (13, 9)
    assert parse_score_selection("over") is None


def test_outcome_probability_derived() -> None:
    o = OutcomeProbability(spec_key="x", selection="team1", p_win=0.6, p_void=0.2, sigma=0.02)
    assert o.p_lose == pytest.approx(0.2)
    assert o.p_win_given_settled == pytest.approx(0.75)


def test_match_format_properties() -> None:
    assert MatchFormat.BO3.n_maps == 3 and MatchFormat.BO3.maps_to_win == 2
    assert MatchFormat.BO2.maps_to_win is None
    assert MatchFormat.BO5.min_maps == 3


# ------------------------------------------------------------------ utils


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Natus Vincere", "natus vincere"),
        ("NAVI", "natus vincere"),
        ("Team Vitality", "vitality"),
        ("MOUZ", "mouz"),
        ("mousesports", "mouz"),
        ("FaZe Clan", "faze"),
        ("G2 Esports", "g2"),
        ("Team Spirit", "spirit"),
        ("Spirit Academy", "spirit academy"),
    ],
)
def test_normalize_team_name(raw: str, expected: str) -> None:
    assert normalize_team_name(raw) == expected


def test_team_similarity() -> None:
    assert team_name_similarity("Natus Vincere", "NAVI") == 100.0
    assert team_name_similarity("Heroic", "HEROIC Esports") == 100.0
    assert team_name_similarity("Vitality", "Virtus.pro") < 80


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("de_dust2", "dust2"), ("Dust II", "dust2"), ("MIRAGE", "mirage"), ("de_ancient", "ancient")],
)
def test_normalize_map_name(raw: str, expected: str) -> None:
    assert normalize_map_name(raw) == expected


def test_money_helpers() -> None:
    assert D(0.1) + D(0.2) == Decimal("0.3")
    assert round_down_to_step(Decimal("23.47"), Decimal("0.10")) == Decimal("23.40")
    assert round_down_to_step(Decimal("23.47"), Decimal("1")) == Decimal("23.00")


def test_time_helpers() -> None:
    assert parse_datetime("2026-03-01T10:00:00Z") == datetime(2026, 3, 1, 10, tzinfo=UTC)
    assert parse_datetime(1_700_000_000_000) == parse_datetime(1_700_000_000)
    assert ensure_utc(datetime(2026, 1, 1)).tzinfo is UTC
    # Warsaw is UTC+1 in winter: local midnight Jan 10 = 23:00 UTC Jan 9
    now = datetime(2026, 1, 10, 12, tzinfo=UTC)
    assert local_day_start(now) == datetime(2026, 1, 9, 23, tzinfo=UTC)
    assert local_week_start(now).weekday() == 6  # Sunday 23:00 UTC == Monday 00:00 Warsaw


# ------------------------------------------------------------------ DB


def test_naive_datetime_rejected(session) -> None:
    session.add(Team(name="X"))
    session.flush()
    m = Match(team1_id=None, team2_id=None, scheduled_at=datetime(2026, 1, 1))  # naive -> error
    session.add(m)
    with pytest.raises(Exception, match="naive datetime"):
        session.flush()


def test_resolve_team_links_sources(session) -> None:
    a = resolve_team(session, name="Natus Vincere", source=DataSource.PANDASCORE, external_id=101)
    b = resolve_team(session, name="NAVI", source=DataSource.HLTV, external_id=4608)
    assert a is not None and b is not None and a.id == b.id
    assert b.pandascore_id == 101 and b.hltv_id == 4608
    c = resolve_team(session, name="Natus Vincere Esports", source=DataSource.PANDASCORE, external_id=101)
    assert c is not None and c.id == a.id
    other = resolve_team(session, name="Heroic", source=DataSource.HLTV, external_id=7175)
    assert other is not None and other.id != a.id


def test_resolve_team_same_name_different_external_id(session) -> None:
    a = resolve_team(session, name="Falcons", source=DataSource.HLTV, external_id=1)
    b = resolve_team(session, name="Falcons", source=DataSource.HLTV, external_id=2)
    assert a is not None and b is not None and a.id != b.id


def test_resolve_player_and_tournament(session) -> None:
    team = resolve_team(session, name="Vitality", source=DataSource.HLTV, external_id=9565)
    assert team is not None
    p1 = resolve_player(session, nickname="ZywOo", source=DataSource.HLTV, external_id=11893, team_id=team.id)
    p2 = resolve_player(
        session, nickname="zywoo", source=DataSource.PANDASCORE, external_id=555, team_id=team.id
    )
    assert p1 is not None and p2 is not None and p1.id == p2.id and p2.pandascore_id == 555
    t = resolve_tournament(
        session, name="IEM Katowice", source=DataSource.PANDASCORE, external_id=9, tier=Tier.S
    )
    t2 = resolve_tournament(session, name="IEM Katowice 2026", source=DataSource.PANDASCORE, external_id=9)
    assert t.id == t2.id and t2.tier is Tier.S


def test_state_kill_switch_pause_and_overrides(session, settings: Settings) -> None:
    assert state.is_trading_halted(session) == (False, None)
    state.set_kill_switch(session, True, actor="test", reason="manual")
    halted, why = state.is_trading_halted(session)
    assert halted and "kill switch" in (why or "")
    state.set_kill_switch(session, False, actor="test")
    state.set_paused(session, True, actor="system", reason="daily loss limit")
    assert state.is_trading_halted(session)[0]
    state.set_config_overrides(session, {"decision": {"min_ev": 0.05}}, actor="test")
    assert state.effective_decision_settings(session, settings).min_ev == 0.05
    assert state.effective_decision_settings(session, settings).min_probability == 0.75
    with pytest.raises(ValueError):
        state.set_config_overrides(session, {"executor": {"dry_run": False}}, actor="test")


def test_track_job(session_factory) -> None:
    from cs2bot.db.models import JobRun

    with track_job("unit", session_factory) as t:
        t.add(3)
    with pytest.raises(RuntimeError), track_job("unit-fail", session_factory):
        raise RuntimeError("boom")
    with session_factory() as s:
        rows = {r.job_name: r for r in s.scalars(select(JobRun))}
    assert rows["unit"].status is JobStatus.SUCCESS and rows["unit"].items == 3
    assert rows["unit-fail"].status is JobStatus.FAILED and "boom" in (rows["unit-fail"].error or "")


# ------------------------------------------------------------------ synthetic universe


def test_synthetic_universe_rules() -> None:
    uni = generate_universe(n_teams=8, n_matches=60, seed=3)
    assert len(uni.matches) == 60
    for m in uni.matches:
        need = m.format.maps_to_win
        assert need is not None
        assert max(m.team1_maps, m.team2_maps) == need
        assert len(m.maps) <= m.format.n_maps
        assert m.veto[-1].action is VetoAction.LEFTOVER
        played = [mp.map_name for mp in m.maps]
        assert len(set(played)) == len(played)
        for mp in m.maps:
            hi, lo = max(mp.team1_rounds, mp.team2_rounds), min(mp.team1_rounds, mp.team2_rounds)
            if mp.overtime_rounds == 0:
                assert hi == 13 and lo <= 11  # 12-12 always goes to overtime in MR12
                assert hi + lo == len(mp.rounds) <= 24
            else:
                assert hi - lo in (1, 2, 3, 4) and hi >= 16 and (hi - 12) % 3 == 1
            assert mp.team1_ct_rounds + mp.team1_t_rounds == mp.team1_rounds
            # sides swap after round 12
            assert mp.rounds[12 if len(mp.rounds) > 12 else -1].team1_side is not None
            if len(mp.rounds) > 12:
                assert mp.rounds[11].team1_side is not mp.rounds[12].team1_side
            for r in mp.rounds:
                if r.end_reason.value == "elimination":
                    assert len(r.kills) >= 5  # every loser dies


def test_synthetic_records_consistent(synthetic_universe) -> None:
    uni = synthetic_universe
    maps = uni.map_results()
    rounds = uni.round_records()
    pms = uni.player_map_records()
    assert len(rounds) == sum(m.total_rounds for m in maps)
    assert len(pms) == 10 * len(maps)
    # kills on a map == deaths on that map
    by_map: Counter[tuple[int, int]] = Counter()
    for p in pms:
        by_map[(p.match_id, p.map_number)] += p.kills - p.deaths
    assert all(v == 0 for v in by_map.values())
    # stronger team wins more often on average
    strongest = max(uni.teams.values(), key=lambda t: t.strength)
    weakest = min(uni.teams.values(), key=lambda t: t.strength)
    pool = uni.map_pool[0]
    assert uni.true_map_win_prob(strongest.id, weakest.id, pool, n_sims=400) > 0.5


def test_ct_side_bias_visible() -> None:
    uni = generate_universe(n_teams=10, n_matches=300, seed=11)
    rr = [r for r in uni.round_records() if r.map_name == "nuke" and r.team1_buy and r.team2_buy]
    ct_wins = sum(1 for r in rr if r.winner_side is Side.CT)
    assert ct_wins / len(rr) > 0.52  # nuke has the largest CT bias


def test_synthetic_odds_have_margin() -> None:
    import numpy as np

    o1, o2 = synthetic_two_way_odds(0.6, margin=0.07, noise_sd=0.0, rng=np.random.default_rng(0))
    assert 1 / o1 + 1 / o2 == pytest.approx(1.07, abs=0.01)


def test_populate_db(session) -> None:
    uni = generate_universe(n_teams=6, n_matches=20, seed=5)
    populate_db(session, uni, with_rounds=True, with_kills=True)
    session.flush()
    assert session.scalar(select(func.count()).select_from(Team)) == 6
    assert session.scalar(select(func.count()).select_from(Player)) == 30
    assert session.scalar(select(func.count()).select_from(Match)) == 20
    n_maps = sum(len(m.maps) for m in uni.matches)
    assert session.scalar(select(func.count()).select_from(MapPlayed)) == n_maps
    assert session.scalar(select(func.count()).select_from(PlayerMapStats)) == 10 * n_maps
    assert session.scalar(select(func.count()).select_from(Veto)) == sum(len(m.veto) for m in uni.matches)
    n_rounds = sum(len(mp.rounds) for m in uni.matches for mp in m.maps)
    assert session.scalar(select(func.count()).select_from(Round)) == n_rounds
    m1 = session.get(Match, 1)
    assert m1 is not None and m1.scheduled_at is not None and m1.scheduled_at.tzinfo is not None
    assert {lu.player_id for lu in m1.lineups} and m1.maps[0].map_number == 1
    assert TeamSlot.TEAM1.other is TeamSlot.TEAM2


def test_find_match_either_orientation(session) -> None:
    from datetime import timedelta

    from cs2bot.db.repo import find_match, is_swapped

    a = resolve_team(session, name="Alpha", source=DataSource.HLTV, external_id=1)
    b = resolve_team(session, name="Bravo", source=DataSource.HLTV, external_id=2)
    assert a is not None and b is not None
    t0 = datetime(2026, 5, 1, 15, tzinfo=UTC)
    m = Match(team1_id=a.id, team2_id=b.id, scheduled_at=t0)
    session.add(m)
    session.flush()
    assert find_match(session, b.id, a.id, t0 + timedelta(hours=2)) is m
    assert find_match(session, a.id, b.id, t0 + timedelta(hours=30)) is None
    assert is_swapped(m, b.id) and not is_swapped(m, a.id)
