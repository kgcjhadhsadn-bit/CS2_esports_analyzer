from __future__ import annotations

from decimal import Decimal

import pytest

from cs2bot.odds.text import clean_text, fold_text, parse_decimal, parse_odds, quantize_odds, tidy, truncate


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,85", Decimal("1.85")),
        ("1.85", Decimal("1.85")),
        (" 2 ", Decimal("2")),
        ("−1,5", Decimal("-1.5")),  # Unicode minus
        ("–1,5", Decimal("-1.5")),  # en dash
        ("+0,5", Decimal("0.5")),
        ("@1.91", Decimal("1.91")),
        ("1 000,5", Decimal("1000.5")),  # non-breaking space inside
        (1.85, Decimal("1.85")),
        (2, Decimal("2")),
        (Decimal("3.10"), Decimal("3.10")),
    ],
)
def test_parse_decimal_accepts_polish_and_english(raw: object, expected: Decimal) -> None:
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize(
    "raw", [None, True, "", "n/a", "1,8,5", "1.85x", float("nan"), float("inf"), [1.5], {"v": 1}]
)
def test_parse_decimal_rejects_garbage(raw: object) -> None:
    assert parse_decimal(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1,85", Decimal("1.850")), ("1.8500000000000001", Decimal("1.850")), (1.0001, Decimal("1.000"))],
)
def test_parse_odds_quantizes(raw: object, expected: Decimal) -> None:
    assert parse_odds(raw) == expected


@pytest.mark.parametrize("raw", ["1,00", "0.95", "-2", "1001", 1])
def test_parse_odds_rejects_implausible(raw: object) -> None:
    assert parse_odds(raw) is None


def test_quantize_odds_half_up() -> None:
    assert quantize_odds(Decimal("1.8455")) == Decimal("1.846")


def test_fold_text_strips_polish_diacritics_and_unifies_dashes() -> None:
    assert fold_text("  Łączna   liczba map – Zwycięzca  ") == "laczna liczba map - zwyciezca"
    assert fold_text("Zabójstwa gracza: s1mple (mapa 1)") == "zabojstwa gracza: s1mple (mapa 1)"
    assert fold_text("Handicap −1,5") == "handicap -1,5"


def test_clean_and_tidy_keep_letters() -> None:
    assert clean_text("Dokładny wynik – mapa 1") == "Dokładny wynik - mapa 1"
    assert tidy("  Dokładny \n wynik ") == "Dokładny wynik"


def test_truncate() -> None:
    assert truncate("abcdef", 3) == "abc"
    assert truncate("ab", 3) == "ab"
    assert truncate(None, 3) is None
