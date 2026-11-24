"""Database operations used by ``cs2bot db upgrade``, ``cs2bot doctor`` and ``cs2bot up``.

The Alembic configuration is built in code (script location inside the installed package), so it
works from any working directory and does not depend on ``alembic.ini`` being next to the code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from sqlalchemy import Engine, text

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "db" / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # '%' must be escaped for ConfigParser (passwords may contain it)
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    """Apply migrations up to ``revision`` (idempotent)."""
    from alembic import command

    command.upgrade(alembic_config(url), revision)


@dataclass(frozen=True)
class MigrationStatus:
    reachable: bool
    current: tuple[str, ...]
    heads: tuple[str, ...]
    error: str | None = None

    @property
    def at_head(self) -> bool:
        return self.reachable and bool(self.heads) and set(self.current) == set(self.heads)

    def describe(self) -> str:
        if not self.reachable:
            return f"database not reachable: {self.error}"
        if self.at_head:
            return f"migrations at head ({', '.join(self.heads)})"
        current = ", ".join(self.current) or "none"
        heads = ", ".join(self.heads)
        return f"migrations not at head (database: {current}, code: {heads}) - run `cs2bot db upgrade`"


def migration_status(engine: Engine) -> MigrationStatus:
    """Whether the database answers and which Alembic revision it is at (never raises)."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    heads = tuple(ScriptDirectory.from_config(alembic_config(str(engine.url))).get_heads())
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            current = tuple(MigrationContext.configure(conn).get_current_heads())
    except Exception as exc:
        return MigrationStatus(False, (), heads, f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}")
    return MigrationStatus(True, current, heads)
