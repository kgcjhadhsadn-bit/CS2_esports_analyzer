"""deep_data: every PandaScore sync path works without a token (one note, no network, no DB writes)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cs2bot.config import PandaScoreSettings, Settings
from cs2bot.db.models import Match
from cs2bot.ingest.pandascore import (
    DISABLED_NOTE,
    PandaScoreClient,
    pandascore_enabled,
    sync_match,
    sync_modified,
    sync_past,
    sync_upcoming,
)
from cs2bot.ingest.pandascore import sync as sync_module


@respx.mock(assert_all_called=False)
def test_all_syncs_are_disabled_without_a_token(
    session: Session, settings: Settings, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = respx.route(host="api.pandascore.co").mock(return_value=httpx.Response(500))
    monkeypatch.setattr(sync_module, "_disabled_logged", False)
    client = PandaScoreClient(PandaScoreSettings(api_token=None))
    assert not pandascore_enabled(client) and not pandascore_enabled(None)
    since = datetime(2026, 11, 1, tzinfo=UTC)
    with caplog.at_level(logging.INFO, logger="cs2bot.ingest.pandascore.sync"):
        reports = [
            sync_upcoming(session, client, settings),
            sync_past(session, client, settings, since),
            sync_modified(session, client, settings, since),
            sync_match(session, client, settings, 123),
            sync_upcoming(session, client, settings),
        ]
    for report in reports:
        assert report.details["disabled"] is True
        assert report.ok and report.items == 0 and report.warnings == [DISABLED_NOTE]
    assert not route.called  # nothing was sent
    assert session.scalar(select(func.count()).select_from(Match)) == 0
    notes = [r for r in caplog.records if DISABLED_NOTE in r.getMessage()]
    assert len(notes) == 1  # a single note, no error spam
    assert all(r.levelno < logging.WARNING for r in notes)


def test_blank_token_counts_as_no_token() -> None:
    client = PandaScoreClient(PandaScoreSettings(api_token=" "))  # type: ignore[arg-type]
    assert not pandascore_enabled(client)
    assert pandascore_enabled(PandaScoreClient(PandaScoreSettings(api_token="tok")))  # type: ignore[arg-type]
