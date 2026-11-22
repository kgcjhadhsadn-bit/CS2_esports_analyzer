"""Sync HLTV news (archive pages + articles) into ``news_items``, tagging mentioned teams / players."""

from __future__ import annotations

import itertools
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.db.models import NewsItem, Player, Team, TeamAlias
from cs2bot.ingest.hltv.errors import HLTVParseError
from cs2bot.ingest.hltv.models import NewsArticle, NewsRow, SyncReport
from cs2bot.ingest.hltv.parsers import parse_news_article, parse_news_list
from cs2bot.ingest.hltv.sync.common import (
    HLTVSyncOptions,
    PageSource,
    fetch_page,
    merge_extra,
    source_base_url,
)
from cs2bot.ingest.hltv.urls import HLTV_BASE_URL, news_archive_path
from cs2bot.utils.names import normalize_player_name, normalize_team_name, normalize_text
from cs2bot.utils.time import utcnow

logger = logging.getLogger(__name__)

NEWS_SOURCE = "hltv"
_WORD = r"(?<![A-Za-z0-9]){}(?![A-Za-z0-9])"
_QUOTED = "[\"'\u201c\u201d\u2018\u2019\u2060]\\s*{}\\s*[\"'\u201c\u201d\u2018\u2019\u2060]"


def _months_back(now: datetime, start: datetime) -> list[tuple[int, int]]:
    """(year, month) pairs from ``now``'s month back to ``start``'s month, newest first."""
    out: list[tuple[int, int]] = []
    year, month = now.year, now.month
    while (year, month) >= (start.year, start.month):
        out.append((year, month))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return out


@dataclass
class MentionIndex:
    """Detect teams / players mentioned in news text.

    Precision first: HLTV ids from article links are exact; names are matched conservatively -
    multi-word team aliases case-insensitively, single-word team names only in their own spelling
    (``Vitality``, ``BIG``, ``NAVI`` - never the plain words "big" / "vitality"), player nicknames
    case-sensitively, and all-lowercase dictionary-like nicknames ("rain") only when quoted
    (``Havard "rain" Nygaard``). Nicknames shared by several players are ignored.
    """

    team_by_hltv: dict[int, int] = field(default_factory=dict)
    player_by_hltv: dict[int, int] = field(default_factory=dict)
    multiword_aliases: dict[str, set[int]] = field(default_factory=dict)
    team_surfaces: list[tuple[re.Pattern[str], int]] = field(default_factory=list)
    player_patterns: dict[str, list[tuple[re.Pattern[str], int]]] = field(default_factory=dict)

    @classmethod
    def build(cls, session: Session) -> MentionIndex:
        index = cls()
        teams = list(session.scalars(select(Team)))
        for team in teams:
            if team.hltv_id is not None:
                index.team_by_hltv[team.hltv_id] = team.id
            for key in {normalize_team_name(team.name), normalize_text(team.name)}:
                if key and " " in key:  # "natus vincere", "the mongolz"
                    index.multiword_aliases.setdefault(key, set()).add(team.id)
            index._add_team_surface(team.name, team.id)
        for alias in session.scalars(select(TeamAlias)):
            if " " in alias.alias:
                index.multiword_aliases.setdefault(alias.alias, set()).add(alias.team_id)
            elif len(alias.alias) >= 2:
                index._add_team_surface(alias.alias.upper(), alias.team_id)  # "navi" -> "NAVI"
        by_nick: dict[str, list[Player]] = {}
        for player in session.scalars(select(Player)):
            if player.hltv_id is not None:
                index.player_by_hltv[player.hltv_id] = player.id
            key = normalize_player_name(player.nickname)
            if key:
                by_nick.setdefault(key, []).append(player)
        for key, players in by_nick.items():
            if len(players) != 1:
                continue
            nick = players[0].nickname.strip()
            if len(nick) < 2 or (len(nick) == 2 and nick.isalpha() and (nick.islower() or nick.isupper())):
                continue  # "jL" is distinctive, "ab" / "AB" are not
            template = _QUOTED if nick.isalpha() and nick.islower() else _WORD
            pattern = re.compile(template.format(re.escape(nick)))
            index.player_patterns.setdefault(key, []).append((pattern, players[0].id))
        return index

    def _add_team_surface(self, surface: str, team_id: int) -> None:
        surface = surface.strip()
        if len(surface) < 2 or " " in surface:
            return
        if surface.islower():  # plain lowercase words are too ambiguous ("big", "spirit")
            return
        self.team_surfaces.append((re.compile(_WORD.format(re.escape(surface))), team_id))

    def detect(self, article: NewsArticle, extra_text: str = "") -> tuple[set[int], set[int]]:
        team_ids = {self.team_by_hltv[h] for h in article.team_hltv_ids if h in self.team_by_hltv}
        player_ids = {self.player_by_hltv[h] for h in article.player_hltv_ids if h in self.player_by_hltv}
        text = "\n".join(t for t in (article.title, article.lead or "", article.body, extra_text) if t)
        tokens = normalize_text(text).split()
        for n in (2, 3, 4):
            for i in range(len(tokens) - n + 1):
                gram = " ".join(tokens[i : i + n])
                ids = self.multiword_aliases.get(gram) or self.multiword_aliases.get(
                    normalize_team_name(gram)
                )
                if ids and len(ids) == 1:
                    team_ids |= ids
        for pattern, team_id in self.team_surfaces:
            if pattern.search(text):
                team_ids.add(team_id)
        candidates = set(tokens) | {a + b for a, b in itertools.pairwise(tokens)}
        for key, patterns in self.player_patterns.items():
            if key not in candidates:  # cheap prefilter before the exact regex
                continue
            for pattern, player_id in patterns:
                if pattern.search(text):
                    player_ids.add(player_id)
        return team_ids, player_ids


def sync_news(
    session: Session,
    fetcher: PageSource,
    *,
    months: int = 1,
    now: datetime | None = None,
    options: HLTVSyncOptions | None = None,
    refresh: bool = False,
) -> SyncReport:
    """Upsert HLTV news from the last ``months`` x 31 days (archive pages, then article pages).

    Articles already stored with a body are not fetched again unless ``refresh``. At most
    ``HLTVSyncOptions.max_news_articles`` article pages are fetched per call (politeness).
    """
    opts = options or HLTVSyncOptions()
    report = SyncReport(job="hltv.sync_news")
    current = now or utcnow()
    window_start = current - timedelta(days=31 * max(1, months))
    base_url = source_base_url(fetcher, HLTV_BASE_URL)
    index = MentionIndex.build(session)
    fetched_articles = 0
    for year, month in _months_back(current, window_start):
        is_current_month = (year, month) == (current.year, current.month)
        path = news_archive_path(year, month)
        html = fetch_page(
            fetcher,
            report,
            path,
            f"news archive {year}-{month:02d}",
            max_age_minutes=opts.listing_ttl_minutes if is_current_month else None,
        )
        if html is None:
            if report.blocked:
                break
            continue
        for row in parse_news_list(html, base_url=base_url, now=current):
            if row.published_at is not None and row.published_at < window_start:
                continue
            existing = session.scalar(select(NewsItem).where(NewsItem.url == row.url))
            if existing is not None and existing.body and not refresh:
                report.skipped += 1
                continue
            if fetched_articles >= opts.max_news_articles:
                report.details["truncated"] = True
                break
            article_html = fetch_page(fetcher, report, row.url, f"news {row.hltv_news_id}")
            fetched_articles += 1
            if article_html is None:
                if report.blocked:
                    break
                continue
            try:
                article = parse_news_article(article_html, row.url)
            except HLTVParseError as exc:
                report.failed += 1
                report.add_error(f"news {row.url}: {exc}")
                continue
            try:
                with session.begin_nested():
                    created = _upsert_news(session, row, article, existing, index)
            except Exception as exc:
                logger.exception("persisting HLTV news %s failed", row.url)
                report.failed += 1
                report.add_error(f"news {row.url}: persisting failed: {type(exc).__name__}: {exc}")
                continue
            if created:
                report.created += 1
            else:
                report.updated += 1
        if report.blocked or report.details.get("truncated"):
            break
    report.details["articles_fetched"] = fetched_articles
    return report


def _upsert_news(
    session: Session, row: NewsRow, article: NewsArticle, existing: NewsItem | None, index: MentionIndex
) -> bool:
    team_ids, player_ids = index.detect(article)
    item = existing
    created = item is None
    if item is None:
        item = NewsItem(source=NEWS_SOURCE, url=row.url[:1024], title=(article.title or row.title)[:512])
        session.add(item)
    item.title = (article.title or row.title)[:512]
    item.body = article.body or item.body
    item.published_at = article.published_at or row.published_at or item.published_at
    item.team_ids = sorted(team_ids)
    item.player_ids = sorted(player_ids)
    item.extra = merge_extra(
        item.extra,
        {
            "news_id": article.hltv_news_id or row.hltv_news_id,
            "author": article.author,
            "lead": article.lead,
            "country": row.country,
            "comments": row.comments,
            "team_hltv_ids": article.team_hltv_ids or None,
            "player_hltv_ids": article.player_hltv_ids or None,
        },
    )
    session.flush()
    return created
