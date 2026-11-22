"""Text and number helpers shared by the Betclic payload parser and the market mapper.

Betclic.pl is a Polish site: decimals use a comma (``"1,85"``), labels contain Polish diacritics
(``"Zwycięzca meczu"``, ``"Łączna liczba map"``) and typographic dashes/minus signs (``"−1,5"``).
Everything that compares or parses labels goes through :func:`fold_text` /
:func:`parse_decimal` so that rules can be written in plain lowercase ASCII.
"""

from __future__ import annotations

import math
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from cs2bot.utils.money import D

#: Odds are stored as ``Numeric(8, 3)``; everything is quantized to this before comparing.
ODDS_QUANTUM = Decimal("0.001")
#: Plausible decimal odds range (exclusive lower bound, inclusive upper bound).
MIN_ODDS_EXCLUSIVE = Decimal("1")
MAX_ODDS = Decimal("1000")

_PUNCT_MAP: dict[str, str] = {
    # dashes and minus signs -> ASCII hyphen-minus
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "―": "-",
    "−": "-",
    "﹘": "-",
    "﹣": "-",
    "－": "-",
    # exotic spaces -> space, zero-width -> nothing
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    "​": "",
    "﻿": "",
    # quotes
    "‘": "'",
    "’": "'",
    "‚": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "×": "x",
}
_LETTER_MAP: dict[str, str] = {
    # letters that do not decompose under NFKD
    "ł": "l",
    "Ł": "L",
    "đ": "d",
    "Đ": "D",
    "ø": "o",
    "Ø": "O",
    "ß": "ss",
    "æ": "ae",
    "Æ": "AE",
    "œ": "oe",
    "Œ": "OE",
}
_PUNCT_TRANSLATION = str.maketrans(_PUNCT_MAP)
_LETTER_TRANSLATION = str.maketrans(_LETTER_MAP)
_WS_RE = re.compile(r"\s+")
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)$")


def tidy(text: str) -> str:
    """Strip and collapse whitespace only - used for raw names, which are stored as received
    (the executor looks markets/selections up by their on-page text)."""
    return " ".join(text.split())


def clean_text(text: str) -> str:
    """NFKC-normalise, unify dashes/spaces/quotes and collapse whitespace (case and letters kept)."""
    out = unicodedata.normalize("NFKC", text).translate(_PUNCT_TRANSLATION)
    return _WS_RE.sub(" ", out).strip()


def fold_text(text: str) -> str:
    """Comparable form of a label: :func:`clean_text` + lowercase + no diacritics (``ł`` -> ``l``).

    Punctuation (``: - + , . ( ) /``) and digits are kept because market rules rely on them
    (``"mapa 1"``, ``"2:1"``, ``"-1,5"``).
    """
    base = clean_text(text).translate(_LETTER_TRANSLATION)
    decomposed = unicodedata.normalize("NFKD", base)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _WS_RE.sub(" ", stripped.lower()).strip()


def parse_decimal(value: object) -> Decimal | None:
    """Parse a number given as int/float/Decimal or a (Polish or English) string.

    ``"1,85"``, ``"1.85"``, ``" 2 "``, ``"−1,5"`` (Unicode minus), ``"+0,5"``, ``"@1.85"``.
    Booleans, NaN/inf and anything else return ``None``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return D(value) if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.translate(_PUNCT_TRANSLATION).strip().replace(" ", "")
        if text.startswith("@"):
            text = text[1:]
        if not text or not _NUMBER_RE.match(text):
            return None
        try:
            return Decimal(text.replace(",", "."))
        except InvalidOperation:
            return None
    return None


def quantize_odds(value: Decimal) -> Decimal:
    return value.quantize(ODDS_QUANTUM, rounding=ROUND_HALF_UP)


def parse_odds(value: object) -> Decimal | None:
    """Decimal odds in (1, 1000], quantized to 0.001; ``None`` when absent or implausible."""
    number = parse_decimal(value)
    if number is None or number <= MIN_ODDS_EXCLUSIVE or number > MAX_ODDS:
        return None
    return quantize_odds(number)


def truncate(text: str | None, length: int) -> str | None:
    """Clip a string to a DB column length (PostgreSQL rejects overlong VARCHAR values)."""
    if text is None:
        return None
    return text if len(text) <= length else text[:length]
