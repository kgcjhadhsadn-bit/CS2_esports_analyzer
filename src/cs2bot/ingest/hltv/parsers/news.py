"""Parsers for HLTV news lists and news articles."""

from __future__ import annotations

import logging
from datetime import datetime

from bs4 import Tag

from cs2bot.ingest.hltv.errors import HLTVParseError
from cs2bot.ingest.hltv.models import NewsArticle, NewsRow
from cs2bot.ingest.hltv.parsers.common import (
    attr,
    clean_text,
    id_from,
    make_soup,
    parse_list_date,
    text_of,
    unix_ms,
)
from cs2bot.ingest.hltv.selectors import NEWS_COMMENTS_RE, NEWS_ID_RE, PLAYER_ID_RE, TEAM_ID_RE, NewsSel
from cs2bot.ingest.hltv.urls import HLTV_BASE_URL, absolute_url, strip_fragment
from cs2bot.utils.time import parse_datetime, utcnow

logger = logging.getLogger(__name__)


def parse_news_list(
    html: str, *, base_url: str = HLTV_BASE_URL, now: datetime | None = None
) -> list[NewsRow]:
    """News entries of a list page (front page, ``/news/archive/YYYY/month``), in page order.

    ``published_at`` accepts relative ("2 hours ago", relative to ``now``) and absolute dates.
    """
    soup = make_soup(html)
    reference = now or utcnow()
    rows: list[NewsRow] = []
    seen: set[str] = set()
    for item in soup.select(NewsSel.LIST_ITEM):
        try:
            row = _parse_list_item(item, base_url, reference)
        except Exception as exc:  # defensive
            logger.debug("skipping malformed HLTV news row: %s", exc)
            continue
        if row is None or row.url in seen:
            continue
        seen.add(row.url)
        rows.append(row)
    return rows


def _parse_list_item(item: Tag, base_url: str, now: datetime) -> NewsRow | None:
    href = attr(item, "href")
    news_id = id_from(href, NEWS_ID_RE)
    if news_id is None or href is None:
        return None
    title = text_of(item.select_one(NewsSel.TITLE)) or text_of(item)
    if not title:
        return None
    comments = None
    meta = item.select_one(NewsSel.META)
    m = NEWS_COMMENTS_RE.search(text_of(meta))
    if m:
        comments = int(m.group(1))
    url = strip_fragment(absolute_url(href, base_url)).split("?", 1)[0]
    return NewsRow(
        url=url,
        hltv_news_id=news_id,
        title=title,
        published_at=parse_list_date(text_of(item.select_one(NewsSel.DATE)), now=now),
        comments=comments,
        country=attr(item.select_one(NewsSel.FLAG), "title"),
    )


def _innermost_blocks(body: Tag) -> list[Tag]:
    tags = NewsSel.BODY_BLOCK_TAGS
    out = []
    for el in body.find_all(list(tags)):
        if el.find(list(tags)) is not None:
            continue  # e.g. <li><p>..</p></li>: keep only the inner block
        out.append(el)
    return out


def parse_news_article(html: str, url: str) -> NewsArticle:
    """Title, publication time, author and plain-text body of a news article.

    Team / player HLTV ids are collected from the article's ``/team/{id}`` and ``/player/{id}`` links
    (HLTV links most mentions), which is more reliable than name matching.
    """
    soup = make_soup(html)
    article = soup.select_one(NewsSel.ARTICLE)
    scope: Tag = article if article is not None else soup
    title = text_of(scope.select_one(NewsSel.HEADLINE)) or clean_text(
        attr(soup.select_one(NewsSel.OG_TITLE), "content")
    )
    if not title:
        raise HLTVParseError(f"{url!r} does not look like an HLTV news article")
    published = unix_ms(attr(scope.select_one(NewsSel.INFO_DATE), "data-unix"))
    if published is None:
        meta_time = attr(soup.select_one(NewsSel.PUBLISHED_META), "content")
        try:
            published = parse_datetime(meta_time) if meta_time else None
        except (ValueError, OverflowError):
            published = None
    body_node = scope.select_one(NewsSel.BODY)
    team_ids: list[int] = []
    player_ids: list[int] = []
    lead: str | None = None
    paragraphs: list[str] = []
    if body_node is not None:
        for link in body_node.select(NewsSel.TEAM_LINK):
            tid = id_from(attr(link, "href"), TEAM_ID_RE)
            if tid is not None and tid not in team_ids:
                team_ids.append(tid)
        for link in body_node.select(NewsSel.PLAYER_LINK):
            pid = id_from(attr(link, "href"), PLAYER_ID_RE)
            if pid is not None and pid not in player_ids:
                player_ids.append(pid)
        for noise in body_node.select(NewsSel.BODY_NOISE):
            noise.decompose()
        lead = text_of(body_node.select_one(NewsSel.LEAD)) or None
        for block in _innermost_blocks(body_node):
            text = text_of(block)
            if text and (not paragraphs or paragraphs[-1] != text):
                paragraphs.append(text)
    author = text_of(scope.select_one(NewsSel.AUTHOR)) or None
    return NewsArticle(
        url=strip_fragment(url).split("?", 1)[0],
        hltv_news_id=id_from(url, NEWS_ID_RE),
        title=title,
        published_at=published,
        author=author,
        lead=lead,
        body="\n\n".join(paragraphs),
        team_hltv_ids=team_ids,
        player_hltv_ids=player_ids,
    )
