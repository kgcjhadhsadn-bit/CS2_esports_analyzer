"""Parser for HLTV map stats pages (``/stats/matches/mapstatsid/{id}/{slug}``).

Page facts relied on (see :mod:`cs2bot.ingest.hltv.selectors` for provenance):

* ``.match-info-box``: event link (``?event=ID``), ``[data-unix]`` date, the map name as a *bare text
  node*, ``.team-left`` / ``.team-right`` (``/stats/teams/{id}`` links, ``.bold`` round totals);
* the first ``.match-info-row`` is the breakdown ``13 : 11 (ct 7 : t 5) (t 6 : ct 6) [( 4 : 2 )]`` -
  the side classes of the first group give the starting sides; a trailing plain group is overtime;
* scoreboards: ``.stats-table.totalstats`` (+ hidden ``.ctstats`` / ``.tstats``) per team, left team first;
  every traditional column has a hidden ``eco-adjusted-data`` twin that must be ignored;
* round history: one ``.round-history-con`` (regulation, or everything for a single overtime) plus an
  optional second one for longer overtimes; two ``.round-history-team-row`` per container (left team
  first); the winner's row shows an outcome icon (``ct_win``/``t_win``/``bomb_exploded``/
  ``bomb_defused``/``stopwatch``), the loser's row ``emptyHistory``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import NamedTuple

from bs4 import BeautifulSoup, Tag

from cs2bot.domain.enums import RoundEndReason, Side, TeamSlot
from cs2bot.ingest.hltv.errors import HLTVParseError
from cs2bot.ingest.hltv.models import (
    EventRef,
    MapStatsPage,
    PlayerStatLine,
    RoundOutcome,
    TeamMapStats,
    TeamRef,
)
from cs2bot.ingest.hltv.parsers.common import (
    attr,
    bare_strings,
    classes,
    clean_text,
    first_int,
    has_class,
    id_from,
    make_soup,
    names_match,
    parse_float,
    parse_int,
    text_of,
    unix_ms,
)
from cs2bot.ingest.hltv.selectors import (
    COL_ADR,
    COL_ASSISTS,
    COL_CLUTCHES,
    COL_DEATHS,
    COL_FK_DIFF,
    COL_KAST,
    COL_KAST_OLD,
    COL_KD_DIFF,
    COL_KILLS,
    COL_MULTIKILLS,
    COL_OPENING_KD,
    COL_RATING,
    COL_SWING,
    EMPTY_OUTCOME_ICON,
    EVENT_QUERY_RE,
    HALF_GROUP_RE,
    KNOWN_MAP_NAMES,
    MAPSTATS_ID_RE,
    MATCH_ID_RE,
    NUM_WITH_PAREN_RE,
    OUTCOME_ICONS,
    PLAYER_ID_RE,
    RATING_VERSION_RE,
    REGULATION_ROUNDS,
    SCORE_PAIR_RE,
    STATS_PLAYER_ID_RE,
    STATS_TEAM_ID_RE,
    TEAM_ID_RE,
    MapStatsSel,
)
from cs2bot.ingest.hltv.urls import strip_fragment
from cs2bot.utils.names import normalize_map_name

logger = logging.getLogger(__name__)


class _Breakdown(NamedTuple):
    halves: list[tuple[int, int]]  # regulation halves (left, right)
    left_sides: list[Side | None]  # left team's side per regulation half
    overtime: tuple[int, int] | None  # rounds won in overtime (left, right), when shown


def parse_mapstats_page(html: str, url: str) -> MapStatsPage:
    """Parse one map stats page. Raises :class:`HLTVParseError` if it is not a map stats page."""
    soup = make_soup(html)
    mapstats_id = (
        id_from(url, MAPSTATS_ID_RE)
        or id_from(attr(soup.select_one(MapStatsSel.SELECTED_MENU_ITEM), "href"), MAPSTATS_ID_RE)
        or id_from(attr(soup.select_one("link[rel='canonical']"), "href"), MAPSTATS_ID_RE)
    )
    if mapstats_id is None:
        raise HLTVParseError(f"no mapstats id in {url!r}")
    box = soup.select_one(MapStatsSel.INFO_BOX)
    if box is None:
        raise HLTVParseError(f"{url!r} does not look like an HLTV map stats page")
    warnings: list[str] = []

    left = _parse_side_team(box.select_one(MapStatsSel.TEAM_LEFT) or soup.select_one(MapStatsSel.TEAM_LEFT))
    right = _parse_side_team(
        box.select_one(MapStatsSel.TEAM_RIGHT) or soup.select_one(MapStatsSel.TEAM_RIGHT)
    )
    if left is None or right is None:
        raise HLTVParseError(f"{url!r}: team names missing on map stats page")
    raw_map = _map_name(box)
    event_link = box.select_one(MapStatsSel.EVENT_LINK)
    event = (
        EventRef(name=text_of(event_link), hltv_id=id_from(attr(event_link, "href"), EVENT_QUERY_RE))
        if event_link is not None and text_of(event_link)
        else None
    )
    info = _section(warnings, "info rows", lambda: _parse_info_rows(soup), _InfoRows())
    breakdown = info.breakdown
    rating_version = info.rating_version or _rating_version(soup)

    tables = _section(warnings, "scoreboard", lambda: _parse_tables(soup, left[0], right[0]), _empty_tables())
    no_rounds: tuple[list[RoundOutcome], list[str]] = ([], [])
    rounds, round_warnings = _section(
        warnings, "round history", lambda: _parse_round_history(soup, left[0], right[0]), no_rounds
    )
    warnings.extend(round_warnings)

    left_total, right_total = left[1], right[1]
    rounds_complete = (
        bool(rounds)
        and left_total is not None
        and right_total is not None
        and sum(1 for r in rounds if r.winner is TeamSlot.TEAM1) == left_total
        and sum(1 for r in rounds if r.winner is TeamSlot.TEAM2) == right_total
    )
    if rounds and not rounds_complete:
        warnings.append("round history does not add up to the final score")

    team1 = TeamMapStats(
        team=left[0],
        rounds=left_total,
        won=left[2],
        team_rating=info.rating[0],
        first_kills=_as_int(info.first_kills[0]),
        clutches_won=_as_int(info.clutches[0]),
        players=tables.totals[0],
        ct_players=tables.ct[0],
        t_players=tables.t[0],
    )
    team2 = TeamMapStats(
        team=right[0],
        rounds=right_total,
        won=right[2],
        team_rating=info.rating[1],
        first_kills=_as_int(info.first_kills[1]),
        clutches_won=_as_int(info.clutches[1]),
        players=tables.totals[1],
        ct_players=tables.ct[1],
        t_players=tables.t[1],
    )
    split_complete = _apply_side_split(team1, team2, breakdown, rounds, rounds_complete, warnings)
    return MapStatsPage(
        mapstats_id=mapstats_id,
        url=strip_fragment(url),
        hltv_match_id=id_from(attr(soup.select_one(MapStatsSel.MATCH_PAGE_LINK), "href"), MATCH_ID_RE),
        map_name=normalize_map_name(raw_map) if raw_map else "unknown",
        raw_map_name=raw_map,
        played_at=unix_ms(attr(box.select_one(MapStatsSel.DATE), "data-unix")),
        event=event,
        team1=team1,
        team2=team2,
        rating_version=rating_version,
        rounds=rounds,
        rounds_complete=rounds_complete,
        side_split_complete=split_complete,
        warnings=warnings,
    )


def _section[T](warnings: list[str], name: str, fn: Callable[[], T], default: T) -> T:
    try:
        return fn()
    except Exception as exc:
        logger.debug("HLTV map stats page: %s section failed: %s", name, exc, exc_info=True)
        warnings.append(f"{name} section could not be parsed: {type(exc).__name__}: {exc}")
        return default


# ---------------------------------------------------------------------------------------- header


def _parse_side_team(node: Tag | None) -> tuple[TeamRef, int | None, bool | None] | None:
    if node is None:
        return None
    link = node.select_one(MapStatsSel.TEAM_LINK)
    href = attr(link, "href")
    name = text_of(link) or attr(node.select_one(MapStatsSel.TEAM_LOGO), "title") or ""
    if not clean_text(name):
        return None
    team_id = id_from(href, STATS_TEAM_ID_RE) or id_from(href, TEAM_ID_RE)
    score_node = node.select_one(MapStatsSel.TEAM_SCORE)
    won = True if has_class(score_node, "won") else False if has_class(score_node, "lost") else None
    return TeamRef(name=clean_text(name), hltv_id=team_id), parse_int(text_of(score_node)), won


def _map_name(box: Tag) -> str | None:
    """The map name is a bare text node inside ``.match-info-box``."""
    candidates = bare_strings(box)
    for text in candidates:
        if normalize_map_name(text) in KNOWN_MAP_NAMES:
            return text
    # fallback: a known map name anywhere in the box text
    words = text_of(box).split()
    for word in words:
        if normalize_map_name(word) in KNOWN_MAP_NAMES:
            return word
    return candidates[0] if candidates else None


def _rating_version(soup: BeautifulSoup) -> str | None:
    header = soup.select_one(MapStatsSel.TABLE_RATING_HEADER)
    desc = soup.select_one(MapStatsSel.RATING_DESC)
    for text in (text_of(desc), text_of(header)):
        m = RATING_VERSION_RE.search(text)
        if m:
            return m.group(1)
    return None


class _InfoRows(NamedTuple):
    breakdown: _Breakdown | None = None
    rating: tuple[float | None, float | None] = (None, None)
    rating_version: str | None = None
    first_kills: tuple[float | None, float | None] = (None, None)
    clutches: tuple[float | None, float | None] = (None, None)


def _parse_info_rows(soup: BeautifulSoup) -> _InfoRows:
    info = _InfoRows()
    for index, row in enumerate(soup.select(MapStatsSel.INFO_ROW)):
        label = text_of(row.select_one(MapStatsSel.INFO_ROW_LABEL)).lower()
        value = row.select_one(MapStatsSel.INFO_ROW_VALUE)
        if value is None:
            continue
        if info.breakdown is None and (
            value.select(MapStatsSel.SIDE_SPANS) or (index == 0 and "breakdown" in label)
        ):
            info = info._replace(breakdown=_parse_breakdown(value))
        elif "rating" in label:
            m = RATING_VERSION_RE.search(label)
            info = info._replace(rating=_float_pair(text_of(value)), rating_version=m.group(1) if m else None)
        elif "first kills" in label:
            info = info._replace(first_kills=_float_pair(text_of(value)))
        elif "clutches" in label:
            info = info._replace(clutches=_float_pair(text_of(value)))
    return info


def _parse_breakdown(value: Tag) -> _Breakdown:
    groups = [(int(a), int(b)) for a, b in HALF_GROUP_RE.findall(text_of(value))]
    spans = value.select(MapStatsSel.SIDE_SPANS)
    sides: list[Side | None] = []
    for i in range(0, len(spans) - 1, 2):
        cls = classes(spans[i])
        sides.append(Side.CT if "ct-color" in cls else Side.T if "t-color" in cls else None)
    regulation = groups[:2]
    overtime = groups[2] if len(groups) > 2 else None
    if len(groups) > 3:  # several plain OT groups: sum them
        overtime = (sum(g[0] for g in groups[2:]), sum(g[1] for g in groups[2:]))
    if len(sides) == 1 and sides[0] is not None:
        sides.append(sides[0].other)
    return _Breakdown(halves=regulation, left_sides=sides[:2], overtime=overtime)


def _float_pair(text: str) -> tuple[float | None, float | None]:
    parts = [p.strip() for p in text.split(":")]
    if len(parts) != 2:
        return None, None
    return parse_float(parts[0]), parse_float(parts[1])


def _as_int(value: float | None) -> int | None:
    return int(value) if value is not None else None


# ---------------------------------------------------------------------------------------- scoreboard


class _Tables(NamedTuple):
    totals: tuple[list[PlayerStatLine], list[PlayerStatLine]]
    ct: tuple[list[PlayerStatLine], list[PlayerStatLine]]
    t: tuple[list[PlayerStatLine], list[PlayerStatLine]]


def _empty_tables() -> _Tables:
    return _Tables(([], []), ([], []), ([], []))


def _assign_tables(tables: list[Tag], left: TeamRef, right: TeamRef) -> tuple[Tag | None, Tag | None]:
    """Which of (up to) two tables belongs to the left / right team (header team name, else order)."""
    if not tables:
        return None, None
    first = tables[0]
    second = tables[1] if len(tables) > 1 else None
    first_name = text_of(first.select_one(MapStatsSel.TABLE_TEAM_NAME))
    if first_name and names_match(first_name, right.name) and not names_match(first_name, left.name):
        return second, first
    return first, second


def _parse_tables(soup: BeautifulSoup, left: TeamRef, right: TeamRef) -> _Tables:
    tables = soup.select(MapStatsSel.STATS_TABLE)
    totals = [t for t in tables if has_class(t, MapStatsSel.TOTAL_CLASS)]
    ct = [t for t in tables if has_class(t, MapStatsSel.CT_CLASS)]
    tt = [t for t in tables if has_class(t, MapStatsSel.T_CLASS)]
    if not totals:  # very old layout without the class: the first two tables are the totals
        totals = [
            t
            for t in tables
            if not has_class(t, MapStatsSel.CT_CLASS) and not has_class(t, MapStatsSel.T_CLASS)
        ][:2]

    def rows(table: Tag | None) -> list[PlayerStatLine]:
        if table is None:
            return []
        out = []
        for tr in table.select(MapStatsSel.ROW) or table.select("tr"):
            line = _parse_stat_row(tr)
            if line is not None:
                out.append(line)
        return out

    lt, rt = _assign_tables(totals, left, right)
    lct, rct = _assign_tables(ct, left, right)
    ltt, rtt = _assign_tables(tt, left, right)
    return _Tables((rows(lt), rows(rt)), (rows(lct), rows(rct)), (rows(ltt), rows(rtt)))


def _cell(tr: Tag, column: str) -> Tag | None:
    """The traditional (non eco-adjusted) cell of a column; desktop variant preferred."""
    candidates = [
        td for td in tr.find_all("td") if column in classes(td) and MapStatsSel.ECO_CLASS not in classes(td)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda td: (
            MapStatsSel.TRADITIONAL_CLASS not in classes(td),
            MapStatsSel.SMARTPHONE_ONLY_CLASS in classes(td),
        )
    )
    return candidates[0]


def _num_paren(cell: Tag | None) -> tuple[int | None, int | None]:
    """ "14 (9)" -> (14, 9); "15" -> (15, None)."""
    if cell is None:
        return None, None
    m = NUM_WITH_PAREN_RE.match(clean_text(cell.get_text("")))
    if not m:
        return None, None
    return int(m.group(1)), (int(m.group(2)) if m.group(2) is not None else None)


def _cell_float(cell: Tag | None) -> float | None:
    if cell is None:
        return None
    for desc in cell.select(MapStatsSel.RATING_DESC):
        desc.extract()
    return parse_float(text_of(cell))


def _parse_stat_row(tr: Tag) -> PlayerStatLine | None:
    player_cell = tr.select_one(MapStatsSel.PLAYER_CELL)
    if player_cell is None:
        return None
    link = player_cell.select_one(MapStatsSel.PLAYER_LINK)
    nickname = text_of(link) if link is not None else ""
    if not nickname:
        return None
    href = attr(link, "href")
    kills, headshots = _num_paren(_cell(tr, COL_KILLS))
    assists, flash = _num_paren(_cell(tr, COL_ASSISTS))
    deaths, traded = _num_paren(_cell(tr, COL_DEATHS))
    opening = SCORE_PAIR_RE.search(text_of(_cell(tr, COL_OPENING_KD)))
    kast_cell = _cell(tr, COL_KAST) or _cell(tr, COL_KAST_OLD)
    kd_diff = first_int(text_of(_cell(tr, COL_KD_DIFF)))
    if kd_diff is None and kills is not None and deaths is not None:
        kd_diff = kills - deaths
    first_kills = int(opening.group(1)) if opening else None
    first_deaths = int(opening.group(2)) if opening else None
    fk_diff = first_int(text_of(_cell(tr, COL_FK_DIFF)))
    if fk_diff is None and first_kills is not None and first_deaths is not None:
        fk_diff = first_kills - first_deaths
    return PlayerStatLine(
        nickname=nickname,
        hltv_id=id_from(href, STATS_PLAYER_ID_RE) or id_from(href, PLAYER_ID_RE),
        country=attr(player_cell.select_one(MapStatsSel.PLAYER_FLAG), "title"),
        kills=kills,
        headshots=headshots,
        assists=assists,
        flash_assists=flash,
        deaths=deaths,
        traded_deaths=traded,
        kast=parse_float(text_of(kast_cell)) if kast_cell is not None else None,
        adr=parse_float(text_of(_cell(tr, COL_ADR))),
        rating=_cell_float(_cell(tr, COL_RATING)),
        first_kills=first_kills,
        first_deaths=first_deaths,
        kd_diff=kd_diff,
        fk_diff=fk_diff,
        multi_kill_rounds=parse_int(text_of(_cell(tr, COL_MULTIKILLS))),
        clutches_won=parse_int(text_of(_cell(tr, COL_CLUTCHES))),
        round_swing=parse_float(text_of(_cell(tr, COL_SWING))),
    )


# ---------------------------------------------------------------------------------------- rounds


def _icon(img: Tag) -> tuple[str, str]:
    src = attr(img, "src") or ""
    name = src.rsplit("/", 1)[-1].split("?", 1)[0].rsplit(".", 1)[0].lower()
    return name, attr(img, "title") or ""


def _parse_round_history(
    soup: BeautifulSoup, left: TeamRef, right: TeamRef
) -> tuple[list[RoundOutcome], list[str]]:
    warnings: list[str] = []
    left_icons: list[tuple[str, str]] = []
    right_icons: list[tuple[str, str]] = []
    for con in soup.select(MapStatsSel.ROUND_HISTORY_CON):
        rows = con.select(MapStatsSel.ROUND_HISTORY_ROW)
        if len(rows) < 2:
            warnings.append("round history container without two team rows")
            continue
        row_a, row_b = rows[0], rows[1]
        title_a = attr(row_a.select_one(MapStatsSel.ROUND_HISTORY_TEAM), "title")
        title_b = attr(row_b.select_one(MapStatsSel.ROUND_HISTORY_TEAM), "title")
        if (
            title_a
            and title_b
            and names_match(title_a, right.name)
            and names_match(title_b, left.name)
            and not names_match(title_a, left.name)
        ):
            row_a, row_b = row_b, row_a
        icons_a = [_icon(img) for img in row_a.select(MapStatsSel.ROUND_OUTCOME)]
        icons_b = [_icon(img) for img in row_b.select(MapStatsSel.ROUND_OUTCOME)]
        length = max(len(icons_a), len(icons_b))
        empty = (EMPTY_OUTCOME_ICON, "")
        icons_a += [empty] * (length - len(icons_a))
        icons_b += [empty] * (length - len(icons_b))
        left_icons.extend(icons_a)
        right_icons.extend(icons_b)

    rounds: list[RoundOutcome] = []
    for (icon_l, title_l), (icon_r, title_r) in zip(left_icons, right_icons, strict=True):
        left_won = icon_l != EMPTY_OUTCOME_ICON
        right_won = icon_r != EMPTY_OUTCOME_ICON
        if not left_won and not right_won:
            continue  # unplayed slot
        if left_won and right_won:
            warnings.append(f"round slot {len(rounds) + 1}: both teams marked as winner")
            continue
        winner = TeamSlot.TEAM1 if left_won else TeamSlot.TEAM2
        icon, title = (icon_l, title_l) if left_won else (icon_r, title_r)
        side_reason = OUTCOME_ICONS.get(icon)
        if side_reason is None:
            warnings.append(f"unknown round outcome icon {icon!r}")
        winner_side = side_reason[0] if side_reason else None
        team1_side = (
            None if winner_side is None else (winner_side if winner is TeamSlot.TEAM1 else winner_side.other)
        )
        score = SCORE_PAIR_RE.search(title)
        number = len(rounds) + 1
        rounds.append(
            RoundOutcome(
                round_number=number,
                winner=winner,
                winner_side=winner_side,
                team1_side=team1_side,
                end_reason=side_reason[1] if side_reason else RoundEndReason.OTHER,
                outcome=icon,
                team1_score=int(score.group(1)) if score else None,
                team2_score=int(score.group(2)) if score else None,
                is_overtime=number > REGULATION_ROUNDS,
            )
        )
    return rounds, warnings


# ---------------------------------------------------------------------------------------- sides


def _apply_side_split(
    team1: TeamMapStats,
    team2: TeamMapStats,
    breakdown: _Breakdown | None,
    rounds: list[RoundOutcome],
    rounds_complete: bool,
    warnings: list[str],
) -> bool:
    """Fill start side + CT/T round counts. Returns True when the counts cover every round of the map.

    The round history (explicit winning side per round) is preferred; the breakdown row only covers
    regulation, so it is used alone when there was no overtime.
    """
    start_from_breakdown = breakdown.left_sides[0] if breakdown and breakdown.left_sides else None
    if rounds_complete and all(r.team1_side is not None for r in rounds):
        start = rounds[0].team1_side
        if start_from_breakdown is not None and start is not None and start_from_breakdown is not start:
            warnings.append("starting side differs between breakdown and round history; using round history")
        counts = {(slot, side): 0 for slot in TeamSlot for side in Side}
        for r in rounds:
            assert r.winner_side is not None
            counts[(r.winner, r.winner_side)] += 1
        team1.start_side = start
        team2.start_side = start.other if start else None
        team1.ct_rounds, team1.t_rounds = counts[(TeamSlot.TEAM1, Side.CT)], counts[(TeamSlot.TEAM1, Side.T)]
        team2.ct_rounds, team2.t_rounds = counts[(TeamSlot.TEAM2, Side.CT)], counts[(TeamSlot.TEAM2, Side.T)]
        team1.overtime_rounds = sum(1 for r in rounds if r.is_overtime and r.winner is TeamSlot.TEAM1)
        team2.overtime_rounds = sum(1 for r in rounds if r.is_overtime and r.winner is TeamSlot.TEAM2)
        return True
    if breakdown is None or len(breakdown.halves) < 2 or len(breakdown.left_sides) < 2:
        return False
    s1, s2 = breakdown.left_sides[0], breakdown.left_sides[1]
    if s1 is None or s2 is None:
        return False
    (l1, r1), (l2, r2) = breakdown.halves[0], breakdown.halves[1]
    team1.start_side, team2.start_side = s1, s1.other
    team1.ct_rounds = (l1 if s1 is Side.CT else 0) + (l2 if s2 is Side.CT else 0)
    team1.t_rounds = (l1 if s1 is Side.T else 0) + (l2 if s2 is Side.T else 0)
    team2.ct_rounds = (r1 if s1 is Side.T else 0) + (r2 if s2 is Side.T else 0)
    team2.t_rounds = (r1 if s1 is Side.CT else 0) + (r2 if s2 is Side.CT else 0)
    regulation_total = l1 + r1 + l2 + r2
    if breakdown.overtime is not None:
        team1.overtime_rounds, team2.overtime_rounds = breakdown.overtime
    elif team1.rounds is not None and team2.rounds is not None:
        team1.overtime_rounds = max(0, team1.rounds - (l1 + l2))
        team2.overtime_rounds = max(0, team2.rounds - (r1 + r2))
    return regulation_total == (team1.rounds or 0) + (team2.rounds or 0)
