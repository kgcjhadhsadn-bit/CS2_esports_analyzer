"""deep_data: HLTV proxy fallback after a block, its own cool-down, and the HLTV-derived tier."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.config import HLTVSettings, Settings
from cs2bot.db.models import Match
from cs2bot.domain.enums import Tier
from cs2bot.ingest.hltv import HLTVBlockedError, HLTVFetcher, LoadedPage, sync_results
from cs2bot.ingest.hltv.fetcher import (
    COOLDOWN_FILE,
    PROXY_COOLDOWN_FILE,
    FetchOptions,
    direct_settings,
    proxy_profile_dir,
)
from cs2bot.ingest.scope import derive_tier
from tests.ingest.hltv.conftest import FakeClock, FakeFetcher, load_fixture

MATCH_HTML = load_fixture("match_bo3_finished.html")
CHALLENGE_HTML = load_fixture("challenge_just_a_moment.html")


class Loader:
    def __init__(self, *responses: LoadedPage | Exception) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url: str) -> LoadedPage:
        self.urls.append(url)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def _settings(tmp_path: Path, **kw: object) -> HLTVSettings:
    return HLTVSettings(cache_dir=tmp_path / "cache", proxy_url=SecretStr("http://u:pw@px:3128"), **kw)  # type: ignore[arg-type]


def _fetcher(settings: HLTVSettings, clock: FakeClock, direct: Loader, proxy: Loader | None) -> HLTVFetcher:
    return HLTVFetcher(
        settings,
        page_loader=direct,
        proxy_page_loader=proxy,
        sleep=clock.sleep,
        clock=clock.monotonic,
        now=clock.now,
        options=FetchOptions(cooldown_base_s=600.0),
        rng=random.Random(1),
    )


def test_blocked_direct_fetch_is_retried_through_the_proxy(tmp_path: Path, clock: FakeClock) -> None:
    direct = Loader(LoadedPage(CHALLENGE_HTML, 403))
    proxy = Loader(LoadedPage(MATCH_HTML, 200))
    fetcher = _fetcher(_settings(tmp_path), clock, direct, proxy)
    assert fetcher.proxy_fallback_enabled
    html = fetcher.get("/matches/2378001/x")
    assert html == MATCH_HTML
    assert len(direct.urls) == 1 and len(proxy.urls) == 1
    # the direct route is cooling down, the proxy route is healthy
    assert fetcher.blocked_until is not None and fetcher.proxy_blocked_until is None
    # next fetch: direct is skipped (cool-down, no network), the proxy serves it
    fetcher.get("/matches/2378002/y")
    assert len(direct.urls) == 1 and len(proxy.urls) == 2


def test_proxy_route_has_its_own_cooldown_and_never_solves_challenges(
    tmp_path: Path, clock: FakeClock
) -> None:
    settings = _settings(tmp_path, proxy_cooldown_base_s=1200.0)
    direct = Loader(LoadedPage(CHALLENGE_HTML, 200))
    proxy = Loader(LoadedPage(CHALLENGE_HTML, 200))
    fetcher = _fetcher(settings, clock, direct, proxy)
    with pytest.raises(HLTVBlockedError) as exc:
        fetcher.get("/matches/2378001/x")
    assert "proxy" in str(exc.value)
    assert fetcher.proxy_blocked_until == clock.now() + timedelta(seconds=1200)
    data = json.loads((tmp_path / "cache" / PROXY_COOLDOWN_FILE).read_text())
    assert data["consecutive_blocks"] == 1
    assert (tmp_path / "cache" / COOLDOWN_FILE).exists()
    # both routes cooling down: no network at all
    with pytest.raises(HLTVBlockedError) as again:
        fetcher.get("/matches/2378001/x")
    assert again.value.reason == "cooldown"
    assert len(direct.urls) == 1 and len(proxy.urls) == 1
    # after the proxy cool-down the proxy is tried again; success clears its state
    clock.wall += timedelta(seconds=1201)
    proxy.responses = [LoadedPage(MATCH_HTML, 200)]
    assert fetcher.get("/matches/2378001/x") == MATCH_HTML
    assert fetcher.proxy_blocked_until is None
    assert not (tmp_path / "cache" / PROXY_COOLDOWN_FILE).exists()


def test_proxy_cooldown_survives_a_restart(tmp_path: Path, clock: FakeClock) -> None:
    settings = _settings(tmp_path)
    fetcher = _fetcher(
        settings, clock, Loader(LoadedPage(CHALLENGE_HTML)), Loader(LoadedPage(CHALLENGE_HTML))
    )
    with pytest.raises(HLTVBlockedError):
        fetcher.get("/matches/1/x")
    fresh_direct, fresh_proxy = Loader(LoadedPage(MATCH_HTML)), Loader(LoadedPage(MATCH_HTML))
    restarted = _fetcher(settings, clock, fresh_direct, fresh_proxy)
    with pytest.raises(HLTVBlockedError):
        restarted.get("/matches/1/x")
    assert fresh_direct.urls == [] and fresh_proxy.urls == []


def test_always_mode_and_no_proxy_keep_one_route(tmp_path: Path, clock: FakeClock) -> None:
    always = _settings(tmp_path, proxy_mode="always")
    direct = Loader(LoadedPage(CHALLENGE_HTML))
    proxy = Loader(LoadedPage(MATCH_HTML))
    fetcher = _fetcher(always, clock, direct, proxy)
    assert not fetcher.proxy_fallback_enabled
    with pytest.raises(HLTVBlockedError):
        fetcher.get("/matches/1/x")
    assert proxy.urls == []  # "always": the single (proxied) loader is the page loader
    plain = HLTVSettings(cache_dir=tmp_path / "c2")
    f2 = _fetcher(plain, clock, Loader(LoadedPage(CHALLENGE_HTML)), Loader(LoadedPage(MATCH_HTML)))
    assert not f2.proxy_fallback_enabled
    with pytest.raises(HLTVBlockedError):
        f2.get("/matches/1/x")


def test_route_settings_helpers(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    assert direct_settings(s).proxy_url is None and s.proxy_url is not None
    assert proxy_profile_dir(Path("data/browser/hltv")) == Path("data/browser/hltv-proxy")
    assert proxy_profile_dir(None) is None
    assert HLTVSettings().proxy_mode == "after_block"


# ----------------------------------------------------------------------------------- tier w/o PandaScore


@pytest.mark.parametrize(
    ("stars", "r1", "r2", "tier"),
    [
        (5, None, None, Tier.S),
        (0, 3, 12, Tier.S),
        (2, None, None, Tier.A),
        (0, 8, 80, Tier.A),
        (0, 25, 28, Tier.A),
        (1, None, None, Tier.B),
        (0, 45, None, Tier.B),
        (0, 90, 150, Tier.C),
        (0, None, None, Tier.UNRANKED),
        (None, -1, 0, Tier.UNRANKED),
    ],
)
def test_derive_tier(stars: int | None, r1: int | None, r2: int | None, tier: Tier) -> None:
    assert derive_tier(stars, r1, r2) is tier


def test_hltv_only_matches_get_a_derived_tier(session: Session, app_settings: Settings) -> None:
    sync_results(session, FakeFetcher(), settings=app_settings, since=datetime(2026, 11, 15, tzinfo=UTC))
    starred = session.scalar(select(Match).where(Match.hltv_id == 2378002))
    assert starred is not None and starred.pandascore_id is None
    assert starred.extra["hltv"]["stars"] == 2
    hl = starred.extra["hltv"]
    expected = derive_tier(2, hl.get("team1_rank"), hl.get("team2_rank"))
    assert expected in (Tier.S, Tier.A)
    assert starred.tier is expected and hl["tier_source"] == "derived"
    assert starred.in_scope
