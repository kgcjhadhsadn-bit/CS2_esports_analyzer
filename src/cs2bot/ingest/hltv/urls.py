"""URL builders and URL classification for HLTV pages."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import date, timedelta
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from cs2bot.ingest.hltv.selectors import (
    ECONOMY_ROUTE_RE,
    MAPSTATS_ROUTE_RE,
    MATCH_ROUTE_RE,
    NEWS_ARCHIVE_PATH_RE,
    NEWS_ROUTE_RE,
    PERFORMANCE_ROUTE_RE,
    RANKING_DATE_PATH_RE,
)

HLTV_BASE_URL = "https://www.hltv.org"
MONTHS: tuple[str, ...] = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
RESULTS_PAGE_SIZE = 100


def match_path(hltv_match_id: int, slug: str = "-") -> str:
    """HLTV ignores the slug after the id, so ``-`` works when the real slug is unknown."""
    return f"/matches/{hltv_match_id}/{slug or '-'}"


def mapstats_path(mapstats_id: int, slug: str = "-") -> str:
    return f"/stats/matches/mapstatsid/{mapstats_id}/{slug or '-'}"


def matches_path() -> str:
    return "/matches"


def results_path(offset: int = 0, query: Mapping[str, str | int] | None = None) -> str:
    params: list[tuple[str, str]] = [(k, str(v)) for k, v in sorted((query or {}).items()) if k != "offset"]
    if offset:
        params.append(("offset", str(offset)))
    return "/results" + (f"?{urlencode(params)}" if params else "")


def ranking_monday(day: date) -> date:
    """HLTV publishes its ranking on Mondays; any date maps to that week's Monday."""
    return day - timedelta(days=day.weekday())


def ranking_path(ranking_date: date | None = None) -> str:
    if ranking_date is None:
        return "/ranking/teams"
    monday = ranking_monday(ranking_date)
    return f"/ranking/teams/{monday.year}/{MONTHS[monday.month - 1]}/{monday.day}"


def news_archive_path(year: int, month: int) -> str:
    return f"/news/archive/{year}/{MONTHS[month - 1]}"


def absolute_url(href: str, base_url: str = HLTV_BASE_URL) -> str:
    """Join a (possibly relative) href with the HLTV base URL."""
    return urljoin(base_url.rstrip("/") + "/", href.strip())


def strip_fragment(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def canonical_url(url: str) -> str:
    """Stable form of a URL for cache keys: slugs after ids dropped, query sorted, no fragment."""
    parts = urlsplit(url)
    path = parts.path or "/"
    for route, template in (
        (MATCH_ROUTE_RE, "/matches/{id}"),
        (MAPSTATS_ROUTE_RE, "/stats/matches/mapstatsid/{id}"),
        (PERFORMANCE_ROUTE_RE, "/stats/matches/performance/mapstatsid/{id}"),
        (ECONOMY_ROUTE_RE, "/stats/matches/economy/mapstatsid/{id}"),
        (NEWS_ROUTE_RE, "/news/{id}"),
    ):
        m = route.match(path)
        if m:
            path = template.format(id=m.group(1))
            break
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def cache_key(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


class UrlKind(StrEnum):
    MATCH = "match"
    MAPSTATS = "mapstats"
    RESULTS = "results"
    MATCHES = "matches"
    RANKING = "ranking"
    NEWS_ARTICLE = "news_article"
    NEWS_LIST = "news_list"
    OTHER = "other"


def classify_url(url: str) -> UrlKind:
    path = urlsplit(url).path or "/"
    if MATCH_ROUTE_RE.match(path):
        return UrlKind.MATCH
    if MAPSTATS_ROUTE_RE.match(path) or PERFORMANCE_ROUTE_RE.match(path) or ECONOMY_ROUTE_RE.match(path):
        return UrlKind.MAPSTATS
    if path.rstrip("/") == "/results":
        return UrlKind.RESULTS
    if path.rstrip("/") == "/matches":
        return UrlKind.MATCHES
    if path.startswith("/ranking/teams"):
        return UrlKind.RANKING
    if NEWS_ROUTE_RE.match(path):
        return UrlKind.NEWS_ARTICLE
    if path.startswith("/news/archive") or path in ("/", ""):
        return UrlKind.NEWS_LIST
    return UrlKind.OTHER


def is_immutable_url(url: str, *, today: date) -> bool:
    """Pages whose content no longer changes once published (cached with the long TTL).

    Map stats pages exist only for finished maps; news articles and past rankings / past news
    archive months do not change. Match pages are handled separately (finished only when the page
    itself says so).
    """
    kind = classify_url(url)
    if kind in (UrlKind.MAPSTATS, UrlKind.NEWS_ARTICLE):
        return True
    path = urlsplit(url).path
    if kind is UrlKind.RANKING:
        # A dated ranking is final once published; a week of margin covers publication delays.
        ranked_on = ranking_date_from_url(path)
        return ranked_on is not None and ranked_on < today - timedelta(days=7)
    archive = NEWS_ARCHIVE_PATH_RE.match(path)
    if archive:
        month = _month_number(archive.group(2))
        if month is None:
            return False
        return (int(archive.group(1)), month) < (today.year, today.month)
    return False


def _month_number(name: str) -> int | None:
    try:
        return MONTHS.index(name.lower()) + 1
    except ValueError:
        return None


def _parse_path_date(year: str, month: str, day: str) -> date | None:
    month_no = _month_number(month)
    if month_no is None:
        return None
    try:
        return date(int(year), month_no, int(day))
    except ValueError:
        return None


def ranking_date_from_url(url: str | None) -> date | None:
    """``/ranking/teams/2026/november/16[/9565]`` -> 2026-11-16."""
    if not url:
        return None
    m = RANKING_DATE_PATH_RE.search(urlsplit(url).path if "://" in url else url)
    if not m:
        return None
    return _parse_path_date(m.group(1), m.group(2), m.group(3))
