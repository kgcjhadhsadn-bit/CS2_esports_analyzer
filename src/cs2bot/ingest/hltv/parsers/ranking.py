"""Parser for the HLTV world ranking page (``/ranking/teams[/YYYY/month/D]``)."""

from __future__ import annotations

import logging
from datetime import date

from bs4 import BeautifulSoup, Tag

from cs2bot.ingest.hltv.models import RankingRow
from cs2bot.ingest.hltv.parsers.common import (
    attr,
    clean_text,
    first_int,
    id_from,
    make_soup,
    parse_human_date,
    text_of,
)
from cs2bot.ingest.hltv.selectors import (
    PLAYER_ID_RE,
    RANK_RE,
    RANKING_HEADER_DATE_RE,
    TEAM_ID_RE,
    RankingSel,
)
from cs2bot.ingest.hltv.urls import ranking_date_from_url

logger = logging.getLogger(__name__)


def parse_ranking_page(
    html: str, *, url: str | None = None, ranking_date: date | None = None
) -> list[RankingRow]:
    """Ranked teams in rank order.

    The ranking date comes from the page header ("World ranking on November 16th, 2026"), else from
    the URL (``/ranking/teams/2026/november/16``), else from ``ranking_date``.
    """
    soup = make_soup(html)
    page_date = header_ranking_date(soup) or ranking_date_from_url(url) or ranking_date
    rows: list[RankingRow] = []
    seen_ranks: set[tuple[int, str]] = set()
    for block in soup.select(RankingSel.TEAM_BLOCK):
        try:
            row = _parse_block(block, page_date)
        except Exception as exc:  # defensive: one broken block must not drop the ranking
            logger.debug("skipping malformed HLTV ranking block: %s", exc)
            continue
        if row is None or (row.rank, row.team_name) in seen_ranks:
            continue
        seen_ranks.add((row.rank, row.team_name))
        rows.append(row)
    rows.sort(key=lambda r: r.rank)
    return rows


def header_ranking_date(soup: BeautifulSoup) -> date | None:
    for selector in (RankingSel.HEADER_TEXT, RankingSel.HEADER):
        text = text_of(soup.select_one(selector))
        m = RANKING_HEADER_DATE_RE.search(text)
        if m:
            parsed = parse_human_date(m.group(1))
            if parsed is not None:
                return parsed
    return None


def _parse_block(block: Tag, page_date: date | None) -> RankingRow | None:
    position = text_of(block.select_one(RankingSel.POSITION))
    m = RANK_RE.search(position)
    rank = int(m.group(1)) if m else first_int(position)
    name = text_of(block.select_one(RankingSel.NAME))
    if rank is None or rank <= 0 or not name:
        return None
    link = block.select_one(RankingSel.TEAM_PROFILE_LINK) or block.select_one(RankingSel.TEAM_LINK)
    points_text = text_of(block.select_one(RankingSel.POINTS))
    change_text = clean_text(text_of(block.select_one(RankingSel.CHANGE)))
    is_new = "new" in change_text.lower()
    change: int | None
    if is_new or not change_text:
        change = None
    elif change_text in ("-", "–", "0"):
        change = 0
    else:
        change = first_int(change_text)
    player_ids: list[int] = []
    for player_link in block.select(RankingSel.PLAYER_LINK):
        pid = id_from(attr(player_link, "href"), PLAYER_ID_RE)
        if pid is not None and pid not in player_ids:
            player_ids.append(pid)
    return RankingRow(
        rank=rank,
        team_name=name,
        hltv_team_id=id_from(attr(link, "href"), TEAM_ID_RE),
        points=first_int(points_text),
        ranking_date=page_date,
        change=change,
        is_new=is_new,
        player_hltv_ids=player_ids,
    )
