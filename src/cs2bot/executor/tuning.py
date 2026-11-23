"""Executor tuning knobs (timeouts, pacing, sanity limits).

The knobs the owner may want to change live in ``cs2bot.config.ExecutorSettings`` (``CS2BOT_EXECUTOR__*``
environment variables); :meth:`ExecutorTuning.from_settings` copies them. The remaining fields are
implementation details with safe defaults. :class:`~cs2bot.executor.betclic.BetclicExecutor` builds its
tuning from the settings unless an explicit ``tuning=`` is passed (tests use short timeouts).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from cs2bot.config import Settings

#: ``ExecutorTuning`` fields that ``ExecutorSettings`` carries today (copied 1:1, same name). Any tuning
#: field added to ``ExecutorSettings`` later under the same name is picked up automatically by
#: :meth:`ExecutorTuning.from_settings`; this tuple documents (and tests pin down) the current set.
SETTINGS_FIELDS: tuple[str, ...] = (
    "element_timeout_s",
    "optional_timeout_s",
    "slip_update_timeout_s",
    "confirmation_timeout_s",
    "min_seconds_between_bets",
    "max_odds_change_rounds",
    "viewport_width",
    "viewport_height",
    "save_html_on_selector_error",
    "health_check_event_page",
    "lock_stale_after_s",
    "lock_heartbeat_s",
)


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
    # Sanity limits
    # Displayed odds above requested_odds x this factor are not taken (ODDS_MOVED): a pre-match price
    # that jumped this far means news the model has not seen, or the wrong outcome - re-price first.
    # ``None`` disables the check.
    max_odds_rise_factor: float | None = 1.5
    # "Możliwa wygrana" shown in the slip must lie within these fractions of stake x odds (when the
    # optional ``slip_potential_win`` element resolves). Poland: 0.88 after the 12% stake tax, 0.79 for
    # winnings above 2280 PLN (extra 10%), 1.0 during "no tax" promos. Catches a misread stake (x10, x100).
    potential_win_min_ratio: float = 0.70
    potential_win_max_ratio: float = 1.05
    # Profile lock
    lock_stale_after_s: float = 600.0  # a lock whose heartbeat is older than this is stale
    lock_heartbeat_s: float = 30.0
    # Placement journal (crash-safe idempotency, see cs2bot.executor.journal)
    journal_retention_days: float = 60.0

    def __post_init__(self) -> None:
        if self.lock_heartbeat_s <= 0 or self.lock_heartbeat_s * 2 > self.lock_stale_after_s:
            raise ValueError("lock_heartbeat_s must be positive and at most half of lock_stale_after_s")
        if self.typing_delay_min_ms < 0 or self.typing_delay_min_ms > self.typing_delay_max_ms:
            raise ValueError("typing delays must satisfy 0 <= typing_delay_min_ms <= typing_delay_max_ms")
        if self.max_odds_change_rounds < 0:
            raise ValueError("max_odds_change_rounds must be >= 0")
        if self.viewport_width <= 0 or self.viewport_height <= 0:
            raise ValueError("viewport_width / viewport_height must be positive")
        for name in (
            "action_timeout_s",
            "element_timeout_s",
            "slip_update_timeout_s",
            "confirmation_timeout_s",
            "poll_interval_s",
            "journal_retention_days",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("optional_timeout_s", "min_seconds_between_bets", "balance_update_wait_s"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.max_odds_rise_factor is not None and self.max_odds_rise_factor <= 1:
            raise ValueError("max_odds_rise_factor must be > 1 (or None to disable the check)")
        if not 0 < self.potential_win_min_ratio < self.potential_win_max_ratio:
            raise ValueError("potential win ratios must satisfy 0 < min < max")

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> ExecutorTuning:
        """Tuning with the knobs of ``settings.executor`` (see :data:`SETTINGS_FIELDS`).

        Every :class:`ExecutorTuning` field that ``settings.executor`` also has (same name) is copied;
        the other fields keep their defaults. ``overrides`` win over the settings (any
        :class:`ExecutorTuning` field). Raises ``ValueError`` for inconsistent values (e.g. a lock
        heartbeat longer than half the stale age) so that a bad ``.env`` is reported at start-up, not at
        the first bet.
        """
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(overrides) - known)
        if unknown:
            raise TypeError(f"unknown ExecutorTuning field(s): {unknown}")
        values: dict[str, Any] = {
            name: getattr(settings.executor, name) for name in known if hasattr(settings.executor, name)
        }
        values.update(overrides)
        return cls(**values)
