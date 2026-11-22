"""Browser lifecycle for the executor: Playwright + persistent Chromium profile + one working tab.

* ``launch_persistent_context`` with the dedicated profile (``settings.betclic.browser_profile_dir``),
  visible by default (``settings.executor.headless``), Polish locale and Warsaw timezone, optional proxy
  and Chromium path from settings. No stealth/fingerprint tricks.
* The profile is guarded by :class:`~cs2bot.executor.lock.ProfileLock` while the browser runs.
* Playwright's sync API is bound to the thread that started it: every call must come from that thread
  (:class:`~cs2bot.executor.base.ExecutorThreadError` otherwise). Use
  :class:`~cs2bot.executor.threaded.ThreadBoundExecutor` to share one executor between threads.
* If the owner closes the browser window, the next call relaunches it with the same profile.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from cs2bot.config import Settings
from cs2bot.executor.base import BrowserStartError, ExecutorThreadError
from cs2bot.executor.lock import ProfileLock
from cs2bot.executor.tuning import ExecutorTuning
from cs2bot.utils.logging import get_logger
from cs2bot.utils.paths import resolve_path

log = get_logger(__name__)

BROWSER_LOCALE = "pl-PL"
BROWSER_TIMEZONE = "Europe/Warsaw"

PlaywrightFactory = Callable[[], Playwright]


def default_playwright_factory() -> Playwright:
    return sync_playwright().start()


def parse_proxy_url(url: str) -> dict[str, str]:
    """``http://user:pass@host:3128`` -> Playwright proxy dict (credentials split out, never logged)."""
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.hostname:
        raise ValueError("invalid proxy URL (expected scheme://[user:password@]host[:port])")
    host = parts.hostname
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    server = f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")
    proxy = {"server": server}
    if parts.username:
        proxy["username"] = unquote(parts.username)
    if parts.password:
        proxy["password"] = unquote(parts.password)
    return proxy


def build_launch_options(settings: Settings, tuning: ExecutorTuning, profile_dir: Path) -> dict[str, Any]:
    """Keyword arguments for ``chromium.launch_persistent_context`` (pure; unit-tested)."""
    options: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": settings.executor.headless,
        "locale": BROWSER_LOCALE,
        "timezone_id": BROWSER_TIMEZONE,
        "viewport": {"width": tuning.viewport_width, "height": tuning.viewport_height},
        "accept_downloads": False,
    }
    if settings.betclic.chromium_executable:
        options["executable_path"] = settings.betclic.chromium_executable
    if settings.betclic.proxy_url is not None:
        options["proxy"] = parse_proxy_url(settings.betclic.proxy_url.get_secret_value())
    return options


class BrowserSession:
    """Owns Playwright, the persistent context, the working page and the profile lock."""

    def __init__(
        self,
        settings: Settings,
        *,
        tuning: ExecutorTuning,
        playwright_factory: PlaywrightFactory | None = None,
    ) -> None:
        self.settings = settings
        self.tuning = tuning
        self._factory = playwright_factory or default_playwright_factory
        self.profile_dir = resolve_path(settings.betclic.browser_profile_dir)
        self.lock = ProfileLock(
            self.profile_dir, stale_after_s=tuning.lock_stale_after_s, heartbeat_s=tuning.lock_heartbeat_s
        )
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._context_closed = True
        self._owner_thread: int | None = None

    # ------------------------------------------------------------------ state

    @property
    def is_running(self) -> bool:
        return self._context is not None and not self._context_closed

    def check_thread(self) -> None:
        owner = self._owner_thread
        if self._playwright is not None and owner is not None and owner != threading.get_ident():
            raise ExecutorThreadError(
                "the Betclic browser was started on another thread; Playwright's sync API must be used "
                "from that thread only (wrap the executor in ThreadBoundExecutor)"
            )

    def current_page(self) -> Page | None:
        """The working page if the browser is running and the tab is open; never launches anything."""
        if not self.is_running or self._page is None:
            return None
        try:
            return None if self._page.is_closed() else self._page
        except Exception:  # pragma: no cover - defensive (driver gone)
            return None

    def ensure_page(self) -> Page:
        """Return the working page, (re)launching the browser / reopening the tab when needed."""
        self.check_thread()
        page = self.current_page()
        if page is not None:
            return page
        if self.is_running and self._context is not None:
            try:
                self._page = self._new_page(self._context)
                return self._page
            except Exception as exc:
                log.warning("could not open a new tab (%s); restarting the browser", type(exc).__name__)
                self._close_context()
        self._start()
        assert self._page is not None
        return self._page

    # ------------------------------------------------------------------ lifecycle

    def _start(self) -> None:
        self.lock.acquire()  # raises ProfileLockedError
        try:
            if self._playwright is None:
                self._playwright = self._factory()
                self._owner_thread = threading.get_ident()
            options = build_launch_options(self.settings, self.tuning, self.profile_dir)
            log.info(
                "starting Chromium (profile %s, headless=%s, proxy=%s)",
                self.profile_dir,
                options["headless"],
                "yes" if "proxy" in options else "no",
            )
            context = self._playwright.chromium.launch_persistent_context(**options)
        except Exception as exc:
            self._stop_playwright()
            self.lock.release()
            raise BrowserStartError(
                f"could not start Chromium with profile {self.profile_dir}: {type(exc).__name__}: "
                f"{str(exc).strip().splitlines()[0] if str(exc).strip() else ''}"
            ) from exc
        self._context = context
        self._context_closed = False
        context.on("close", self._on_context_close)
        context.set_default_navigation_timeout(self.settings.executor.navigation_timeout_s * 1000)
        context.set_default_timeout(self.tuning.action_timeout_s * 1000)
        pages = [p for p in context.pages if not p.is_closed()]
        self._page = pages[0] if pages else self._new_page(context)

    def _new_page(self, context: BrowserContext) -> Page:
        page = context.new_page()
        page.set_default_navigation_timeout(self.settings.executor.navigation_timeout_s * 1000)
        page.set_default_timeout(self.tuning.action_timeout_s * 1000)
        return page

    def _on_context_close(self, _context: BrowserContext) -> None:
        if not self._context_closed:
            log.warning("the Betclic browser was closed (owner closed the window or Chromium exited)")
        self._context_closed = True

    def _close_context(self) -> None:
        context, self._context, self._page = self._context, None, None
        was_open = not self._context_closed
        self._context_closed = True  # before close(): our own close is not "the owner closed the window"
        if context is not None and was_open:
            try:
                context.close()
            except Exception as exc:
                log.warning("error while closing the browser: %s", type(exc).__name__)

    def _stop_playwright(self) -> None:
        playwright, self._playwright = self._playwright, None
        self._owner_thread = None
        if playwright is not None:
            with contextlib.suppress(Exception):
                playwright.stop()

    def close(self) -> None:
        """Close the browser, stop Playwright and release the profile lock (idempotent)."""
        try:
            self._close_context()
            self._stop_playwright()
        finally:
            self._owner_thread = None
            self.lock.release()
