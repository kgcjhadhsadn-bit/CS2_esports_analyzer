"""Polite HLTV page fetcher: disk cache, jittered rate limit, real browser, back-off on challenges.

HLTV is scraped from the owner's home connection, so the fetcher is deliberately gentle:

* every page is cached on disk (``<cache_dir>/<aa>/<sha256>.html.gz`` + ``.json`` metadata). The default
  TTL is ``cache_ttl_minutes``; finished match pages, map stats, news articles and past rankings use
  ``finished_match_cache_ttl_minutes``. Cache hits never touch the network or the rate limiter;
* real network fetches go through :class:`cs2bot.utils.http.RateLimiter` (jittered
  ``min_delay_s``..``max_delay_s`` gap);
* ``fetch_mode="browser"`` drives one reused Playwright Chromium context (headless per settings,
  ``executable_path=settings.chromium_executable``, proxy from ``settings.proxy_url``);
  ``fetch_mode="http"`` uses httpx;
* Cloudflare challenge / block pages are detected. In browser mode the fetcher waits up to
  ``challenge_wait_s`` for the page to clear *by itself*. If it does not, :class:`HLTVBlockedError` is
  raised and an exponential cool-down (persisted in ``<cache_dir>/_cooldown.json``) blocks further
  network fetches. There is deliberately no challenge solving, CAPTCHA handling or fingerprint
  spoofing / stealth tooling.
* Proxy fallback (deep_data, for a VPS whose datacenter IP gets challenged): with ``proxy_url`` set and
  ``proxy_mode="after_block"`` (default) pages are fetched directly; when the direct route hits a
  challenge, or its cool-down is active, the same page is retried once through the proxy. The proxy
  route has its own cool-down (``proxy_cooldown_base_s`` .. ``proxy_cooldown_max_s``, persisted in
  ``<cache_dir>/_cooldown_proxy.json``) and its own browser profile (``<profile>-proxy``), so a block
  on one route never hides the other. ``proxy_mode="always"`` sends every fetch through the proxy (one
  route, the classic behaviour). Challenges are never solved on either route.

Not thread-safe across threads for the browser mode (Playwright's sync API is bound to the thread that
started it); use one fetcher per worker thread, outside any running asyncio loop.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self
from urllib.parse import unquote, urlsplit

from cs2bot.config import HLTVSettings
from cs2bot.ingest.hltv.errors import HLTVBlockedError, HLTVError, HLTVFetchError, HLTVNotFoundError
from cs2bot.ingest.hltv.selectors import (
    BLOCK_PAGE_MAX_BYTES,
    BLOCK_PAGE_PATTERNS,
    CHALLENGE_PATTERNS,
    CHALLENGE_TITLE_RE,
    DELETED_MATCH_HTML_RE,
    DEMO_LINK_HTML_RE,
    FINISHED_MATCH_HTML_RE,
    MATCH_HEADER_UNIX_RE,
)
from cs2bot.ingest.hltv.urls import UrlKind, cache_key, classify_url, is_immutable_url
from cs2bot.utils.http import RateLimiter, build_http_client
from cs2bot.utils.paths import resolve_path
from cs2bot.utils.time import parse_datetime, utcnow

logger = logging.getLogger(__name__)

COOLDOWN_FILE = "_cooldown.json"
PROXY_COOLDOWN_FILE = "_cooldown_proxy.json"


@dataclass(frozen=True)
class LoadedPage:
    html: str
    status: int | None = None
    final_url: str | None = None


class PageLoader(Protocol):
    """Anything that loads one URL. Plain ``(url) -> str`` callables are accepted too."""

    def __call__(self, url: str) -> LoadedPage | str: ...


class SupportsWait(Protocol):
    def wait(self) -> float: ...


@dataclass
class FetchOptions:
    """Resolved fetcher knobs.

    :class:`cs2bot.config.HLTVSettings` is the source of truth for ``challenge_wait_s``,
    ``cooldown_base_s``, ``cooldown_max_s``, ``block_resources`` and ``browser_profile_dir``: build the
    options with :meth:`from_settings` (what :class:`HLTVFetcher` does when ``options`` is None). The
    remaining fields are not settings (polling interval, transport retries, locale). Constructing
    ``FetchOptions(...)`` directly is meant for tests / special callers; its defaults mirror the settings
    defaults except ``browser_profile_dir`` (None = a throw-away browser context).
    """

    challenge_wait_s: float = 20.0  # browser mode: how long a challenge may take to clear by itself
    challenge_poll_s: float = 1.0
    cooldown_base_s: float = 15 * 60.0  # first cool-down after a block; doubles on each repeat
    cooldown_max_s: float = 12 * 3600.0
    transport_retries: int = 1  # retries for connection errors only (never for 4xx / challenges)
    transport_retry_delay_s: float = 10.0
    block_resources: bool = True  # browser: skip images / media / fonts (less traffic for HLTV and us)
    browser_profile_dir: Path | None = None  # browser: persistent profile (keeps cookies between runs)
    locale: str = "en-US"
    # A "Match over" page only gets the long TTL once it is final: the GOTV demo link is shown, or the
    # match started at least this long before the page was fetched (demo / stats links lag the result).
    finished_match_settle_hours: float = 48.0

    @classmethod
    def from_settings(cls, settings: HLTVSettings, **overrides: Any) -> FetchOptions:
        """Options taken from ``settings``; ``overrides`` replace individual fields (non-settings knobs)."""
        values: dict[str, Any] = {
            "challenge_wait_s": max(0.0, float(settings.challenge_wait_s)),
            "cooldown_base_s": max(0.0, float(settings.cooldown_base_s)),
            "cooldown_max_s": max(0.0, float(settings.cooldown_max_s)),
            "block_resources": settings.block_resources,
            "browser_profile_dir": settings.browser_profile_dir,
        }
        values.update(overrides)
        return cls(**values)


@dataclass
class FetchStats:
    network_fetches: int = 0
    cache_hits: int = 0
    blocked: int = 0
    errors: int = 0
    slept_s: float = 0.0
    last_url: str | None = None
    by_kind: dict[str, int] = field(default_factory=dict)


def detect_challenge(html: str) -> str | None:
    """Return why ``html`` looks like a Cloudflare challenge / block page, else ``None``.

    Normal HLTV pages also embed Cloudflare's bot-detection script (``challenge-platform/scripts/jsd``),
    so only interstitial-specific markers count: the "Just a moment..." / "Attention Required!" titles,
    ``cf-chl`` tokens, the challenge orchestrator path, the challenge form and the block-page texts.
    """
    if not html:
        return None
    head = html[:200_000]
    title = CHALLENGE_TITLE_RE.search(head)
    if title:
        return f"title:{title.group('title').strip()}"
    for name, pattern in CHALLENGE_PATTERNS:
        if pattern.search(head):
            return name
    if len(html) <= BLOCK_PAGE_MAX_BYTES:
        for name, pattern in BLOCK_PAGE_PATTERNS:
            if pattern.search(html):
                return name
    return None


# ============================================================================================ cache


@dataclass(frozen=True)
class CachedPage:
    url: str
    html: str
    fetched_at: datetime
    status: int | None = None


class DiskCache:
    """One gzip-compressed HTML file + one JSON metadata file per URL (sha256 of the canonical URL).

    HLTV pages are large (hundreds of KB), so a multi-year backfill would otherwise use many GB;
    gzip shrinks them ~85%. Plain ``.html`` entries are still read (older cache layout).
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def _paths(self, key: str) -> tuple[Path, Path, Path]:
        folder = self.root / key[:2]
        return folder / f"{key}.html.gz", folder / f"{key}.html", folder / f"{key}.json"

    def read(self, key: str) -> CachedPage | None:
        gz_path, html_path, meta_path = self._paths(key)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if gz_path.exists():
                html = gzip.decompress(gz_path.read_bytes()).decode("utf-8")
            else:
                html = html_path.read_text(encoding="utf-8")
            fetched_at = parse_datetime(meta["fetched_at"])
        except (OSError, ValueError, KeyError, TypeError, EOFError, gzip.BadGzipFile):
            return None
        if fetched_at is None:
            return None
        return CachedPage(
            url=str(meta.get("url", "")), html=html, fetched_at=fetched_at, status=meta.get("status")
        )

    def write(self, key: str, page: CachedPage) -> None:
        gz_path, html_path, meta_path = self._paths(key)
        gz_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(gz_path, gzip.compress(page.html.encode("utf-8"), compresslevel=6))
        html_path.unlink(missing_ok=True)
        meta = {"url": page.url, "fetched_at": page.fetched_at.isoformat(), "status": page.status}
        _atomic_write(meta_path, json.dumps(meta))

    def delete(self, key: str) -> None:
        for path in self._paths(key):
            path.unlink(missing_ok=True)

    def prune(self, cutoff: datetime) -> int:
        """Delete entries fetched before ``cutoff``; returns how many were removed."""
        removed = 0
        if not self.root.is_dir():
            return 0
        for meta_path in self.root.glob("*/*.json"):
            try:
                fetched_at = parse_datetime(json.loads(meta_path.read_text(encoding="utf-8"))["fetched_at"])
            except (OSError, ValueError, KeyError, TypeError):
                fetched_at = None
            if fetched_at is None or fetched_at < cutoff:
                self.delete(meta_path.stem)
                removed += 1
        return removed


def _atomic_write(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ============================================================================================ loaders


def playwright_proxy(proxy_url: str) -> dict[str, str]:
    """``http://user:pass@host:port`` -> Playwright proxy dict (credentials passed separately)."""
    parts = urlsplit(proxy_url)
    if not parts.hostname:
        raise ValueError("proxy URL has no host")
    server = f"{parts.scheme or 'http'}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    proxy = {"server": server}
    if parts.username:
        proxy["username"] = unquote(parts.username)
    if parts.password:
        proxy["password"] = unquote(parts.password)
    return proxy


def browser_launch_kwargs(settings: HLTVSettings, *, headless: bool) -> dict[str, Any]:
    """Chromium launch arguments from the settings (executable path and proxy are settings, never fixed)."""
    kwargs: dict[str, Any] = {"headless": headless}
    if settings.chromium_executable:
        kwargs["executable_path"] = settings.chromium_executable
    if settings.proxy_url is not None:
        kwargs["proxy"] = playwright_proxy(settings.proxy_url.get_secret_value())
    return kwargs


def browser_context_kwargs(settings: HLTVSettings, *, locale: str = "en-US") -> dict[str, Any]:
    kwargs: dict[str, Any] = {"locale": locale}
    if settings.user_agent:
        kwargs["user_agent"] = settings.user_agent
    return kwargs


class ChallengePage(Protocol):
    """The subset of a Playwright ``Page`` used while waiting for a challenge to clear."""

    def content(self) -> str: ...

    def wait_for_timeout(self, timeout: float) -> None: ...


def wait_for_challenge_to_clear(
    page: ChallengePage,
    html: str,
    *,
    timeout_s: float,
    poll_s: float,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str, bool]:
    """Re-read the page until it no longer looks like a challenge or ``timeout_s`` passes.

    Only waits - it never interacts with the page. Returns ``(html, cleared)``.
    """
    if detect_challenge(html) is None:
        return html, True
    deadline = clock() + timeout_s
    current = html
    while clock() < deadline:
        page.wait_for_timeout(poll_s * 1000.0)
        try:
            current = page.content()
        except Exception:  # navigation in progress (challenge redirect): retry on the next poll
            continue
        if detect_challenge(current) is None:
            return current, True
    return current, False


class BrowserPageLoader:
    """Playwright (sync API) loader: one browser + context + page, started lazily and reused."""

    _BLOCKED_RESOURCES = frozenset({"image", "media", "font"})

    def __init__(
        self,
        settings: HLTVSettings,
        options: FetchOptions | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.options = options or FetchOptions.from_settings(settings)
        self._clock = clock
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    def _launch_kwargs(self) -> dict[str, Any]:
        return browser_launch_kwargs(self.settings, headless=self.settings.headless)

    def _context_kwargs(self) -> dict[str, Any]:
        return browser_context_kwargs(self.settings, locale=self.options.locale)

    def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright

        try:
            self._playwright = sync_playwright().start()
            chromium = self._playwright.chromium
            if self.options.browser_profile_dir is not None:
                profile = resolve_path(self.options.browser_profile_dir)
                profile.mkdir(parents=True, exist_ok=True)
                self._context = chromium.launch_persistent_context(
                    str(profile), **self._launch_kwargs(), **self._context_kwargs()
                )
            else:
                self._browser = chromium.launch(**self._launch_kwargs())
                self._context = self._browser.new_context(**self._context_kwargs())
            timeout_ms = self.settings.navigation_timeout_s * 1000.0
            self._context.set_default_navigation_timeout(timeout_ms)
            self._context.set_default_timeout(timeout_ms)
            if self.options.block_resources:
                # Images / media / fonts are not needed to read the HTML (less traffic for HLTV and
                # the owner's connection). Note: Playwright disables its HTTP cache while routing.
                self._context.route("**/*", self._route)
            pages = list(self._context.pages)
            self._page = pages[0] if pages else self._context.new_page()
        except BaseException:
            self.close()  # never leak a half-started driver / browser process
            raise
        return self._page

    def _route(self, route: Any) -> None:
        if route.request.resource_type in self._BLOCKED_RESOURCES:
            route.abort()
        else:
            route.continue_()

    def __call__(self, url: str) -> LoadedPage:
        page = self._ensure_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded")
        except Exception:
            # timeouts or a crashed / disconnected browser: start fresh on the next call
            self.close()
            raise
        status = response.status if response is not None else None
        html = self._content(page)
        if detect_challenge(html) is not None:
            logger.info(
                "HLTV challenge page shown for %s; waiting up to %.0f s for it to clear",
                url,
                self.options.challenge_wait_s,
            )
            html, cleared = wait_for_challenge_to_clear(
                page,
                html,
                timeout_s=self.options.challenge_wait_s,
                poll_s=self.options.challenge_poll_s,
                clock=self._clock,
            )
            if cleared:
                status = 200
        return LoadedPage(html=html, status=status, final_url=page.url)

    def _content(self, page: Any) -> str:
        for _ in range(5):
            try:
                return str(page.content())
            except Exception:  # navigation still settling ("execution context was destroyed")
                page.wait_for_timeout(500)
        return str(page.content())

    def close(self) -> None:
        for name in ("_page", "_context", "_browser"):
            obj = getattr(self, name)
            setattr(self, name, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception as exc:  # already closed / browser crashed
                    logger.debug("closing playwright %s failed: %s", name, exc)
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:
                logger.debug("stopping playwright failed: %s", exc)
            self._playwright = None


def direct_settings(settings: HLTVSettings) -> HLTVSettings:
    """``settings`` without the proxy (the direct route of ``proxy_mode="after_block"``)."""
    return settings.model_copy(update={"proxy_url": None})


def proxy_profile_dir(profile: Path | None) -> Path | None:
    """Separate persistent profile for the proxy route (Chromium cannot open one profile twice, and
    cookies earned through the proxy IP should not be replayed from the direct IP)."""
    if profile is None:
        return None
    return profile.with_name(f"{profile.name}-proxy")


class HttpPageLoader:
    """httpx loader (no JavaScript: Cloudflare challenges cannot clear in this mode)."""

    def __init__(self, settings: HLTVSettings, *, client: Any = None) -> None:
        proxy = settings.proxy_url.get_secret_value() if settings.proxy_url is not None else None
        self._client = client or build_http_client(
            timeout_s=settings.navigation_timeout_s,
            proxy=proxy,
            user_agent=settings.user_agent,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )

    def __call__(self, url: str) -> LoadedPage:
        response = self._client.get(url)
        return LoadedPage(html=response.text, status=response.status_code, final_url=str(response.url))

    def close(self) -> None:
        self._client.close()


# ============================================================================================ fetcher


def _match_start(html: str) -> datetime | None:
    """Start time from a match page header (``data-unix`` in ms) without a full parse."""
    m = MATCH_HEADER_UNIX_RE.search(html[:300_000])
    if not m:
        return None
    try:
        return parse_datetime(int(m.group(1) or m.group(2)))
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def _bare_host(netloc: str) -> str:
    host = netloc.lower().rsplit("@", 1)[-1]
    return host[4:] if host.startswith("www.") else host


def hltv_url(base_url: str, path_or_url: str) -> str:
    """Absolute URL of ``path_or_url`` on the configured HLTV host.

    Absolute URLs (and scheme-relative ``//host/...``) must point at the same site as ``base_url``
    (``hltv.org`` and ``www.hltv.org`` are the same site) and are rewritten onto ``base_url``; any other
    host raises :class:`ValueError`, so a link scraped from a page can never make the bot fetch
    elsewhere. Fragments are dropped.
    """
    base = base_url.rstrip("/")
    value = path_or_url.strip()
    if value.lower().startswith(("http://", "https://", "//")):
        parts = urlsplit(value)
        if not parts.netloc or _bare_host(parts.netloc) != _bare_host(urlsplit(base).netloc):
            raise ValueError(f"refusing to fetch a non-HLTV URL: {value!r}")
        value = parts.path or "/"
        if parts.query:
            value += f"?{parts.query}"
    value = value.split("#", 1)[0]
    if not value.startswith("/"):
        value = "/" + value
    return base + value


class HLTVFetcher:
    """``get(path_or_url) -> html`` with disk cache, rate limit and challenge back-off.

    ``page_loader`` replaces the real browser / httpx loader (tests, custom transports); ``clock`` is a
    monotonic clock for the rate limiter, ``now`` the wall clock for cache ages and cool-downs, and
    ``sleep`` is used for every wait the fetcher itself performs.
    """

    def __init__(
        self,
        settings: HLTVSettings,
        *,
        page_loader: PageLoader | Callable[[str], LoadedPage | str] | None = None,
        rate_limiter: SupportsWait | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = utcnow,
        options: FetchOptions | None = None,
        rng: random.Random | None = None,
        proxy_page_loader: PageLoader | Callable[[str], LoadedPage | str] | None = None,
    ) -> None:
        """``proxy_page_loader`` replaces the loader of the proxy route (tests); it is only used when
        ``settings.proxy_url`` is set and ``settings.proxy_mode == "after_block"``."""
        self.settings = settings
        self.options = options or FetchOptions.from_settings(settings)
        self.base_url = settings.base_url.rstrip("/")
        self.cache_dir = resolve_path(settings.cache_dir)
        self.stats = FetchStats()
        self._cache = DiskCache(self.cache_dir)
        self._loader = page_loader
        self._owns_loader = page_loader is None
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._limiter: SupportsWait = rate_limiter or RateLimiter(
            max(0.0, settings.min_delay_s), max(0.0, settings.max_delay_s), clock=clock, sleep=sleep, rng=rng
        )
        self._lock = threading.RLock()
        self._blocked_until: datetime | None = None
        self._consecutive_blocks = 0
        self._block_reason: str | None = None
        self._proxy_loader = proxy_page_loader
        self._owns_proxy_loader = proxy_page_loader is None
        self._proxy_blocked_until: datetime | None = None
        self._proxy_consecutive_blocks = 0
        self._proxy_block_reason: str | None = None
        self._proxy_notified = False
        self._load_cooldown()
        self._load_proxy_cooldown()

    @property
    def proxy_fallback_enabled(self) -> bool:
        """True when a blocked direct fetch is retried through ``settings.proxy_url``."""
        return self.settings.proxy_url is not None and self.settings.proxy_mode == "after_block"

    # ------------------------------------------------------------------ public API
    def url_for(self, path_or_url: str) -> str:
        """Absolute URL on the configured HLTV host (other hosts are refused)."""
        return hltv_url(self.base_url, path_or_url)

    def get(self, path_or_url: str, *, max_age_minutes: int | None = None) -> str:
        """HTML of a page: from the cache when fresh enough, otherwise from the network.

        ``max_age_minutes`` overrides the TTL (``0`` forces a refresh). Raises
        :class:`HLTVBlockedError` (challenge / cool-down), :class:`HLTVNotFoundError` (404/410) or
        :class:`HLTVFetchError` (other failures).
        """
        url = self.url_for(path_or_url)
        key = cache_key(url)
        with self._lock:
            cached = self._cache.read(key)
            if cached is not None and self._is_fresh(url, cached, max_age_minutes):
                self.stats.cache_hits += 1
                return cached.html
            loaded = self._fetch_network(url)
            try:
                self._cache.write(
                    key, CachedPage(url=url, html=loaded.html, fetched_at=self._now(), status=loaded.status)
                )
            except OSError as exc:  # a full / read-only disk must not lose the page we just fetched
                logger.warning("could not write HLTV cache entry for %s: %s", url, exc)
            return loaded.html

    def cached(self, path_or_url: str) -> CachedPage | None:
        """The cached copy (any age) without touching the network."""
        return self._cache.read(cache_key(self.url_for(path_or_url)))

    def invalidate(self, path_or_url: str) -> None:
        self._cache.delete(cache_key(self.url_for(path_or_url)))

    def prune_cache(self, older_than: timedelta | None = None) -> int:
        """Delete cache entries older than ``older_than`` (default: the long, finished-match TTL).

        Meant for a periodic maintenance job; returns the number of removed pages.
        """
        age = older_than or timedelta(minutes=self.settings.finished_match_cache_ttl_minutes)
        with self._lock:
            return self._cache.prune(self._now() - age)

    @property
    def blocked_until(self) -> datetime | None:
        """End of the direct route's cool-down (the only route unless the proxy fallback is on)."""
        return self._blocked_until

    @property
    def proxy_blocked_until(self) -> datetime | None:
        """End of the proxy route's own cool-down (``proxy_mode="after_block"`` only)."""
        return self._proxy_blocked_until

    def close(self) -> None:
        loader, self._loader = self._loader, (None if self._owns_loader else self._loader)
        if self._owns_loader and loader is not None:
            close = getattr(loader, "close", None)
            if callable(close):
                close()
        proxy, self._proxy_loader = (
            self._proxy_loader,
            (None if self._owns_proxy_loader else self._proxy_loader),
        )
        if self._owns_proxy_loader and proxy is not None:
            close = getattr(proxy, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    # ------------------------------------------------------------------ cache policy
    def ttl_minutes(
        self, url: str, cached_html: str | None = None, fetched_at: datetime | None = None
    ) -> int:
        """Default TTL for ``url``: long for content that no longer changes, short otherwise.

        A finished match page ("Match over") is only final once HLTV has added the GOTV demo link
        (often an hour or more after the result) or the match started at least
        ``FetchOptions.finished_match_settle_hours`` before the page was fetched (``fetched_at``,
        default now); until then it keeps the normal TTL so the demo / map stats links are picked up.
        "Match deleted" pages are final at once.
        """
        long_ttl = self.settings.finished_match_cache_ttl_minutes
        if is_immutable_url(url, today=self._now().date()):
            return long_ttl
        if classify_url(url) is UrlKind.MATCH and cached_html and FINISHED_MATCH_HTML_RE.search(cached_html):
            if DELETED_MATCH_HTML_RE.search(cached_html) or DEMO_LINK_HTML_RE.search(cached_html):
                return long_ttl
            started = _match_start(cached_html)
            seen = fetched_at or self._now()
            if started is not None and seen - started >= timedelta(
                hours=self.options.finished_match_settle_hours
            ):
                return long_ttl
        return self.settings.cache_ttl_minutes

    def _is_fresh(self, url: str, cached: CachedPage, max_age_minutes: int | None) -> bool:
        ttl = (
            max_age_minutes
            if max_age_minutes is not None
            else self.ttl_minutes(url, cached.html, cached.fetched_at)
        )
        if ttl <= 0:
            return False
        age = self._now() - cached.fetched_at
        if age < timedelta(minutes=-5):  # clock went backwards a lot: do not trust the entry
            return False
        return age <= timedelta(minutes=ttl)

    # ------------------------------------------------------------------ network
    def _ensure_loader(self) -> PageLoader | Callable[[str], LoadedPage | str]:
        if self._loader is None:
            # with the after-block fallback the primary route is direct; "always" keeps the proxy in it
            settings = direct_settings(self.settings) if self.proxy_fallback_enabled else self.settings
            if settings.fetch_mode == "browser":
                self._loader = BrowserPageLoader(settings, self.options, clock=self._clock)
            else:
                self._loader = HttpPageLoader(settings)
        return self._loader

    def _ensure_proxy_loader(self) -> PageLoader | Callable[[str], LoadedPage | str]:
        if self._proxy_loader is None:
            if self.settings.fetch_mode == "browser":
                options = FetchOptions(**self.options.__dict__)
                options.browser_profile_dir = proxy_profile_dir(self.options.browser_profile_dir)
                self._proxy_loader = BrowserPageLoader(self.settings, options, clock=self._clock)
            else:
                self._proxy_loader = HttpPageLoader(self.settings)
        return self._proxy_loader

    def _call_loader(self, url: str, *, via_proxy: bool = False) -> LoadedPage:
        loader = self._ensure_proxy_loader() if via_proxy else self._ensure_loader()
        attempts = max(0, self.options.transport_retries) + 1
        for attempt in range(1, attempts + 1):
            try:
                result = loader(url)
            except HLTVError:
                raise
            except Exception as exc:  # transport / browser errors
                if attempt >= attempts:
                    self.stats.errors += 1
                    raise HLTVFetchError(
                        f"fetching {url} failed: {type(exc).__name__}: {exc}", url=url
                    ) from exc
                logger.warning(
                    "HLTV fetch of %s failed (%s); retrying once in %.0f s",
                    url,
                    type(exc).__name__,
                    self.options.transport_retry_delay_s,
                )
                self._sleep(self.options.transport_retry_delay_s)
                continue
            return result if isinstance(result, LoadedPage) else LoadedPage(html=str(result))
        raise HLTVFetchError(f"fetching {url} failed", url=url)  # pragma: no cover - loop always returns

    def _fetch_network(self, url: str) -> LoadedPage:
        if not self.proxy_fallback_enabled:
            return self._fetch_route(url, via_proxy=False)
        try:
            return self._fetch_route(url, via_proxy=False)
        except HLTVBlockedError as exc:
            if not self._proxy_notified:
                logger.warning(
                    "HLTV direct route blocked (%s): falling back to the configured proxy", exc.reason
                )
                self._proxy_notified = True
            else:
                logger.info("HLTV direct route blocked (%s); fetching %s through the proxy", exc.reason, url)
            return self._fetch_route(url, via_proxy=True)

    def _fetch_route(self, url: str, *, via_proxy: bool) -> LoadedPage:
        if via_proxy:
            self._check_proxy_cooldown(url)
        else:
            self._check_cooldown(url)
        slept = self._limiter.wait()
        self.stats.slept_s += slept
        self.stats.network_fetches += 1
        self.stats.last_url = url
        kind = classify_url(url).value
        self.stats.by_kind[kind] = self.stats.by_kind.get(kind, 0) + 1
        logger.info("HLTV fetch %s%s", url, " (via proxy)" if via_proxy else "")
        loaded = self._call_loader(url, via_proxy=via_proxy)
        status = loaded.status
        reason = detect_challenge(loaded.html)
        if reason is None and status in (403, 429):
            reason = f"http {status}"
        if reason is not None:
            if via_proxy:
                self._register_proxy_block(url, reason, status)
            else:
                self._register_block(url, reason, status)
        if status in (404, 410):
            raise HLTVNotFoundError(f"HLTV page not found: {url}", url=url, status=status)
        if status is not None and status >= 400:
            self.stats.errors += 1
            raise HLTVFetchError(f"HLTV answered HTTP {status} for {url}", url=url, status=status)
        if via_proxy:
            self._register_proxy_success()
        else:
            self._register_success()
        return loaded

    # ------------------------------------------------------------------ cool-down
    def _cooldown_path(self) -> Path:
        return self.cache_dir / COOLDOWN_FILE

    def _load_cooldown(self) -> None:
        try:
            data = json.loads(self._cooldown_path().read_text(encoding="utf-8"))
            self._blocked_until = parse_datetime(data.get("blocked_until"))
            self._consecutive_blocks = int(data.get("consecutive_blocks", 0))
            self._block_reason = data.get("reason")
        except (OSError, ValueError, TypeError, AttributeError):  # corrupt file: start clean
            self._blocked_until, self._consecutive_blocks = None, 0

    def _save_cooldown(self) -> None:
        """Persist the cool-down; a disk problem only loses the persistence, never the fetch result."""
        path = self._cooldown_path()
        try:
            if self._blocked_until is None and self._consecutive_blocks == 0:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(
                path,
                json.dumps(
                    {
                        "blocked_until": self._blocked_until.isoformat() if self._blocked_until else None,
                        "consecutive_blocks": self._consecutive_blocks,
                        "reason": self._block_reason,
                    }
                ),
            )
        except OSError as exc:
            logger.warning("could not persist the HLTV cool-down in %s: %s", path, exc)

    def _check_cooldown(self, url: str) -> None:
        if self._blocked_until is None:
            return
        remaining = (self._blocked_until - self._now()).total_seconds()
        if remaining > 0:
            raise HLTVBlockedError(
                f"HLTV cool-down active for another {remaining:.0f} s "
                f"(last block: {self._block_reason}); not fetching {url}",
                url=url,
                reason="cooldown",
                retry_after_s=remaining,
            )

    def _register_block(self, url: str, reason: str, status: int | None) -> None:
        self._consecutive_blocks += 1
        exponent = min(max(0, self._consecutive_blocks - 1), 40)  # a corrupt counter must not overflow
        cooldown = min(self.options.cooldown_base_s * 2.0**exponent, self.options.cooldown_max_s)
        self._blocked_until = self._now() + timedelta(seconds=cooldown)
        self._block_reason = reason
        self.stats.blocked += 1
        self._save_cooldown()
        logger.warning(
            "HLTV block/challenge detected (%s) for %s; pausing HLTV fetches for %.0f min (block #%d)",
            reason,
            url,
            cooldown / 60.0,
            self._consecutive_blocks,
        )
        raise HLTVBlockedError(
            f"HLTV returned a challenge/block page ({reason}) for {url}",
            url=url,
            status=status,
            reason=reason,
            retry_after_s=cooldown,
        )

    def _register_success(self) -> None:
        if self._consecutive_blocks or self._blocked_until is not None:
            self._consecutive_blocks = 0
            self._blocked_until = None
            self._block_reason = None
            self._save_cooldown()

    # ------------------------------------------------------------------ proxy route cool-down
    def _proxy_cooldown_path(self) -> Path:
        return self.cache_dir / PROXY_COOLDOWN_FILE

    def _load_proxy_cooldown(self) -> None:
        try:
            data = json.loads(self._proxy_cooldown_path().read_text(encoding="utf-8"))
            self._proxy_blocked_until = parse_datetime(data.get("blocked_until"))
            self._proxy_consecutive_blocks = int(data.get("consecutive_blocks", 0))
            self._proxy_block_reason = data.get("reason")
        except (OSError, ValueError, TypeError, AttributeError):  # missing / corrupt file: start clean
            self._proxy_blocked_until, self._proxy_consecutive_blocks = None, 0

    def _save_proxy_cooldown(self) -> None:
        path = self._proxy_cooldown_path()
        try:
            if self._proxy_blocked_until is None and self._proxy_consecutive_blocks == 0:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            until = self._proxy_blocked_until
            _atomic_write(
                path,
                json.dumps(
                    {
                        "blocked_until": until.isoformat() if until else None,
                        "consecutive_blocks": self._proxy_consecutive_blocks,
                        "reason": self._proxy_block_reason,
                    }
                ),
            )
        except OSError as exc:
            logger.warning("could not persist the HLTV proxy cool-down in %s: %s", path, exc)

    def _check_proxy_cooldown(self, url: str) -> None:
        if self._proxy_blocked_until is None:
            return
        remaining = (self._proxy_blocked_until - self._now()).total_seconds()
        if remaining > 0:
            direct = (self._blocked_until - self._now()).total_seconds() if self._blocked_until else 0.0
            raise HLTVBlockedError(
                f"HLTV blocked on both routes: proxy cool-down active for another {remaining:.0f} s "
                f"(last proxy block: {self._proxy_block_reason}); not fetching {url}",
                url=url,
                reason="cooldown",
                retry_after_s=max(0.0, min(remaining, direct) if direct > 0 else remaining),
            )

    def _register_proxy_block(self, url: str, reason: str, status: int | None) -> None:
        self._proxy_consecutive_blocks += 1
        exponent = min(max(0, self._proxy_consecutive_blocks - 1), 40)
        base = max(0.0, float(self.settings.proxy_cooldown_base_s))
        cooldown = min(base * 2.0**exponent, max(0.0, float(self.settings.proxy_cooldown_max_s)))
        self._proxy_blocked_until = self._now() + timedelta(seconds=cooldown)
        self._proxy_block_reason = reason
        self.stats.blocked += 1
        self._save_proxy_cooldown()
        logger.warning(
            "HLTV block/challenge through the proxy too (%s) for %s; pausing proxy fetches for %.0f min "
            "(proxy block #%d)",
            reason,
            url,
            cooldown / 60.0,
            self._proxy_consecutive_blocks,
        )
        raise HLTVBlockedError(
            f"HLTV returned a challenge/block page through the proxy ({reason}) for {url}",
            url=url,
            status=status,
            reason=reason,
            retry_after_s=cooldown,
        )

    def _register_proxy_success(self) -> None:
        if self._proxy_consecutive_blocks or self._proxy_blocked_until is not None:
            self._proxy_consecutive_blocks = 0
            self._proxy_blocked_until = None
            self._proxy_block_reason = None
            self._save_proxy_cooldown()
