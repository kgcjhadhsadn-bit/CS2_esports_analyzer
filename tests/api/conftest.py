"""Fixtures for the dashboard API tests: a seeded SQLite DB, a fixed clock and TestClients.

The dataset is described in :mod:`tests.api.api_seed`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.api import ApiOptions, create_app
from cs2bot.config import Settings
from tests.api.api_seed import BASE_URL, NOW, Seed, make_settings, seed_database


@pytest.fixture
def seeded(session_factory: sessionmaker[Session], tmp_path: Path) -> Seed:
    with session_factory() as s:
        seed = seed_database(s, tmp_path)
        s.commit()
    return seed


AppFactory = Callable[..., FastAPI]


@pytest.fixture
def app_factory(session_factory: sessionmaker[Session], tmp_path: Path) -> AppFactory:
    """Build an app on the test DB with a fixed clock: ``app_factory(api={...}, login_launcher=...)``."""

    def build(
        *,
        settings: Settings | None = None,
        login_launcher: Callable[[], None] | None = None,
        executor_health_provider: Any = None,
        event_remapper: Any = None,
        scheduler_status_provider: Any = None,
        options: ApiOptions | None = None,
        now: datetime = NOW,
        **sections: Any,
    ) -> FastAPI:
        app = create_app(
            settings or make_settings(tmp_path, **sections),
            session_factory,
            login_launcher=login_launcher,
            executor_health_provider=executor_health_provider,
            event_remapper=event_remapper,
            scheduler_status_provider=scheduler_status_provider,
            options=options,
        )
        app.state.cs2bot.clock = lambda: now
        return app

    return build


@pytest.fixture
def client(app_factory: AppFactory, seeded: Seed) -> Iterator[TestClient]:
    with TestClient(app_factory(), base_url=BASE_URL) as c:
        yield c


@pytest.fixture
def auth_settings(tmp_path: Path) -> Settings:
    return make_settings(
        tmp_path, api={"password": SecretStr("s3cret-pass"), "session_secret": SecretStr("x" * 32)}
    )
