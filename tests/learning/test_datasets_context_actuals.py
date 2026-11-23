"""cs2bot.datasets: MatchContext, data completeness and MatchActuals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from cs2bot import datasets
from cs2bot.config import Settings
from cs2bot.datasets import CompletenessConfig, MatchNotFoundError
from cs2bot.db.models import (
    BookmakerEvent,
    ContextFlagRow,
    MapPlayed,
    Match,
    MatchLineup,
    Player,
    PlayerMapStats,
    Round,
    Team,
    Tournament,
    Veto,
)
from cs2bot.domain.enums import (
    Bookmaker,
    ContextFlagType,
    DataSource,
    LinkStatus,
    MatchFormat,
    MatchStatus,
    PlayerRole,
    Side,
    TeamSlot,
    Tier,
    VetoAction,
)
from cs2bot.testing.synthetic import SynthUniverse
from tests.learning.learning_helpers import populate_small

T0 = datetime(2025, 3, 1, 18, 0, tzinfo=UTC)


# ----------------------------------------------------------------------------------- match context


def test_context_of_a_finished_synthetic_match(
    learning_db: Session, learning_universe: SynthUniverse, settings: Settings
):
    match = learning_universe.matches[-1]
    ctx = datasets.load_match_context(learning_db, match.id, settings=settings)
    assert (ctx.match_id, ctx.team1_id, ctx.team2_id) == (match.id, match.team1_id, match.team2_id)
    assert ctx.scheduled_at == match.played_at
    assert (ctx.format, ctx.tier, ctx.is_lan) == (match.format, match.tier, match.is_lan)
    assert ctx.team1_name == learning_universe.teams[match.team1_id].name
    assert ctx.tournament_name == f"Synthetic {match.tier.value.upper()}-tier"
    # lineups with roles from players.primary_role
    for lineup, team_id in ((ctx.team1_lineup, match.team1_id), (ctx.team2_lineup, match.team2_id)):
        expected = {p.id: p for p in learning_universe.teams[team_id].players}
        assert {p.player_id for p in lineup} == set(expected)
        assert all(p.role is expected[p.player_id].role and not p.is_standin for p in lineup)
        assert all(p.nickname == expected[p.player_id].nickname for p in lineup)
    # the veto in team1/team2 orientation, the first-veto team and the pool of seven
    assert ctx.known_veto == list(match.veto)
    assert ctx.first_veto_team is TeamSlot.TEAM1
    assert ctx.veto_complete
    assert ctx.map_pool == sorted(learning_universe.map_pool)
    assert ctx.start_sides == {m.map_name: m.team1_start_side for m in match.maps}
    assert ctx.context_flags == []


def test_context_of_an_upcoming_match_uses_fallbacks(session: Session, settings: Settings):
    uni = populate_small(session, n_matches=120, n_teams=6, seed=9)
    last = uni.matches[-1]
    t1, t2 = last.team1_id, last.team2_id
    when = last.played_at + timedelta(days=2)
    session.add(
        Match(
            id=9001, team1_id=t2, team2_id=t1, scheduled_at=when, status=MatchStatus.SCHEDULED, in_scope=True
        )
    )
    session.flush()
    now = when - timedelta(hours=3)
    ctx = datasets.load_match_context(session, 9001, settings=settings, now=now)
    assert (ctx.team1_id, ctx.team2_id) == (t2, t1)
    assert ctx.format is MatchFormat.BO3  # nothing known: the most common format
    assert ctx.known_veto == [] and ctx.first_veto_team is None and ctx.start_sides == {}
    assert not ctx.veto_complete
    # no lineup rows yet: each team's most recent lineup is carried over
    assert {p.player_id for p in ctx.team1_lineup} == {p.id for p in uni.teams[t2].players}
    assert {p.player_id for p in ctx.team2_lineup} == {p.id for p in uni.teams[t1].players}
    # the pool actually played in the last 60 days is detected from results
    assert ctx.map_pool == sorted(uni.map_pool)

    # HLTV's best-of hint and a partial veto: orientation by team id, first veto team from step 1
    match = session.get(Match, 9001)
    match.extra = {"hltv": {"best_of": 1}}
    session.add(Veto(match_id=9001, step=1, team_id=t1, action=VetoAction.BAN, map_name="de_nuke"))
    session.add(Veto(match_id=9001, step=2, team_id=t2, action=VetoAction.BAN, map_name="Dust II"))
    session.add(
        MapPlayed(
            match_id=9001,
            map_number=1,
            map_name="Mirage",
            team1_start_side=Side.T,
            status=MatchStatus.SCHEDULED,
        )
    )
    session.flush()
    ctx = datasets.load_match_context(session, 9001, settings=settings, now=now)
    assert ctx.format is MatchFormat.BO1
    assert [(s.team, s.map_name) for s in ctx.known_veto] == [
        (TeamSlot.TEAM2, "nuke"),
        (TeamSlot.TEAM1, "dust2"),
    ]
    assert ctx.first_veto_team is TeamSlot.TEAM2
    assert ctx.start_sides == {"mirage": Side.T}
    assert not ctx.veto_complete


def test_context_map_pool_falls_back_to_settings_and_lineups_carry_standins(
    session: Session, settings: Settings
):
    for tid in (1, 2):
        session.add(Team(id=tid, name=f"Team {tid}"))
    session.flush()
    for pid in range(1, 12):
        session.add(Player(id=pid, nickname=f"p{pid}", primary_role=PlayerRole.RIFLER if pid < 6 else None))
    tour = Tournament(name="Cup", tier=Tier.A, is_lan=True)
    session.add(tour)
    session.flush()
    session.add(
        Match(id=1, team1_id=1, team2_id=2, scheduled_at=T0 - timedelta(days=5), status=MatchStatus.FINISHED)
    )
    session.add(
        Match(
            id=2,
            team1_id=1,
            team2_id=2,
            scheduled_at=T0,
            status=MatchStatus.SCHEDULED,
            tournament_id=tour.id,
            stage="Grand final",
        )
    )
    session.flush()
    for pid in range(1, 6):  # team 1's previous lineup, with a stand-in
        session.add(
            MatchLineup(match_id=1, team_id=1, player_id=pid, is_standin=pid == 5, source=DataSource.HLTV)
        )
    for pid in range(6, 11):  # team 2 is confirmed for the upcoming match
        session.add(MatchLineup(match_id=2, team_id=2, player_id=pid, source=DataSource.HLTV))
    session.flush()
    custom = settings.model_copy(deep=True)
    custom.scope.active_map_pool = ["de_ancient", "Dust II", "inferno", "mirage", "nuke", "overpass", "train"]
    ctx = datasets.load_match_context(session, 2, settings=custom, now=T0 - timedelta(hours=1))
    # no recent results at all -> configured pool (normalised names)
    assert ctx.map_pool == ["ancient", "dust2", "inferno", "mirage", "nuke", "overpass", "train"]
    assert (ctx.tier, ctx.is_lan, ctx.stage, ctx.tournament_name) == (Tier.A, True, "Grand final", "Cup")
    assert [p.player_id for p in ctx.team1_lineup] == [1, 2, 3, 4, 5]
    assert [p.is_standin for p in ctx.team1_lineup] == [False, False, False, False, True]
    assert ctx.team1_lineup[0].role is PlayerRole.RIFLER
    assert [p.player_id for p in ctx.team2_lineup] == [6, 7, 8, 9, 10]

    # completeness: team 1 only has a carried-over lineup (half credit), nothing else is known
    parts = datasets.completeness_breakdown(session, ctx, now=T0 - timedelta(hours=1))
    assert parts == {
        "lineups": 0.75,
        "recent_maps": 0.0,
        "veto_history": 0.0,
        "player_stats": 0.0,
        "odds_link": 0.0,
    }
    assert datasets.data_completeness(session, ctx, now=T0 - timedelta(hours=1)) == pytest.approx(0.30 * 0.75)


def test_context_flags_and_missing_matches(session: Session, settings: Settings):
    uni = populate_small(session)
    match = uni.matches[-1]
    now = match.played_at - timedelta(hours=1)

    def flag(**kw) -> ContextFlagRow:
        base = {
            "match_id": match.id,
            "flag_type": ContextFlagType.STAND_IN,
            "severity": 0.6,
            "confidence": 0.9,
            "rationale": "stand-in announced",
            "created_at": now - timedelta(hours=2),
        }
        return ContextFlagRow(**{**base, **kw})

    session.add_all(
        [
            flag(team_id=match.team2_id, logit_shift=0.12, evidence=["https://example.org/news/1", 17]),
            flag(flag_type=ContextFlagType.ILLNESS, active=False),
            flag(flag_type=ContextFlagType.VISA_ISSUE, expires_at=now - timedelta(minutes=5)),
            flag(
                flag_type=ContextFlagType.DEAD_RUBBER,
                veto=True,
                severity=7.0,
                expires_at=now + timedelta(days=1),
            ),
        ]
    )
    session.flush()
    ctx = datasets.load_match_context(session, match.id, settings=settings, now=now)
    assert [f.flag_type for f in ctx.context_flags] == [ContextFlagType.STAND_IN, ContextFlagType.DEAD_RUBBER]
    stand_in, dead = ctx.context_flags
    assert stand_in.team is TeamSlot.TEAM2 and stand_in.logit_shift == pytest.approx(0.12)
    assert stand_in.evidence == ["https://example.org/news/1", "17"]
    assert dead.team is None and dead.veto and dead.severity == 1.0  # out-of-range severity is clipped
    bare = datasets.load_match_context(
        session, match.id, settings=settings, now=now, include_context_flags=False
    )
    assert bare.context_flags == []

    with pytest.raises(MatchNotFoundError):
        datasets.load_match_context(session, 123456, settings=settings)
    session.add(Match(id=9002, team1_id=match.team1_id, team2_id=None, scheduled_at=now))
    session.flush()
    with pytest.raises(MatchNotFoundError):
        datasets.load_match_context(session, 9002, settings=settings)


# ------------------------------------------------------------------------------- data completeness


def test_data_completeness_components_and_weights(session: Session, settings: Settings):
    uni = populate_small(session, n_matches=120, n_teams=6, seed=9)
    match = uni.matches[-1]
    ctx = datasets.load_match_context(session, match.id, settings=settings)
    parts = datasets.completeness_breakdown(session, ctx)
    assert parts == {
        "lineups": 1.0,
        "recent_maps": 1.0,
        "veto_history": 1.0,
        "player_stats": 1.0,
        "odds_link": 0.0,
    }
    # everything but the Betclic link: 0.90 - below the default betting gate of 0.95
    assert datasets.data_completeness(session, ctx) == pytest.approx(0.90)
    assert datasets.data_completeness(session, ctx) < settings.decision.min_data_completeness

    def link(external_id: str, status: LinkStatus) -> None:
        session.add(
            BookmakerEvent(
                bookmaker=Bookmaker.BETCLIC,
                external_id=external_id,
                name="A - B",
                team1_raw="A",
                team2_raw="B",
                match_id=match.id,
                link_status=status,
                first_seen_at=match.played_at,
                last_seen_at=match.played_at,
            )
        )
        session.flush()

    link("e1", LinkStatus.PENDING_REVIEW)  # an unconfirmed link does not count
    assert datasets.data_completeness(session, ctx) == pytest.approx(0.90)
    link("e2", LinkStatus.AUTO)
    assert datasets.data_completeness(session, ctx) == pytest.approx(1.0)

    # a lineup with only four confirmed players loses that team's lineup credit and a fifth of its
    # player-stat credit: 1 - 0.15 - 0.02
    victim = ctx.team1_lineup[0].player_id
    session.execute(
        delete(MatchLineup).where(MatchLineup.match_id == match.id, MatchLineup.player_id == victim)
    )
    session.flush()
    short = datasets.load_match_context(session, match.id, settings=settings)
    assert len(short.team1_lineup) == 4
    assert datasets.data_completeness(session, short) == pytest.approx(1.0 - 0.15 - 0.02)

    # custom weights are normalised; the history of the first match of the universe is empty
    first = datasets.load_match_context(session, uni.matches[0].id, settings=settings)
    early = datasets.completeness_breakdown(session, first)
    assert early["recent_maps"] == 0.0 and early["veto_history"] == 0.0 and early["player_stats"] == 0.0
    only_lineups = CompletenessConfig(
        weight_lineups=2.0,
        weight_recent_maps=0.0,
        weight_veto_history=0.0,
        weight_player_stats=0.0,
        weight_odds_link=0.0,
    )
    assert datasets.data_completeness(session, first, config=only_lineups) == pytest.approx(1.0)
    score = datasets.data_completeness(session, first)
    assert 0.0 <= score <= 1.0 and score == pytest.approx(0.30)


def test_data_completeness_never_counts_the_match_itself_or_the_future(session: Session, settings: Settings):
    uni = populate_small(session, n_matches=120, n_teams=6, seed=9)
    match = uni.matches[60]
    ctx = datasets.load_match_context(session, match.id, settings=settings)
    before = datasets.completeness_breakdown(session, ctx)
    # wipe everything that happened from this match on: the answer must not change
    later = [m.id for m in uni.matches[60:]]
    map_ids = list(session.scalars(select(MapPlayed.id).where(MapPlayed.match_id.in_(later))))
    session.execute(delete(PlayerMapStats).where(PlayerMapStats.map_played_id.in_(map_ids)))
    session.execute(delete(Round).where(Round.map_played_id.in_(map_ids)))
    session.execute(delete(MapPlayed).where(MapPlayed.id.in_(map_ids)))
    session.execute(delete(Veto).where(Veto.match_id.in_(later)))
    session.flush()
    assert datasets.completeness_breakdown(session, ctx) == before


# ----------------------------------------------------------------------------------- match actuals


def test_actuals_of_synthetic_matches(learning_db: Session, learning_universe: SynthUniverse):
    assert datasets.load_match_actuals(learning_db, 999_999) is None
    for match in learning_universe.matches[:25]:
        actuals = datasets.load_match_actuals(learning_db, match.id)
        assert actuals is not None and actuals.complete and not actuals.forfeited and not actuals.cancelled
        assert (actuals.format, actuals.team1_id, actuals.team2_id) == (
            match.format,
            match.team1_id,
            match.team2_id,
        )
        assert (actuals.team1_maps, actuals.team2_maps) == (match.team1_maps, match.team2_maps)
        assert [m.map_number for m in actuals.maps] == [m.map_number for m in match.maps]
        for got, want in zip(actuals.maps, match.maps, strict=True):
            assert (got.map_name, got.team1_rounds, got.team2_rounds) == (
                want.map_name,
                want.team1_rounds,
                want.team2_rounds,
            )
            assert got.round_winners == tuple(r.winner for r in want.rounds)
            kills: dict[int, int] = {}
            for rnd in want.rounds:
                for attacker, _ in rnd.kills:
                    kills[attacker] = kills.get(attacker, 0) + 1
            assert len(got.player_kills) == 10
            assert {p: k for p, k in got.player_kills.items() if k} == kills
            assert got.went_to_overtime == (want.overtime_rounds > 0)


def _series(session: Session, scores: list[tuple[int, int]], **match_kw) -> None:
    for tid in (1, 2):
        session.add(Team(id=tid, name=f"Team {tid}"))
    session.flush()
    for pid in (1, 2):
        session.add(Player(id=pid, nickname=f"p{pid}"))
    session.flush()
    kwargs = {"status": MatchStatus.FINISHED, "format": MatchFormat.BO3, "scheduled_at": T0, **match_kw}
    session.add(Match(id=1, team1_id=1, team2_id=2, **kwargs))
    session.flush()
    for number, (r1, r2) in enumerate(scores, start=1):
        session.add(
            MapPlayed(
                id=number,
                match_id=1,
                map_number=number,
                map_name=["mirage", "nuke", "inferno"][number - 1],
                status=MatchStatus.FINISHED,
                team1_rounds=r1,
                team2_rounds=r2,
            )
        )
    session.flush()


def test_actuals_round_history_sources_and_kill_preference(session: Session):
    _series(session, [(13, 2), (2, 13), (13, 1)], team1_score=2, team2_score=1)
    # map 1: complete rounds table
    for number in range(1, 16):
        session.add(
            Round(
                map_played_id=1,
                round_number=number,
                team1_side=Side.CT if number <= 12 else Side.T,
                winner_side=(Side.T if number in (3, 9) else Side.CT) if number <= 12 else Side.T,
                source=DataSource.HLTV,
            )
        )
    # map 2: rounds table has a hole -> not usable; map 3: only HLTV's stored round history
    for number in (1, 2, 4):
        session.add(Round(map_played_id=2, round_number=number, team1_side=Side.CT, winner_side=Side.T))
    history = [
        {"round": n, "winner": "team2" if n == 5 else "team1", "team1_side": "ct", "winner_side": "ct"}
        for n in range(14, 0, -1)
    ]
    session.get(MapPlayed, 3).extra = {"hltv": {"rounds_complete": True, "rounds": history}}
    for source, kills in ((DataSource.PANDASCORE, 5), (DataSource.DEMO, 21), (DataSource.HLTV, 20)):
        session.add(
            PlayerMapStats(map_played_id=1, player_id=1, team_id=1, source=source, kills=kills, deaths=9)
        )
    session.add(
        PlayerMapStats(map_played_id=1, player_id=2, team_id=2, source=DataSource.HLTV, kills=8, deaths=20)
    )
    session.flush()

    actuals = datasets.load_match_actuals(session, 1)
    assert actuals is not None and actuals.complete
    assert (actuals.team1_maps, actuals.team2_maps, actuals.maps_played) == (2, 1, 3)
    m1, m2, m3 = actuals.maps
    assert m1.round_winners is not None and len(m1.round_winners) == 15
    assert [i + 1 for i, w in enumerate(m1.round_winners) if w is TeamSlot.TEAM2] == [3, 9]
    assert m2.round_winners is None
    assert m3.round_winners is not None and m3.round_winners.count(TeamSlot.TEAM2) == 1
    assert m3.round_winners[4] is TeamSlot.TEAM2
    assert m1.player_kills == {1: 21, 2: 8}  # DEMO beats HLTV beats PandaScore
    assert m2.player_kills == {}
    assert actuals.map(2) is m2 and actuals.map(5) is None

    # round winners that contradict the final score are not trusted
    session.execute(
        update(Round).where(Round.map_played_id == 1, Round.round_number == 1).values(winner_side=Side.T)
    )
    session.flush()
    assert datasets.load_match_actuals(session, 1).maps[0].round_winners is None


def test_actuals_completeness_rules(session: Session):
    _series(session, [(13, 7), (9, 13)], team1_score=1, team2_score=1)
    # a BO3 at 1-1 is not decided, even if the match row says FINISHED
    assert datasets.load_match_actuals(session, 1).complete is False
    session.add(
        MapPlayed(
            id=3,
            match_id=1,
            map_number=3,
            map_name="inferno",
            status=MatchStatus.FINISHED,
            team1_rounds=16,
            team2_rounds=14,
        )
    )
    session.flush()
    # stored series score (1-1) disagrees with the maps (2-1): wait for the next sync
    assert datasets.load_match_actuals(session, 1).complete is False
    match = session.get(Match, 1)
    match.team1_score, match.team2_score = 2, 1
    session.flush()
    actuals = datasets.load_match_actuals(session, 1)
    assert actuals.complete and actuals.maps[2].went_to_overtime
    # still running
    match.status = MatchStatus.LIVE
    session.flush()
    assert datasets.load_match_actuals(session, 1).complete is False
    # partial forfeit: maps are listed, the series result is left to the bookmaker
    match.status = MatchStatus.FINISHED
    match.extra = {"hltv": {"forfeited_maps": [3]}}
    session.flush()
    partial = datasets.load_match_actuals(session, 1)
    assert partial.maps_played == 3 and not partial.complete and not partial.forfeited


@pytest.mark.parametrize(
    ("extra", "forfeited", "cancelled"),
    [
        ({"hltv": {"forfeit": True, "forfeit_winner_team_id": 1}}, True, False),
        ({"pandascore": {"forfeit": True}}, True, False),
        ({}, False, True),
        ({"hltv": "garbage"}, False, True),
    ],
)
def test_actuals_forfeit_and_cancellation(session: Session, extra: dict, forfeited: bool, cancelled: bool):
    _series(session, [], status=MatchStatus.CANCELED, format=None, extra=extra)
    actuals = datasets.load_match_actuals(session, 1)
    assert actuals is not None
    assert (actuals.forfeited, actuals.cancelled, actuals.complete) == (forfeited, cancelled, True)
    assert actuals.maps == () and actuals.format is MatchFormat.BO3


def test_actuals_format_inference(session: Session):
    _series(session, [(13, 11)], format=None, extra={"hltv": {"best_of": 1}}, team1_score=13, team2_score=11)
    actuals = datasets.load_match_actuals(session, 1)
    # a BO1 whose series score holds the round score is still complete
    assert actuals.format is MatchFormat.BO1 and actuals.complete and actuals.team1_maps == 1
    session.get(Match, 1).extra = {}
    session.flush()
    assert datasets.load_match_actuals(session, 1).format is MatchFormat.BO1  # one map row


def test_context_map_pool_follows_the_patch_calendar_when_nothing_else_is_known(
    session: Session, settings: Settings
):
    for tid in (1, 2):
        session.add(Team(id=tid, name=f"Team {tid}"))
    session.flush()
    session.add(Match(id=1, team1_id=1, team2_id=2, scheduled_at=datetime(2024, 6, 1, tzinfo=UTC)))
    session.add(Match(id=2, team1_id=1, team2_id=2, scheduled_at=datetime(2026, 11, 30, tzinfo=UTC)))
    session.flush()
    now = datetime(2026, 11, 23, tzinfo=UTC)
    # no results, default settings: the Active Duty pool of the match day (config/patches.yaml)
    old = datasets.load_match_context(session, 1, settings=settings, now=now)
    assert old.map_pool == ["ancient", "anubis", "dust2", "inferno", "mirage", "nuke", "vertigo"]
    upcoming = datasets.load_match_context(session, 2, settings=settings, now=now)
    assert upcoming.map_pool == ["ancient", "anubis", "cache", "dust2", "inferno", "mirage", "nuke"]
    assert len(upcoming.map_pool) == 7 and upcoming.team1_lineup == []


# ------------------------------------------------------------------------------------- review round 1


def test_actuals_partial_forfeit_as_stored_by_hltv_never_voids_played_maps(session: Session):
    """HLTV sets ``forfeit=True`` for a partial forfeit too (any "Default" map). Bets on the maps that
    were played stand at the bookmaker, so the match must not be reported as a walkover."""
    _series(
        session,
        [(13, 7), (9, 13)],
        team1_score=2,
        team2_score=1,
        extra={"hltv": {"forfeit": True, "forfeit_winner_team_id": 1, "forfeited_maps": [3]}},
    )
    actuals = datasets.load_match_actuals(session, 1)
    assert actuals is not None
    assert (actuals.forfeited, actuals.cancelled, actuals.complete) == (False, False, False)
    assert actuals.maps_played == 2  # the played maps are still listed
    # a forfeit note next to fully played maps (no "Default" map recorded) is just as ambiguous
    match = session.get(Match, 1)
    match.extra = {"hltv": {"forfeit": True, "forfeit_note": "Team 2 forfeited map 3"}}
    session.flush()
    actuals = datasets.load_match_actuals(session, 1)
    assert (actuals.forfeited, actuals.complete) == (False, False)
    # PandaScore's flag follows the same rule
    match.extra = {"pandascore": {"forfeit": True}}
    session.flush()
    assert datasets.load_match_actuals(session, 1).forfeited is False


def test_actuals_cancelled_match_with_played_maps_is_left_to_the_bookmaker(session: Session):
    _series(session, [(13, 7)], status=MatchStatus.CANCELED)
    actuals = datasets.load_match_actuals(session, 1)
    assert actuals is not None and actuals.maps_played == 1
    assert (actuals.cancelled, actuals.forfeited, actuals.complete) == (False, False, False)
    # once no map was played, it is a plain cancellation again
    session.execute(delete(MapPlayed).where(MapPlayed.match_id == 1))
    session.flush()
    actuals = datasets.load_match_actuals(session, 1)
    assert (actuals.cancelled, actuals.forfeited, actuals.complete) == (True, False, True)


def test_context_skips_flags_with_non_finite_values(session: Session, settings: Settings):
    uni = populate_small(session)
    match = uni.matches[-1]
    now = match.played_at - timedelta(hours=1)
    base = {
        "match_id": match.id,
        "flag_type": ContextFlagType.STAND_IN,
        "severity": 0.5,
        "confidence": 0.5,
        "rationale": "x",
        "created_at": now - timedelta(hours=2),
    }
    session.add_all(
        [
            ContextFlagRow(**{**base, "logit_shift": float("inf")}),  # SQLite cannot store NaN
            ContextFlagRow(**{**base, "confidence": float("inf")}),
            ContextFlagRow(**{**base, "logit_shift": -0.1}),
        ]
    )
    session.flush()
    ctx = datasets.load_match_context(session, match.id, settings=settings, now=now)
    assert [f.logit_shift for f in ctx.context_flags] == [pytest.approx(-0.1)]
