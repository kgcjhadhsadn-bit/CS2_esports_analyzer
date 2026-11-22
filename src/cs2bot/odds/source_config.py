"""Tuning knobs of the Betclic odds sources that are not (yet) part of :class:`cs2bot.config.Settings`.

Defaults work out of the box; the owner can override any of them in the optional ``source:``
section of ``config/betclic_markets.yaml`` (``settings.betclic.market_rules_file``) without code
changes. Unknown keys are ignored with a warning.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Literal

import yaml

from cs2bot.config import Settings
from cs2bot.odds.grpc_web import DEFAULT_GRPC_BASE_URL
from cs2bot.utils.paths import resolve_path

logger = logging.getLogger(__name__)

DEFAULT_CS2_PATTERN = r"counter[\s\-_]*strike|\bcs\s*-?\s*2\b|\bcs:?\s*go\b|\bcsgo\b|\bcs2\b"

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
        known = {f.name: f for f in fields(cls)}
        values: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                logger.warning("ignoring unknown Betclic source option %r", key)
                continue
            if isinstance(value, list):
                value = tuple(value)
            values[key] = value
        return replace(cls(), **values)

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
