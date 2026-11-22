"""Executor knobs that are not (yet) part of ``cs2bot.config.ExecutorSettings``.

Everything that IS in settings is read from there (headless, delays, navigation timeout, screenshot dir,
profile dir, Chromium path, proxy, credentials source). These extra values have safe defaults; pass a
custom :class:`ExecutorTuning` to :class:`~cs2bot.executor.betclic.BetclicExecutor` to change them
(tests use short timeouts). Requested as foundation fields in the P9 report.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutorTuning:
    # Browser window
    viewport_width: int = 1440
    viewport_height: int = 900
    # Timeouts (seconds)
    action_timeout_s: float = 15.0  # Playwright default for clicks / fills / reads
    element_timeout_s: float = 10.0  # resolving a required element
    optional_timeout_s: float = 1.5  # probing optional elements (tabs, confirm dialogs, receipt parts)
    login_check_timeout_s: float = 8.0
    login_logged_out_stable_s: float = 1.5  # "Zaloguj się" must stay visible this long -> logged out
    login_poll_s: float = 1.0  # polling while the owner logs in by hand
    slip_update_timeout_s: float = 8.0  # bet slip reacting to a click
    confirmation_timeout_s: float = 45.0  # receipt / error after the final click, else UNCERTAIN
    balance_update_wait_s: float = 5.0  # waiting for the header balance to change after a bet
    poll_interval_s: float = 0.2
    slip_settle_s: float = 0.3  # pause before counting slip items (late renders)
    # Behaviour
    max_odds_change_rounds: int = 2  # accepted odds-change dialogs per bet before giving up
    min_seconds_between_bets: float = 20.0  # human-like pacing between consecutive placements
    typing_delay_min_ms: int = 60
    typing_delay_max_ms: int = 180
    max_market_candidates: int = 400
    max_selection_candidates: int = 200
    history_max_load_more: int = 10
    health_check_event_page: bool = True  # health check also opens one CS2 event page
    save_html_on_selector_error: bool = True  # page HTML next to the screenshot, to fix selectors
    max_screenshot_key_len: int = 60
    # Profile lock
    lock_stale_after_s: float = 600.0  # a lock whose heartbeat is older than this is stale
    lock_heartbeat_s: float = 30.0

    def __post_init__(self) -> None:
        if self.lock_heartbeat_s * 2 > self.lock_stale_after_s:
            raise ValueError("lock_heartbeat_s must be at most half of lock_stale_after_s")
        if self.typing_delay_min_ms > self.typing_delay_max_ms:
            raise ValueError("typing_delay_min_ms must be <= typing_delay_max_ms")
        if self.max_odds_change_rounds < 0:
            raise ValueError("max_odds_change_rounds must be >= 0")
