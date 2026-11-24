"""Text helpers for the Betclic executor.

* Polish number parsing/formatting: odds ``"1,85"``, money ``"1 234,56 zł"``, stake input ``"12,50"``.
* :func:`normalise_text` for matching market/selection names (case, accents, whitespace, dashes).
* Polish date parsing for the bet history ("22.11.2026 18:30", "Dzisiaj, 18:30", "22 lis 2026").
* Screenshot file naming.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from cs2bot.utils.time import WARSAW, parse_datetime, utcnow

# Characters used as thousands separators: space, NBSP, narrow NBSP, thin space, figure space, apostrophe.
_GROUP_CHARS = "     '"
_MINUS_SIGNS = frozenset({"-", "−"})

_NUMBER_RE = re.compile(
    r"""
    (?<!\d)
    (?P<sign>[-+−])?
    (?P<num>
        \d{1,3}(?:[     ']\d{3})+(?:[.,]\d+)?   # 1 234,56 | 10 000
      | \d{1,3}(?:\.\d{3})+,\d+                                          # 1.234,56
      | \d{1,3}(?:,\d{3})+\.\d+                                          # 1,234.56
      | \d+(?:[.,]\d+)?                                                  # 1,85 | 12.50 | 3
    )
    (?![\d])
    """,
    re.VERBOSE,
)


def _number_to_decimal(sign: str | None, num: str) -> Decimal:
    digits = num
    for ch in _GROUP_CHARS:
        digits = digits.replace(ch, "")
    if "," in digits and "." in digits:
        # Both separators: the last one is the decimal separator.
        if digits.rfind(",") > digits.rfind("."):
            digits = digits.replace(".", "").replace(",", ".")
        else:
            digits = digits.replace(",", "")
    else:
        digits = digits.replace(",", ".")
    try:
        value = Decimal(digits)
    except InvalidOperation as exc:  # pragma: no cover - the regex only yields valid numbers
        raise ValueError(f"not a number: {num!r}") from exc
    return -value if sign in _MINUS_SIGNS else value


def find_polish_decimals(text: str | None) -> list[Decimal]:
    """All numbers in ``text`` (Polish or English separators), in order of appearance."""
    if not text:
        return []
    return [_number_to_decimal(m.group("sign"), m.group("num")) for m in _NUMBER_RE.finditer(text)]


def parse_polish_decimal(text: str | None) -> Decimal:
    """Parse exactly one number from text such as ``"1,85"``, ``"1 234,56 zł"``, ``"Kurs: 2.10"``.

    Thousands separators (spaces, NBSP, dots before a decimal comma) are removed; a single ``.`` or ``,``
    is the decimal separator. Raises ``ValueError`` when the text contains no number or more than one
    (ambiguous text is never guessed).
    """
    values = find_polish_decimals(text)
    if not values:
        raise ValueError(f"no number in {text!r}")
    if len(values) > 1:
        raise ValueError(f"ambiguous number text {text!r} ({len(values)} numbers)")
    return values[0]


def try_parse_polish_decimal(text: str | None) -> Decimal | None:
    """:func:`parse_polish_decimal` returning ``None`` instead of raising."""
    try:
        return parse_polish_decimal(text)
    except ValueError:
        return None


def format_polish_decimal(value: Decimal | int | str, places: int = 2, *, thousands_sep: str = "") -> str:
    """``Decimal("12.5")`` -> ``"12,50"`` (half-up rounding to ``places`` decimals).

    The default has no thousands separator because that is what stake inputs accept; pass
    ``thousands_sep="\\u00a0"`` for display.
    """
    if places < 0:
        raise ValueError("places must be >= 0")
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    if not dec.is_finite():
        raise ValueError(f"cannot format {value!r}")
    quantum = Decimal(1).scaleb(-places)
    q = dec.quantize(quantum, rounding=ROUND_HALF_UP)
    sign = "-" if q < 0 else ""
    integer, _, fraction = format(abs(q), "f").partition(".")
    if thousands_sep:
        groups: list[str] = []
        while len(integer) > 3:
            groups.insert(0, integer[-3:])
            integer = integer[:-3]
        groups.insert(0, integer)
        integer = thousands_sep.join(groups)
    return f"{sign}{integer},{fraction}" if places else f"{sign}{integer}"


_TRANSLATE = str.maketrans(
    {
        "ł": "l",
        "Ł": "l",
        "đ": "d",
        "Đ": "d",
        "ø": "o",
        "Ø": "o",
        "−": "-",
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
    }
)
_DECORATION_RE = re.compile(r"[()\[\]{}\"'`´„”“’‘«»]")
_DECIMAL_COMMA_RE = re.compile(r"(?<=\d),(?=\d)")
_WS_RE = re.compile(r"\s+")


def normalise_text(text: str | None) -> str:
    """Comparable form of a market/selection/message text.

    Casefolded, accents removed (incl. ``ł`` -> ``l``), NBSP/odd spaces and dash variants unified,
    brackets/quotes dropped, decimal commas turned into dots (``"26,5"`` == ``"26.5"``), whitespace
    collapsed. Signs are kept (``"-1.5"`` and ``"+1.5"`` stay different).
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text).translate(_TRANSLATE)
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = s.casefold()
    s = _DECIMAL_COMMA_RE.sub(".", s)
    s = _DECORATION_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def contains_phrase(text: str, phrase: str) -> bool:
    """``phrase`` occurs in ``text`` (both already normalised) starting at a word boundary."""
    if not phrase:
        return False
    return re.search(r"(?<![0-9a-z])" + re.escape(phrase), text) is not None


def classify_text(text: str | None, phrases: Mapping[str, Sequence[str]], order: Iterable[str]) -> str | None:
    """First category (in ``order``) whose normalised phrase occurs in the normalised ``text``."""
    norm = normalise_text(text)
    if not norm:
        return None
    for category in order:
        for phrase in phrases.get(category, ()):
            if contains_phrase(norm, normalise_text(phrase)):
                return category
    return None


def names_compatible(shown: str, wanted: str, *, min_len: int = 3) -> bool:
    """Loose check that a bet-slip label refers to the wanted selection (both normalised).

    Equal, or one contains the other as whole words (the shorter one at least ``min_len`` chars).
    Numbers are whole too (decimal commas are already dots): ``"powyzej 26"`` does not fit
    ``"powyzej 26.5"`` and ``"5 rund"`` does not fit ``"26.5 rund"`` - another line is another bet.
    Used only as a sanity check after the exact match on the event page.
    """
    if not shown or not wanted:
        return False
    if shown == wanted:
        return True
    short, long_ = (shown, wanted) if len(shown) <= len(wanted) else (wanted, shown)
    if len(short) < min_len:
        return False
    pattern = r"(?<![0-9a-z])(?<!\d\.)" + re.escape(short) + r"(?![0-9a-z])(?!\.\d)"
    return re.search(pattern, long_) is not None


def first_regex_group(text: str | None, patterns: Iterable[re.Pattern[str]]) -> str | None:
    """Group 1 (or the whole match) of the first pattern that matches ``text``."""
    if not text:
        return None
    for pattern in patterns:
        m = pattern.search(text)
        if m:
            value = m.group(1) if m.groups() else m.group(0)
            if value:
                return value.strip()
    return None


# ------------------------------------------------------------------------------------- dates

_MONTHS = {
    "sty": 1,
    "lut": 2,
    "mar": 3,
    "kwi": 4,
    "maj": 5,
    "cze": 6,
    "lip": 7,
    "sie": 8,
    "wrz": 9,
    "paz": 10,
    "lis": 11,
    "gru": 12,
}
_TIME_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)")
_NUMERIC_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?(?!\d)")
_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_NAMED_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s+([a-z]{3,})\.?(?:\s+(\d{4}))?")


def parse_polish_datetime(
    text: str | None, *, now: datetime | None = None, tz: ZoneInfo = WARSAW
) -> datetime | None:
    """Parse a Betclic history date (Warsaw local time) into an aware UTC datetime.

    Accepts ``"22.11.2026 18:30"``, ``"22/11/26, 18:30:05"``, ``"2026-11-22 18:30"``, ISO strings with a
    ``T``, ``"Dzisiaj 18:30"`` / ``"Dziś"`` / ``"Wczoraj"`` / ``"Jutro"``, ``"22 listopada 2026 18:30"``
    and ``"22 lis, 18:30"``. A missing year means the current year (the previous one if that would be
    more than a day in the future). Returns ``None`` when nothing date-like is found.
    """
    if not text or not text.strip():
        return None
    raw = text.strip()
    if "T" in raw and re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?", raw):
        try:
            return parse_datetime(raw)
        except (ValueError, OverflowError):
            return None
    norm = normalise_text(raw)
    local_now = (now or utcnow()).astimezone(tz)

    hour = minute = second = 0
    tm = _TIME_RE.search(norm)
    if tm:
        hour, minute, second = int(tm.group(1)), int(tm.group(2)), int(tm.group(3) or 0)
        norm_wo_time = norm[: tm.start()] + " " + norm[tm.end() :]
    else:
        norm_wo_time = norm

    year: int | None
    if re.search(r"\b(dzisiaj|dzis)\b", norm_wo_time):
        day, month, year = local_now.day, local_now.month, local_now.year
    elif re.search(r"\bwczoraj\b", norm_wo_time):
        d = local_now - timedelta(days=1)
        day, month, year = d.day, d.month, d.year
    elif re.search(r"\bjutro\b", norm_wo_time):
        d = local_now + timedelta(days=1)
        day, month, year = d.day, d.month, d.year
    elif m := _ISO_DATE_RE.search(norm_wo_time):
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    elif m := _NUMERIC_DATE_RE.search(norm_wo_time):
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
        if year is not None and year < 100:
            year += 2000
    elif (m := _NAMED_DATE_RE.search(norm_wo_time)) and m.group(2)[:3] in _MONTHS:
        day, month = int(m.group(1)), _MONTHS[m.group(2)[:3]]
        year = int(m.group(3)) if m.group(3) else None
    elif tm:  # time only -> today
        day, month, year = local_now.day, local_now.month, local_now.year
    else:
        return None

    try:
        if year is None:
            candidate = datetime(local_now.year, month, day, hour, minute, second, tzinfo=tz)
            if candidate - local_now > timedelta(days=1):
                candidate = datetime(local_now.year - 1, month, day, hour, minute, second, tzinfo=tz)
        else:
            candidate = datetime(year, month, day, hour, minute, second, tzinfo=tz)
    except ValueError:
        return None
    return candidate.astimezone(UTC)


# ------------------------------------------------------------------------------- screenshots

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename_part(value: str, max_len: int = 60) -> str:
    """Make ``value`` safe for Windows and POSIX file names (no ``<>:"/\\|?*``, no spaces)."""
    cleaned = _UNSAFE_FILENAME_CHARS.sub("-", value).strip("-.")
    return (cleaned or "x")[:max_len]


def screenshot_filename(
    bet_id: int | str, idempotency_key: str, status: str, at: datetime | None = None, *, max_key_len: int = 60
) -> str:
    """``20261122T183012Z_bet42_<idempotency-key>_<status>.png`` (UTC timestamp first, sortable)."""
    ts = (at or utcnow()).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    key = sanitize_filename_part(idempotency_key, max_key_len)
    return f"{ts}_bet{sanitize_filename_part(str(bet_id), 20)}_{key}_{sanitize_filename_part(status, 30)}.png"
