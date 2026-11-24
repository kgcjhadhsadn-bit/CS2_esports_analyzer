"""Shared pytest fixtures.

* ``engine`` / ``session``: fresh in-memory SQLite with the full schema (fast, default).
* ``pg_session``: PostgreSQL session, only when CS2BOT_TEST_DATABASE_URL is set (marker ``postgres``).
* ``synthetic_universe``: small deterministic synthetic CS2 universe (module-scoped, cached).
* ``settings``: isolated Settings (env vars prefixed CS2BOT_ are ignored except DB URL overrides).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import cs2bot.db.models  # noqa: F401 - register tables
from cs2bot.config import Settings, reset_settings_cache
from cs2bot.db.base import Base
from cs2bot.db.session import make_engine
from cs2bot.testing.synthetic import SynthUniverse, generate_universe


def pytest_configure(config: pytest.Config) -> None:
    """Give every pytest-xdist worker its own PostgreSQL test database.

    The PostgreSQL tests drop and create tables in the database named by CS2BOT_TEST_DATABASE_URL, so
    parallel workers sharing it would destroy each other's tables. Each worker ``gwN`` uses (and creates
    if needed) ``<database>_gwN`` instead. Without the permission to create databases the shared one is
    kept (then run the postgres tests with ``-n 0``).
    """
    url = os.environ.get("CS2BOT_TEST_DATABASE_URL")
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not url or not worker:
        return
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    base = make_url(url)
    name = f"{base.database}_{worker}"
    try:
        admin = create_engine(base, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
        admin.dispose()
    except Exception:
        return
    os.environ["CS2BOT_TEST_DATABASE_URL"] = base.set(database=name).render_as_string(hide_password=False)


@pytest.fixture(autouse=True)
def _isolated_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    """Keep tests independent of the developer's .env and real credentials."""
    for key in list(os.environ):
        if key.startswith("CS2BOT_") and key != "CS2BOT_TEST_DATABASE_URL":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CS2BOT_ENVIRONMENT", "test")
    monkeypatch.setenv("CS2BOT_DATABASE__URL", "sqlite://")
    monkeypatch.setenv("CS2BOT_DATA_DIR", str(tmp_path_factory.mktemp("data")))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))  # no stray .env picked up
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite://")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    s = session_factory()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def pg_session() -> Iterator[Session]:
    url = os.environ.get("CS2BOT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("CS2BOT_TEST_DATABASE_URL not set")
    eng = make_engine(url)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng, expire_on_commit=False)()
    try:
        yield s
    finally:
        s.rollback()
        s.close()
        Base.metadata.drop_all(eng)
        eng.dispose()


_UNIVERSE_CACHE: dict[tuple[int, int, int], SynthUniverse] = {}


def get_synthetic_universe(n_teams: int = 12, n_matches: int = 250, seed: int = 7) -> SynthUniverse:
    key = (n_teams, n_matches, seed)
    if key not in _UNIVERSE_CACHE:
        _UNIVERSE_CACHE[key] = generate_universe(n_teams=n_teams, n_matches=n_matches, seed=seed)
    return _UNIVERSE_CACHE[key]


@pytest.fixture(scope="session")
def synthetic_universe() -> SynthUniverse:
    return get_synthetic_universe()
