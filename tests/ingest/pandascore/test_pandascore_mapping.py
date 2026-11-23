"""Mapping tables: PandaScore vocabulary -> domain enums, and the payload models' tolerance."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cs2bot.domain.enums import MatchFormat, MatchStatus, RoundEndReason, Tier
from cs2bot.ingest.pandascore import (
    PSGame,
    PSMatch,
    PSTeam,
    PSTournament,
    PSTournamentRosters,
    game_map_name,
    map_game_status,
    map_is_lan,
    map_match_format,
    map_match_status,
    map_round_outcome,
    map_tier,
    overtime_rounds,
    parse_prizepool_usd,
    stage_label,
)
from cs2bot.ingest.pandascore.mapping import regulation_rounds, tournament_display_name
from tests.ingest.pandascore.conftest import (
    FINISHED_ID,
    PS_NAVI,
    PS_VITALITY,
    finished_games,
    finished_match,
    load_json,
    upcoming_match,
)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("not_started", MatchStatus.SCHEDULED),
        ("running", MatchStatus.LIVE),
        ("finished", MatchStatus.FINISHED),
        ("canceled", MatchStatus.CANCELED),
        ("cancelled", MatchStatus.CANCELED),
        ("postponed", MatchStatus.POSTPONED),
        (" Finished ", MatchStatus.FINISHED),
        ("something_new", MatchStatus.UNKNOWN),
        ("", MatchStatus.UNKNOWN),
        (None, MatchStatus.UNKNOWN),
    ],
)
def test_match_status(status: str | None, expected: MatchStatus) -> None:
    assert map_match_status(status) is expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("finished", MatchStatus.FINISHED),
        ("running", MatchStatus.LIVE),
        ("not_started", MatchStatus.SCHEDULED),
        ("not_played", MatchStatus.CANCELED),
        (None, MatchStatus.UNKNOWN),
    ],
)
def test_game_status(status: str | None, expected: MatchStatus) -> None:
    assert map_game_status(status) is expected


@pytest.mark.parametrize(
    ("match_type", "games", "expected"),
    [
        ("best_of", 1, MatchFormat.BO1),
        ("best_of", 2, MatchFormat.BO2),
        ("best_of", 3, MatchFormat.BO3),
        ("best_of", 5, MatchFormat.BO5),
        ("best_of", 7, None),
        ("BEST_OF", 3, MatchFormat.BO3),
        ("first_to", 1, MatchFormat.BO1),
        ("first_to", 2, MatchFormat.BO3),
        ("first_to", 3, MatchFormat.BO5),
        ("first_to", 4, None),
        ("all_games_played", 1, MatchFormat.BO1),
        ("all_games_played", 2, MatchFormat.BO2),  # both maps are played, a 1-1 draw is possible
        ("all_games_played", 3, None),
        ("custom", 3, None),
        ("ow_best_of", 3, None),
        (None, 3, None),
        ("best_of", None, None),
        ("best_of", 0, None),
    ],
)
def test_match_format(match_type: str | None, games: int | None, expected: MatchFormat | None) -> None:
    assert map_match_format(match_type, games) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("s", Tier.S),
        ("a", Tier.A),
        ("b", Tier.B),
        ("c", Tier.C),
        ("d", Tier.D),
        ("unranked", Tier.UNRANKED),
        (" S ", Tier.S),
        (None, Tier.UNRANKED),
        ("", Tier.UNRANKED),
        ("legendary", Tier.UNRANKED),
    ],
)
def test_tier(raw: str | None, expected: Tier) -> None:
    assert map_tier(raw) is expected


def test_lan_flag_round_outcomes_and_overtime() -> None:
    assert map_is_lan("offline") is True and map_is_lan("Online") is False
    assert map_is_lan("online/offline") is None and map_is_lan(None) is None
    assert map_round_outcome("defused") is RoundEndReason.BOMB_DEFUSED
    assert map_round_outcome("exploded") is RoundEndReason.BOMB_EXPLODED
    assert map_round_outcome("eliminated") is RoundEndReason.ELIMINATION
    assert map_round_outcome("planted_eliminated") is RoundEndReason.ELIMINATION
    assert map_round_outcome("timeout") is RoundEndReason.TIME_EXPIRED
    assert (
        map_round_outcome("weird") is RoundEndReason.OTHER and map_round_outcome(None) is RoundEndReason.OTHER
    )
    assert overtime_rounds(13, 11) == 0 and overtime_rounds(13, 4) == 0
    assert overtime_rounds(16, 14) == 6 and overtime_rounds(19, 17) == 12
    assert overtime_rounds(None, 13) == 0
    assert overtime_rounds(19, 17, regulation=30) == 6  # CS:GO (MR15)


@pytest.mark.parametrize(
    ("name", "slug", "expected"),
    [
        ("Mirage", "mirage", "mirage"),
        ("Dust2", "dust2", "dust2"),
        ("Dust II", None, "dust2"),
        ("de_inferno", None, "inferno"),
        (None, "de_nuke", "nuke"),
        ("TBD", "ancient", "ancient"),  # placeholder name, usable slug
        ("TBA", None, None),
        ("", "", None),
    ],
)
def test_game_map_name(name: str | None, slug: str | None, expected: str | None) -> None:
    game = PSGame.model_validate({"id": 1, "map": {"id": 9, "name": name, "slug": slug}})
    assert game_map_name(game) == expected


def test_game_without_map_has_no_name() -> None:
    assert game_map_name(PSGame.model_validate({"id": 1, "map": None})) is None
    assert game_map_name(PSGame.model_validate({"id": 1})) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1000000 United States Dollar", 1_000_000),
        ("1,250,000 United States Dollar", 1_250_000),
        ("5000 USD", 5000),
        ("$100.000", 100_000),
        ("2500.00 US Dollar", 2500),
        ("50000 Euro", None),
        ("2000000 Chinese Yuan", None),
        ("TBD United States Dollar", None),
        ("", None),
        (None, None),
    ],
)
def test_prizepool(raw: str | None, expected: int | None) -> None:
    assert parse_prizepool_usd(raw) == expected


def test_stage_and_tournament_names() -> None:
    assert stage_label("Playoffs", "Semifinal 1: NAVI vs VIT") == "Playoffs - Semifinal 1"
    assert stage_label("Group A", "NAVI vs G2") == "Group A"
    assert stage_label("Playoffs", "Playoffs: A vs B") == "Playoffs"
    assert stage_label(None, "Grand final: A vs B") == "Grand final"
    assert stage_label(None, "A vs B") is None and stage_label("", None) is None
    assert len(stage_label("x" * 200, None) or "") == 128
    assert tournament_display_name("ESL Pro League", "Season 22 2026", "Group A") == (
        "ESL Pro League Season 22 2026 - Group A"
    )
    assert tournament_display_name(None, None, "Playoffs") == "Playoffs"
    assert tournament_display_name("BLAST Premier", None, None) == "BLAST Premier"
    assert tournament_display_name(None, None, None) == "Unknown tournament"


# ------------------------------------------------------------------------------------- payload models


def test_match_fixture_parses_into_typed_model() -> None:
    match = PSMatch.model_validate(finished_match())
    assert match.id == FINISHED_ID and match.status == "finished" and match.match_type == "best_of"
    assert match.number_of_games == 3 and match.winner_id == PS_VITALITY and match.draw is False
    assert match.scheduled_at == datetime(2026, 11, 21, 16, 30, tzinfo=UTC)
    assert match.begin_at == datetime(2026, 11, 21, 16, 36, 4, tzinfo=UTC)
    assert match.kickoff == match.scheduled_at and match.is_team_match
    assert [t.id for t in match.team_opponents()] == [PS_NAVI, PS_VITALITY]
    assert (match.score_for(PS_NAVI), match.score_for(PS_VITALITY), match.score_for(1)) == (1, 2, None)
    assert match.tournament is not None and match.tournament.tier == "s"
    assert match.serie is not None and match.serie.full_name == "Cologne 2026"
    assert match.league is not None and match.league.name == "Intel Extreme Masters"
    assert match.videogame_title is not None and match.videogame_title.slug == "cs-2"
    assert [g.position for g in match.games] == [1, 2, 3]
    assert [g.winner_team_id for g in match.games] == [PS_NAVI, PS_VITALITY, PS_VITALITY]
    assert regulation_rounds(match) == 24

    upcoming = PSMatch.model_validate(upcoming_match())
    assert upcoming.status == "not_started" and upcoming.end_at is None and upcoming.winner_id is None
    assert [g.winner_team_id for g in upcoming.games] == [None, None, None]


def test_game_fixture_parses_into_typed_model() -> None:
    games = [PSGame.model_validate(raw) for raw in finished_games()]
    mirage = games[0]
    assert mirage.map is not None and mirage.map.slug == "mirage" and mirage.complete is True
    assert (mirage.rounds_for(PS_NAVI), mirage.rounds_for(PS_VITALITY), mirage.rounds_for(None)) == (
        13,
        11,
        None,
    )
    assert len(mirage.rounds) == 24 and mirage.rounds[0].ct == PS_VITALITY
    assert {r.winner_side for r in mirage.rounds} == {"ct", "terrorists"}
    assert len(mirage.players) == 10
    line = mirage.players[5]
    assert line.player is not None and line.player.name == "ZywOo" and line.pandascore_player_id == 17512
    assert line.team is not None and line.team.id == PS_VITALITY
    assert line.player.real_name == "Mathieu Herbaut" and line.kills is not None
    assert [len(g.rounds) for g in games] == [24, 20, 36]


def test_models_tolerate_nulls_unknown_fields_and_blanks() -> None:
    match = PSMatch.model_validate(
        {
            "id": 1,
            "opponents": None,
            "results": None,
            "games": None,
            "tournament": None,
            "scheduled_at": "",
            "begin_at": None,
            "name": "   ",
            "brand_new_field": {"anything": [1, 2, 3]},
        }
    )
    assert match.opponents == [] and match.results == [] and match.games == []
    assert match.kickoff is None and match.name is None and match.team_opponents() == []
    assert PSMatch.model_validate({}).id is None
    # a timestamp falls back: scheduled_at -> begin_at -> original_scheduled_at
    late = PSMatch.model_validate({"id": 2, "original_scheduled_at": "2026-11-21T16:30:00+01:00"})
    assert late.kickoff == datetime(2026, 11, 21, 15, 30, tzinfo=UTC)
    team = PSTeam.model_validate({"id": 5, "name": "X", "players": None, "location": ""})
    assert team.players == [] and team.location is None
    game = PSGame.model_validate({"id": 3, "winner": {"id": 9, "type": "Player"}, "rounds": None})
    assert game.winner_team_id is None and game.rounds == []
    with pytest.raises(ValidationError):
        PSMatch.model_validate({"id": 1, "scheduled_at": "not a date"})
    with pytest.raises(ValidationError):
        PSMatch.model_validate({"id": 1, "opponents": "oops"})


def test_tbd_and_player_opponents() -> None:
    tbd = PSMatch.model_validate(
        {"id": 1, "opponents": [{"type": "Team", "opponent": {"id": 7, "name": "Seven"}}]}
    )
    assert len(tbd.team_opponents()) == 1
    solo = PSMatch.model_validate(
        {"id": 2, "opponents": [{"type": "Player", "opponent": {"id": 1, "name": "s1mple"}}]}
    )
    assert solo.is_team_match is False and solo.team_opponents() == []


def test_tournament_team_and_roster_fixtures() -> None:
    tournament = PSTournament.model_validate(load_json("tournament_iem_cologne_playoffs.json"))
    assert tournament.name == "Playoffs" and tournament.prizepool == "1000000 United States Dollar"
    assert tournament.country == "DE" and tournament.region == "WEU" and tournament.serie_id == 9801
    assert tournament.begin_at == datetime(2026, 11, 20, 14, 0, tzinfo=UTC)
    assert [t.name for t in tournament.teams] == ["Natus Vincere", "Vitality"]
    assert [len(entry.players) for entry in tournament.expected_roster] == [5, 5]
    team = PSTeam.model_validate(load_json("team_vitality.json"))
    assert team.acronym == "VIT" and team.location == "FR"
    assert [p.active for p in team.players].count(False) == 1
    rosters = PSTournamentRosters.model_validate(load_json("rosters_major_playoffs.json"))
    assert {t.name for t in rosters.rosters} == {"Team Spirit", "G2"}
    assert rosters.rosters[0].players[0].nationality == "RU"
