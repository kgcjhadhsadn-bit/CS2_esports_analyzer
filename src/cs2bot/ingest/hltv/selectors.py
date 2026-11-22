"""Every CSS selector, regex and marker the HLTV parsers rely on, kept in one module.

When HLTV changes its markup, this is the file to update (plus the HTML fixtures under
``tests/ingest/hltv/fixtures``).

Ground truth: hltv.org cannot be reached from the build machine, so the selectors come from
maintained open-source scrapers and recorded page structure:

* gigobyte/HLTV (TypeScript): ``getMatch.ts``, ``getMatchMapStats.ts``, ``getResults.ts``,
  ``getMatches.ts``, ``getTeamRanking.ts``, ``getNews.ts``;
* the 2026-02-15 page reconnaissance of w1tte/hltv-scraper (match overview, map stats and results
  listing pages, verified against 24 saved pages incl. BO1/BO3/BO5, overtime and forfeits);
* 2026 ``/matches`` markup (``div.match-wrapper[data-match-id]``) from the test data of
  RATING3PRO/HLTV-API and woq/hltv-bot, with the pre-2025 ``.upcomingMatch`` layout as fallback;
* ranking / news markup from julienc91/hltv-ranking, Almighty-ap/astrbot_plugin_cs2_results and real
  saved news pages (f4bio/hltvapi.rs).
"""

from __future__ import annotations

import re

from cs2bot.domain.enums import RoundEndReason, Side

# ------------------------------------------------------------------------------------- ids in URLs
MATCH_ID_RE = re.compile(r"/matches/(\d+)(?=[/?#]|$)")
TEAM_ID_RE = re.compile(r"/team/(\d+)(?=[/?#]|$)")
STATS_TEAM_ID_RE = re.compile(r"/stats/teams/(\d+)(?=[/?#]|$)")
PLAYER_ID_RE = re.compile(r"/player/(\d+)(?=[/?#]|$)")
STATS_PLAYER_ID_RE = re.compile(r"/stats/players/(\d+)(?=[/?#]|$)")
EVENT_ID_RE = re.compile(r"/events/(\d+)(?=[/?#]|$)")
EVENT_QUERY_RE = re.compile(r"[?&]event=(\d+)")
MAPSTATS_ID_RE = re.compile(r"/mapstatsid/(\d+)(?=[/?#]|$)")
DEMO_ID_RE = re.compile(r"/download/demo/(\d+)")
NEWS_ID_RE = re.compile(r"/news/(\d+)(?=[/?#]|$)")
RANKING_DATE_PATH_RE = re.compile(r"/ranking/teams/(\d{4})/([a-z]+)/(\d{1,2})(?=[/?#]|$)", re.I)
NEWS_ARCHIVE_PATH_RE = re.compile(r"^/news/archive/(\d{4})/([a-z]+)/?$", re.I)

# Path routes used to canonicalise cache keys (slugs after the id are ignored by HLTV).
MATCH_ROUTE_RE = re.compile(r"^/matches/(\d+)(?:/.*)?$")
MAPSTATS_ROUTE_RE = re.compile(r"^/stats/matches/mapstatsid/(\d+)(?:/.*)?$")
PERFORMANCE_ROUTE_RE = re.compile(r"^/stats/matches/performance/mapstatsid/(\d+)(?:/.*)?$")
ECONOMY_ROUTE_RE = re.compile(r"^/stats/matches/economy/mapstatsid/(\d+)(?:/.*)?$")
NEWS_ROUTE_RE = re.compile(r"^/news/(\d+)(?:/.*)?$")

# ------------------------------------------------------------------------------------- shared text
RANK_RE = re.compile(r"#\s*(\d+)")
BEST_OF_RE = re.compile(r"best\s+of\s+(\d+)", re.I)
LOCATION_RE = re.compile(r"\(\s*(LAN|Online)\s*\)", re.I)
INT_RE = re.compile(r"[-+]?\d+")
FLOAT_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
SCORE_PAIR_RE = re.compile(r"(\d+)\s*[:\-]\s*(\d+)")
ORDINAL_SUFFIX_RE = re.compile(r"(\d+)(st|nd|rd|th)\b", re.I)
PLACEHOLDER_TEAM_NAMES = frozenset({"tbd", "tba", "-", ""})

KNOWN_MAP_NAMES = frozenset(
    {
        "ancient",
        "anubis",
        "cache",
        "cobblestone",
        "dust2",
        "inferno",
        "mirage",
        "nuke",
        "overpass",
        "season",
        "train",
        "tuscan",
        "vertigo",
    }
)


class ResultsSel:
    """``/results?offset=N`` (100 rows per page; page 1 also has a duplicated "big results" block)."""

    ROW = ".result-con"
    BIG_RESULTS_CLASS = "big-results"
    SUBLIST_CLASS = "results-sublist"
    SUBLIST_HEADLINE = ".standard-headline"
    ROW_LINK = "a.a-reset[href]"
    ROW_LINK_FALLBACK = "a[href*='/matches/']"
    ROW_UNIX_ATTR = "data-zonedgrouping-entry-unix"
    TEAM1_NAME = ".team1 .team"
    TEAM2_NAME = ".team2 .team"
    TEAM_WON_CLASS = "team-won"
    SCORE_SPANS = ".result-score span"
    MAP_TEXT = ".map-text"
    STARS = ".stars i"
    EVENT_NAME = ".event-name"
    EVENT_LOGO = "img.event-logo"
    PAGINATION_DATA = ".pagination-data"
    PAGINATION_NEXT = ".pagination-next"


RESULTS_PAGINATION_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s+of\s+(\d+)")
RESULTS_HEADLINE_PREFIX_RE = re.compile(r"^\s*results\s+for\s+", re.I)
RESULTS_FORFEIT_TOKEN = "def"
RESULTS_FORMAT_RE = re.compile(r"^bo(\d)$", re.I)
# BO1 rows show an abbreviated map name instead of "bo1".
RESULTS_MAP_SLUGS: dict[str, str] = {
    "anb": "anubis",
    "anc": "ancient",
    "cbl": "cobblestone",
    "cch": "cache",
    "d2": "dust2",
    "dst2": "dust2",
    "inf": "inferno",
    "mrg": "mirage",
    "nuke": "nuke",
    "ovp": "overpass",
    "tcn": "tuscan",
    "trn": "train",
    "vtg": "vertigo",
}


class MatchesSel:
    """``/matches`` - 2025+ layout first, pre-2025 layout as fallback."""

    WRAPPER = "div.match-wrapper"
    WRAPPER_ID_ATTR = "data-match-id"
    WRAPPER_STARS_ATTR = "data-stars"
    WRAPPER_EVENT_ID_ATTR = "data-event-id"
    LINK = "a[href*='/matches/']"
    TEAM_BLOCK = ".match-team"
    TEAM_NAME = ".match-teamname"
    TIME = ".match-time[data-unix]"
    META = ".match-meta"
    META_LIVE = ".match-meta-live"
    EVENT_HEADLINE = "[data-event-headline]"
    EVENT_NAME = ".match-event-name"
    EVENT_LOGO = "img.match-event-logo"
    NO_INFO = ".match-no-info, .match-info-empty"
    STAGE = ".match-stage"
    MAPS_ATTR = "data-maps"
    LIVE_CLASS = "live-match-container"
    # pre-2025 layout
    LEGACY = "div.upcomingMatch, div.liveMatch-container"
    LEGACY_TEAM_NAME = ".matchTeamName"
    LEGACY_TEAM_BLOCK = ".matchTeam"
    LEGACY_TIME = ".matchTime[data-unix]"
    LEGACY_LIVE = ".matchTime.matchLive"
    LEGACY_META = ".matchMeta"
    LEGACY_RATING_STARS = ".matchRating i"
    LEGACY_EVENT_NAME = ".matchEventName"
    LEGACY_EVENT_LOGO = "img.matchEventLogo"
    LEGACY_EMPTY = ".matchInfoEmpty"


class MatchSel:
    """``/matches/{id}/{slug}`` - the match overview page."""

    CANONICAL = "link[rel='canonical']"
    TEAM_GRADIENT = ".team{n}-gradient"
    TEAM_NAME = ".teamName"
    TEAM_LINK = "a[href*='/team/']"
    TEAM_SCORE = ".won, .lost, .tie"
    DATE = ".timeAndEvent .date[data-unix]"
    TIME = ".timeAndEvent .time[data-unix]"
    EVENT_LINK = ".timeAndEvent .event a"
    COUNTDOWN = ".countdown"
    VETO_BOX = ".veto-box"
    PREFORMATTED = ".preformatted-text"
    VETO_LINE_DIVS = ".padding > div"
    MAPHOLDER = ".mapholder"
    MAP_NAME = ".mapname"
    MAP_PLAYED_CLASS = "played"
    MAP_OPTIONAL_CLASS = "optional"
    MAP_LEFT = ".results-left"
    MAP_RIGHT = ".results-right"
    MAP_TEAM_SCORE = ".results-team-score"
    MAP_STATS_LINK = "a.results-stats[href]"
    MAP_HALF_SCORE = ".results-center-half-score"
    LINEUPS = ".lineups"
    LINEUP_BLOCK = ".lineup"
    LINEUP_TEAM_LINK = ".box-headline a[href*='/team/']"
    LINEUP_RANK = ".teamRanking a"
    PLAYERS = ".players"
    PLAYER_WITH_ID = "[data-player-id]"
    PLAYER_ID_ATTR = "data-player-id"
    PLAYER_NAME = ".text-ellipsis"
    PLAYER_FLAG = "img.flag"
    PLAYER_LINK = "a[href*='/player/']"
    PLAYER_PHOTO = "img.player-photo, img"
    DEMO_LINK = "a[href*='/download/demo/']"
    DEMO_ATTR_ELEMENTS = "[data-demo-link]"
    DEMO_ATTR = "data-demo-link"


# ``.countdown`` text -> HLTV match status (anything else is a running countdown = scheduled).
COUNTDOWN_STATUS: dict[str, str] = {
    "live": "live",
    "match over": "finished",
    "match postponed": "postponed",
    "match deleted": "deleted",
}
VETO_PICK_BAN_RE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s+(removed|picked)\s+(.+?)\s*$", re.I)
VETO_LEFTOVER_RE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s+was\s+left\s+over\s*$", re.I)
FORFEIT_NOTE_RE = re.compile(r"forfeit|walkover|default\s+win|w/o\b", re.I)
FORFEIT_MAP_NAME = "default"
STANDIN_RE = re.compile(r"stand-?\s?in", re.I)
LINEUP_FULL_NAME_RE = re.compile(r"^(?P<first>.*?)\s*'(?P<nick>[^']+)'\s*(?P<last>.*)$")


class MapStatsSel:
    """``/stats/matches/mapstatsid/{id}/{slug}`` - per-map statistics."""

    MATCH_PAGE_LINK = ".match-page-link[href]"
    SELECTED_MENU_ITEM = ".stats-top-menu-item.selected[href]"
    INFO_BOX = ".match-info-box"
    TEAM_LEFT = ".team-left"
    TEAM_RIGHT = ".team-right"
    TEAM_LINK = "a[href]"
    TEAM_LOGO = "img.team-logo"
    TEAM_SCORE = ".bold"
    EVENT_LINK = "a[href*='event=']"
    DATE = "[data-unix]"
    INFO_ROW = ".match-info-row"
    INFO_ROW_LABEL = ".bold"
    INFO_ROW_VALUE = ".right"
    SIDE_SPANS = "span.ct-color, span.t-color"
    STATS_TABLE = ".stats-table"
    TOTAL_CLASS = "totalstats"
    CT_CLASS = "ctstats"
    T_CLASS = "tstats"
    TABLE_TEAM_NAME = "th.st-teamname"
    TABLE_RATING_HEADER = "th.st-rating"
    ROW = "tbody tr"
    PLAYER_CELL = "td.st-player"
    PLAYER_LINK = "a[href]"
    PLAYER_FLAG = "img.flag"
    ECO_CLASS = "eco-adjusted-data"
    TRADITIONAL_CLASS = "traditional-data"
    SMARTPHONE_ONLY_CLASS = "smartphone-only"
    RATING_DESC = ".ratingDesc"
    ROUND_HISTORY_CON = ".round-history-con"
    ROUND_HISTORY_ROW = ".round-history-team-row"
    ROUND_HISTORY_TEAM = "img.round-history-team"
    ROUND_OUTCOME = "img.round-history-outcome"


# Scoreboard column classes (``td.<class>``).
COL_KILLS = "st-kills"
COL_ASSISTS = "st-assists"
COL_DEATHS = "st-deaths"
COL_KAST = "st-kast"
COL_KAST_OLD = "st-kdratio"  # pre-2025 layout put KAST in the "kdratio" column
COL_ADR = "st-adr"
COL_OPENING_KD = "st-opkd"
COL_MULTIKILLS = "st-mks"
COL_CLUTCHES = "st-clutches"
COL_KD_DIFF = "st-kddiff"
COL_FK_DIFF = "st-fkdiff"
COL_SWING = "st-roundSwing"
COL_RATING = "st-rating"

RATING_VERSION_RE = re.compile(r"(\d\.\d)")
NUM_WITH_PAREN_RE = re.compile(r"^\s*(-?\d+)(?:[.,]\d+)?\s*(?:\(\s*(\d+)\s*\))?")
HALF_GROUP_RE = re.compile(r"\(\s*(\d+)\s*:\s*(\d+)\s*\)")

# Round-history outcome icons (``img.round-history-outcome[src=".../<name>.svg"]``).
OUTCOME_ICONS: dict[str, tuple[Side, RoundEndReason]] = {
    "ct_win": (Side.CT, RoundEndReason.ELIMINATION),
    "t_win": (Side.T, RoundEndReason.ELIMINATION),
    "bomb_defused": (Side.CT, RoundEndReason.BOMB_DEFUSED),
    "bomb_exploded": (Side.T, RoundEndReason.BOMB_EXPLODED),
    "stopwatch": (Side.CT, RoundEndReason.TIME_EXPIRED),
}
EMPTY_OUTCOME_ICON = "emptyhistory"
REGULATION_ROUNDS = 24  # MR12 (CS2)


class RankingSel:
    """``/ranking/teams[/YYYY/month/D]`` - HLTV world ranking."""

    HEADER_TEXT = ".regional-ranking-header-text"
    HEADER = ".regional-ranking-header"
    TEAM_BLOCK = ".ranked-team"
    POSITION = ".position"
    NAME = ".ranking-header .name, .teamLine .name, .name"
    POINTS = ".points"
    CHANGE = ".change"
    TEAM_PROFILE_LINK = "a.moreLink[href*='/team/']"
    TEAM_LINK = "a[href*='/team/']"
    PLAYER_LINK = "a[href*='/player/']"
    PLAYER_NICK = ".nick"


RANKING_HEADER_DATE_RE = re.compile(r"ranking\s+on\s+(.+?)\s*(?:beta)?\s*$", re.I)


class NewsSel:
    """News lists (front page / ``/news/archive/YYYY/month``) and article pages."""

    LIST_ITEM = "a.newsline[href], a.article[href]"
    TITLE = ".newstext"
    DATE = ".newsrecent"
    META = ".newstc"
    FLAG = "img.newsflag"
    ARTICLE = "article.newsitem"
    HEADLINE = "h1.headline"
    OG_TITLE = "meta[property='og:title']"
    PUBLISHED_META = "meta[property='article:published_time']"
    INFO_DATE = ".article-info [data-unix]"
    AUTHOR = ".authorName"
    BODY = ".newstext-con"
    LEAD = "p.headertext"
    BODY_NOISE = (
        ".image-con, .featured-quote, .tooltip-con, .video-con, .audioCon, script, style, iframe, "
        ".newsdsl-tournament-stat-widget, .ad, .ads, blockquote.twitter-tweet"
    )
    BODY_BLOCK_TAGS = ("p", "h2", "h3", "li")
    TEAM_LINK = "a[href*='/team/']"
    PLAYER_LINK = "a[href*='/player/']"


NEWS_COMMENTS_RE = re.compile(r"(\d+)\s+comments?", re.I)
NEWS_RELATIVE_RE = re.compile(
    r"^(?P<n>an?|one|\d+)\s+(?P<unit>second|minute|hour|day|week|month|year)s?\s+ago$", re.I
)

# ------------------------------------------------------------------------------------- anti-bot pages
# Cloudflare injects a bot-detection script ("/cdn-cgi/challenge-platform/scripts/jsd/main.js") into
# *normal* pages, so a bare "challenge-platform" substring is not a challenge marker. These patterns only
# match the interstitial challenge / block pages themselves.
CHALLENGE_TITLE_RE = re.compile(
    r"<title[^>]*>\s*(?P<title>just a moment\.{0,3}|attention required!\s*\|\s*cloudflare|"
    r"access denied\s*\|[^<]*cloudflare[^<]*)\s*</title>",
    re.I,
)
# Always checked: tokens that only exist on interstitial pages.
CHALLENGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cf-chl", re.compile(r"(?<![a-z0-9])_?_?cf[-_]chl[-_a-z]*", re.I)),
    ("challenge-platform", re.compile(r"/cdn-cgi/challenge-platform/(?:h/|[^\"'\s<>]*orchestrate)", re.I)),
    (
        "challenge-form",
        re.compile(r"id=[\"'](?:challenge-form|challenge-error-text|cf-challenge-running)[\"']", re.I),
    ),
)
# Only checked on small pages (block pages are a few KB; an article could quote these phrases).
BLOCK_PAGE_MAX_BYTES = 60_000
BLOCK_PAGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "cf-block",
        re.compile(
            r"sorry, you have been blocked|enable javascript and cookies to continue|"
            r"checking your browser before accessing|error code:\s*10\d\d",
            re.I,
        ),
    ),
)
FINISHED_MATCH_HTML_RE = re.compile(
    r"\bcountdown\b[^>]*>\s*(?:<[^>]*>\s*)*(?:match over|match deleted)", re.I
)
