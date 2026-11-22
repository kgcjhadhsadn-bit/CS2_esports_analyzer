"""Parsers for the world ranking, news lists and news articles."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from cs2bot.ingest.hltv import HLTVParseError, parse_news_article, parse_news_list, parse_ranking_page
from cs2bot.ingest.hltv.parsers.common import parse_human_date, parse_list_date
from tests.ingest.hltv.conftest import load_fixture

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def test_ranking_rows() -> None:
    rows = parse_ranking_page(load_fixture("ranking_2026_11_16.html"))
    assert len(rows) == 32 and [r.rank for r in rows] == list(range(1, 33))
    top = rows[0]
    assert (top.team_name, top.hltv_team_id, top.points, top.change, top.is_new) == (
        "Vitality",
        9565,
        1000,
        0,
        False,
    )
    assert top.ranking_date == date(2026, 11, 16)
    assert top.player_hltv_ids == [7322, 11893, 20113, 18462, 11816]
    assert rows[5].is_new and rows[5].change is None
    assert rows[1].change == 1 and rows[2].change == -1
    assert rows[11].team_name == "BIG" and rows[11].hltv_team_id == 7532


def test_ranking_date_fallbacks() -> None:
    html = load_fixture("ranking_2026_11_16.html").replace(
        "World ranking on November 16th, 2026", "World ranking"
    )
    from_url = parse_ranking_page(html, url="https://www.hltv.org/ranking/teams/2026/november/9")
    assert from_url[0].ranking_date == date(2026, 11, 9)
    given = parse_ranking_page(html, ranking_date=date(2026, 11, 2))
    assert given[0].ranking_date == date(2026, 11, 2)


def test_ranking_skips_broken_blocks() -> None:
    html = load_fixture("ranking_2026_11_16.html").replace('<span class="position">#2</span>', "", 1)
    rows = parse_ranking_page(html)
    assert len(rows) == 31 and 2 not in [r.rank for r in rows]


def test_news_archive_list_absolute_dates() -> None:
    rows = parse_news_list(load_fixture("news_archive_2026_november.html"), now=NOW)
    assert [r.hltv_news_id for r in rows] == [43100, 43098, 43095, 43090]
    first = rows[0]
    assert first.url == "https://www.hltv.org/news/43100/natus-vincere-bench-jl-iam-to-stand-in"
    assert first.title == "Natus Vincere bench jL, iM steps up to IGL"
    assert first.published_at == datetime(2026, 11, 21, tzinfo=UTC)
    assert first.comments == 412 and first.country == "Europe"


def test_news_front_page_relative_dates() -> None:
    rows = parse_news_list(load_fixture("news_front.html"), now=NOW)
    assert [r.published_at for r in rows] == [
        NOW - timedelta(hours=2),
        NOW - timedelta(days=1),
        NOW - timedelta(days=3),
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2 hours ago", NOW - timedelta(hours=2)),
        ("an hour ago", NOW - timedelta(hours=1)),
        ("a day ago", NOW - timedelta(days=1)),
        ("13 minutes ago", NOW - timedelta(minutes=13)),
        ("yesterday", NOW - timedelta(days=1)),
        ("2026-11-05", datetime(2026, 11, 5, tzinfo=UTC)),
        ("05/11/2026", datetime(2026, 11, 5, tzinfo=UTC)),  # European day-first
        ("November 5th 2026", datetime(2026, 11, 5, tzinfo=UTC)),
        ("", None),
        ("soon", None),
    ],
)
def test_parse_list_date(text: str, expected: datetime | None) -> None:
    assert parse_list_date(text, now=NOW) == expected


def test_parse_human_date_iso_is_not_day_first() -> None:
    assert parse_human_date("2026-11-05") == date(2026, 11, 5)
    assert parse_human_date("21st of November 2026") == date(2026, 11, 21)


def test_news_article() -> None:
    url = "https://www.hltv.org/news/43100/natus-vincere-bench-jl-iam-to-stand-in"
    article = parse_news_article(load_fixture("news_article_43100.html"), url)
    assert article.hltv_news_id == 43100 and article.title == "Natus Vincere bench jL, iM steps up to IGL"
    assert article.published_at == datetime.fromtimestamp(1795290000, tz=UTC)
    assert article.author == "Striker"
    assert article.lead is not None and article.lead.startswith("Natus Vincere have moved")
    assert "iM" in article.body and "What's next" in article.body
    # image captions, featured quotes and embedded tweets are not article text
    assert "jL will sit out" not in article.body and "fresh start" not in article.body
    assert "Thank you for everything" not in article.body
    assert article.team_hltv_ids == [4608] and article.player_hltv_ids == [19206]


def test_news_article_requires_a_title() -> None:
    with pytest.raises(HLTVParseError):
        parse_news_article("<html><body><p>x</p></body></html>", "https://www.hltv.org/news/1/x")
