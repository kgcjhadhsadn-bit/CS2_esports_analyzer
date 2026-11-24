"""``cs2bot doctor``: environment checks for the owner's PC.

Each check returns a :class:`Check` with level ``ok`` / ``warn`` / ``fail``. ``fail`` means the bot
cannot do its job (no database, migrations behind, config files missing, no Chromium, no PandaScore
token); ``warn`` marks something worth fixing that the bot can live with (no Anthropic key: no news
flags, template rationales; no RAR tool: .rar demos fail; little disk space; no trained model yet).
No check ever raises, and no secret value is printed (only whether it is set).
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cs2bot.config import Settings
from cs2bot.utils.paths import resolve_path

Level = Literal["ok", "warn", "fail"]

#: Free space below this fails the check; below ``WARN_FREE_GB`` it warns (PLAN.md: 100-200 GB).
FAIL_FREE_GB = 5.0
WARN_FREE_GB = 50.0


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    message: str


def _guard(name: str, fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as exc:  # a check must never crash the doctor
        return Check(name, "fail", f"check crashed: {type(exc).__name__}: {exc}")


def check_database(settings: Settings) -> list[Check]:
    from cs2bot.db.session import make_engine
    from cs2bot.services.dbops import migration_status

    engine = make_engine(settings.database.url)
    try:
        status = migration_status(engine)
    finally:
        engine.dispose()
    if not status.reachable:
        return [
            Check("database", "fail", f"{status.describe()} - is PostgreSQL running? (docker compose up -d)")
        ]
    return [
        Check("database", "ok", f"reachable ({engine.url.get_backend_name()})"),
        Check("migrations", "ok" if status.at_head else "fail", status.describe()),
    ]


def default_chromium_path() -> str | None:
    """Playwright's bundled Chromium (starts the Playwright driver, not a browser)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        return str(p.chromium.executable_path)


def check_chromium(settings: Settings, finder: Callable[[], str | None] = default_chromium_path) -> Check:
    configured = settings.betclic.chromium_executable or settings.hltv.chromium_executable
    if configured:
        ok = Path(configured).is_file()
        return Check(
            "chromium",
            "ok" if ok else "fail",
            f"configured executable {'found' if ok else 'NOT found'}: {configured}",
        )
    try:
        path = finder()
    except Exception as exc:
        return Check(
            "chromium",
            "fail",
            f"Playwright not usable ({type(exc).__name__}): run `uv run playwright install chromium`",
        )
    if path and Path(path).is_file():
        return Check("chromium", "ok", f"Playwright Chromium at {path}")
    return Check("chromium", "fail", "Playwright Chromium missing: run `uv run playwright install chromium`")


def check_pandascore(settings: Settings) -> Check:
    if settings.pandascore.api_token is None:
        return Check(
            "pandascore token", "fail", "not set: CS2BOT_PANDASCORE__API_TOKEN in .env (fixtures/results)"
        )
    return Check("pandascore token", "ok", f"set ({settings.pandascore.max_requests_per_hour} requests/hour)")


def check_anthropic(settings: Settings) -> Check:
    if not settings.llm.enabled:
        return Check(
            "anthropic key", "warn", "Claude layer disabled (CS2BOT_LLM__ENABLED=false): no news flags"
        )
    if settings.llm.api_key is not None or os.environ.get("ANTHROPIC_API_KEY"):
        return Check("anthropic key", "ok", "set")
    return Check(
        "anthropic key",
        "warn",
        "not set (CS2BOT_LLM__API_KEY or ANTHROPIC_API_KEY): no news context flags, template rationales",
    )


def check_archive_tool() -> Check:
    from cs2bot.ingest.demos import find_rar_tools

    tools = find_rar_tools()
    if tools:
        return Check("rar tool", "ok", ", ".join(f"{t.kind} ({t.executable})" for t in tools[:3]))
    return Check("rar tool", "warn", "no RAR extractor found: install 7-Zip (HLTV demos are .rar archives)")


def check_disk(settings: Settings) -> Check:
    path = resolve_path(settings.data_dir)
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free_gb = shutil.disk_usage(probe).free / 1024**3
    level: Level = "ok" if free_gb >= WARN_FREE_GB else ("warn" if free_gb >= FAIL_FREE_GB else "fail")
    return Check(
        "disk space", level, f"{free_gb:.1f} GB free at {probe} (plan: 100-200 GB for demos and data)"
    )


def check_config_files(settings: Settings) -> list[Check]:
    out: list[Check] = []
    for label, raw in (
        ("market rules", settings.betclic.market_rules_file),
        ("betclic selectors", settings.betclic.selectors_file),
    ):
        path = resolve_path(raw)
        out.append(Check(f"config: {label}", "ok" if path.is_file() else "fail", str(path)))
    return out


def check_trading_state(settings: Settings) -> list[Check]:
    from cs2bot.db import state as db_state
    from cs2bot.db.session import make_engine, make_session_factory

    mode = "DRY RUN (no real bets)" if settings.executor.dry_run else "LIVE (real money)"
    checks = [
        Check(
            "betting mode",
            "ok",
            f"{mode}; executor {'enabled' if settings.executor.enabled else 'disabled'}",
        )
    ]
    engine = make_engine(settings.database.url)
    try:
        with make_session_factory(engine)() as session:
            halted, reason = db_state.is_trading_halted(session)
    finally:
        engine.dispose()
    checks.append(
        Check("trading", "warn" if halted else "ok", f"halted: {reason}" if halted else "not halted")
    )
    return checks


def check_models(settings: Settings) -> Check:
    from cs2bot.services.registry import list_bundles

    bundles = list_bundles(settings)
    if not bundles:
        return Check(
            "models", "warn", "no trained bundle yet: `cs2bot models train` (cs2bot up trains on start)"
        )
    trained_at, path = bundles[0]
    head = f"latest bundle {path.name} trained {trained_at:%Y-%m-%d %H:%M} UTC"
    calibration = _calibration_status(path)
    if calibration is not None:
        # final review: the decision rule assumes calibrated probabilities (PLAN.md 5.8 / 6); without the
        # ensemble the bot bets on the rating model's own, uncalibrated probabilities
        return Check("models", "warn", f"{head}; {calibration}")
    return Check("models", "ok", head)


def _calibration_status(bundle_dir: Path) -> str | None:
    """Why the bundle's probabilities are not calibrated (None = calibrated ensemble active)."""
    try:
        manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "manifest unreadable: cannot tell whether the probabilities are calibrated"
    ensemble = (manifest.get("metrics") or {}).get("ensemble")
    if not isinstance(ensemble, dict):
        return (
            "no ensemble (fewer labelled maps than EnsembleConfig.min_rows?): probabilities come from the "
            "rating model and are NOT calibrated - backfill more history or raise the decision thresholds"
        )
    if ensemble.get("fallback"):
        reason = ensemble.get("fallback_reason") or "unknown reason"
        return f"ensemble fell back to the rating model ({reason}): probabilities are NOT calibrated"
    if ensemble.get("calibrator") == "identity":
        return "the calibration holdout was too small (identity calibrator): probabilities are NOT calibrated"
    return None


def check_dashboard(settings: Settings) -> Check:
    dist = resolve_path(settings.api.dashboard_dist_dir)
    if (dist / "index.html").is_file():
        return Check("dashboard build", "ok", str(dist))
    return Check("dashboard build", "warn", f"{dist} not built: run `npm ci && npm run build` in dashboard/")


def run_checks(settings: Settings, *, chromium_finder: Callable[[], str | None] | None = None) -> list[Check]:
    """All checks in display order."""
    checks: list[Check] = []
    try:
        db = check_database(settings)
    except Exception as exc:
        db = [Check("database", "fail", f"check crashed: {type(exc).__name__}: {exc}")]
    checks.extend(db)
    checks.extend(check_config_files(settings))
    finder = chromium_finder or default_chromium_path
    checks.append(_guard("chromium", lambda: check_chromium(settings, finder)))
    checks.append(check_pandascore(settings))
    checks.append(check_anthropic(settings))
    checks.append(_guard("rar tool", check_archive_tool))
    checks.append(_guard("disk space", lambda: check_disk(settings)))
    checks.append(_guard("models", lambda: check_models(settings)))
    checks.append(_guard("dashboard build", lambda: check_dashboard(settings)))
    if all(c.level == "ok" for c in db):
        try:
            checks.extend(check_trading_state(settings))
        except Exception as exc:
            checks.append(Check("trading", "fail", f"could not read the trading state: {exc}"))
    return checks
