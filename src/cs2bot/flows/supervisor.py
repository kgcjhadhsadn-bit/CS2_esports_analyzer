"""``cs2bot up``: one foreground process running the scheduler, the executor and the local dashboard.

Start-up order (:func:`run_up`):

1. database reachable and migrations at head (else exit code 2 with the fix in the message);
2. models: the newest saved bundle, else a first training run (no data yet -> the bot still starts;
   prediction and betting jobs wait for the first successful retrain);
3. the Betclic executor (``executor.create_betclic_executor``; the browser starts lazily on the first
   call, in the executor's own thread). A :class:`~cs2bot.executor.FakeExecutor` is never created
   here: tests inject one through ``executor_factory``;
4. the Claude context analyzer (disabled without a key: template rationales, no flags);
5. the scheduler (``flows.jobs.build_jobs``) and the API on ``settings.api.host:port`` (``api.make_server``
   with ``login_launcher``, ``executor_health_provider``, ``scheduler_status_provider``,
   ``event_remapper``); the dashboard URL is printed.

Shutdown: Ctrl+C (SIGINT -> ``KeyboardInterrupt``), SIGTERM or, on Windows, SIGBREAK set the stop
event; the main thread only waits on that event with a short timeout, which keeps Ctrl+C working on
Windows. The API is stopped first (no new commands), then the scheduler (running jobs finish their
current step; bets are committed before every click so an interrupted cycle is safe), then the
executor (closes Chromium, releases the profile lock) and the analyzer. The whole shutdown is bounded
(``SHUTDOWN_BUDGET_S``) to stay inside the 90 s the Windows start script waits. Exit code 0 = a
normal stop; non-zero = could not start or crashed.
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
    registry: ModelRegistry, session_factory: sessionmaker[Session], train_if_missing: bool
) -> None:
    bundle = registry.load_latest()
    if bundle is not None:
        log.info("models: using bundle %s (trained %s)", bundle.version, bundle.trained_at.isoformat())
        return
    if not train_if_missing:
        log.warning(
            "models: no saved bundle; predictions wait for `cs2bot models train` or the nightly retrain"
        )
        return
    log.info("models: no saved bundle - training now (this can take a few minutes)")
    try:
        report = registry.retrain(session_factory)
        log.info("models: trained %s on %d maps", report.version, report.n_map_results)
    except NotEnoughDataError as exc:
        log.warning("models: %s; the bot starts without a model", exc)
    except Exception:
        log.exception("models: first training failed; the bot starts without a model")


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
    runtime_overrides: dict[str, Any] | None = None,
    check_migrations: bool = True,
    train_if_missing: bool = True,
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
        _load_or_train(registry, session_factory, train_if_missing)
    except KeyboardInterrupt:  # Ctrl+C during the first training: a normal, deliberate stop
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

    health = HealthCache()
    runtime = Runtime(
        settings=settings,
        session_factory=session_factory,
        registry=registry,
        executor=executor,
        analyzer=analyzer,
        health_cache=health,
        **(runtime_overrides or {}),
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
        _shutdown(server, api_thread, scheduler, runtime, executor, analyzer)
        restore_signals()
        if engine is not None:
            engine.dispose()
    echo("cs2bot stopped.")
    return exit_code


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
