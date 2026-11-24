"""Self-test of the cloud image: ``docker compose run --rm --no-deps app selftest``.

Run by the container entrypoint (``docker-entrypoint.sh selftest``) after it started Xvfb, so a
visible (headful) Chromium can be tested exactly like the Betclic executor will run it. It needs
neither the database nor the internet, which makes it suitable for CI and for a quick check after an
update on the VPS. Exit code 0 = every essential check passed, 1 = at least one failed.

Checks: the package imports and the settings load (secrets are never printed), the built dashboard is
present, the data directory is writable, a RAR tool for the demo archives exists, the X display is
reachable, and Playwright's Chromium starts both on the virtual display (headful) and headless.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path

Result = tuple[bool, str]


def _check_package() -> Result:
    import cs2bot

    return True, f"cs2bot {cs2bot.__version__} from {Path(cs2bot.__file__).parent}"


def _check_settings() -> Result:
    from cs2bot.config import get_settings

    s = get_settings()
    return True, (
        f"api {s.api.host}:{s.api.port}, auth {'on' if s.api.password else 'off'}, "
        f"allowed hosts {s.api.allowed_hosts}, dry_run={s.executor.dry_run}, "
        f"headless={s.executor.headless}, autonomy={s.autonomy.enabled}"
    )


def _check_dashboard() -> Result:
    from cs2bot.config import get_settings
    from cs2bot.utils.paths import resolve_path

    dist = resolve_path(get_settings().api.dashboard_dist_dir)
    index = dist / "index.html"
    if not index.is_file():
        return False, f"{index} is missing (the dashboard was not built into the image)"
    return True, str(dist)


def _check_data_dir() -> Result:
    from cs2bot.config import get_settings
    from cs2bot.utils.paths import resolve_path

    data = resolve_path(get_settings().data_dir)
    data.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=data, prefix=".selftest-", delete=True) as fh:
        fh.write(b"ok")
    return True, f"{data} is writable"


def _check_rar_tools() -> Result:
    from cs2bot.ingest.demos.archive import find_rar_tools

    tools = find_rar_tools()
    if not tools:
        return False, "no RAR tool (unar / bsdtar) on PATH: .rar demos cannot be unpacked"
    return True, ", ".join(f"{t.kind}={t.executable}" for t in tools)


def _check_display() -> Result:
    display = os.environ.get("DISPLAY", "")
    if not display:
        return False, "DISPLAY is not set (the entrypoint starts Xvfb and sets DISPLAY=:99)"
    number = display.rsplit(":", 1)[-1].split(".", 1)[0]
    socket = Path("/tmp/.X11-unix") / f"X{number}"
    if not socket.exists():
        return False, f"DISPLAY={display} but {socket} does not exist (Xvfb not running?)"
    return True, f"DISPLAY={display}"


def _launch(headless: bool, rounds: int = 3) -> Result:
    """Start Chromium like the executor does (persistent profile, 1440x900, pl-PL) and take screenshots.

    Headful Chromium on Xvfb with GPU compositing takes screenshots unreliably ("Unable to capture
    screenshot" on some launches); the image's ``cs2bot-chromium`` wrapper adds ``--disable-gpu``, which
    fixed it in every trial. Several rounds catch such flakiness.
    """
    from playwright.sync_api import sync_playwright

    from cs2bot.config import get_settings

    settings = get_settings()
    executable = settings.betclic.chromium_executable
    started = time.monotonic()
    version = "?"
    with sync_playwright() as p:
        for _ in range(rounds):
            with tempfile.TemporaryDirectory(prefix="selftest-profile-") as profile:
                kwargs: dict[str, object] = {
                    "headless": headless,
                    "viewport": {"width": 1440, "height": 900},
                    "locale": "pl-PL",
                    "timezone_id": "Europe/Warsaw",
                }
                if executable:
                    kwargs["executable_path"] = executable
                context = p.chromium.launch_persistent_context(profile, **kwargs)  # type: ignore[arg-type]
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_content("<html><body><h1 id='t'>Zażółć gęślą jaźń</h1></body></html>")
                    text = page.inner_text("#t")
                    shot = page.screenshot()
                    version = context.browser.version if context.browser is not None else version
                finally:
                    context.close()
                if text != "Zażółć gęślą jaźń" or not shot:
                    return False, f"page rendered unexpectedly: {text!r}"
    mode = "headless" if headless else "headful on the virtual display"
    return True, (
        f"Chromium {version} {mode}, {rounds} launches + screenshots in {time.monotonic() - started:.1f} s "
        f"(executable: {executable or 'Playwright default'})"
    )


def _check_effective_headless() -> Result:
    from cs2bot.config import get_settings
    from cs2bot.executor.session import effective_headless

    settings = get_settings()
    headless = effective_headless(settings)
    if not settings.executor.headless and headless:
        return False, "the executor would fall back to headless (no display visible to the app)"
    return True, f"executor browser headless={headless}"


CHECKS: list[tuple[str, Callable[[], Result]]] = [
    ("package", _check_package),
    ("settings", _check_settings),
    ("dashboard", _check_dashboard),
    ("data dir", _check_data_dir),
    ("rar tools", _check_rar_tools),
    ("display", _check_display),
    ("executor display mode", _check_effective_headless),
    ("chromium (headful)", lambda: _launch(headless=False)),
    ("chromium (headless)", lambda: _launch(headless=True)),
]


def main() -> int:
    failed = 0
    for name, check in CHECKS:
        try:
            ok, message = check()
        except Exception as exc:  # report every check, never stop at the first error
            ok, message = False, f"{type(exc).__name__}: {exc}"
            traceback.print_exc(file=sys.stderr)
        failed += 0 if ok else 1
        print(f"[{'OK' if ok else 'FAIL':>4}] {name}: {message}", flush=True)
    print("selftest passed" if not failed else f"selftest FAILED ({failed} check(s))", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
