"""Polish number/date parsing, formatting, matching normalisation and screenshot naming."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from cs2bot.executor.textutils import (
    classify_text,
    contains_phrase,
    find_polish_decimals,
    first_regex_group,
    format_polish_decimal,
    names_compatible,
    normalise_text,
    parse_polish_datetime,
    parse_polish_decimal,
    sanitize_filename_part,
    screenshot_filename,
    try_parse_polish_decimal,
)

NOW = datetime(2026, 11, 22, 17, 0, tzinfo=UTC)  # 18:00 in Warsaw (CET)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,85", "1.85"),
        ("1.85", "1.85"),
        ("12,50", "12.50"),
        ("1 234,56 zł", "1234.56"),
        ("1 234,56 zł", "1234.56"),
        ("1 234,56 PLN", "1234.56"),
        ("Saldo: 10 000,00 zł", "10000.00"),
        ("1.234,56", "1234.56"),
        ("1,234.56", "1234.56"),
        ("Kurs: 2,10", "2.10"),
        ("-1,5", "-1.5"),
        ("−1,5", "-1.5"),
        ("+2,5", "2.5"),
        ("0,5 zł", "0.5"),
        ("7", "7"),
    ],
)
def test_parse_polish_decimal(text: str, expected: str) -> None:
    assert parse_polish_decimal(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["", "   ", "brak", "zł", None])
def test_parse_polish_decimal_rejects_text_without_numbers(text: str | None) -> None:
    with pytest.raises(ValueError, match="no number"):
        parse_polish_decimal(text)
    assert try_parse_polish_decimal(text) is None


@pytest.mark.parametrize("text", ["1.234.567", "2 1,85", "1,85 (2 zakłady)", "1,85 / 2,10"])
def test_parse_polish_decimal_never_guesses_ambiguous_text(text: str) -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        parse_polish_decimal(text)


def test_find_polish_decimals_in_free_text() -> None:
    assert find_polish_decimals("Kurs zmienił się z 1,85 na 1,70") == [Decimal("1.85"), Decimal("1.70")]
    assert find_polish_decimals(None) == []


@pytest.mark.parametrize(
    ("value", "kwargs", "expected"),
    [
        (Decimal("12.5"), {}, "12,50"),
        (Decimal("12.50"), {}, "12,50"),
        (Decimal("2"), {}, "2,00"),
        (Decimal("0.005"), {}, "0,01"),  # half-up
        (Decimal("1234.565"), {"thousands_sep": " "}, "1 234,57"),
        (Decimal("1234567.1"), {"thousands_sep": " "}, "1 234 567,10"),
        (Decimal("1234.5"), {}, "1234,50"),  # stake inputs: no thousands separator
        (Decimal("-3.456"), {}, "-3,46"),
        (Decimal("7.6"), {"places": 0}, "8"),
        ("3.1", {}, "3,10"),
        (5, {}, "5,00"),
    ],
)
def test_format_polish_decimal(value: Decimal | str | int, kwargs: dict[str, object], expected: str) -> None:
    assert format_polish_decimal(value, **kwargs) == expected  # type: ignore[arg-type]


def test_format_polish_decimal_round_trips_through_parse() -> None:
    for raw in ("0.01", "2.00", "12.34", "999.99", "1234.56"):
        assert parse_polish_decimal(format_polish_decimal(Decimal(raw))) == Decimal(raw)


def test_format_polish_decimal_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        format_polish_decimal(Decimal("NaN"))
    with pytest.raises(ValueError):
        format_polish_decimal(Decimal("1"), places=-1)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Zwycięzca meczu", "ZWYCIĘZCA   MECZU "),
        ("Zwycięzca meczu", "zwyciezca meczu"),
        ("Łódź", "lodz"),
        ("Natus Vincere (-1,5)", "natus vincere -1.5"),
        ("Natus Vincere (−1,5)", "Natus Vincere -1,5"),
        ("Powyżej 26,5", "Powyzej 26.5"),
        ("NAVI – Vitality", "navi - vitality"),
        ("„Mapa 1”", "mapa 1"),
    ],
)
def test_normalise_text_equivalences(a: str, b: str) -> None:
    assert normalise_text(a) == normalise_text(b)


def test_normalise_text_keeps_meaningful_differences() -> None:
    assert normalise_text("Vitality +1,5") != normalise_text("Vitality -1,5")
    assert normalise_text("Powyżej 2,5") != normalise_text("Poniżej 2,5")
    assert normalise_text("Mapa 1") != normalise_text("Mapa 2")
    assert normalise_text(None) == "" and normalise_text("   ") == ""


def test_contains_phrase_is_word_prefix_based() -> None:
    text = normalise_text("Rynek jest chwilowo zawieszony")
    assert contains_phrase(text, "zawieszon")
    assert not contains_phrase(text, "wieszon")  # must start at a word boundary
    assert not contains_phrase(text, "")


def test_classify_text_respects_order() -> None:
    phrases = {"odds": ["kurs uległ zmianie"], "rejected": ["nie został przyjęty"]}
    msg = "Zakład nie został przyjęty: kurs uległ zmianie"
    assert classify_text(msg, phrases, ["odds", "rejected"]) == "odds"
    assert classify_text(msg, phrases, ["rejected", "odds"]) == "rejected"
    assert classify_text("Wszystko w porządku", phrases, ["odds", "rejected"]) is None
    assert classify_text("", phrases, ["odds"]) is None


def test_names_compatible() -> None:
    assert names_compatible("natus vincere", "natus vincere")
    assert names_compatible("natus vincere", "natus vincere -1.5")
    assert not names_compatible("vitality", "natus vincere")
    assert not names_compatible("1", "natus vincere -1.5")  # too short to be trusted
    assert not names_compatible("", "x")


def test_first_regex_group() -> None:
    patterns = [re.compile(r"nr\s*([0-9]+)", re.I), re.compile(r"id=(\w+)")]
    assert first_regex_group("Kupon NR 12345", patterns) == "12345"
    assert first_regex_group("id=abc", patterns) == "abc"
    assert first_regex_group("nic", patterns) is None
    assert first_regex_group(None, patterns) is None
    assert first_regex_group("x 42", [re.compile(r"\d+")]) == "42"  # no group -> whole match


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("22.11.2026 18:30", datetime(2026, 11, 22, 17, 30, tzinfo=UTC)),
        ("22/11/26, 18:30:05", datetime(2026, 11, 22, 17, 30, 5, tzinfo=UTC)),
        ("22-11-2026 08:05", datetime(2026, 11, 22, 7, 5, tzinfo=UTC)),
        ("2026-11-22 18:30", datetime(2026, 11, 22, 17, 30, tzinfo=UTC)),
        ("2026-11-22T18:30:00Z", datetime(2026, 11, 22, 18, 30, tzinfo=UTC)),
        ("Dzisiaj, 18:30", datetime(2026, 11, 22, 17, 30, tzinfo=UTC)),
        ("Dziś 09:00", datetime(2026, 11, 22, 8, 0, tzinfo=UTC)),
        ("Wczoraj 23:59", datetime(2026, 11, 21, 22, 59, tzinfo=UTC)),
        ("Jutro 12:00", datetime(2026, 11, 23, 11, 0, tzinfo=UTC)),
        ("22 listopada 2026, 18:30", datetime(2026, 11, 22, 17, 30, tzinfo=UTC)),
        ("5 paź 2026 20:00", datetime(2026, 10, 5, 18, 0, tzinfo=UTC)),  # CEST (UTC+2)
        ("22 lis 18:30", datetime(2026, 11, 22, 17, 30, tzinfo=UTC)),
        ("30.12 10:00", datetime(2025, 12, 30, 9, 0, tzinfo=UTC)),  # no year, would be in the future
        ("18:30", datetime(2026, 11, 22, 17, 30, tzinfo=UTC)),
        ("01.07.2026", datetime(2026, 6, 30, 22, 0, tzinfo=UTC)),  # date only -> local midnight
    ],
)
def test_parse_polish_datetime(text: str, expected: datetime) -> None:
    parsed = parse_polish_datetime(text, now=NOW)
    assert parsed == expected
    assert parsed is not None and parsed.tzinfo is not None


@pytest.mark.parametrize("text", [None, "", "brak daty", "31.02.2026 10:00", "Nr zakładu"])
def test_parse_polish_datetime_returns_none_for_garbage(text: str | None) -> None:
    assert parse_polish_datetime(text, now=NOW) is None


def test_screenshot_filename_is_safe_and_informative() -> None:
    name = screenshot_filename(
        42, 'pred:123/abc def?<>|"*', "placed", datetime(2026, 11, 22, 18, 30, 1, tzinfo=UTC)
    )
    assert name == "20261122T183001Z_bet42_pred-123-abc-def_placed.png"
    assert not re.search(r'[<>:"/\\|?* ]', name)
    long_key = screenshot_filename(1, "k" * 500, "uncertain", max_key_len=20)
    assert len(long_key) < 80 and "k" * 20 in long_key and "k" * 21 not in long_key


def test_sanitize_filename_part() -> None:
    assert sanitize_filename_part("..") == "x"
    assert sanitize_filename_part("a b/c") == "a-b-c"
