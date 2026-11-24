"""``cs2bot up``: one foreground process running the scheduler, the executor and the local dashboard.

Start-up order (:func:`run_up`):

1. database reachable and migrations at head (else exit code 2 with the fix in the message);
2. models: the newest saved bundle, else a first training run in a background thread (final review,
   Lens C: the scheduler and the API start at once - odds are collected and the container is healthy
   while it trains; no data yet -> the bot still starts; prediction and betting jobs wait for the first
   successful training);
3. the Betclic executor (``executor.create_betclic_executor``; the browser starts lazily on the first
   call, in the executor's own thread). A :class:`~cs2bot.executor.FakeExecutor` is never created
   here: tests inject one through ``executor_factory``;
4. the Claude context analyzer (disabled without a key: template rationales, no flags);
4b. cloud autonomy: the Telegram client (``telegram.enabled`` with token + chat id; a "started" message
   goes out once everything runs) and a reset of an auto-login that stopped for missing credentials
   (a wrong password / locked account stays stopped across restarts until the owner resumes, so a
   crash loop can never hammer the Betclic login form);
5. the scheduler (``flows.jobs.build_jobs``) and the API on ``settings.api.host:port`` (``api.make_server``
   with ``login_launcher``, ``executor_health_provider``, ``scheduler_status_provider``,
   ``event_remapper``); the dashboard URL is printed.

Shutdown: Ctrl+C (SIGINT -> ``KeyboardInterrupt``), SIGTERM (``docker stop`` on the VPS) or, on Windows,
SIGBREAK set the stop event (a "stopping" Telegram message is sent first); the main thread only waits on
that event with a short timeout, which keeps Ctrl+C working on Windows. The API is stopped first (no new
commands), then the scheduler (running jobs finish their current step; bets are committed before every
click so an interrupted cycle is safe), then the executor (closes Chromium, releases the profile lock)
and the analyzer. The whole shutdown is bounded (``SHUTDOWN_BUDGET_S``) to stay inside the 90 s the
Windows start script waits. Exit code 0 = a normal stop; non-zero = could not start or crashed.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import sys
import threading
import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from types import FrameType
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from cs2bot.config import Settings
from cs2bot.flows.jobs import Runtime, build_jobs
from cs2bot.flows.scheduler import Scheduler
from cs2bot.services.health import HealthCache
from cs2bot.services.registry import ModelRegistry, NotEnoughDataError

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_READY = 2
SHUTDOWN_BUDGET_S = 75.0
#: The "stopping" Telegram message gets one try of at most this long (it runs before the shutdown).
LIFECYCLE_SEND_TIMEOUT_S = 5.0
API_START_TIMEOUT_S = 30.0


@dataclass
class UpHandles:
    """What ``run_up`` started (handed to ``on_started`` - used by tests and for diagnostics)."""

    runtime: Runtime
    scheduler: Scheduler | None
    server: Any | None
    stop: threading.Event
    url: str


def dashboard_url(settings: Settings, port: int | None = None) -> str:
    host = settings.api.host
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    if ":" in shown and not shown.startswith("["):
        shown = f"[{shown}]"
    return f"http://{shown}:{port if port is not None else settings.api.port}/"


def _install_signal_handlers(stop: threading.Event) -> Callable[[], None]:
    """SIGTERM / SIGBREAK set ``stop`` (main thread only); returns a function restoring the old ones."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous: list[tuple[int, Any]] = []

    def _handler(signum: int, frame: FrameType | None) -> None:
        log.info("signal %s received: shutting down", signum)
        stop.set()

    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            previous.append((sig, signal.signal(sig, _handler)))

    def restore() -> None:
        for sig, old in previous:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, old)

    return restore


def _load_or_train(
    registry: ModelRegistry,
    session_factory: sessionmaker[Session],
    train_if_missing: bool,
    *,
    background: bool = False,
) -> threading.Thread | None:
    """Load the newest bundle; without one, train (in a background thread when ``background``).

    Final review (Lens C): the first training used to run before the scheduler and the API started.
    With a backfilled history it can take longer than the container's healthcheck start period
    (15 min on the VPS), and meanwhile no odds were collected and the dashboard did not answer; a
    ``docker stop`` during it also killed the process before any signal handler existed. Now it runs
    in a daemon thread (the registry's train lock keeps the nightly job from training at the same time;
    prediction and betting jobs skip until the bundle is there).
    """
    bundle = registry.load_latest()
    if bundle is not None:
        log.info("models: using bundle %s (trained %s)", bundle.version, bundle.trained_at.isoformat())
        return None
    if not train_if_missing:
        log.warning(
            "models: no saved bundle; predictions wait for `cs2bot models train` or the nightly retrain"
        )
        return None
    if background:
        thread = threading.Thread(
            target=_first_training,
            args=(registry, session_factory),
            name="cs2bot-first-training",
            daemon=True,
        )
        thread.start()
        return thread
    _first_training(registry, session_factory)
    return None


def _first_training(registry: ModelRegistry, session_factory: sessionmaker[Session]) -> None:
    log.info("models: no saved bundle - training now (this can take a few minutes)")
    try:
        report = registry.retrain(session_factory)
        log.info("models: trained %s on %d maps", report.version, report.n_map_results)
    except NotEnoughDataError as exc:
        log.warning("models: %s; the bot runs without a model until the next retrain", exc)
    except Exception:
        log.exception("models: first training failed; the bot runs without a model until the next retrain")


def _start_api(server: Any) -> threading.Thread:
    thread = threading.Thread(target=server.run, name="cs2bot-api", daemon=True)
    thread.start()
    deadline = _time.monotonic() + API_START_TIMEOUT_S
    while _time.monotonic() < deadline:
        if getattr(server, "started", False):
            return thread
        if not thread.is_alive():
            raise RuntimeError("the dashboard API could not start (is the port already in use?)")
        _time.sleep(0.05)
    raise RuntimeError("the dashboard API did not start within 30 s")


def _bound_port(server: Any) -> int | None:
    try:
        for srv in getattr(server, "servers", []) or []:
            for sock in srv.sockets:
                return int(sock.getsockname()[1])
    except Exception:  # pragma: no cover - diagnostics only
        return None
    return None


def run_up(
    settings: Settings,
    *,
    session_factory: sessionmaker[Session] | None = None,
    executor_factory: Callable[[Settings, sessionmaker[Session]], Any] | None = None,
    analyzer_factory: Callable[[Settings], Any] | None = None,
    telegram_factory: Callable[[Settings], Any] | None = None,
    runtime_overrides: dict[str, Any] | None = None,
    check_migrations: bool = True,
    train_if_missing: bool = True,
    train_in_background: bool = True,
    start_api: bool = True,
    stop_event: threading.Event | None = None,
    on_started: Callable[[UpHandles], None] | None = None,
    echo: Callable[[str], None] = print,
) -> int:
    """Run the bot until stopped (see the module docstring). Returns the process exit code."""
    from cs2bot.db.session import make_engine, make_session_factory

    stop = stop_event or threading.Event()
    engine = None
    if session_factory is None:
        engine = make_engine(settings.database.url)
        session_factory = make_session_factory(engine)
    bind = session_factory.kw.get("bind")
    if check_migrations and bind is not None:
        from cs2bot.services.dbops import migration_status

        status = migration_status(bind)
        if not status.at_head:
            echo(f"cs2bot up: {status.describe()}")
            return EXIT_NOT_READY

    registry = ModelRegistry(settings)
    try:
        _load_or_train(registry, session_factory, train_if_missing, background=train_in_background)
    except KeyboardInterrupt:  # Ctrl+C during a foreground first training: a normal, deliberate stop
        echo("Ctrl+C received during start-up - stopped.")
        if engine is not None:
            engine.dispose()
        return EXIT_OK

    executor: Any | None = None
    if settings.executor.enabled:
        try:
            if executor_factory is not None:
                executor = executor_factory(settings, session_factory)
            else:
                from cs2bot.executor import create_betclic_executor

                executor = create_betclic_executor(settings, session_factory)
        except Exception as exc:
            echo(f"cs2bot up: the Betclic executor could not be created: {exc}")
            return EXIT_NOT_READY
    analyzer: Any | None = None
    if settings.llm.enabled:
        try:
            if analyzer_factory is not None:
                analyzer = analyzer_factory(settings)
            else:
                from cs2bot.llm import ContextAnalyzer

                analyzer = ContextAnalyzer(settings)
        except Exception:
            log.exception("the Claude context layer could not start; continuing without it")

    telegram = _start_telegram(settings, telegram_factory)
    _reset_auto_login(session_factory)
    health = HealthCache()
    runtime_kwargs: dict[str, Any] = {"telegram": telegram, **(runtime_overrides or {})}
    runtime = Runtime(
        settings=settings,
        session_factory=session_factory,
        registry=registry,
        executor=executor,
        analyzer=analyzer,
        health_cache=health,
        stop_event=stop,  # a betting cycle running at Ctrl+C places nothing more
        **runtime_kwargs,
    )
    scheduler: Scheduler | None = None
    server: Any | None = None
    api_thread: threading.Thread | None = None
    restore_signals = _install_signal_handlers(stop)
    exit_code = EXIT_OK
    try:
        if settings.scheduler.enabled:
            scheduler = Scheduler(build_jobs(runtime), session_factory, tick_s=settings.scheduler.tick_s)
            scheduler.start()
        else:
            echo("scheduler disabled (CS2BOT_SCHEDULER__ENABLED=false): no jobs will run")
        url = dashboard_url(settings)
        if start_api:
            from cs2bot.api import make_server

            server = make_server(
                settings,
                session_factory,
                login_launcher=_login_launcher(executor),
                executor_health_provider=health.get,
                event_remapper=_event_remapper(runtime),
                scheduler_status_provider=scheduler.status if scheduler is not None else None,
            )
            api_thread = _start_api(server)
            url = dashboard_url(settings, _bound_port(server))
        mode = "DRY RUN (no real bets)" if settings.executor.dry_run else "LIVE - real money"
        echo(f"cs2bot is running. Dashboard: {url}  Betting mode: {mode}. Press Ctrl+C to stop.")
        _notify_lifecycle(runtime, _startup_text(settings, session_factory, mode))
        if on_started is not None:
            on_started(UpHandles(runtime=runtime, scheduler=scheduler, server=server, stop=stop, url=url))
        while not stop.wait(0.5):
            if api_thread is not None and not api_thread.is_alive():
                log.error("the dashboard API stopped unexpectedly")
                exit_code = EXIT_ERROR
                break
    except KeyboardInterrupt:
        echo("Ctrl+C received - shutting down...")
    except Exception as exc:
        log.exception("cs2bot up failed")
        echo(f"cs2bot up: {exc}")
        exit_code = EXIT_ERROR
    finally:
        stop.set()
        # bounded: Telegram being slow or down must not eat the shutdown budget (docker stop grace period)
        _notify_lifecycle(
            runtime,
            f"cs2bot is stopping ({'error' if exit_code else 'normal stop'}).",
            attempts=1,
            timeout_s=LIFECYCLE_SEND_TIMEOUT_S,
        )
        _shutdown(server, api_thread, scheduler, runtime, executor, analyzer)
        if runtime.telegram is not None:
            _safe_close("telegram", runtime.telegram)
        restore_signals()
        if engine is not None:
            engine.dispose()
    echo("cs2bot stopped.")
    return exit_code


def _start_telegram(settings: Settings, factory: Callable[[Settings], Any] | None) -> Any | None:
    """The Telegram client when enabled and configured (else None; problems are logged, never fatal)."""
    if not settings.telegram.enabled:
        return None
    try:
        if factory is not None:
            return factory(settings)
        from cs2bot.services.telegram import TelegramClient

        client = TelegramClient.from_settings(settings)
        if client is None:
            log.warning("telegram.enabled but the bot token or chat id is missing: no Telegram")
        return client
    except Exception:
        log.exception("the Telegram client could not start; continuing without it")
        return None


def _reset_auto_login(session_factory: sessionmaker[Session]) -> None:
    """A restart re-reads the credentials: an auto-login stopped for missing credentials may try again
    (any other stop waits for the owner's resume, see ``services.autonomy.clear_login_block``)."""
    try:
        from cs2bot.services.autonomy import clear_login_block

        with session_factory() as session:
            clear_login_block(session, actor="startup", startup=True)
            session.commit()
    except Exception:
        log.warning("could not reset the auto-login state", exc_info=True)


def _startup_text(settings: Settings, session_factory: sessionmaker[Session], mode: str) -> str:
    lines = [f"cs2bot started. Betting mode: {mode}."]
    try:
        from cs2bot.db import state as db_state

        with session_factory() as session:
            if db_state.is_kill_switch_on(session):
                lines.append("Kill switch is ON (send /go to allow betting).")
            reasons = db_state.get_pause_reasons(session)
            if reasons:
                lines.append("Paused: " + ", ".join(r.code.value for r in reasons) + " (see /status).")
    except Exception:
        log.warning("could not read the trading state for the startup message", exc_info=True)
    if settings.autonomy.enabled:
        lines.append(
            "Autonomy on: automatic pauses lift themselves"
            + (", auto-login on." if settings.autonomy.auto_login else ".")
        )
    return "\n".join(lines)


def _notify_lifecycle(runtime: Runtime, text: str, **send_options: Any) -> None:
    client = runtime.telegram
    if client is None or not runtime.settings.telegram.notify_lifecycle:
        return
    try:
        client.send(text, **send_options)
    except Exception:  # send never raises, but a test double might
        log.warning("could not send the Telegram lifecycle message", exc_info=True)


def _login_launcher(executor: Any | None) -> Callable[[], object] | None:
    if executor is None or not hasattr(executor, "open_login_window"):
        return None

    def launch() -> object:
        return executor.open_login_window()

    return launch


def _event_remapper(runtime: Runtime) -> Callable[[Session, Any], object]:
    def remap(session: Session, event: Any) -> object:
        from cs2bot.odds import remap_event

        return remap_event(session, event, runtime.market_mapper(), settings=runtime.settings)

    return remap


def _shutdown(
    server: Any | None,
    api_thread: threading.Thread | None,
    scheduler: Scheduler | None,
    runtime: Runtime,
    executor: Any | None,
    analyzer: Any | None,
) -> None:
    """Stop everything within ``SHUTDOWN_BUDGET_S`` (each step gets what is left; errors are logged)."""
    deadline = _time.monotonic() + SHUTDOWN_BUDGET_S

    def left(cap: float) -> float:
        return max(1.0, min(cap, deadline - _time.monotonic()))

    if server is not None:
        server.should_exit = True
        if api_thread is not None:
            api_thread.join(timeout=left(10.0))
    if scheduler is not None:
        try:
            scheduler.stop(timeout_s=left(40.0) / max(1, len({j.lane for j in scheduler.jobs})))
        except Exception:
            log.exception("scheduler stop failed")
    runtime.close()
    for name, obj in (("executor", executor), ("analyzer", analyzer)):
        if obj is None or not hasattr(obj, "close"):
            continue
        closer = threading.Thread(target=_safe_close, args=(name, obj), name=f"close-{name}", daemon=True)
        closer.start()
        closer.join(timeout=left(20.0))
        if closer.is_alive():
            log.warning("%s did not close in time; exiting anyway", name)
    sys.stdout.flush()


def _safe_close(name: str, obj: Any) -> None:
    try:
        obj.close()
    except Exception:
        log.exception("closing the %s failed", name)
