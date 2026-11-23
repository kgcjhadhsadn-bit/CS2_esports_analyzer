"""Tuning knobs of the Betclic odds sources that are not (yet) part of :class:`cs2bot.config.Settings`.

Defaults work out of the box; the owner can override any of them in the optional ``source:``
section of ``config/betclic_markets.yaml`` (``settings.betclic.market_rules_file``) without code
changes. Unknown keys are ignored with a warning.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Literal

import yaml

from cs2bot.config import Settings
from cs2bot.odds.grpc_web import DEFAULT_GRPC_BASE_URL
from cs2bot.odds.text import fold_text
from cs2bot.utils.paths import resolve_path

logger = logging.getLogger(__name__)

DEFAULT_CS2_PATTERN = r"counter[\s\-_]*strike|\bcs\s*-?\s*2\b|\bcs:?\s*go\b|\bcsgo\b|\bcs2\b"

# Other e-sports Betclic lists in the same "E-sport" section. Organisations such as G2, Fnatic,
# Team Liquid or NAVI field teams in several of them, so a League of Legends "G2 - Fnatic" played
# around the same time as a CS2 "G2 - Fnatic" would otherwise be linked to our CS2 match (same team
# names, close start time) and its "Zwycięzca meczu" would be priced with the CS2 model. Matched on
# folded sport / competition / URL-section text (never on team names); spaces also match "-" / "_".
_OTHER_GAMES: tuple[str, ...] = (
    "league of legends",
    r"\blol\b",
    r"\bdota\b",
    "valorant",
    "rainbow six",
    r"\br6\b",
    "overwatch",
    "starcraft",
    "warcraft",
    "rocket league",
    r"\bea (?:sports )?fc\b",
    r"\befootball\b",
    r"\bfifa\b",
    r"\bnba 2k",
    "mobile legends",
    "king of glory",
    "honor of kings",
    "arena of valor",
    "wild rift",
    "teamfight tactics",
    r"\bpubg\b",
    "call of duty",
    "apex legends",
    "hearthstone",
    "fortnite",
    "age of empires",
    "crossfire",
    "free fire",
    "brawl stars",
    "street fighter",
    r"\btekken\b",
    "marvel rivals",
    "deadlock",
    r"\b(?:lec|lck|lpl|lcs|vct)\b",  # League of Legends / Valorant league abbreviations
)
DEFAULT_OTHER_GAMES_PATTERN = "|".join(p.replace(" ", r"[\s\-_]*") for p in _OTHER_GAMES)
_CS2_RE = re.compile(DEFAULT_CS2_PATTERN)
_OTHER_GAMES_RE = re.compile(DEFAULT_OTHER_GAMES_PATTERN)


def url_section(url: str | None) -> str | None:
    """A URL without its last path segment (the event slug carries team names, not the game)."""
    if not url:
        return None
    path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    head, _, _ = path.rpartition("/")
    return head or None


def is_other_game(
    hints: Iterable[str | None],
    *,
    cs2_pattern: str | re.Pattern[str] | None = None,
    other_pattern: str | re.Pattern[str] | None = None,
) -> bool:
    """Do the sport / competition / URL-section hints name another e-sport (and not CS2)?

    ``False`` when no hint is conclusive - this is a veto against clear mismatches only.
    """
    cs2_re = re.compile(cs2_pattern) if isinstance(cs2_pattern, str) else (cs2_pattern or _CS2_RE)
    other_re = (
        re.compile(other_pattern) if isinstance(other_pattern, str) else (other_pattern or _OTHER_GAMES_RE)
    )
    folded = [fold_text(h) for h in hints if h]
    if any(cs2_re.search(h) for h in folded):
        return False
    return any(other_re.search(h) for h in folded)


DEFAULT_IGNORED_URLS: tuple[str, ...] = (
    "google",
    "doubleclick",
    "facebook",
    "hotjar",
    "datadog",
    "sentry",
    "optimizely",
    "segment.",
    "cookiebot",
    "onetrust",
    "didomi",
    "clarity.ms",
    "newrelic",
    "/analytics",
    "/collect",
    "/track",
)


def _coerce(default: Any, value: Any) -> Any:
    """``value`` converted to the type of the option's default; raises on a mismatch."""
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise TypeError("expected a boolean")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            raise TypeError("expected an integer")
        number = float(value)
        if not number.is_integer():
            raise ValueError("expected an integer")
        return int(number)
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            raise TypeError("expected a number")
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            raise ValueError("expected a finite number")
        return number
    if isinstance(default, tuple):
        if not isinstance(value, list | tuple):
            raise TypeError("expected a list")
        return tuple(str(v) for v in value)
    if value is None:
        if default is None:
            return None
        raise TypeError("expected a value")
    if not isinstance(value, str):
        raise TypeError("expected text")
    return value


@dataclass(frozen=True)
class BetclicSourceConfig:
    # --- crawl scope & pacing (browser) ---------------------------------------------------------
    max_events_per_run: int = 40
    min_page_delay_s: float = 2.5
    max_page_delay_s: float = 6.0
    navigation_timeout_s: float = 45.0
    settle_s: float = 2.5  # wait after DOMContentLoaded (gRPC streams keep the network busy forever)
    body_timeout_s: float = 8.0
    max_payload_bytes: int = 15_000_000
    cs2_pattern: str = DEFAULT_CS2_PATTERN
    event_link_pattern: str = r"-m(\d+)(?=$|[/?#])"
    # fallback URL when an event id is only known from a payload (Betclic redirects on the -m<id>)
    event_url_template: str = "{base_url}{listing_path}/wydarzenie-c0/wydarzenie-m{event_id}"
    # --- markets beyond the default "Top" tab -------------------------------------------------------
    fetch_categories: bool = True
    max_categories_per_event: int = 20
    min_category_delay_s: float = 0.6
    max_category_delay_s: float = 1.6
    click_category_tabs: bool = True
    category_tab_selectors: tuple[str, ...] = (
        "[role='tab']",
        "[data-qa*='category'] button",
        "[class*='categor'] button",
    )
    max_tab_clicks: int = 15
    consent_selectors: tuple[str, ...] = (
        "#didomi-notice-agree-button",
        "#onetrust-accept-btn-handler",
        "button:has-text('Akceptuj')",
        "button:has-text('Zaakceptuj')",
        "button:has-text('Zgadzam')",
    )
    # --- network capture ------------------------------------------------------------------------
    grpc_base_url: str = DEFAULT_GRPC_BASE_URL
    grpc_url_markers: tuple[str, ...] = ("offering.access.api", "grpc")
    grpc_max_frames: int = 1
    grpc_read_timeout_s: float = 6.0
    ignore_url_patterns: tuple[str, ...] = DEFAULT_IGNORED_URLS
    user_agent: str | None = None
    viewport_width: int = 1366
    viewport_height: int = 900
    # --- raw payload archive --------------------------------------------------------------------
    save_raw: bool = True
    save_html: bool = True
    raw_retention_days: int = 14
    # --- direct API source (BetclicApiSource) ------------------------------------------------------
    api_protocol: Literal["auto", "rest", "grpc"] = "auto"
    api_sport_codes: tuple[str, ...] = ("counter_strike",)  # gRPC sport codes - UNVERIFIED for CS2
    api_sport_ids: tuple[str, ...] = ("102",)  # REST "sportIds" (102 = e-sport in the PL listing path)
    api_application: str = "2"
    api_page_size: int = 40
    api_max_pages: int = 5
    api_list_template: str = (
        "{base}/api/pub/v4/events?application={application}&countrycode={country}&language={language}"
        "&sitecode={sitecode}&sportIds={sport_id}&limit={limit}&offset={offset}"
        "&sortBy=ByLiveRankingPreliveDate&fetchMultipleDefaultMarkets=true"
    )
    api_event_template: str = (
        "{base}/api/pub/v5/events/{event_id}?application={application}&countrycode={country}"
        "&language={language}&sitecode={sitecode}"
    )
    api_min_delay_s: float = 1.0
    api_max_delay_s: float = 2.5
    api_timeout_s: float = 20.0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> BetclicSourceConfig:
        """Defaults overridden by ``data``; unknown keys and values of the wrong type are ignored
        with a warning (a typo in the YAML must not crash a crawl half-way through)."""
        defaults = cls()
        known = {f.name for f in fields(cls)}
        values: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                logger.warning("ignoring unknown Betclic source option %r", key)
                continue
            try:
                values[key] = _coerce(getattr(defaults, key), value)
            except (TypeError, ValueError):
                logger.warning("ignoring Betclic source option %r: bad value %r", key, value)
        return replace(defaults, **values)

    @classmethod
    def load(cls, settings: Settings) -> BetclicSourceConfig:
        """Defaults overridden by the ``source:`` section of the market rules file (if any)."""
        return cls.from_yaml(settings.betclic.market_rules_file)

    @classmethod
    def from_yaml(cls, path: str | Path) -> BetclicSourceConfig:
        resolved = resolve_path(path)
        if not resolved.is_file():
            return cls()
        try:
            with resolved.open(encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError):
            logger.warning("could not read %s; using default Betclic source options", resolved, exc_info=True)
            return cls()
        section = data.get("source") if isinstance(data, Mapping) else None
        return cls.from_mapping(section) if isinstance(section, Mapping) else cls()
