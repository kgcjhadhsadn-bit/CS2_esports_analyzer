"""Parsers for the ``/results`` and ``/matches`` listing pages."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, time

from bs4 import Tag

from cs2bot.domain.enums import TeamSlot
from cs2bot.ingest.hltv.models import BEST_OF_TO_FORMAT, EventRef, Pagination, ResultRow, UpcomingRow
from cs2bot.ingest.hltv.parsers.common import (
    attr,
    classes,
    clean_text,
    first_int,
    has_class,
    id_from,
    make_soup,
    parse_human_date,
    parse_int,
    team_ref,
    text_of,
    unix_ms,
)
from cs2bot.ingest.hltv.selectors import (
    MATCH_ID_RE,
    RESULTS_FORFEIT_TOKEN,
    RESULTS_FORMAT_RE,
    RESULTS_HEADLINE_PREFIX_RE,
    RESULTS_MAP_SLUGS,
    RESULTS_PAGINATION_RE,
    MatchesSel,
    ResultsSel,
)
from cs2bot.ingest.hltv.urls import HLTV_BASE_URL, absolute_url, match_path, strip_fragment
from cs2bot.utils.names import normalize_map_name

logger = logging.getLogger(__name__)


# ============================================================================================ results


def parse_results_page(html: str, *, base_url: str = HLTV_BASE_URL) -> list[ResultRow]:
    """Rows of one ``/results`` page, newest first.

    The duplicated "featured results" block on page 1 is skipped. Rows that cannot be parsed are
    dropped (logged at debug level) - one malformed row never invalidates the page.
    """
    soup = make_soup(html)
    rows: list[ResultRow] = []
    seen: set[int] = set()
    for con in soup.select(ResultsSel.ROW):
        if con.find_parent(class_=ResultsSel.BIG_RESULTS_CLASS) is not None:
            continue
        try:
            row = _parse_result_row(con, base_url)
        except Exception as exc:  # defensive: never let one row break the listing
            logger.debug("skipping malformed HLTV result row: %s", exc)
            continue
        if row is None or row.hltv_match_id in seen:
            continue
        seen.add(row.hltv_match_id)
        rows.append(row)
    return rows


def parse_results_pagination(html: str, *, base_url: str = HLTV_BASE_URL) -> Pagination | None:
    soup = make_soup(html)
    data = soup.select_one(ResultsSel.PAGINATION_DATA)
    m = RESULTS_PAGINATION_RE.search(text_of(data))
    if not m:
        return None
    next_el = soup.select_one(ResultsSel.PAGINATION_NEXT)
    next_href = attr(next_el, "href") if next_el is not None and not has_class(next_el, "inactive") else None
    return Pagination(
        start=int(m.group(1)),
        end=int(m.group(2)),
        total=int(m.group(3)),
        next_url=absolute_url(next_href, base_url) if next_href else None,
    )


def _decode_map_text(value: str) -> tuple[int | None, str | None, bool]:
    """``.map-text`` -> (best_of, bo1 map name, is_forfeit)."""
    token = value.strip().lower()
    if not token:
        return None, None, False
    if token == RESULTS_FORFEIT_TOKEN:
        return None, None, True
    m = RESULTS_FORMAT_RE.match(token)
    if m:
        return int(m.group(1)), None, False
    return 1, RESULTS_MAP_SLUGS.get(token, normalize_map_name(token)), False


def _sublist_date(con: Tag) -> datetime | None:
    sublist = con.find_parent(class_=ResultsSel.SUBLIST_CLASS)
    if not isinstance(sublist, Tag):
        return None
    headline = sublist.select_one(ResultsSel.SUBLIST_HEADLINE)
    day = parse_human_date(RESULTS_HEADLINE_PREFIX_RE.sub("", text_of(headline)))
    return datetime.combine(day, time(), tzinfo=UTC) if day else None


def _parse_result_row(con: Tag, base_url: str) -> ResultRow | None:
    link = con.select_one(ResultsSel.ROW_LINK) or con.select_one(ResultsSel.ROW_LINK_FALLBACK)
    href = attr(link, "href")
    match_id = id_from(href, MATCH_ID_RE)
    if match_id is None or href is None:
        return None
    t1_node = con.select_one(ResultsSel.TEAM1_NAME)
    t2_node = con.select_one(ResultsSel.TEAM2_NAME)
    team1 = team_ref(text_of(t1_node), parse_int(attr(con, "team1")) or parse_int(attr(con, "data-team1-id")))
    team2 = team_ref(text_of(t2_node), parse_int(attr(con, "team2")) or parse_int(attr(con, "data-team2-id")))
    if team1 is None or team2 is None:
        return None
    scores = [n for n in (parse_int(text_of(s)) for s in con.select(ResultsSel.SCORE_SPANS)) if n is not None]
    s1 = scores[0] if len(scores) >= 2 else None
    s2 = scores[1] if len(scores) >= 2 else None
    winner: TeamSlot | None = None
    if has_class(t1_node, ResultsSel.TEAM_WON_CLASS):
        winner = TeamSlot.TEAM1
    elif has_class(t2_node, ResultsSel.TEAM_WON_CLASS):
        winner = TeamSlot.TEAM2
    elif s1 is not None and s2 is not None and s1 != s2:
        winner = TeamSlot.TEAM1 if s1 > s2 else TeamSlot.TEAM2
    map_text = text_of(con.select_one(ResultsSel.MAP_TEXT))
    best_of, map_name, forfeit = _decode_map_text(map_text)
    event_name = text_of(con.select_one(ResultsSel.EVENT_NAME)) or (
        attr(con.select_one(ResultsSel.EVENT_LOGO), "alt") or ""
    )
    played_at = unix_ms(attr(con, ResultsSel.ROW_UNIX_ATTR)) or _sublist_date(con)
    return ResultRow(
        hltv_match_id=match_id,
        url=strip_fragment(absolute_url(href, base_url)),
        team1=team1,
        team2=team2,
        team1_score=s1,
        team2_score=s2,
        winner=winner,
        event=EventRef(name=clean_text(event_name)) if clean_text(event_name) else None,
        stars=len(con.select(ResultsSel.STARS)),
        played_at=played_at,
        format=BEST_OF_TO_FORMAT.get(best_of) if best_of else None,
        map_name=map_name,
        is_forfeit=forfeit,
        format_hint=map_text.lower() or None,
    )


# ============================================================================================ matches


def parse_matches_page(html: str, *, base_url: str = HLTV_BASE_URL) -> list[UpcomingRow]:
    """Upcoming and live matches of ``/matches`` (2025+ layout, with the older layout as fallback)."""
    soup = make_soup(html)
    rows: dict[int, UpcomingRow] = {}
    for wrapper in soup.select(MatchesSel.WRAPPER):
        _merge_row(rows, _safe_parse(_parse_match_wrapper, wrapper, base_url))
    for legacy in soup.select(MatchesSel.LEGACY):
        _merge_row(rows, _safe_parse(_parse_legacy_match, legacy, base_url))
    return list(rows.values())


def _safe_parse(fn: Callable[[Tag, str], UpcomingRow | None], node: Tag, base_url: str) -> UpcomingRow | None:
    try:
        return fn(node, base_url)
    except Exception as exc:  # defensive: never let one row break the listing
        logger.debug("skipping malformed HLTV match row: %s", exc)
        return None


def _merge_row(rows: dict[int, UpcomingRow], row: UpcomingRow | None) -> None:
    if row is None:
        return
    existing = rows.get(row.hltv_match_id)
    if existing is None:
        rows[row.hltv_match_id] = row
        return
    updates = {
        name: getattr(row, name)
        for name in UpcomingRow.model_fields
        if getattr(existing, name) in (None, [], 0, False) and getattr(row, name) not in (None, [], 0, False)
    }
    if updates:
        rows[row.hltv_match_id] = existing.model_copy(update=updates)


def _bool_attr(value: str | None) -> bool | None:
    if value is None:
        return None
    low = value.strip().lower()
    if low in ("true", "1", "yes"):
        return True
    if low in ("false", "0", "no"):
        return False
    return None


def _format_from_texts(texts: list[str]) -> tuple[int | None, str | None]:
    for raw in texts:
        m = re.search(r"\bbo(\d)\b", raw.lower())
        if m:
            return int(m.group(1)), m.group(0)
    return None, None


def _team_names(node: Tag, block_selector: str, name_selector: str) -> tuple[str | None, str | None]:
    team1 = team2 = None
    blocks = node.select(block_selector)
    for index, block in enumerate(blocks):
        name = text_of(block.select_one(name_selector))
        if not name:
            continue
        cls = classes(block)
        if "team1" in cls:
            team1 = name
        elif "team2" in cls:
            team2 = name
        elif index == 0 and team1 is None:
            team1 = name
        elif team2 is None:
            team2 = name
    if team1 is None or team2 is None:
        names = [text_of(n) for n in node.select(name_selector)]
        if team1 is None and names:
            team1 = names[0]
        if team2 is None and len(names) > 1:
            team2 = names[1]
    return team1, team2


def _parse_match_wrapper(wrapper: Tag, base_url: str) -> UpcomingRow | None:
    link = wrapper.select_one(MatchesSel.LINK)
    href = attr(link, "href")
    match_id = parse_int(attr(wrapper, MatchesSel.WRAPPER_ID_ATTR)) or id_from(href, MATCH_ID_RE)
    if match_id is None:
        return None
    name1, name2 = _team_names(wrapper, MatchesSel.TEAM_BLOCK, MatchesSel.TEAM_NAME)
    team1 = team_ref(name1, parse_int(attr(wrapper, "team1")) or parse_int(attr(wrapper, "data-team1-id")))
    team2 = team_ref(name2, parse_int(attr(wrapper, "team2")) or parse_int(attr(wrapper, "data-team2-id")))
    meta_texts = [text_of(m) for m in wrapper.select(MatchesSel.META)]
    best_of, fmt_hint = _format_from_texts(meta_texts)
    is_live = (
        _bool_attr(attr(wrapper, "live")) is True
        or has_class(wrapper, MatchesSel.LIVE_CLASS)
        or wrapper.select_one(MatchesSel.META_LIVE) is not None
    )
    event_name = (
        attr(wrapper.select_one(MatchesSel.EVENT_HEADLINE), "data-event-headline")
        or text_of(wrapper.select_one(MatchesSel.EVENT_NAME))
        or attr(wrapper.select_one(MatchesSel.EVENT_LOGO), "alt")
        or ""
    )
    event_id = parse_int(attr(wrapper, MatchesSel.WRAPPER_EVENT_ID_ATTR))
    maps_attr = attr(wrapper, MatchesSel.MAPS_ATTR) or attr(
        wrapper.select_one(f"[{MatchesSel.MAPS_ATTR}]"), "data-maps"
    )
    title = text_of(wrapper.select_one(MatchesSel.NO_INFO)) or None
    return UpcomingRow(
        hltv_match_id=match_id,
        url=strip_fragment(absolute_url(href or match_path(match_id), base_url)),
        team1=team1,
        team2=team2,
        scheduled_at=unix_ms(attr(wrapper.select_one(MatchesSel.TIME), "data-unix")),
        format=BEST_OF_TO_FORMAT.get(best_of) if best_of else None,
        format_hint=fmt_hint,
        event=EventRef(name=clean_text(event_name), hltv_id=event_id)
        if clean_text(event_name) or event_id
        else None,
        stars=parse_int(attr(wrapper, MatchesSel.WRAPPER_STARS_ATTR)) or 0,
        is_live=is_live,
        is_lan=_bool_attr(attr(wrapper, "lan")),
        title=title if not (team1 and team2) else None,
        map_names=[normalize_map_name(m) for m in (maps_attr or "").split(",") if m.strip()],
    )


def _parse_legacy_match(node: Tag, base_url: str) -> UpcomingRow | None:
    link = node.select_one(MatchesSel.LINK)
    href = attr(link, "href")
    match_id = id_from(href, MATCH_ID_RE) or parse_int(attr(node, "data-scorebot-id"))
    if match_id is None:
        return None
    name1, name2 = _team_names(node, MatchesSel.LEGACY_TEAM_BLOCK, MatchesSel.LEGACY_TEAM_NAME)
    team1 = team_ref(name1, parse_int(attr(node, "team1")) or parse_int(attr(node, "data-team1-id")))
    team2 = team_ref(name2, parse_int(attr(node, "team2")) or parse_int(attr(node, "data-team2-id")))
    best_of, fmt_hint = _format_from_texts([text_of(m) for m in node.select(MatchesSel.LEGACY_META)])
    star_icons = node.select(MatchesSel.LEGACY_RATING_STARS)
    stars = (
        sum(1 for icon in star_icons if not has_class(icon, "faded"))
        if star_icons
        else (first_int(attr(node, "stars")) or 0)
    )
    time_node = node.select_one(MatchesSel.LEGACY_TIME)
    scheduled = unix_ms(attr(time_node, "data-unix")) or unix_ms(attr(node, "data-zonedgrouping-entry-unix"))
    event_name = text_of(node.select_one(MatchesSel.LEGACY_EVENT_NAME)) or (
        attr(node.select_one(MatchesSel.LEGACY_EVENT_LOGO), "title") or ""
    )
    is_live = (
        has_class(node, "liveMatch-container")
        or node.select_one(MatchesSel.LEGACY_LIVE) is not None
        or text_of(node.select_one(".matchTime")).upper() == "LIVE"
    )
    title = text_of(node.select_one(MatchesSel.LEGACY_EMPTY)) or None
    return UpcomingRow(
        hltv_match_id=match_id,
        url=strip_fragment(absolute_url(href or match_path(match_id), base_url)),
        team1=team1,
        team2=team2,
        scheduled_at=scheduled,
        format=BEST_OF_TO_FORMAT.get(best_of) if best_of else None,
        format_hint=fmt_hint,
        event=EventRef(name=clean_text(event_name)) if clean_text(event_name) else None,
        stars=stars,
        is_live=is_live,
        is_lan=_bool_attr(attr(node, "lan")),
        title=title if not (team1 and team2) else None,
        map_names=[normalize_map_name(m) for m in (attr(node, "data-maps") or "").split(",") if m.strip()],
    )
