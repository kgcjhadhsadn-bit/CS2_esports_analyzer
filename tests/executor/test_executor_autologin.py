"""Auto-login (cloud autonomy) against the mock Betclic site, plus the headless / display fallback.

The mock's login form answers a submit with a login, an SMS-code step ("Weryfikacja dwuetapowa"), a
wrong-password or an account-locked message (``MockState.login_mode`` / ``login_credentials``).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import SecretStr

from cs2bot.config import ExecutorSettings, Settings
from cs2bot.domain.control import LoginStatus
from cs2bot.executor import FakeExecutor, ThreadBoundExecutor
from cs2bot.executor.session import build_launch_options, effective_headless, has_display
from cs2bot.executor.tuning import ExecutorTuning
from tests.executor.conftest import ExecutorFactory, requires_chromium
from tests.executor.mock_betclic import MockBetclicServer

USER, PASSWORD = "owner@example.com", "S3cret-Haslo!"


def _with_credentials(settings: Settings, *, source: str = "env") -> Settings:
    betclic = settings.betclic.model_copy(
        update={"credentials_source": source, "username": SecretStr(USER), "password": SecretStr(PASSWORD)}
    )
    executor = settings.executor.model_copy(update={"login_submit_timeout_s": 3.0})
    return settings.model_copy(update={"betclic": betclic, "executor": executor})


def _no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert PASSWORD not in logs and USER not in logs


@pytest.mark.browser
@requires_chromium
def test_auto_login_fills_and_submits_the_form(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    executor_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    site.state.logged_in = False
    site.state.login_credentials = (USER, PASSWORD)
    executor = make_executor(settings=_with_credentials(executor_settings))
    with caplog.at_level(logging.DEBUG):
        attempt = executor.auto_login()
    assert attempt.status is LoginStatus.LOGGED_IN, attempt.message
    assert attempt.submitted
    assert site.state.login_submits == 1 and site.state.login_payloads == [
        {"username": USER, "password": PASSWORD}
    ]
    assert site.state.logged_in and executor.is_logged_in()
    _no_secrets(caplog)
    # a second call sees the session and submits nothing
    assert executor.auto_login().status is LoginStatus.ALREADY_LOGGED_IN
    assert site.state.login_submits == 1


@pytest.mark.browser
@requires_chromium
def test_auto_login_stops_at_the_sms_step(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    executor_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    site.state.logged_in = False
    site.state.login_mode = "2fa"
    with caplog.at_level(logging.DEBUG):
        attempt = make_executor(settings=_with_credentials(executor_settings)).auto_login()
    assert attempt.status is LoginStatus.TWO_FA_REQUIRED
    assert attempt.status.stops_retrying is False and not attempt.status.logged_in
    assert site.state.login_submits == 1 and site.state.logged_in is False
    _no_secrets(caplog)


@pytest.mark.browser
@requires_chromium
def test_auto_login_detects_a_wrong_password(
    site: MockBetclicServer, make_executor: ExecutorFactory, executor_settings: Settings
) -> None:
    site.state.logged_in = False
    site.state.login_credentials = (USER, "another-password")
    attempt = make_executor(settings=_with_credentials(executor_settings)).auto_login()
    assert attempt.status is LoginStatus.BAD_CREDENTIALS and attempt.status.stops_retrying
    assert "Nieprawidłowy login lub hasło" in attempt.message
    assert PASSWORD not in attempt.message


@pytest.mark.browser
@requires_chromium
def test_auto_login_detects_a_locked_account(
    site: MockBetclicServer, make_executor: ExecutorFactory, executor_settings: Settings
) -> None:
    site.state.logged_in = False
    site.state.login_mode = "locked"
    attempt = make_executor(settings=_with_credentials(executor_settings)).auto_login()
    assert attempt.status is LoginStatus.ACCOUNT_LOCKED and attempt.status.stops_retrying


@pytest.mark.browser
@requires_chromium
def test_auto_login_without_an_answer_fails_after_the_timeout(
    site: MockBetclicServer, make_executor: ExecutorFactory, executor_settings: Settings
) -> None:
    site.state.logged_in = False
    site.state.login_mode = "silent"
    attempt = make_executor(settings=_with_credentials(executor_settings)).auto_login(timeout_s=1.0)
    assert attempt.status is LoginStatus.FAILED and "no login outcome" in attempt.message
    assert attempt.submitted  # counts towards autonomy.max_login_failures


@pytest.mark.browser
@requires_chromium
def test_a_browser_error_never_leaks_the_credentials(
    site: MockBetclicServer,
    make_executor: ExecutorFactory,
    executor_settings: Settings,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review round 1: Playwright's call log contains ``fill("<value>")``; an error raised after the
    form was filled must not carry the password / username into the attempt (logged, stored in the
    database, sent to Telegram). Any exception is an outcome, never raised."""
    from playwright.sync_api import Error as PlaywrightError

    site.state.logged_in = False
    executor = make_executor(settings=_with_credentials(executor_settings))

    def boom(page: object, timeout_s: float) -> object:
        raise PlaywrightError(
            f'Locator.fill: fill("{PASSWORD}") for {USER} failed\nCall log: fill("{PASSWORD}")'
        )

    monkeypatch.setattr(executor, "_await_login_outcome", boom)
    with caplog.at_level(logging.DEBUG):
        attempt = executor.auto_login()
    assert attempt.status is LoginStatus.FAILED and attempt.submitted
    assert PASSWORD not in attempt.message and USER not in attempt.message
    assert "<redacted>" in attempt.message
    _no_secrets(caplog)

    def other(page: object, timeout_s: float) -> object:
        raise RuntimeError(f"unexpected {PASSWORD}")

    monkeypatch.setattr(executor, "_await_login_outcome", other)
    site.state.logged_in = False  # the mock accepted the first submit
    attempt = executor.auto_login()
    assert attempt.status is LoginStatus.FAILED and PASSWORD not in attempt.message


@pytest.mark.browser
@requires_chromium
def test_auto_login_without_credentials_submits_nothing(
    site: MockBetclicServer, make_executor: ExecutorFactory
) -> None:
    site.state.logged_in = False
    attempt = make_executor().auto_login()  # credentials_source "none"
    assert attempt.status is LoginStatus.NO_CREDENTIALS and attempt.status.stops_retrying
    assert site.state.login_submits == 0 and not attempt.submitted


@pytest.mark.browser
@requires_chromium
def test_a_crashed_browser_is_recreated_on_the_next_call(
    site: MockBetclicServer, make_executor: ExecutorFactory
) -> None:
    executor = make_executor()
    assert executor.is_logged_in()
    page = executor._session.current_page()
    assert page is not None
    page.context.close()  # Chromium gone (crash / OOM kill): the context's close event fires
    assert executor._session.current_page() is None
    assert executor.is_logged_in()  # relaunched with the same profile on the next job
    assert executor.health_check().logged_in is True


# ------------------------------------------------------------------ fakes / wrapper without a browser


def test_fake_executor_auto_login_script() -> None:
    fake = FakeExecutor(
        logged_in=False, auto_login_results=[LoginStatus.TWO_FA_REQUIRED, LoginStatus.LOGGED_IN]
    )
    assert fake.auto_login().status is LoginStatus.TWO_FA_REQUIRED and not fake.logged_in
    assert fake.auto_login().status is LoginStatus.LOGGED_IN and fake.logged_in
    assert fake.auto_login().status is LoginStatus.ALREADY_LOGGED_IN


def test_thread_bound_wrapper_forwards_auto_login() -> None:
    wrapper = ThreadBoundExecutor(lambda: FakeExecutor(logged_in=False))
    try:
        assert wrapper.auto_login().status is LoginStatus.LOGGED_IN
        assert wrapper.is_logged_in()
    finally:
        wrapper.close()


def test_thread_bound_wrapper_without_auto_login_reports_failed() -> None:
    class Minimal(FakeExecutor):
        auto_login = None  # type: ignore[assignment]

    wrapper = ThreadBoundExecutor(lambda: Minimal())
    try:
        assert wrapper.auto_login().status is LoginStatus.FAILED
    finally:
        wrapper.close()


# ------------------------------------------------------------------ headless / display


def test_has_display() -> None:
    assert has_display({}, "win32") and has_display({}, "darwin")
    assert not has_display({}, "linux")
    assert has_display({"DISPLAY": ":99"}, "linux") and has_display({"WAYLAND_DISPLAY": "w-0"}, "linux")


def test_effective_headless_falls_back_without_a_display() -> None:
    visible = Settings(executor=ExecutorSettings(headless=False))
    assert effective_headless(visible, environ={"DISPLAY": ":99"}, platform="linux") is False
    assert effective_headless(visible, environ={}, platform="linux") is True
    assert effective_headless(visible, environ={}, platform="win32") is False
    strict = Settings(executor=ExecutorSettings(headless=False, headless_without_display=False))
    assert effective_headless(strict, environ={}, platform="linux") is False  # fails loudly at launch
    assert effective_headless(Settings(executor=ExecutorSettings(headless=True)), environ={}) is True


def test_launch_options_use_the_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr("sys.platform", "linux")
    opts = build_launch_options(
        Settings(executor=ExecutorSettings(headless=False)), ExecutorTuning(), tmp_path
    )
    assert opts["headless"] is True
    monkeypatch.setenv("DISPLAY", ":99")
    opts = build_launch_options(
        Settings(executor=ExecutorSettings(headless=False)), ExecutorTuning(), tmp_path
    )
    assert opts["headless"] is False
