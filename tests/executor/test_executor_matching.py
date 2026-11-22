"""The "never guess" rules for picking the market / selection on the event page."""

from __future__ import annotations

from cs2bot.executor.matching import Candidate, MatchKind, id_matches, pick_candidate
from cs2bot.executor.textutils import normalise_text


def _cand(index: int, *names: str, ids: tuple[str, ...] = ()) -> Candidate:
    return Candidate(index=index, names=tuple(normalise_text(n) for n in names), ids=ids, display=names[0])


def test_found_by_normalised_name() -> None:
    cands = [_cand(0, "Zwycięzca meczu"), _cand(1, "Zwycięzca mapy 1"), _cand(2, "Handicap map")]
    outcome = pick_candidate(cands, "ZWYCIĘZCA MAPY 1")
    assert outcome.kind is MatchKind.FOUND and outcome.index == 1


def test_any_name_variant_matches() -> None:
    cands = [_cand(0, "Natus Vincere", "Natus Vincere -1,5"), _cand(1, "Natus Vincere", "Natus Vincere +1,5")]
    outcome = pick_candidate(cands, "Natus Vincere (-1,5)")
    assert outcome.kind is MatchKind.FOUND and outcome.index == 0
    # the bare team name fits both lines -> never guess
    assert pick_candidate(cands, "Natus Vincere").kind is MatchKind.AMBIGUOUS


def test_not_found_and_no_fuzzy_matching() -> None:
    cands = [_cand(0, "Zwycięzca meczu")]
    assert pick_candidate(cands, "Zwycięzca").kind is MatchKind.NOT_FOUND
    assert pick_candidate(cands, "Zwyciezca meczu (BO3)").kind is MatchKind.NOT_FOUND
    assert pick_candidate([], "x").kind is MatchKind.NOT_FOUND
    assert pick_candidate(cands, "   ").kind is MatchKind.NOT_FOUND


def test_ambiguous_names() -> None:
    cands = [_cand(0, "Vitality"), _cand(1, "VITALITY ")]
    outcome = pick_candidate(cands, "Vitality")
    assert outcome.kind is MatchKind.AMBIGUOUS and outcome.index is None
    assert "2 elements" in outcome.detail


def test_ids_disambiguate_equal_names() -> None:
    cands = [_cand(0, "Zwycięzca mapy", ids=("market-111",)), _cand(1, "Zwycięzca mapy", ids=("market-222",))]
    outcome = pick_candidate(cands, "Zwycięzca mapy", "222")
    assert outcome.kind is MatchKind.FOUND and outcome.index == 1


def test_id_on_an_element_with_another_name_is_a_conflict() -> None:
    cands = [_cand(0, "Vitality", ids=("S1",)), _cand(1, "Natus Vincere", ids=("S2",))]
    outcome = pick_candidate(cands, "Natus Vincere", "S1")
    assert outcome.kind is MatchKind.CONFLICT
    assert "Vitality" in outcome.detail


def test_two_elements_with_id_and_name_are_ambiguous() -> None:
    cands = [_cand(0, "Vitality", ids=("x-77",)), _cand(1, "Vitality", ids=("77",))]
    assert pick_candidate(cands, "Vitality", "77").kind is MatchKind.AMBIGUOUS


def test_ids_absent_from_dom_fall_back_to_names() -> None:
    cands = [_cand(0, "Vitality", ids=("S9",)), _cand(1, "Natus Vincere")]
    outcome = pick_candidate(cands, "Natus Vincere", "S-not-in-dom")
    assert outcome.kind is MatchKind.FOUND and outcome.index == 1


def test_id_matches_tokens_only() -> None:
    assert id_matches("123", "123")
    assert id_matches("market-123", "123")
    assert id_matches("123_x", "123")  # "_" separates tokens
    assert not id_matches("1234", "123")
    assert not id_matches("a123", "123")
    assert not id_matches(None, "123")
    assert not id_matches("123", "")
