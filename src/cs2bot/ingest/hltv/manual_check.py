"""Open HLTV in a VISIBLE browser window with the bot's own profile (``cs2bot ingest hltv-browser``).

When Cloudflare keeps challenging the bot, the owner can pass the check once by hand: this opens
the fetcher's persistent Chromium profile (``HLTVSettings.browser_profile_dir``) in a normal window,
the owner completes whatever the page asks for and closes the window. The clearance cookies stay in
the profile, so the next headless :class:`~cs2bot.ingest.hltv.fetcher.HLTVFetcher` run reuses them.

Nothing here interacts with the challenge: the code only navigates once and then watches the page
(no clicking, no solving, no fingerprint changes). Assumptions:

* the profile directory can be opened by one browser at a time, so the bot (which holds the same
  profile while fetching) must be stopped first;
* Cloudflare binds its clearance cookie to the user agent. A visible Chromium and a headless one send
  different user agents unless ``HLTVSettings.user_agent`` is set, so set ``CS2BOT_HLTV__USER_AGENT``
  (or run the bot with ``CS2BOT_HLTV__HEADLESS=false``) for the cookie to keep working afterwards.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cs2bot.config import HLTVSettings, Settings
from cs2bot.ingest.hltv.fetcher import (
    COOLDOWN_FILE,
    browser_context_kwargs,
    browser_launch_kwargs,
    detect_challenge,
    hltv_url,
)
from cs2bot.utils.paths import resolve_path

logger = logging.getLogger(__name__)

DEFAULT_MANUAL_WAIT_S = 600.0


@dataclass(frozen=True)
class ManualCheckResult:
    """What happened while the window was open."""

    url: str
    profile_dir: Path
    cleared: bool  # the last page seen was a normal page (no challenge / block page)
    window_closed: bool  # the owner closed the window (False: ``wait_s`` ran out)
    waited_s: float
    cooldown_cleared: bool  # the fetcher's persisted cool-down was lifted (only when ``cleared``)
    last_challenge: str | None = None  # detect_challenge() reason of the last page seen, if any


def _start_playwright() -> Any:
    from playwright.sync_api import sync_playwright

    return sync_playwright().start()


def _page_closed(page: Any) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:  # the browser itself is gone
        return True


def open_browser_for_manual_check(
    settings: HLTVSettings | Settings,
    *,
    url: str | None = None,
    wait_s: float = DEFAULT_MANUAL_WAIT_S,
    poll_s: float = 1.0,
    clear_cooldown: bool = True,
    start_playwright: Callable[[], Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ManualCheckResult:
    """Open ``url`` (default: the HLTV front page) in a visible window using the bot's browser profile.

    Blocks until the owner closes the window or ``wait_s`` seconds pass, then closes the browser
    (which writes the profile's cookies to disk). When the last page seen was not a challenge and
    ``clear_cooldown`` is set, the fetcher's persisted cool-down (``<cache_dir>/_cooldown.json``) is
    removed so the bot resumes fetching immediately.

    ``settings`` may be the full :class:`~cs2bot.config.Settings` or its ``hltv`` section. Raises
    :class:`ValueError` when no persistent profile is configured (a throw-away profile would lose the
    cookies) or ``url`` is not an HLTV URL. ``start_playwright`` (returns a started Playwright object)
    and ``clock`` are injectable for tests.
    """
    if isinstance(settings, Settings):
        settings = settings.hltv
    if settings.browser_profile_dir is None:
        raise ValueError(
            "HLTVSettings.browser_profile_dir is not set: a manual check would not persist "
            "(set CS2BOT_HLTV__BROWSER_PROFILE_DIR)"
        )
    if wait_s <= 0:
        raise ValueError("wait_s must be positive")
    target = hltv_url(settings.base_url, url or "/")
    profile = resolve_path(settings.browser_profile_dir)
    profile.mkdir(parents=True, exist_ok=True)

    playwright = (start_playwright or _start_playwright)()
    context: Any = None
    started = clock()
    window_closed = False
    seen_page = False
    last_reason: str | None = None
    try:
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            **browser_launch_kwargs(settings, headless=False),
            **browser_context_kwargs(settings),
        )
        timeout_ms = settings.navigation_timeout_s * 1000.0
        context.set_default_navigation_timeout(timeout_ms)
        pages = list(context.pages)
        page = pages[0] if pages else context.new_page()
        logger.info(
            "HLTV manual check: opened %s with profile %s (close the window when done)", target, profile
        )
        try:
            page.goto(target, wait_until="domcontentloaded")
        except Exception as exc:  # slow challenge / network: the window stays open for the owner
            logger.warning("HLTV manual check: navigation to %s did not finish: %s", target, exc)
        deadline = started + wait_s
        while clock() < deadline:
            if _page_closed(page):
                window_closed = True
                break
            try:
                html = str(page.content())
            except Exception:  # navigating (challenge redirect) or closed: decided on the next poll
                html = None
            if html is not None:
                seen_page = True
                last_reason = detect_challenge(html)
            try:
                page.wait_for_timeout(max(0.05, poll_s) * 1000.0)
            except Exception:
                if _page_closed(page):
                    window_closed = True
                    break
    finally:
        if context is not None:
            try:
                context.close()
            except Exception as exc:  # already closed by the owner
                logger.debug("closing the manual-check browser failed: %s", exc)
        try:
            playwright.stop()
        except Exception as exc:
            logger.debug("stopping playwright failed: %s", exc)

    cleared = seen_page and last_reason is None
    cooldown_cleared = False
    if cleared and clear_cooldown:
        cooldown = resolve_path(settings.cache_dir) / COOLDOWN_FILE
        if cooldown.exists():
            try:
                cooldown.unlink()
                cooldown_cleared = True
            except OSError as exc:
                logger.warning("could not remove the HLTV cool-down file %s: %s", cooldown, exc)
    result = ManualCheckResult(
        url=target,
        profile_dir=profile,
        cleared=cleared,
        window_closed=window_closed,
        waited_s=max(0.0, clock() - started),
        cooldown_cleared=cooldown_cleared,
        last_challenge=last_reason,
    )
    logger.info(
        "HLTV manual check finished: cleared=%s window_closed=%s cooldown_cleared=%s",
        result.cleared,
        result.window_closed,
        result.cooldown_cleared,
    )
    return result
