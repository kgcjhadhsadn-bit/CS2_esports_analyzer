"""Small, defensive BeautifulSoup helpers shared by the HLTV parsers."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

from bs4 import BeautifulSoup, NavigableString, Tag
from dateutil import parser as dateutil_parser

from cs2bot.domain.enums import TeamSlot
from cs2bot.ingest.hltv.models import TeamRef
from cs2bot.ingest.hltv.selectors import (
    FLOAT_RE,
    INT_RE,
    NEWS_RELATIVE_RE,
    ORDINAL_SUFFIX_RE,
    PLACEHOLDER_TEAM_NAMES,
)
from cs2bot.utils.names import normalize_team_name, team_name_similarity
from cs2bot.utils.time import parse_datetime


def make_soup(html: str) -> BeautifulSoup:
    """Parse HTML with lxml (fast, lenient); fall back to the stdlib parser if lxml is unavailable."""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover - lxml is a declared dependency
        return BeautifulSoup(html, "html.parser")


def clean_text(value: str | None) -> str:
    return " ".join(value.split()) if value else ""


def text_of(node: Tag | None, separator: str = " ") -> str:
    if node is None:
        return ""
    return clean_text(node.get_text(separator))


def attr(node: Tag | None, name: str) -> str | None:
    """Attribute as a plain string (multi-valued attributes are space-joined)."""
    if node is None:
        return None
    value = node.get(name)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return " ".join(str(v) for v in value)


def classes(node: Tag | None) -> list[str]:
    if node is None:
        return []
    value = node.get("class")
    if value is None:
        return []
    if isinstance(value, str):
        return value.split()
    return [str(v) for v in value]


def has_class(node: Tag | None, name: str) -> bool:
    return name in classes(node)


def first_int(value: str | None) -> int | None:
    """First integer in a string ("#12" -> 12, "(985 points)" -> 985, "+3" -> 3, "-2" -> -2)."""
    if not value:
        return None
    m = INT_RE.search(value.replace(",", "").replace("−", "-"))
    return int(m.group(0)) if m else None


def parse_int(value: str | None) -> int | None:
    """Strict integer: the whole (cleaned) string must be an integer, else None ("-" -> None)."""
    text = clean_text(value).replace(",", "").replace("−", "-")
    if not text or not re.fullmatch(r"[-+]?\d+", text):
        return None
    return int(text)


def parse_float(value: str | None) -> float | None:
    """First number in a string ("66.7%" -> 66.7, "+2.90%" -> 2.9, "1,05" -> 1.05)."""
    if not value:
        return None
    m = FLOAT_RE.search(value.replace("−", "-"))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def id_from(href: str | None, pattern: re.Pattern[str]) -> int | None:
    if not href:
        return None
    m = pattern.search(href)
    return int(m.group(1)) if m else None


def unix_ms(value: str | None) -> datetime | None:
    """``data-unix`` attribute (milliseconds) -> aware UTC datetime."""
    number = parse_int(value)
    if number is None or number <= 0:
        return None
    try:
        return parse_datetime(number)
    except (OverflowError, OSError, ValueError):
        return None


def bare_strings(node: Tag) -> list[str]:
    """Non-empty text nodes that are direct children of ``node`` (comments excluded)."""
    out = []
    for child in node.children:
        if type(child) is NavigableString:
            text = clean_text(str(child))
            if text:
                out.append(text)
    return out


def strip_ordinals(text: str) -> str:
    return ORDINAL_SUFFIX_RE.sub(r"\1", text)


def parse_human_date(text: str | None) -> date | None:
    """ "November 16th, 2026" / "21st of November 2026" / "2026-11-16" -> date."""
    cleaned = clean_text(strip_ordinals(text or "")).replace(" of ", " ")
    if not cleaned:
        return None
    try:
        iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", cleaned)
        if iso:  # dayfirst=True would read 2026-11-05 as 11 May
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        return dateutil_parser.parse(cleaned, dayfirst=True, fuzzy=True).date()
    except (ValueError, OverflowError):
        return None


def parse_list_date(text: str | None, *, now: datetime) -> datetime | None:
    """Date strings used on news lists: relative ("2 hours ago", "a day ago") or absolute.

    Absolute day-first forms ("21/11/2026") follow HLTV's European convention. Date-only values
    become midnight UTC.
    """
    cleaned = clean_text(text).lower()
    if not cleaned:
        return None
    if cleaned in ("just now", "moments ago", "now"):
        return now
    if cleaned == "yesterday":
        return now - timedelta(days=1)
    m = NEWS_RELATIVE_RE.match(cleaned)
    if m:
        raw_n = m.group("n")
        n = 1 if raw_n in ("a", "an", "one") else int(raw_n)
        unit = m.group("unit")
        seconds = {
            "second": 1,
            "minute": 60,
            "hour": 3600,
            "day": 86400,
            "week": 7 * 86400,
            "month": 30 * 86400,
            "year": 365 * 86400,
        }[unit]
        return now - timedelta(seconds=n * seconds)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", cleaned):
            parsed = dateutil_parser.isoparse(cleaned)
        else:
            parsed = dateutil_parser.parse(strip_ordinals(cleaned), dayfirst=True)
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def team_ref(name: str | None, hltv_id: int | None = None) -> TeamRef | None:
    """TeamRef unless the name is a placeholder ("TBD")."""
    cleaned = clean_text(name)
    if cleaned.lower() in PLACEHOLDER_TEAM_NAMES:
        return None
    return TeamRef(name=cleaned, hltv_id=hltv_id)


def names_match(a: str | None, b: str | None, *, threshold: float = 90.0) -> bool:
    if not a or not b:
        return False
    if clean_text(a).casefold() == clean_text(b).casefold():
        return True
    if normalize_team_name(a) == normalize_team_name(b):
        return True
    return team_name_similarity(a, b) >= threshold


def resolve_slot(name: str | None, team1: TeamRef | None, team2: TeamRef | None) -> TeamSlot | None:
    """Which of the page's two teams a free-text team name refers to (exact > normalised > fuzzy)."""
    if not name:
        return None
    candidates = [(TeamSlot.TEAM1, team1), (TeamSlot.TEAM2, team2)]
    target = clean_text(name)
    for compare in (
        lambda a, b: a == b,
        lambda a, b: a.casefold() == b.casefold(),
        lambda a, b: normalize_team_name(a) == normalize_team_name(b),
    ):
        hits = [
            slot for slot, team in candidates if team is not None and compare(clean_text(team.name), target)
        ]
        if len(hits) == 1:
            return hits[0]
    scored = sorted(
        ((team_name_similarity(target, team.name), slot) for slot, team in candidates if team is not None),
        reverse=True,
    )
    if scored and scored[0][0] >= 90.0 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 5.0):
        return scored[0][1]
    return None
