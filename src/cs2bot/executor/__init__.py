"""cs2bot.executor - Betclic.pl bet placement (see docs/ARCHITECTURE.md, P9).

Typical wiring::

    from cs2bot.executor import create_betclic_executor

    executor = create_betclic_executor(settings, session_factory)  # thread-safe, browser starts lazily
    result = executor.place(order)  # PlacementResult
    executor.close()  # on shutdown: closes Chromium, releases the profile lock

Tests and integration work use :class:`FakeExecutor` (same interface, no browser).
"""

from cs2bot.executor.base import (
    AccountExecutor,
    BetExecutor,
    BookmakerBetRecord,
    BookmakerBetStatus,
    BrowserStartError,
    ExecutorClosedError,
    ExecutorError,
    ExecutorThreadError,
    HaltCheck,
    LoginRequiredError,
)
from cs2bot.executor.betclic import BetclicExecutor, LoginState
from cs2bot.executor.factory import create_betclic_executor
from cs2bot.executor.fake import FakeExecutor
from cs2bot.executor.lock import ProfileLock, ProfileLockedError
from cs2bot.executor.safety import HumanPacer, db_halt_check, never_halted
from cs2bot.executor.selectors import (
    ElementSpec,
    LocatorSpec,
    ResolvedElement,
    SelectorConfigError,
    SelectorNotFoundError,
    SelectorSet,
    load_selectors,
    resolve,
)
from cs2bot.executor.textutils import (
    format_polish_decimal,
    normalise_text,
    parse_polish_datetime,
    parse_polish_decimal,
    screenshot_filename,
)
from cs2bot.executor.threaded import ThreadBoundExecutor
from cs2bot.executor.tuning import ExecutorTuning

__all__ = [
    "AccountExecutor",
    "BetExecutor",
    "BetclicExecutor",
    "BookmakerBetRecord",
    "BookmakerBetStatus",
    "BrowserStartError",
    "ElementSpec",
    "ExecutorClosedError",
    "ExecutorError",
    "ExecutorThreadError",
    "ExecutorTuning",
    "FakeExecutor",
    "HaltCheck",
    "HumanPacer",
    "LocatorSpec",
    "LoginRequiredError",
    "LoginState",
    "ProfileLock",
    "ProfileLockedError",
    "ResolvedElement",
    "SelectorConfigError",
    "SelectorNotFoundError",
    "SelectorSet",
    "ThreadBoundExecutor",
    "create_betclic_executor",
    "db_halt_check",
    "format_polish_decimal",
    "load_selectors",
    "never_halted",
    "normalise_text",
    "parse_polish_datetime",
    "parse_polish_decimal",
    "resolve",
    "screenshot_filename",
]
