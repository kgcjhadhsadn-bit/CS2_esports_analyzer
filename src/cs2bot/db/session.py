"""Engine / session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from cs2bot.config import get_settings


def make_engine(url: str | None = None, *, echo: bool | None = None) -> Engine:
    """Create an engine. SQLite URLs get FK enforcement; in-memory SQLite shares one connection."""
    settings = get_settings()
    url = url or settings.database.url
    echo = settings.database.echo if echo is None else echo
    if url.startswith("sqlite"):
        kwargs: dict[str, object] = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url in ("sqlite://", "sqlite+pysqlite://"):
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, echo=echo, **kwargs)

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    return create_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return make_engine()


def make_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False, autoflush=False)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return make_session_factory()


@contextmanager
def session_scope(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    session = (factory or get_session_factory())()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_cache() -> None:
    get_session_factory.cache_clear()
    get_engine.cache_clear()
