"""Command-line entry point: ``cs2bot <group> <command>`` (names fixed in docs/ARCHITECTURE.md).

Commands are thin: parse the arguments, call a service function, print a summary. Expected problems
(a missing token, an unreachable database, HLTV blocking us) end with a one-line explanation and exit
code 1 instead of a traceback. ``cs2bot up`` exits 0 only after a normal stop (Ctrl+C).
"""

from __future__ import annotations

import functools
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import typer
from sqlalchemy.orm import Session, sessionmaker

from cs2bot import __version__
from cs2bot.config import Settings, get_settings
from cs2bot.utils.logging import configure_logging

log = logging.getLogger(__name__)

app = typer.Typer(help="CS2 analyzer & Betclic betting bot", no_args_is_help=True)
db_app = typer.Typer(help="Database management", no_args_is_help=True)
ingest_app = typer.Typer(help="Data ingestion (PandaScore, HLTV)", no_args_is_help=True)
demos_app = typer.Typer(help="HLTV demo pipeline", no_args_is_help=True)
odds_app = typer.Typer(help="Betclic odds collection", no_args_is_help=True)
models_app = typer.Typer(help="Model training", no_args_is_help=True)
predict_app = typer.Typer(help="Predictions", no_args_is_help=True)
bet_app = typer.Typer(help="Betting", no_args_is_help=True)
executor_app = typer.Typer(help="Betclic executor (browser)", no_args_is_help=True)
control_app = typer.Typer(help="Safety controls (kill switch, pause)", no_args_is_help=True)
telegram_app = typer.Typer(help="Telegram notifications / remote control", no_args_is_help=True)
for _sub, _name in (
    (db_app, "db"),
    (ingest_app, "ingest"),
    (demos_app, "demos"),
    (odds_app, "odds"),
    (models_app, "models"),
    (predict_app, "predict"),
    (bet_app, "bet"),
    (executor_app, "executor"),
    (control_app, "control"),
    (telegram_app, "telegram"),
):
    app.add_typer(_sub, name=_name)


class CLIError(Exception):
    """An expected problem: printed as one line, exit code 1."""


def _friendly(exc: BaseException) -> str | None:
    """A one-line explanation for expected errors (None = unexpected: show the traceback)."""
    from cs2bot.services.ingestion import JobFailedError, MissingCredentialError

    if isinstance(exc, CLIError | MissingCredentialError | JobFailedError):
        return str(exc)
    name = type(exc).__name__
    if name == "PandaScoreAuthError":
        return f"PandaScore refused the token or none is set (CS2BOT_PANDASCORE__API_TOKEN): {exc}"
    if name in ("HLTVBlockedError", "DemoDownloadBlockedError"):
        return f"HLTV is blocking requests right now ({exc}); try `cs2bot ingest hltv-browser` or wait"
    if name == "ProfileLockedError":
        return f"the Betclic browser profile is in use (is `cs2bot up` running?): {exc}"
    if name == "NotEnoughDataError":
        return f"{exc}"
    if name in ("OperationalError", "InterfaceError"):
        first = str(exc).splitlines()[0][:200] if str(exc) else name
        if "connect" in first.lower() or "refused" in first.lower():
            return f"database not reachable ({first}) - is PostgreSQL running? (docker compose up -d)"
        return f"database error: {first}"
    return None


def _command[F: Callable[..., Any]](fn: F) -> F:
    """Turn expected errors into a message + exit code 1."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except typer.Exit:
            raise
        except Exception as exc:
            message = _friendly(exc)
            if message is None:
                raise
            typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None

    return wrapper  # type: ignore[return-value]


@app.callback()
def _main() -> None:
    s = get_settings()
    configure_logging(s.log_level, json_output=s.log_json)


def _settings() -> Settings:
    return get_settings()


def _factory(settings: Settings | None = None) -> sessionmaker[Session]:
    from cs2bot.db.session import make_engine, make_session_factory

    settings = settings or _settings()
    return make_session_factory(make_engine(settings.database.url))


def _print_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def _parse_day(value: str, name: str) -> datetime:
    try:
        return datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=UTC)
    except ValueError as exc:
        raise CLIError(f"{name} must be YYYY-MM-DD, got {value!r}") from exc


def _report(report: Any, *, title: str) -> None:
    data = report.as_dict() if hasattr(report, "as_dict") else report.model_dump(mode="json")
    typer.echo(f"{title}: {json.dumps(_brief(data), default=str, ensure_ascii=False)}")


def _brief(data: dict[str, Any]) -> dict[str, Any]:
    """Drop long lists from a report for the terminal."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, list) and len(value) > 10:
            out[key] = [*value[:10], f"... {len(value) - 10} more"]
        else:
            out[key] = value
    return out


def _bundle(settings: Settings) -> Any:
    from cs2bot.services.registry import load_latest

    bundle = load_latest(settings)
    if bundle is None:
        raise CLIError("no trained model found - run `cs2bot models train` first")
    return bundle


def _analyzer(settings: Settings) -> Any | None:
    if not settings.llm.enabled:
        return None
    from cs2bot.llm import ContextAnalyzer

    analyzer = ContextAnalyzer(settings)
    return analyzer if analyzer.enabled else None


def _executor(settings: Settings, factory: sessionmaker[Session]) -> Any:
    if not settings.executor.enabled:
        raise CLIError("the executor is disabled (CS2BOT_EXECUTOR__ENABLED=false)")
    from cs2bot.executor import create_betclic_executor

    return create_betclic_executor(settings, factory)


# ---------------------------------------------------------------------------------------------- basics


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
@_command
def up(
    no_api: bool = typer.Option(False, "--no-api", help="Run the scheduler without the dashboard"),
) -> None:
    """Start the API + dashboard on 127.0.0.1 and the built-in scheduler (one command, Ctrl+C stops)."""
    from cs2bot.flows.supervisor import run_up

    code = run_up(_settings(), start_api=not no_api, echo=typer.echo)
    raise typer.Exit(code)


@app.command()
@_command
def doctor() -> None:
    """Check the environment; exits 1 when something must be fixed."""
    from cs2bot.services.doctor import default_telegram_probe, run_checks

    settings = _settings()
    checks = run_checks(settings, telegram_probe=default_telegram_probe(settings))
    for check in checks:
        colour = {"ok": typer.colors.GREEN, "warn": typer.colors.YELLOW, "fail": typer.colors.RED}[
            check.level
        ]
        typer.secho(f"[{check.level.upper():>4}] {check.name}: {check.message}", fg=colour)
    failed = [c for c in checks if c.level == "fail"]
    if failed:
        typer.echo(f"{len(failed)} problem(s) found.")
        raise typer.Exit(1)
    typer.echo("all essential checks passed.")


# -------------------------------------------------------------------------------------------------- db


@db_app.command("upgrade")
@_command
def db_upgrade(revision: str = "head") -> None:
    """Apply migrations up to REVISION (default: head). Idempotent."""
    from cs2bot.services.dbops import upgrade

    upgrade(_settings().database.url, revision)
    typer.echo(f"database upgraded to {revision}")


@db_app.command("status")
@_command
def db_status() -> None:
    """Show whether the database is reachable and at the latest migration."""
    from cs2bot.db.session import make_engine
    from cs2bot.services.dbops import migration_status

    engine = make_engine(_settings().database.url)
    try:
        status = migration_status(engine)
    finally:
        engine.dispose()
    typer.echo(status.describe())
    if not status.at_head:
        raise typer.Exit(1)


@db_app.command("seed-synthetic")
@_command
def db_seed_synthetic(n_teams: int = 16, n_matches: int = 300, seed: int = 0) -> None:
    """Fill an EMPTY database with a synthetic universe (for demos / dashboard development)."""
    from sqlalchemy import select

    from cs2bot.db.models import Match
    from cs2bot.testing.synthetic import generate_universe, populate_db

    factory = _factory()
    with factory() as s:
        if s.scalar(select(Match.id).limit(1)) is not None:
            raise CLIError("the database already holds matches; seed-synthetic only fills an empty database")
        uni = generate_universe(n_teams=n_teams, n_matches=n_matches, seed=seed)
        populate_db(s, uni)
        s.commit()
    typer.echo(f"seeded {len(uni.teams)} teams, {len(uni.matches)} matches")


# ---------------------------------------------------------------------------------------------- ingest

SOURCE_HELP = "all (default), pandascore or hltv"


def _sources(source: str, settings: Settings) -> tuple[bool, bool]:
    source = source.lower()
    if source not in ("all", "pandascore", "hltv"):
        raise CLIError(f"--source must be all, pandascore or hltv (got {source!r})")
    use_ps = source in ("all", "pandascore")
    if use_ps and settings.pandascore.api_token is None:
        if source == "pandascore":
            raise CLIError("PandaScore token missing: set CS2BOT_PANDASCORE__API_TOKEN in .env")
        typer.echo("note: PandaScore skipped (CS2BOT_PANDASCORE__API_TOKEN not set)")
        use_ps = False
    return use_ps, source in ("all", "hltv")


@ingest_app.command("upcoming")
@_command
def ingest_upcoming(source: str = typer.Option("all", help=SOURCE_HELP)) -> None:
    """Sync upcoming matches (fixtures, lineups, vetoes)."""
    from cs2bot.services import ingestion

    s = _settings()
    factory = _factory(s)
    use_ps, use_hltv = _sources(source, s)
    if use_ps:
        _report(ingestion.pandascore_upcoming(factory, s), title="pandascore upcoming")
    if use_hltv:
        _report(ingestion.hltv_upcoming(factory, s), title="hltv upcoming")
    ingestion.refresh_scope(factory, s)


@ingest_app.command("results")
@_command
def ingest_results(
    since: str | None = typer.Option(None, help="YYYY-MM-DD (default: two days ago)"),
    source: str = typer.Option("all", help=SOURCE_HELP),
) -> None:
    """Sync recent results (map scores, stats, round history)."""
    from cs2bot.services import ingestion

    s = _settings()
    factory = _factory(s)
    start = _parse_day(since, "--since") if since else None
    use_ps, use_hltv = _sources(source, s)
    if use_ps:
        _report(ingestion.pandascore_results(factory, s, since=start), title="pandascore results")
    if use_hltv:
        _report(ingestion.hltv_results(factory, s, since=start), title="hltv results")


@ingest_app.command("backfill")
@_command
def ingest_backfill(
    since: str = typer.Option("2023-09-27", help="YYYY-MM-DD (default: the CS2 release)"),
    until: str | None = typer.Option(None, help="YYYY-MM-DD (default: now)"),
    source: str = typer.Option("all", help=SOURCE_HELP),
    no_resume: bool = typer.Option(False, "--no-resume", help="Start from --since again"),
) -> None:
    """Historical backfill (slow and polite, resumable: run it again to continue)."""
    from cs2bot.services import ingestion

    s = _settings()
    use_ps, use_hltv = _sources(source, s)
    report = ingestion.backfill(
        _factory(s),
        s,
        _parse_day(since, "--since"),
        until=_parse_day(until, "--until") if until else None,
        use_pandascore=use_ps,
        use_hltv=use_hltv,
        resume=not no_resume,
    )
    _report(report, title="backfill")


@ingest_app.command("rankings")
@_command
def ingest_rankings() -> None:
    """Sync the latest HLTV world ranking (and refresh which matches are in scope)."""
    from cs2bot.services import ingestion

    s = _settings()
    _report(ingestion.hltv_rankings(_factory(s), s), title="hltv rankings")


@ingest_app.command("news")
@_command
def ingest_news(months: int = typer.Option(1, help="How many months of the news archive")) -> None:
    """Sync HLTV news and let Claude triage the new items (when an Anthropic key is set)."""
    from cs2bot.services import ingestion

    s = _settings()
    factory = _factory(s)
    _report(ingestion.hltv_news(factory, s, months=months), title="hltv news")
    analyzer = _analyzer(s)
    if analyzer is None:
        typer.echo("note: news triage skipped (LLM disabled or no Anthropic key)")
        return
    try:
        triage = ingestion.triage_news(factory, analyzer)
        typer.echo(f"news triage: {triage.status} triaged={triage.triaged} candidates={triage.candidates}")
    finally:
        analyzer.close()


@ingest_app.command("match")
@_command
def ingest_match(hltv_id: int = typer.Argument(..., help="HLTV match id")) -> None:
    """Sync one HLTV match page (and its map stats), whatever its scope."""
    from cs2bot.services import ingestion

    s = _settings()
    _report(ingestion.hltv_match(_factory(s), s, hltv_id), title=f"hltv match {hltv_id}")


@ingest_app.command("scope")
@_command
def ingest_scope() -> None:
    """Re-evaluate which open matches are in scope (tier / HLTV top N)."""
    from cs2bot.services import ingestion

    s = _settings()
    typer.echo(f"in_scope flags changed: {ingestion.refresh_scope(_factory(s), s)}")


@ingest_app.command("hltv-browser")
@_command
def ingest_hltv_browser(
    url: str | None = typer.Option(None, help="HLTV page to open (default: front page)"),
    wait_s: float = typer.Option(600.0, help="Seconds to keep the window open"),
) -> None:
    """Open HLTV in a visible window with the bot's browser profile (pass a challenge by hand).

    Stop `cs2bot up` first: one browser profile can only be used by one browser.
    """
    from cs2bot.ingest.hltv import open_browser_for_manual_check

    result = open_browser_for_manual_check(_settings(), url=url, wait_s=wait_s)
    typer.echo(f"cleared={result.cleared} window_closed={result.window_closed} waited={result.waited_s:.0f}s")
    if not result.cleared:
        raise typer.Exit(1)


# ----------------------------------------------------------------------------------------------- demos


@demos_app.command("process")
@_command
def demos_process(limit: int = typer.Option(5, help="Maximum demos to process")) -> None:
    """Download + parse pending demos, then rebuild position / duel aggregates and roles."""
    from cs2bot.services import ingestion

    s = _settings()
    _report(ingestion.process_demos(_factory(s), s, limit=limit), title="demos")


@demos_app.command("aggregates")
@_command
def demos_aggregates() -> None:
    """Rebuild position / duel aggregates and inferred player roles."""
    from cs2bot.services import ingestion

    _print_json(ingestion.rebuild_aggregates(_factory()))


# ------------------------------------------------------------------------------------------------ odds


@odds_app.command("collect")
@_command
def odds_collect(
    once: bool = typer.Option(False, "--once", help="One pass, then exit"),
    interval_s: int | None = typer.Option(
        None, help="Seconds between passes (default betclic.poll_interval_s)"
    ),
) -> None:
    """Collect Betclic CS2 odds (every few minutes until Ctrl+C, or once)."""
    from cs2bot.odds import MarketMapper
    from cs2bot.services import ingestion

    s = _settings()
    factory = _factory(s)
    mapper = MarketMapper.from_yaml(s.betclic.market_rules_file)
    source = ingestion.build_odds_source(s)
    pause = interval_s or s.betclic.poll_interval_s
    try:
        while True:
            report = ingestion.collect_odds(factory, s, source=source, mapper=mapper)
            typer.echo(
                f"{datetime.now(UTC):%H:%M:%S} events={report.events_seen} markets={report.markets_seen} "
                f"snapshots={report.snapshots_written} unmapped={len(report.unmapped_markets)} "
                f"errors={len(report.errors)}"
            )
            if once:
                break
            time.sleep(pause)
    except KeyboardInterrupt:
        typer.echo("stopped.")
    finally:
        if hasattr(source, "close"):
            source.close()


@odds_app.command("unmapped")
@_command
def odds_unmapped(limit: int = typer.Option(50, help="How many market names")) -> None:
    """List Betclic market names that no rule maps (extend config/betclic_markets.yaml)."""
    from cs2bot.odds import MarketMapper, list_unmapped

    s = _settings()
    mapper = MarketMapper.from_yaml(s.betclic.market_rules_file)
    with _factory(s)() as session:
        rows = list_unmapped(session, limit=limit)
    if not rows:
        typer.echo("no unmapped markets")
        return
    for name, count in rows:
        rule = mapper.match_rule(name)
        typer.echo(
            f"{count:6d}  {name}" + (f"   (rule {rule} matches the name, not the selections)" if rule else "")
        )


# ---------------------------------------------------------------------------------------------- models


@models_app.command("train")
@_command
def models_train(
    as_of: str | None = typer.Option(None, help="Train on data before YYYY-MM-DD (default: now)"),
    ensemble: bool | None = typer.Option(None, "--ensemble/--no-ensemble", help="Default: when enough data"),
) -> None:
    """Fit ratings, veto, round, kill and (with enough data) ensemble models; save the bundle."""
    from cs2bot.services.registry import ModelRegistry

    s = _settings()
    registry = ModelRegistry(s)
    report = registry.retrain(
        _factory(s), as_of=_parse_day(as_of, "--as-of") if as_of else None, use_ensemble=ensemble
    )
    typer.echo(
        f"trained {report.version}: {report.n_map_results} maps, {report.n_vetoes} vetoes, "
        f"{report.n_rounds} rounds, {report.n_player_maps} player lines; "
        f"ensemble={report.ensemble_fitted and not report.ensemble_fallback}"
    )
    for warning in report.warnings:
        typer.echo(f"warning: {warning}")
    typer.echo(f"saved to {report.path}")


DEFAULT_TUNE_GRID: dict[str, list[Any]] = {
    "initial_sigma": [0.04, 0.06, 0.09],
    "map_offset_sd": [0.3, 0.6, 0.9],
    "mov_weight": [0.0, 0.1],
    "pick_advantage": [0.0, 0.1],
}


@models_app.command("tune")
@_command
def models_tune(
    holdout: float = typer.Option(0.25, help="Share of the most recent maps used for evaluation"),
    grid: str | None = typer.Option(None, help="JSON grid, e.g. '{\"tau\": [0.3, 0.5]}' (default: built-in)"),
) -> None:
    """Grid-search the rating model on a chronological holdout; the next training uses the best config."""
    from cs2bot import datasets
    from cs2bot.models.ratings import tune
    from cs2bot.services.registry import load_rating_config, save_rating_config

    s = _settings()
    try:
        search = json.loads(grid) if grid else DEFAULT_TUNE_GRID
    except json.JSONDecodeError as exc:
        raise CLIError(f"--grid is not valid JSON: {exc}") from exc
    with _factory(s)() as session:
        records = datasets.load_map_results(session)
    if len(records) < 50:
        raise CLIError(f"only {len(records)} map results: ingest more history before tuning")
    best, report = tune(records, search, holdout_fraction=holdout, base_config=load_rating_config(s))
    path = save_rating_config(s, best, report)
    typer.echo(
        f"best {report['best_params']}: log-loss {report['best_log_loss']:.4f} "
        f"(coin flip {report['baseline_log_loss']:.4f}); saved to {path}"
    )


@models_app.command("list")
@_command
def models_list() -> None:
    """List saved model bundles (newest first)."""
    from cs2bot.services.registry import list_bundles

    for trained_at, path in list_bundles(_settings()):
        typer.echo(f"{trained_at.isoformat()}  {path}")


# --------------------------------------------------------------------------------------------- predict


@predict_app.command("upcoming")
@_command
def predict_upcoming_cmd(
    force: bool = typer.Option(False, "--force", help="Re-simulate every match"),
) -> None:
    """Predict every in-scope match inside the betting window and price its Betclic outcomes."""
    from cs2bot.services.prediction import predict_upcoming

    s = _settings()
    analyzer = _analyzer(s)
    try:
        report = predict_upcoming(_factory(s), s, _bundle(s), analyzer=analyzer, force=force)
    finally:
        if analyzer is not None:
            analyzer.close()
    _report(report, title="predictions")


@predict_app.command("match")
@_command
def predict_match_cmd(match_id: int = typer.Argument(..., help="Internal match id")) -> None:
    """Predict one match (stored like the scheduled job) and print the headline numbers."""
    from cs2bot.services.prediction import predict_match

    s = _settings()
    analyzer = _analyzer(s)
    try:
        with _factory(s)() as session:
            prediction = predict_match(session, match_id, _bundle(s), s, analyzer=analyzer)
            if prediction is None:
                raise CLIError(f"match {match_id} cannot be predicted (unknown match or teams not known)")
            session.commit()
    finally:
        if analyzer is not None:
            analyzer.close()
    central = prediction.central
    typer.echo(
        f"match {match_id}: P(team1 wins)={central.p_team1_win:.3f} (sigma {prediction.sigma_team1_win:.3f}) "
        f"model={prediction.model_version} completeness={prediction.data_completeness:.2f} "
        f"veto_known={prediction.veto_known}"
    )
    if central.map_score_pmf:
        scores = ", ".join(f"{k}: {v:.3f}" for k, v in sorted(central.map_score_pmf.items()))
        typer.echo(f"map scores: {scores}")


# ------------------------------------------------------------------------------------------------- bet


@bet_app.command("run")
@_command
def bet_run(
    dry_run: bool = typer.Option(False, "--dry-run", help="Record paper bets only (never clicks)"),
    no_executor: bool = typer.Option(False, "--no-executor", help="Record decisions only, open no browser"),
) -> None:
    """One betting cycle: decide + stake + execute for upcoming matches.

    Real bets need CS2BOT_EXECUTOR__DRY_RUN=false and no --dry-run. Do not run this while `cs2bot up`
    is running (both would use the same Betclic profile).
    """
    from cs2bot.services.betting import run_betting_cycle

    s = _settings()
    factory = _factory(s)
    bundle = _bundle(s)
    executor = None if no_executor else _executor(s, factory)
    live = not (dry_run or s.executor.dry_run)
    typer.echo(
        "mode: LIVE - real money" if live and executor is not None else "mode: dry run / decisions only"
    )
    try:
        report = run_betting_cycle(factory, s, executor, bundle, dry_run=dry_run)
    finally:
        if executor is not None:
            executor.close()
    _report(report, title="betting cycle")
    for b in report.bets:
        typer.echo(
            f"  bet {b.bet_id}: match {b.match_id} {b.spec_key} {b.selection_key} "
            f"{b.stake} @ {b.odds} -> {b.placement}"
        )


@app.command()
@_command
def settle(
    no_executor: bool = typer.Option(
        False, "--no-executor", help="Settle from match results only (no browser)"
    ),
) -> None:
    """Settle finished bets: bet history, results, closing odds, CLV, ledger, balance reconciliation."""
    from cs2bot.services.settlement import settle_bets

    s = _settings()
    factory = _factory(s)
    executor = None if no_executor or not s.executor.enabled else _executor(s, factory)
    try:
        report = settle_bets(factory, s, executor)
    finally:
        if executor is not None:
            executor.close()
    _report(report, title="settlement")


@app.command()
@_command
def backtest(
    date_from: str = typer.Option(..., "--from", help="First kick-off day, YYYY-MM-DD"),
    date_to: str = typer.Option(..., "--to", help="Last kick-off day (exclusive), YYYY-MM-DD"),
    sims: int = typer.Option(2000, help="Map simulations per prediction"),
    ensemble: bool = typer.Option(
        True,
        "--ensemble/--no-ensemble",
        help="Use the LightGBM ensemble like the live bot does (it falls back to the ratings on little data)",
    ),
    output: Path | None = typer.Option(None, help="Also write the Markdown report to this file"),
) -> None:
    """Walk-forward backtest over stored history and Betclic odds; stored in model_runs (kind backtest)."""
    from cs2bot.backtest import BacktestConfig, PipelineConfig, run_db_backtest
    from cs2bot.db.models import ModelRun
    from cs2bot.domain.enums import JobStatus
    from cs2bot.services.registry import load_rating_config
    from cs2bot.utils.time import utcnow

    s = _settings()
    # the live model uses the `models tune` result: so does the backtest (it was tuned on a holdout that
    # may overlap the backtest window - a mild optimism, stated in the report notes)
    rating_config = load_rating_config(s)
    since, until = _parse_day(date_from, "--from"), _parse_day(date_to, "--to")
    if until <= since:
        raise CLIError("--to must be after --from")
    started = utcnow()
    with _factory(s)() as session:
        report = run_db_backtest(
            session,
            settings=s,
            since=since,
            until=until,
            config=BacktestConfig(retrain_every=timedelta(days=7)),
            pipeline_config=PipelineConfig(
                simulate=True, n_sims=sims, use_ensemble=ensemble, rating_config=rating_config
            ),
        )
        if rating_config is not None:
            report.warnings.append(
                "rating model: the tuned configuration of `cs2bot models tune` (tuned on a holdout that may "
                "overlap this window)"
            )
        session.add(
            ModelRun(
                kind="backtest",
                model_version=f"bt{started:%Y%m%dT%H%M%S}",
                status=JobStatus.SUCCESS,
                started_at=started,
                finished_at=utcnow(),
                params={
                    "from": date_from,
                    "to": date_to,
                    "sims": sims,
                    "ensemble": ensemble,
                    "rating_config": "tuned" if rating_config is not None else "default",
                },
                metrics=json.loads(report.model_dump_json()),
            )
        )
        session.commit()
    text = report.to_markdown()
    typer.echo(text)
    if output is not None:
        output.write_text(text, encoding="utf-8")
        typer.echo(f"written to {output}")


# -------------------------------------------------------------------------------------------- executor


@executor_app.command("login")
@_command
def executor_login(
    timeout_s: float = typer.Option(600.0, help="Seconds to wait for the manual login"),
) -> None:
    """Open the Betclic login window (visible browser, bot profile); exit 0 only when logged in.

    Run it while `cs2bot up` is NOT running (or use the dashboard's login button instead).
    """
    s = _settings()
    factory = _factory(s)
    executor = _executor(s, factory)
    try:
        ok = bool(executor.open_login_window(timeout_s=timeout_s))
        if ok:
            from cs2bot.services.health import refresh_executor_health

            refresh_executor_health(factory, executor)
    finally:
        executor.close()
    typer.echo("logged in." if ok else "login not completed.")
    if not ok:
        raise typer.Exit(1)


@executor_app.command("health")
@_command
def executor_health() -> None:
    """Check login, balance and the page selectors; stored for the dashboard."""
    from cs2bot.services.health import refresh_executor_health

    s = _settings()
    factory = _factory(s)
    executor = _executor(s, factory)
    try:
        health = refresh_executor_health(factory, executor)
    finally:
        executor.close()
    _print_json(health.model_dump(mode="json"))
    if not health.ok:
        raise typer.Exit(1)


@executor_app.command("balance")
@_command
def executor_balance() -> None:
    """Print the Betclic balance read from the page header (and the ledger balance)."""
    from cs2bot.engine import ledger

    s = _settings()
    factory = _factory(s)
    executor = _executor(s, factory)
    try:
        balance = executor.get_balance()
    finally:
        executor.close()
    with factory() as session:
        book = ledger.current_balance(session)
    typer.echo(
        f"Betclic balance: {balance if balance is not None else 'unavailable (logged out?)'}; ledger: {book}"
    )
    if balance is None:
        raise typer.Exit(1)


# --------------------------------------------------------------------------------------------- control


@control_app.command("kill-switch")
@_command
def control_kill_switch(
    state: str = typer.Argument(..., help="on or off"),
    reason: str = typer.Option("", help="Why (shown on the dashboard)"),
) -> None:
    """Switch the global kill switch on or off (checked before every bet)."""
    from cs2bot.db import state as db_state

    value = state.strip().lower()
    if value not in ("on", "off"):
        raise CLIError("kill-switch takes 'on' or 'off'")
    with _factory()() as session:
        db_state.set_kill_switch(session, value == "on", actor="cli", reason=reason)
        session.commit()
    typer.echo(f"kill switch {value}")


@control_app.command("pause")
@_command
def control_pause(reason: str = typer.Option("paused from the command line", help="Why")) -> None:
    """Pause betting (data collection and predictions continue)."""
    from cs2bot.db import state as db_state

    with _factory()() as session:
        db_state.set_paused(session, True, actor="cli", reason=reason)
        session.commit()
    typer.echo("paused")


@control_app.command("resume")
@_command
def control_resume() -> None:
    """Resume betting after a pause (a loss-limit pause too: check the reason first)."""
    from cs2bot.db import state as db_state

    with _factory()() as session:
        paused, reason = db_state.get_pause(session)
        db_state.set_paused(session, False, actor="cli", reason="resumed from the command line")
        session.commit()
    typer.echo(f"resumed (was paused: {reason})" if paused else "resumed (was not paused)")


@control_app.command("status")
@_command
def control_status() -> None:
    """Show kill switch, every pause reason (auto-resume ETA, circuit breaker) and the betting mode."""
    from cs2bot.db import state as db_state
    from cs2bot.services.autonomy import pause_overview
    from cs2bot.utils.time import to_local

    s = _settings()
    with _factory(s)() as session:
        kill = db_state.get_state(session, db_state.KILL_SWITCH)
        overview = pause_overview(session, s)
    typer.echo(f"kill switch: {'ON' if kill.get('on') else 'off'} {kill.get('reason') or ''}".rstrip())
    if not overview["paused"]:
        typer.echo("paused: no")
    else:
        typer.echo("paused: yes")
        for r in overview["reasons"]:
            how = "lifts automatically" if r["auto_resume"] else "needs the owner"
            eta = ""
            if r["eta"]:
                local = to_local(datetime.fromisoformat(r["eta"]), s.timezone)
                eta = f", expected {local:%Y-%m-%d %H:%M} Warsaw"
            typer.echo(f"  - {r['code']} ({how}{eta}) since {r['since']}: {r['message']}")
        if overview["circuit_open"]:
            typer.echo("  circuit breaker OPEN: resume by hand after checking (cs2bot control resume)")
    typer.echo(
        f"mode: {'dry run' if s.executor.dry_run else 'LIVE'}; "
        f"executor {'enabled' if s.executor.enabled else 'disabled'}; "
        f"autonomy {'on' if s.autonomy.enabled else 'off'}"
        + (" (auto-login on)" if s.autonomy.enabled and s.autonomy.auto_login else "")
        + f"; telegram {'on' if s.telegram.enabled else 'off'}"
    )


# -------------------------------------------------------------------------------------------- telegram


@telegram_app.command("test")
@_command
def telegram_test(message: str = typer.Option("cs2bot: Telegram test message", help="Text to send")) -> None:
    """Send a test message to the configured chat (checks the token and the chat id)."""
    from cs2bot.services.telegram import TelegramClient

    s = _settings()
    if not s.telegram.enabled:
        raise CLIError("Telegram is disabled: set CS2BOT_TELEGRAM__ENABLED=true")
    client = TelegramClient.from_settings(s)
    if client is None:
        raise CLIError("set CS2BOT_TELEGRAM__BOT_TOKEN and CS2BOT_TELEGRAM__CHAT_ID")
    try:
        ok = client.send(message)
    finally:
        client.close()
    if not ok:
        raise CLIError(f"sending failed: {client.last_error}")
    typer.echo(f"sent to chat {s.telegram.chat_id}")


if __name__ == "__main__":
    app()
