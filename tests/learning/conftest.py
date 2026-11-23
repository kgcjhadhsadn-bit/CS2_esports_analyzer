"""Fixtures shared by the datasets / features tests.

``learning_db`` is a read-only session on one SQLite database populated once per test session with
a synthetic universe (rounds and kills included). Tests that change data use the function-scoped
``session`` fixture from ``tests/conftest.py`` together with ``learning_helpers.populate_small``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import cs2bot.db.models  # noqa: F401 - register tables
from cs2bot.db.base import Base
from cs2bot.testing.synthetic import SynthUniverse, generate_universe, populate_db


@pytest.fixture(scope="session")
def learning_universe() -> SynthUniverse:
    return generate_universe(n_teams=10, n_matches=150, seed=11, days=300)


@pytest.fixture(scope="session")
def learning_engine(learning_universe: SynthUniverse) -> Iterator[Engine]:
    # Built without cs2bot.db.session.make_engine: that reads Settings, and this fixture is created
    # before the per-test environment isolation runs.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)() as s:
        populate_db(s, learning_universe, with_rounds=True, with_kills=True)
        s.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def learning_db(learning_engine: Engine) -> Iterator[Session]:
    s = sessionmaker(bind=learning_engine, expire_on_commit=False, autoflush=False)()
    try:
        yield s
    finally:
        s.rollback()
        s.close()
