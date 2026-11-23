"""Table-driven tests of the Betclic market mapping rules (config/betclic_markets.yaml)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cs2bot.db.models import BookmakerEvent, BookmakerMarket, BookmakerSelection
from cs2bot.domain.enums import Bookmaker
from cs2bot.domain.markets import MarketType
from cs2bot.domain.odds import CollectedEvent
from cs2bot.odds.market_mapping import (
    MarketMapper,
    extract_map_number,
    extract_maps_scope,
    lineup_player_resolver,
    list_unmapped,
    list_unmapped_selections,
    make_name_resolver,
    rules_fingerprint,
)
from cs2bot.utils.names import normalize_player_name
from cs2bot.utils.paths import project_root
from tests.odds.odds_testkit import add_lineup, make_match, make_team

EVENT = CollectedEvent(
    external_id="1", name="Natus Vincere - FaZe Clan", team1_raw="Natus Vincere", team2_raw="FaZe Clan"
)
PLAYERS = {"s1mple": 11, "zywoo": 22, "ropz": 33, "b1t": 12, "910": 44}


def fake_resolver(name: str) -> int | None:
    return PLAYERS.get(normalize_player_name(name))


@pytest.fixture(scope="module")
def mapper() -> MarketMapper:
    return MarketMapper.from_yaml(project_root() / "config" / "betclic_markets.yaml")


def _key(mapper: MarketMapper, market: str, selection: str, swapped: bool, **kw: object) -> str | None:
    mapped = mapper.map(market, selection, event=EVENT, swapped=swapped, player_resolver=fake_resolver, **kw)  # type: ignore[arg-type]
    return f"{mapped.spec.key()}#{mapped.selection_key}" if mapped else None


CASES: list[tuple[str, str, bool, str | None]] = [
    # --- match winner (PL / EN, swap, alias) ------------------------------------------------
    ("Zwycięzca meczu", "Natus Vincere", False, "match_winner#team1"),
    ("Zwycięzca meczu", "Natus Vincere", True, "match_winner#team2"),
    ("Wynik meczu", "1", False, "match_winner#team1"),
    ("Wynik meczu", "2", True, "match_winner#team1"),
    ("Kto wygra mecz?", "Remis", False, "match_winner#draw"),
    ("Match winner", "FaZe Clan", False, "match_winner#team2"),
    ("Zwycięzca meczu", "NAVI", False, "match_winner#team1"),
    # --- map winner -------------------------------------------------------------------------
    ("Zwycięzca mapy 1", "FaZe Clan", False, "map_winner|map=1#team2"),
    ("1. mapa - zwycięzca", "Natus Vincere", True, "map_winner|map=1#team2"),
    ("Mapa 2", "2", False, "map_winner|map=2#team2"),
    ("Kto wygra mapę 3?", "Natus Vincere", False, "map_winner|map=3#team1"),
    ("Map 1 Winner", "FaZe Clan", True, "map_winner|map=1#team1"),
    # --- correct map score ------------------------------------------------------------------
    ("Dokładny wynik", "2:1", False, "correct_map_score#2-1"),
    ("Dokładny wynik", "2-0", True, "correct_map_score#0-2"),
    ("Dokładny wynik", "FaZe Clan 2:1", False, "correct_map_score#1-2"),
    ("Correct score", "0:2", False, "correct_map_score#0-2"),
    # --- map handicap: spec.line is OUR team1's handicap ----------------------------------------
    ("Handicap map", "Natus Vincere (-1,5)", False, "map_handicap|line=-1.5#team1"),
    ("Handicap map", "FaZe Clan (+1,5)", False, "map_handicap|line=-1.5#team2"),
    ("Handicap map", "FaZe Clan -1,5", False, "map_handicap|line=1.5#team2"),
    ("Handicap map", "Natus Vincere (-1,5)", True, "map_handicap|line=1.5#team2"),
    ("Handicap map (-1,5)", "1", False, "map_handicap|line=-1.5#team1"),
    ("Handicap map (-1,5)", "2", True, "map_handicap|line=1.5#team1"),
    ("Map handicap", "FaZe Clan +1.5", True, "map_handicap|line=1.5#team1"),
    # --- total maps / both teams win a map -------------------------------------------------------
    ("Liczba map", "Powyżej 2,5", False, "total_maps|line=2.5#over"),
    ("Total maps", "Under 2.5", True, "total_maps|line=2.5#under"),
    ("Obie drużyny wygrają mapę", "Tak", False, "both_teams_win_a_map#yes"),
    ("Both teams to win a map", "No", False, "both_teams_win_a_map#no"),
    # --- round handicap -------------------------------------------------------------------------
    ("Handicap rund - mapa 1", "Natus Vincere (-3,5)", False, "round_handicap|map=1|line=-3.5#team1"),
    ("Handicap rund - mapa 1", "FaZe Clan (+3,5)", True, "round_handicap|map=1|line=3.5#team1"),
    ("Round handicap - map 2", "FaZe Clan +4.5", False, "round_handicap|map=2|line=-4.5#team2"),
    # --- totals of rounds -----------------------------------------------------------------------
    ("Liczba rund - mapa 1", "Powyżej 21,5", False, "total_rounds|map=1|line=21.5#over"),
    ("Mapa 2 - suma rund", "Poniżej 22,5", True, "total_rounds|map=2|line=22.5#under"),
    ("Map 1 - Total rounds", "Under 21.5", False, "total_rounds|map=1|line=21.5#under"),
    (
        "Liczba rund FaZe Clan - mapa 1",
        "Powyżej 10,5",
        False,
        "team_total_rounds|map=1|team=team2|line=10.5#over",
    ),
    (
        "Liczba rund FaZe Clan - mapa 1",
        "Powyżej 10,5",
        True,
        "team_total_rounds|map=1|team=team1|line=10.5#over",
    ),
    ("Parzysta/Nieparzysta liczba rund - mapa 1", "Nieparzysta", False, "total_rounds_odd_even|map=1#odd"),
    ("Liczba rund parzysta/nieparzysta - mapa 2", "Parzysta", False, "total_rounds_odd_even|map=2#even"),
    # --- map exact score / overtime -------------------------------------------------------------
    ("Dokładny wynik - mapa 1", "13:9", False, "map_correct_score|map=1#13-9"),
    ("Dokładny wynik - mapa 1", "13:9", True, "map_correct_score|map=1#9-13"),
    ("Dokładny wynik - mapa 1", "Natus Vincere 13:11", True, "map_correct_score|map=1#11-13"),
    ("Dogrywka - mapa 1", "Tak", False, "map_overtime|map=1#yes"),
    ("Map 1 - Overtime", "No", True, "map_overtime|map=1#no"),
    # --- pistol rounds / race to / halves -------------------------------------------------------
    ("Runda pistoletowa - mapa 1", "Natus Vincere", False, "pistol_round_winner|map=1|round=1#team1"),
    ("13. runda - mapa 2", "FaZe Clan", True, "pistol_round_winner|map=2|round=13#team1"),
    ("2. runda pistoletowa - mapa 1", "1", False, "pistol_round_winner|map=1|round=13#team1"),
    ("1st pistol round - map 1", "FaZe Clan", False, "pistol_round_winner|map=1|round=1#team2"),
    ("Pierwszy do 5 rund - mapa 1", "Natus Vincere", False, "race_to_rounds|map=1|line=5#team1"),
    ("Race to 10 rounds - map 2", "FaZe Clan", True, "race_to_rounds|map=2|line=10#team1"),
    ("1. połowa - mapa 1", "Remis", False, "half_winner|map=1|half=1#draw"),
    ("Zwycięzca 2. połowy - mapa 2", "FaZe Clan", True, "half_winner|map=2|half=2#team1"),
    # --- player markets (fake resolver) ---------------------------------------------------------
    (
        "Zabójstwa gracza - mapa 1",
        "s1mple Powyżej 19,5",
        False,
        "player_kills|map=1|player=11|line=19.5#over",
    ),
    (
        "Liczba zabójstw - ZywOo - mapa 1",
        "Poniżej 20,5",
        True,
        "player_kills|map=1|player=22|line=20.5#under",
    ),
    (
        "Zabójstwa gracza (mapy 1-2)",
        "ropz Powyżej 33,5",
        False,
        "player_kills|maps=1,2|player=33|line=33.5#over",
    ),
    ("Player kills - map 1", "910 Over 18.5", False, "player_kills|map=1|player=44|line=18.5#over"),
    (
        "s1mple vs ZywOo - zabójstwa (mapa 1)",
        "ZywOo",
        False,
        "player_kills_h2h|map=1|player=11|opp=22#player:22",
    ),
    (
        "Pojedynek graczy: ZywOo - s1mple - mecz",
        "s1mple",
        False,
        "player_kills_h2h|player=11|opp=22#player:11",
    ),
    ("Pojedynek graczy: ZywOo - s1mple", "s1mple", False, None),  # no scope: one map or the series?
    ("Najwięcej zabójstw - mapa 1", "s1mple", False, "top_fragger|map=1#player:11"),
    ("Najwięcej zabójstw w drużynie FaZe Clan", "ropz", True, "top_fragger|team=team1#player:33"),
    # --- unknown / unsupported -> unmapped ------------------------------------------------------
    ("Zwycięzca turnieju", "Natus Vincere", False, None),
    ("Pierwsza krew - mapa 1", "Natus Vincere", False, None),
    ("16. runda - mapa 1", "Natus Vincere", False, None),
    ("Handicap rund - 1. połowa - mapa 1", "Natus Vincere (-1,5)", False, None),
    ("Liczba rund", "Powyżej 45,5", False, None),
    ("Zabójstwa gracza - mapa 1", "unknown Powyżej 19,5", False, None),
    ("Zwycięzca meczu", "Team Liquid", False, None),
    ("Liczba map", "Natus Vincere", False, None),
    ("Najlepszy gracz meczu", "s1mple", False, None),
    ("Zwycięzca", "Natus Vincere", False, None),  # ambiguous: match or map?
    # --- a line / score / combination inside a team or player selection is never stripped ----------
    ("Zwycięzca meczu", "Natus Vincere (-1,5)", False, None),
    ("Kto wygra mecz", "Natus Vincere / Remis", False, None),
    ("Zwycięzca mapy 1", "Natus Vincere i Tak", False, None),
    ("Zwycięzca mapy 1", "Natus Vincere 13:9", False, None),
    ("Najwięcej zabójstw - mapa 1", "s1mple (+2,5)", False, None),
    ("s1mple vs ZywOo - zabójstwa (mapa 1)", "ZywOo (+1,5)", False, None),
    ("Handicap zabójstw: s1mple vs ZywOo", "s1mple (-2,5)", False, None),
    ("Handicap map", "Natus Vincere 2:0", False, None),
    # --- settlement wording: the pricing engine counts overtime (defaults) ---------------------------
    ("Liczba rund - mapa 1 (bez dogrywki)", "Powyżej 21,5", False, None),
    ("Liczba rund - mapa 1 - regulaminowy czas gry", "Powyżej 21,5", False, None),
    ("Mapa 1 - liczba rund w czasie regulaminowym", "Powyżej 21,5", False, None),
    ("Liczba rund - mapa 1 (nie wliczając dogrywki)", "Powyżej 21,5", False, None),
    ("Map 1 - Total rounds (regular time)", "Over 21.5", False, None),
    ("Handicap rund - mapa 1 (bez dogrywki)", "Natus Vincere (-3,5)", False, None),
    ("Mapa 1 - liczba rund: parzysta/nieparzysta (bez dogrywki)", "Parzysta", False, None),
    ("Liczba rund Natus Vincere - mapa 1 (bez dogrywki)", "Powyżej 10,5", False, None),
    ("Liczba rund - mapa 1 (z dogrywką)", "Powyżej 21,5", False, "total_rounds|map=1|line=21.5#over"),
    ("Mapa 1 - liczba rund w dogrywce", "Powyżej 3,5", False, None),  # rounds played IN overtime
    # markets always priced on the final result: a "regulation only" variant is another market
    ("Zwycięzca meczu (regulaminowy czas)", "Natus Vincere", False, None),
    ("Zwycięzca mapy 1 (bez dogrywki)", "Natus Vincere", False, None),
    ("Dokładny wynik - mapa 1 (bez dogrywki)", "13:9", False, None),
    ("Zabójstwa gracza - mapa 1 (bez dogrywki)", "s1mple Powyżej 19,5", False, None),
    # --- unsupported wording --------------------------------------------------------------------
    ("Zwycięzca meczu (bez remisu)", "Natus Vincere", False, None),
    ("Podwójna szansa", "Natus Vincere lub remis", False, None),
    ("Zwycięzca meczu i liczba map", "Natus Vincere i Powyżej 2,5", False, None),
    ("Zwycięzca mapy 1 + Liczba rund", "Natus Vincere", False, None),
    ("Liczba map", "Powyżej 2,5 i Natus Vincere", False, None),
    ("Obie drużyny wygrają mapę", "Natus Vincere - Tak", False, None),
    # --- a round line must never be read as a map line (it would look like a certain winner) ---------
    ("Handicap", "Natus Vincere (-1,5)", False, None),
    ("Handicap map 1", "Natus Vincere (-2,5)", False, None),
    ("Mapa 1 - handicap map", "Natus Vincere (-2,5)", False, None),
    ("Mapa 1 - Handicap", "Natus Vincere (-3,5)", False, "round_handicap|map=1|line=-3.5#team1"),
    ("Handicap map", "Natus Vincere (-4,5)", False, None),  # outside the map-handicap line bounds
    ("Liczba map", "Powyżej 21,5", False, None),
    ("Liczba rund - mapa 1", "Powyżej 2,5", False, None),
    ("Liczba map - mapa 1", "Powyżej 2,5", False, None),
    # --- kill lines need an explicit scope ---------------------------------------------------------
    ("Zabójstwa gracza", "s1mple Powyżej 19,5", False, None),
    ("Zabójstwa gracza - mecz", "s1mple Powyżej 49,5", False, "player_kills|player=11|line=49.5#over"),
    ("Liczba zabójstw w meczu - s1mple", "Poniżej 49,5", False, "player_kills|player=11|line=49.5#under"),
    (
        "Zabójstwa gracza - mapa 1+2",
        "s1mple Powyżej 39,5",
        False,
        "player_kills|maps=1,2|player=11|line=39.5#over",
    ),
    # --- team markets that look like player markets --------------------------------------------------
    ("Mapa 1 - Liczba zabójstw - Natus Vincere", "Powyżej 70,5", False, None),
    ("Mapa 1 - Liczba zabójstw - NAVI", "Powyżej 70,5", False, None),
    ("Mapa 1 - Łączna liczba zabójstw", "Powyżej 140,5", False, None),
    # --- look-alikes of mapped markets -------------------------------------------------------------
    ("Obie drużyny wygrają rundę pistoletową - mapa 1", "Tak", False, None),
    ("Obie rundy pistoletowe - mapa 1", "Natus Vincere", False, None),
    ("Mapa 1 - Runda 13 - zwycięzca", "Natus Vincere", False, "pistol_round_winner|map=1|round=13#team1"),
    ("Mapa 1 - Runda 5 - zwycięzca", "Natus Vincere", False, None),
    ("Mapy 1-2 - zwycięzca", "Natus Vincere", False, None),
]


@pytest.mark.parametrize(("market", "selection", "swapped", "expected"), CASES)
def test_mapping_table(
    mapper: MarketMapper, market: str, selection: str, swapped: bool, expected: str | None
) -> None:
    assert _key(mapper, market, selection, swapped) == expected


def test_table_is_large_enough() -> None:
    assert len(CASES) >= 40


@pytest.mark.parametrize(
    ("selection", "own_handicap", "slot"),
    [
        ("Natus Vincere (-1,5)", -1.5, 1),
        ("Natus Vincere (+1,5)", 1.5, 1),
        ("FaZe Clan (-1,5)", -1.5, 2),
        ("FaZe Clan (+2,5)", 2.5, 2),
    ],
)
@pytest.mark.parametrize("swapped", [False, True])
def test_handicap_normalisation_rule(
    mapper: MarketMapper, selection: str, own_handicap: float, slot: int, swapped: bool
) -> None:
    """spec.line is team1's handicap; the selected team's own handicap is +line (team1) / -line (team2)."""
    mapped = mapper.map("Handicap map", selection, event=EVENT, swapped=swapped)
    assert mapped is not None and mapped.spec.market_type is MarketType.MAP_HANDICAP
    expected_selection = "team1" if (slot == 1) != swapped else "team2"
    assert mapped.selection_key == expected_selection
    line = mapped.spec.line
    assert line is not None
    assert (line if expected_selection == "team1" else -line) == pytest.approx(own_handicap)


def test_task_example_team2_minus_line(mapper: MarketMapper) -> None:
    mapped = mapper.map("Handicap map", "FaZe Clan -1.5", event=EVENT, swapped=False)
    assert mapped is not None
    assert (mapped.spec.line, mapped.selection_key) == (1.5, "team2")


def test_player_markets_need_a_resolver(mapper: MarketMapper) -> None:
    assert mapper.map("Zabójstwa gracza - mapa 1", "s1mple Powyżej 19,5", event=EVENT, swapped=False) is None


def test_h2h_opponent_from_sibling_selections(mapper: MarketMapper) -> None:
    key = _key(mapper, "Więcej zabójstw (mapa 1)", "s1mple", False, sibling_selections=["s1mple", "ZywOo"])
    assert key == "player_kills_h2h|map=1|player=11|opp=22#player:11"
    assert _key(mapper, "Więcej zabójstw (mapa 1)", "s1mple", False, sibling_selections=["s1mple"]) is None


def _key_with(mapper: MarketMapper, market: str, selection: str, **kw: object) -> str | None:
    mapped = mapper.map(market, selection, event=EVENT, swapped=False, player_resolver=fake_resolver, **kw)  # type: ignore[arg-type]
    return f"{mapped.spec.key()}#{mapped.selection_key}" if mapped else None


def test_three_way_h2h_follows_the_tie_rule(mapper: MarketMapper) -> None:
    """A head-to-head listing "Remis": the players LOSE on a tie - only priceable with h2h_tie_rule=lose."""
    siblings = ["s1mple", "ZywOo", "Remis"]
    assert mapper.settlement.h2h_tie_rule == "push"
    assert _key_with(mapper, "Więcej zabójstw (mapa 1)", "s1mple", sibling_selections=siblings) is None
    assert _key_with(mapper, "Więcej zabójstw (mapa 1)", "Remis", sibling_selections=siblings) is None
    lose = mapper.with_settlement(totals_include_overtime=True, h2h_tie_rule="lose")
    assert lose is not mapper and lose.settlement.h2h_tie_rule == "lose"
    key = _key_with(lose, "Więcej zabójstw (mapa 1)", "s1mple", sibling_selections=siblings)
    assert key == "player_kills_h2h|map=1|player=11|opp=22#player:11"
    draw = _key_with(lose, "Więcej zabójstw (mapa 1)", "Remis", sibling_selections=siblings)
    assert draw == "player_kills_h2h|map=1|player=11|opp=22#draw"


@pytest.mark.parametrize(
    ("market", "selection", "siblings"),
    [
        ("Zwycięzca mapy 1", "Natus Vincere", ["Natus Vincere", "Remis", "FaZe Clan"]),
        ("Zwycięzca mapy 1", "1", ["1", "X", "2"]),
        ("Mapa 1 - Handicap", "Natus Vincere (-3,5)", ["Natus Vincere (-3,5)", "Remis (-3,5)", "FaZe Clan"]),
        (
            "Handicap map",
            "Natus Vincere (-1,5)",
            ["Natus Vincere (-1,5)", "Remis (-1,5)", "FaZe Clan (+1,5)"],
        ),
        ("Pierwszy do 5 rund - mapa 1", "Natus Vincere", ["Natus Vincere", "Żaden", "FaZe Clan"]),
        ("Runda pistoletowa - mapa 1", "Natus Vincere", ["Natus Vincere", "Remis", "FaZe Clan"]),
        ("Liczba rund - mapa 1", "Powyżej 21", ["Powyżej 21", "Dokładnie 21", "Poniżej 21"]),
        ("Total maps", "Over 2", ["Over 2", "Exactly 2", "Under 2"]),
    ],
)
def test_three_way_variants_of_two_way_markets_stay_unmapped(
    mapper: MarketMapper, market: str, selection: str, siblings: list[str]
) -> None:
    two_way = [s for s in siblings if s in (siblings[0], siblings[-1])]
    assert _key_with(mapper, market, selection, sibling_selections=two_way) is not None  # the control
    assert _key_with(mapper, market, selection, sibling_selections=siblings) is None


def test_match_winner_with_a_draw_needs_a_series_that_can_end_level(mapper: MarketMapper) -> None:
    """ "1 X 2" in a BO1/BO3/BO5 is a regulation-time market: the teams lose on overtime."""
    three = ["Natus Vincere", "Remis", "FaZe Clan"]
    for selection, key in (("Natus Vincere", "team1"), ("Remis", "draw"), ("FaZe Clan", "team2")):
        expected = f"match_winner#{key}"
        assert _key_with(mapper, "Zwycięzca meczu", selection, sibling_selections=three) == expected
        assert (
            _key_with(mapper, "Zwycięzca meczu", selection, sibling_selections=three, draw_possible=True)
            == expected
        )
        assert (
            _key_with(mapper, "Zwycięzca meczu", selection, sibling_selections=three, draw_possible=False)
            is None
        )
    two = ["Natus Vincere", "FaZe Clan"]
    assert (
        _key_with(mapper, "Zwycięzca meczu", "Natus Vincere", sibling_selections=two, draw_possible=False)
        == "match_winner#team1"
    )
    # a half can end 6-6 whatever the series format
    half = _key_with(mapper, "1. połowa - mapa 1", "Remis", sibling_selections=three, draw_possible=False)
    assert half == "half_winner|map=1|half=1#draw"


def test_round_count_markets_follow_totals_include_overtime(mapper: MarketMapper) -> None:
    """With totals_include_overtime=False only names that EXCLUDE overtime... and silent ones map."""
    regulation = mapper.with_settlement(totals_include_overtime=False, h2h_tie_rule="push")
    assert mapper.with_settlement(totals_include_overtime=True, h2h_tie_rule="push") is mapper
    name, sel = "Liczba rund - mapa 1 (bez dogrywki)", "Powyżej 21,5"
    assert _key_with(mapper, name, sel) is None
    assert _key_with(regulation, name, sel) == "total_rounds|map=1|line=21.5#over"
    assert _key_with(regulation, "Liczba rund - mapa 1 (z dogrywką)", sel) is None
    assert _key_with(mapper, "Liczba rund - mapa 1 (z dogrywką)", sel) == "total_rounds|map=1|line=21.5#over"
    assert (
        _key_with(regulation, "Handicap rund - mapa 1 - regulaminowy czas", "FaZe Clan (+3,5)")
        == "round_handicap|map=1|line=-3.5#team2"
    )
    # never for markets priced on the final result, whatever the setting
    assert _key_with(regulation, "Zwycięzca mapy 1 (bez dogrywki)", "Natus Vincere") is None
    # contradictory wording is never guessed
    assert _key_with(mapper, "Liczba rund - mapa 1 (z dogrywką) bez dogrywki", sel) is None
    with pytest.raises(ValueError, match="h2h_tie_rule"):
        mapper.with_settlement(totals_include_overtime=True, h2h_tie_rule="void")


def test_shipped_guards_match_the_code_defaults() -> None:
    """The YAML ``guards:`` section replaces the defaults: keep both in sync."""
    from cs2bot.odds.market_mapping import MarketGuards

    shipped = MarketMapper.from_yaml(project_root() / "config" / "betclic_markets.yaml").guards
    default = MarketGuards.build()
    for attr in ("excludes_overtime", "includes_overtime", "unsupported"):
        assert [p.pattern for p in getattr(shipped, attr)] == [p.pattern for p in getattr(default, attr)]
    assert dict(shipped.line_bounds) == dict(default.line_bounds)


def test_guards_section_is_validated_and_overridable() -> None:
    rules = {"rules": [_minimal_rule(market_type="total_maps", patterns=["^mapy$"], selection="over_under")]}
    custom = MarketMapper.from_dict({**rules, "guards": {"line_bounds": {"total_maps": [0.5, 9.5]}}})
    assert custom.map("Mapy", "Powyżej 8,5", event=EVENT, swapped=False) is not None
    assert MarketMapper.from_dict(rules).map("Mapy", "Powyżej 8,5", event=EVENT, swapped=False) is None
    with pytest.raises(ValueError, match="line_bounds"):
        MarketMapper.from_dict({**rules, "guards": {"line_bounds": {"total_maps": [1]}}})
    with pytest.raises(ValueError, match="guards"):
        MarketMapper.from_dict({**rules, "guards": ["nope"]})
    with pytest.raises(ValueError, match="bad regex"):
        MarketMapper.from_dict({**rules, "guards": {"unsupported": ["("]}})


def test_team_prefixed_nicknames_are_still_players(mapper: MarketMapper) -> None:
    resolver = make_name_resolver({"s1mple": 11, "zywoo": 22})
    mapped = mapper.map(
        "Najwięcej zabójstw - mapa 1", "NAVI s1mple", event=EVENT, swapped=False, player_resolver=resolver
    )
    assert mapped is not None and mapped.selection_key == "player:11"
    kills = mapper.map(
        "Zabójstwa gracza - mapa 1",
        "Natus Vincere s1mple Powyżej 19,5",
        event=EVENT,
        swapped=False,
        player_resolver=resolver,
    )
    assert kills is not None and kills.spec.player_id == 11
    # ... but a bare team name is a team market, never a player
    assert _key_with(mapper, "Najwięcej zabójstw - mapa 1", "Natus Vincere") is None


def test_mapping_rule_names_the_rule_that_mapped(mapper: MarketMapper) -> None:
    assert mapper.match_rule("Liczba rund - mapa 1") == "team_total_rounds"  # first rule fitting the name
    assert mapper.mapping_rule("Liczba rund - mapa 1", "Powyżej 21,5", event=EVENT) == "total_rounds"
    assert mapper.mapping_rule("Zwycięzca meczu", "Natus Vincere", event=EVENT) == "match_winner"
    assert mapper.mapping_rule("Zwycięzca turnieju", "Natus Vincere", event=EVENT) is None
    assert mapper.match_rule("Liczba rund - mapa 1 (bez dogrywki)") is None  # fails the settlement guard


def test_academy_teams_are_not_confused(mapper: MarketMapper) -> None:
    spirit = CollectedEvent(
        external_id="2",
        name="Team Spirit - Spirit Academy",
        team1_raw="Team Spirit",
        team2_raw="Spirit Academy",
    )
    first = mapper.map("Zwycięzca meczu", "Spirit Academy", event=spirit, swapped=False)
    second = mapper.map("Zwycięzca meczu", "Spirit", event=spirit, swapped=False)
    assert first is not None and first.selection_key == "team2"
    assert second is not None and second.selection_key == "team1"


def test_group_key_is_swap_independent_and_separates_lines(mapper: MarketMapper) -> None:
    a = mapper.group_key("Handicap map", "Natus Vincere (-1,5)", event=EVENT)
    b = mapper.group_key("Handicap map", "FaZe Clan (+1,5)", event=EVENT)
    c = mapper.group_key("Handicap map", "Natus Vincere (+1,5)", event=EVENT)
    assert a is not None and a == b and a != c
    assert mapper.group_key("Liczba rund - mapa 1", "Powyżej 21,5", event=EVENT) != mapper.group_key(
        "Liczba rund - mapa 1", "Powyżej 23,5", event=EVENT
    )
    assert mapper.group_key("Zwycięzca turnieju", "x", event=EVENT) is None


def test_match_rule_diagnostics(mapper: MarketMapper) -> None:
    assert mapper.match_rule("Zwycięzca meczu") == "match_winner"
    assert mapper.match_rule("Liczba rund - mapa 1") == "team_total_rounds"  # selection decides later
    assert mapper.match_rule("Zwycięzca turnieju") is None
    assert len(rules_fingerprint(mapper)) == 12


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mapa 1", 1),
        ("1. mapa - zwyciezca", 1),
        ("na mapie 2", 2),
        ("map 3 winner", 3),
        ("pierwsza mapa", 1),
        ("liczba map", None),
        ("liczba map 2,5", None),
        ("mapa 1+2", None),  # several maps are never a single map
        ("mapy 1-2", None),
        ("mapa 9", None),
    ],
)
def test_extract_map_number(text: str, expected: int | None) -> None:
    assert extract_map_number(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mapy 1-2", (1, 2)),
        ("mapy 1 i 2", (1, 2)),
        ("maps 1-3", (1, 2, 3)),
        ("mapa 1 - 2. polowa", None),
        ("mapa 1", None),
        ("mapa 1+2", (1, 2)),
        ("mapy 1, 2 i 3", (1, 2, 3)),
        ("mapy 2-1", None),
    ],
)
def test_extract_maps_scope(text: str, expected: tuple[int, ...] | None) -> None:
    assert extract_maps_scope(text) == expected


# --------------------------------------------------------------------------- YAML validation


def _minimal_rule(**overrides: object) -> dict[str, object]:
    rule: dict[str, object] = {
        "id": "r",
        "market_type": "match_winner",
        "patterns": ["^wynik$"],
        "selection": "team",
    }
    rule.update(overrides)
    return rule


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        (_minimal_rule(market_type="nope"), "market_type"),
        (_minimal_rule(selection="nope"), "selection kind"),
        (_minimal_rule(patterns=["("]), "bad regex"),
        (_minimal_rule(patterns=[]), "at least one pattern"),
        (_minimal_rule(extract=["colour"]), "extractors"),
        (_minimal_rule(requires=["colour"]), "requirements"),
        (_minimal_rule(team_in_market="maybe"), "team_in_market"),
        (_minimal_rule(defaults={"colour": 1}), "parameter"),
    ],
)
def test_invalid_rules_are_rejected(rule: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        MarketMapper.from_dict({"rules": [rule]})


def test_duplicate_rule_ids_and_empty_rules_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        MarketMapper.from_dict({"rules": [_minimal_rule(), _minimal_rule()]})
    with pytest.raises(ValueError, match="non-empty"):
        MarketMapper.from_dict({"rules": []})


def test_custom_vocabulary_and_anchor_lists_flattened() -> None:
    mapper = MarketMapper.from_dict(
        {
            "settings": {"words": {"over": ["wiecej"], "under": ["mniej"]}},
            "rules": [
                {
                    "id": "tm",
                    "market_type": "total_maps",
                    "patterns": ["^mapy$"],
                    "exclude": [["rund"], "zabojstw"],
                    "requires": ["line"],
                    "selection": "over_under",
                }
            ],
        }
    )
    mapped = mapper.map("Mapy", "Więcej 2,5", event=EVENT, swapped=False)
    assert mapped is not None and mapped.selection_key == "over"
    assert mapper.map("Mapy", "Powyżej 2,5", event=EVENT, swapped=False) is None  # default word replaced


# --------------------------------------------------------------------------- DB helpers


def test_lineup_player_resolver(session) -> None:
    navi, faze = make_team(session, "Natus Vincere"), make_team(session, "FaZe Clan")
    match = make_match(session, navi, faze, datetime(2026, 11, 23, 17, tzinfo=UTC))
    ids = add_lineup(session, match, navi, ["s1mple", "b1t", "iM"])
    ids |= add_lineup(session, match, faze, ["ropz", "broky"])
    resolve = lineup_player_resolver(session, match.id)
    assert resolve("S1MPLE") == ids["s1mple"]
    assert resolve("NAVI b1t") == ids["b1t"]  # whole-token containment
    assert resolve("brokky") == ids["broky"]  # fuzzy
    assert resolve("ZywOo") is None


def test_lineup_resolver_falls_back_to_rosters_and_rejects_ambiguity(session) -> None:
    from cs2bot.db.models import Player

    a, b = make_team(session, "Alpha"), make_team(session, "Bravo")
    match = make_match(session, a, b, datetime(2026, 11, 23, 17, tzinfo=UTC))
    session.add_all(
        [
            Player(nickname="dup", current_team_id=a.id),
            Player(nickname="DUP", current_team_id=b.id),
            Player(nickname="solo", current_team_id=b.id),
        ]
    )
    session.flush()
    resolve = lineup_player_resolver(session, match.id)
    assert resolve("solo") is not None
    assert resolve("dup") is None
    assert make_name_resolver({})("anyone") is None


def test_list_unmapped(session) -> None:
    now = datetime(2026, 11, 22, 12, tzinfo=UTC)
    ev = BookmakerEvent(
        bookmaker=Bookmaker.BETCLIC,
        external_id="1",
        name="A - B",
        team1_raw="A",
        team2_raw="B",
        first_seen_at=now,
        last_seen_at=now,
    )
    ev2 = BookmakerEvent(
        bookmaker=Bookmaker.BETCLIC,
        external_id="2",
        name="C - D",
        team1_raw="C",
        team2_raw="D",
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add_all([ev, ev2])
    session.flush()
    rows = [
        BookmakerMarket(event_id=ev.id, raw_name="Pierwsza krew", first_seen_at=now, last_seen_at=now),
        BookmakerMarket(event_id=ev2.id, raw_name="Pierwsza krew", first_seen_at=now, last_seen_at=now),
        BookmakerMarket(
            event_id=ev.id, raw_name="Zwycięzca meczu", is_mapped=True, first_seen_at=now, last_seen_at=now
        ),
        BookmakerMarket(
            event_id=ev2.id, raw_name="Zwycięzca meczu", is_mapped=False, first_seen_at=now, last_seen_at=now
        ),
        BookmakerMarket(event_id=ev.id, raw_name="Asy", first_seen_at=now, last_seen_at=now),
    ]
    session.add_all(rows)
    session.flush()
    session.add(BookmakerSelection(market_id=rows[2].id, raw_name="Ktoś inny", is_mapped=False))
    session.flush()
    assert list_unmapped(session) == [("Pierwsza krew", 2), ("Asy", 1)]
    assert list_unmapped(session, limit=1) == [("Pierwsza krew", 2)]
    assert list_unmapped_selections(session) == [("Zwycięzca meczu", "Ktoś inny", 1)]
