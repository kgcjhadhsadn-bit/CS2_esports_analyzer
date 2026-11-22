"""Command-line entry point: ``cs2bot <group> <command>``.

The integration layer registers more command groups here (ingest, odds, demos, models, predict,
bet, settle, backtest, executor, api). Keep commands thin: parse args, call a service function.
"""

from __future__ import annotations

from pathlib import Path

import typer

from cs2bot import __version__
from cs2bot.config import get_settings
from cs2bot.utils.logging import configure_logging

app = typer.Typer(help="CS2 analyzer & Betclic betting bot", no_args_is_help=True)
db_app = typer.Typer(help="Database management", no_args_is_help=True)
app.add_typer(db_app, name="db")


@app.callback()
def _main() -> None:
    s = get_settings()
    configure_logging(s.log_level, json_output=s.log_json)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


def _alembic_config() -> object:
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).parent / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database.url)
    return cfg


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Apply migrations up to REVISION (default: head)."""
    from alembic import command

    command.upgrade(_alembic_config(), revision)  # type: ignore[arg-type]
    typer.echo(f"database upgraded to {revision}")


@db_app.command("seed-synthetic")
def db_seed_synthetic(n_teams: int = 16, n_matches: int = 300, seed: int = 0) -> None:
    """Fill an EMPTY database with a synthetic universe (for demos / dashboard development)."""
    from cs2bot.db.session import session_scope
    from cs2bot.testing.synthetic import generate_universe, populate_db

    uni = generate_universe(n_teams=n_teams, n_matches=n_matches, seed=seed)
    with session_scope() as s:
        populate_db(s, uni)
    typer.echo(f"seeded {len(uni.teams)} teams, {len(uni.matches)} matches")


if __name__ == "__main__":
    app()
