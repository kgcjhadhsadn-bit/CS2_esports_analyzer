"""HLTVFetcher: disk cache + TTLs, rate limiting, challenge detection, cool-down, http mode."""

from __future__ import annotations

import json
import random
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx

from cs2bot.config import HLTVSettings
from cs2bot.ingest.hltv import (
    HLTVBlockedError,
    HLTVFetcher,
    HLTVFetchError,
    HLTVNotFoundError,
    LoadedPage,
    detect_challenge,
)
from cs2bot.ingest.hltv.fetcher import (
    FetchOptions,
    InjectableRateLimiter,
    playwright_proxy,
    wait_for_challenge_to_clear,
)
from cs2bot.ingest.hltv.urls import cache_key, canonical_url
from tests.ingest.hltv.conftest import FIXTURES, FakeClock, load_fixture

MATCH_HTML = load_fixture("match_bo3_finished.html")
UPCOMING_HTML = load_fixture("match_upcoming.html")
CHALLENGE_HTML = load_fixture("challenge_just_a_moment.html")


class ScriptedLoader:
    """Fake page loader returning queued pages (or raising queued exceptions)."""

    def __init__(self, *responses: LoadedPage | str | Exception) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []
        self.closed = False

    def __call__(self, url: str) -> LoadedPage | str:
        self.urls.append(url)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def make_fetcher(
    settings: HLTVSettings, clock: FakeClock, loader: ScriptedLoader, **options: object
) -> HLTVFetcher:
    return HLTVFetcher(
        settings,
        page_loader=loader,
        sleep=clock.sleep,
        clock=clock.monotonic,
        now=clock.now,
        options=FetchOptions(**options),  # type: ignore[arg-type]
        rng=random.Random(1),
    )


# ------------------------------------------------------------------------------------------- challenge


@pytest.mark.parametrize(
    "name",
    sorted(p.name for p in FIXTURES.glob("*.html") if not p.name.startswith(("challenge", "blocked"))),
)
def test_normal_pages_are_not_challenges(name: str) -> None:
    # every fixture embeds Cloudflare's normal-page bot script (/cdn-cgi/challenge-platform/scripts/jsd/)
    assert detect_challenge(load_fixture(name)) is None


def test_challenge_and_block_pages_are_detected() -> None:
    assert detect_challenge(CHALLENGE_HTML) == "title:Just a moment..."
    assert (detect_challenge(load_fixture("blocked_attention_required.html")) or "").startswith(
        "title:Attention"
    )
    assert detect_challenge("<html><script>window._cf_chl_opt={}</script></html>") == "cf-chl"
    assert detect_challenge(
        '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>'
    ) == ("challenge-platform")
    assert detect_challenge("<p>Sorry, you have been blocked</p>") == "cf-block"
    assert detect_challenge('<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>') is None
    assert detect_challenge("") is None


# ------------------------------------------------------------------------------------------- cache


def test_cache_hit_skips_network_and_rate_limiter(hltv_settings: HLTVSettings, clock: FakeClock) -> None:
    loader = ScriptedLoader(LoadedPage(UPCOMING_HTML, 200))
    fetcher = make_fetcher(hltv_settings, clock, loader)
    assert fetcher.get("/matches/2378010/g2-vs-spirit") == UPCOMING_HTML
    clock.advance(60)
    # a different slug for the same match id hits the same cache entry
    assert fetcher.get("https://www.hltv.org/matches/2378010/-") == UPCOMING_HTML
    assert len(loader.urls) == 1
    assert fetcher.stats.network_fetches == 1 and fetcher.stats.cache_hits == 1
    assert clock.sleeps == []  # first fetch is not delayed, cache hits never are
    files = list(Path(hltv_settings.cache_dir).rglob("*.json"))
    meta = json.loads(files[0].read_text())
    assert meta["url"].endswith("/matches/2378010/g2-vs-spirit") and meta["status"] == 200


def test_default_ttl_expires(hltv_settings: HLTVSettings, clock: FakeClock) -> None:
    loader = ScriptedLoader(LoadedPage(UPCOMING_HTML, 200))
    fetcher = make_fetcher(hltv_settings, clock, loader)
    fetcher.get("/matches/2378010/x")
    clock.advance(hltv_settings.cache_ttl_minutes * 60 - 30)
    fetcher.get("/matches/2378010/x")
    assert len(loader.urls) == 1
    clock.advance(60)
    fetcher.get("/matches/2378010/x")
    assert len(loader.urls) == 2


def test_finished_matches_and_mapstats_use_long_ttl(hltv_settings: HLTVSettings, clock: FakeClock) -> None:
    loader = ScriptedLoader(LoadedPage(MATCH_HTML, 200))
    fetcher = make_fetcher(hltv_settings, clock, loader)
    fetcher.get("/matches/2378001/x")
    fetcher.get("/stats/matches/mapstatsid/210001/x")
    clock.advance(hltv_settings.cache_ttl_minutes * 60 * 3)  # far beyond the default TTL
    fetcher.get("/matches/2378001/x")
    fetcher.get("/stats/matches/mapstatsid/210001/x")
    assert len(loader.urls) == 2
    assert fetcher.ttl_minutes("https://www.hltv.org/matches/2378001/x", MATCH_HTML) == (
        hltv_settings.finished_match_cache_ttl_minutes
    )
    assert (
        fetcher.ttl_minutes("https://www.hltv.org/matches/2378010/x", UPCOMING_HTML)
        == hltv_settings.cache_ttl_minutes
    )
    assert fetcher.ttl_minutes("https://www.hltv.org/ranking/teams/2026/november/2") == (
        hltv_settings.finished_match_cache_ttl_minutes
    )
    assert fetcher.ttl_minutes("https://www.hltv.org/ranking/teams") == hltv_settings.cache_ttl_minutes
    assert fetcher.ttl_minutes("https://www.hltv.org/news/archive/2026/october") == (
        hltv_settings.finished_match_cache_ttl_minutes
    )
    assert (
        fetcher.ttl_minutes("https://www.hltv.org/news/archive/2026/november")
        == hltv_settings.cache_ttl_minutes
    )


def test_max_age_override_and_forced_refresh(hltv_settings: HLTVSettings, clock: FakeClock) -> None:
    loader = ScriptedLoader(LoadedPage(MATCH_HTML, 200))
    fetcher = make_fetcher(hltv_settings, clock, loader)
    fetcher.get("/matches/2378001/x")
    clock.advance(11 * 60)
    fetcher.get("/matches/2378001/x", max_age_minutes=10)  # older than 10 min -> refetch
    fetcher.get("/matches/2378001/x", max_age_minutes=10)  # fresh again
    fetcher.get("/matches/2378001/x", max_age_minutes=0)  # forced
    assert len(loader.urls) == 3
    assert fetcher.cached("/matches/2378001/x") is not None
    fetcher.invalidate("/matches/2378001/x")
    assert fetcher.cached("/matches/2378001/x") is None


def test_cache_keys_are_canonical() -> None:
    assert canonical_url("https://WWW.hltv.org/matches/1/a-vs-b") == "https://www.hltv.org/matches/1"
    assert (
        canonical_url("https://www.hltv.org/results?offset=100&a=1")
        == "https://www.hltv.org/results?a=1&offset=100"
    )
    assert cache_key("https://www.hltv.org/news/5/x") == cache_key("https://www.hltv.org/news/5/y#c")
    assert cache_key("https://www.hltv.org/matches/1/x") != cache_key("https://www.hltv.org/matches/2/x")


# ------------------------------------------------------------------------------------------- rate limit


def test_network_fetches_are_spaced_by_the_rate_limiter(
    hltv_settings: HLTVSettings, clock: FakeClock
) -> None:
    loader = ScriptedLoader(LoadedPage(UPCOMING_HTML, 200))
    fetcher = make_fetcher(hltv_settings, clock, loader)
    fetcher.get("/matches/1/x")
    fetcher.get("/matches/2/x")
    fetcher.get("/matches/1/x")  # cache hit: no wait
    fetcher.get("/matches/3/x")
    assert len(loader.urls) == 3
    assert len(clock.sleeps) == 2
    assert all(hltv_settings.min_delay_s <= s <= hltv_settings.max_delay_s for s in clock.sleeps)
    assert fetcher.stats.slept_s == pytest.approx(sum(clock.sleeps))


def test_injectable_rate_limiter_only_sleeps_the_remaining_gap(clock: FakeClock) -> None:
    limiter = InjectableRateLimiter(4.0, 9.0, clock=clock.monotonic, sleep=clock.sleep, rng=random.Random(3))
    assert limiter.wait() == 0.0
    clock.advance(2.0)
    slept = limiter.wait()
    assert 2.0 <= slept <= 7.0
    clock.advance(30.0)
    assert limiter.wait() == 0.0


# ------------------------------------------------------------------------------------------- blocks


def test_challenge_raises_and_starts_exponential_cooldown(
    hltv_settings: HLTVSettings, clock: FakeClock
) -> None:
    loader = ScriptedLoader(LoadedPage(CHALLENGE_HTML, 403))
    fetcher = make_fetcher(hltv_settings, clock, loader, cooldown_base_s=600.0)
    with pytest.raises(HLTVBlockedError) as first:
        fetcher.get("/matches/1/x")
    assert first.value.retry_after_s == 600.0 and first.value.reason == "title:Just a moment..."
    assert fetcher.cached("/matches/1/x") is None  # challenge pages are never cached
    # during the cool-down nothing reaches the network
    clock.advance(300)
    with pytest.raises(HLTVBlockedError) as cooling:
        fetcher.get("/matches/2/x")
    assert cooling.value.reason == "cooldown" and cooling.value.retry_after_s == pytest.approx(300)
    assert len(loader.urls) == 1
    # after the cool-down: blocked again -> twice as long
    clock.advance(301)
    with pytest.raises(HLTVBlockedError) as second:
        fetcher.get("/matches/2/x")
    assert second.value.retry_after_s == 1200.0 and len(loader.urls) == 2
    # a successful fetch resets the escalation
    loader.responses = [LoadedPage(UPCOMING_HTML, 200)]
    clock.advance(1201)
    assert fetcher.get("/matches/2/x") == UPCOMING_HTML
    assert fetcher.blocked_until is None
    assert not (Path(hltv_settings.cache_dir) / "_cooldown.json").exists()


def test_cooldown_survives_a_restart(hltv_settings: HLTVSettings, clock: FakeClock) -> None:
    blocked = make_fetcher(hltv_settings, clock, ScriptedLoader(LoadedPage(CHALLENGE_HTML, 503)))
    with pytest.raises(HLTVBlockedError):
        blocked.get("/matches/1/x")
    fresh_loader = ScriptedLoader(LoadedPage(UPCOMING_HTML, 200))
    restarted = make_fetcher(hltv_settings, clock, fresh_loader)
    assert restarted.blocked_until is not None
    with pytest.raises(HLTVBlockedError):
        restarted.get("/matches/1/x")
    assert fresh_loader.urls == []
    clock.advance(FetchOptions().cooldown_base_s + 1)
    assert restarted.get("/matches/1/x") == UPCOMING_HTML


def test_cache_hits_still_work_during_cooldown(hltv_settings: HLTVSettings, clock: FakeClock) -> None:
    loader = ScriptedLoader(LoadedPage(UPCOMING_HTML, 200), LoadedPage(CHALLENGE_HTML, 403))
    fetcher = make_fetcher(hltv_settings, clock, loader)
    fetcher.get("/matches/1/x")
    with pytest.raises(HLTVBlockedError):
        fetcher.get("/matches/2/x")
    assert fetcher.get("/matches/1/x") == UPCOMING_HTML


def test_http_429_is_a_block(hltv_settings: HLTVSettings, clock: FakeClock) -> None:
    fetcher = make_fetcher(hltv_settings, clock, ScriptedLoader(LoadedPage("<html>slow down</html>", 429)))
    with pytest.raises(HLTVBlockedError) as exc:
        fetcher.get("/results")
    assert exc.value.reason == "http 429"


def test_not_found_and_server_errors(hltv_settings: HLTVSettings, clock: FakeClock) -> None:
    fetcher = make_fetcher(hltv_settings, clock, ScriptedLoader(LoadedPage("<html>404</html>", 404)))
    with pytest.raises(HLTVNotFoundError):
        fetcher.get("/matches/9/x")
    assert fetcher.cached("/matches/9/x") is None and fetcher.blocked_until is None
    fetcher2 = make_fetcher(hltv_settings, clock, ScriptedLoader(LoadedPage("<html>oops</html>", 500)))
    with pytest.raises(HLTVFetchError) as exc:
        fetcher2.get("/matches/10/x")
    assert exc.value.status == 500 and not isinstance(exc.value, HLTVBlockedError)


def test_transport_errors_retry_once_then_fail(hltv_settings: HLTVSettings, clock: FakeClock) -> None:
    loader = ScriptedLoader(ConnectionError("reset"), LoadedPage(UPCOMING_HTML, 200))
    fetcher = make_fetcher(hltv_settings, clock, loader, transport_retry_delay_s=7.0)
    assert fetcher.get("/matches/1/x") == UPCOMING_HTML
    assert 7.0 in clock.sleeps and len(loader.urls) == 2
    failing = make_fetcher(
        hltv_settings, clock, ScriptedLoader(ConnectionError("down")), transport_retry_delay_s=1.0
    )
    with pytest.raises(HLTVFetchError, match="ConnectionError"):
        failing.get("/matches/2/x")


def test_foreign_hosts_are_refused(hltv_settings: HLTVSettings, clock: FakeClock) -> None:
    fetcher = make_fetcher(hltv_settings, clock, ScriptedLoader("<html></html>"))
    with pytest.raises(ValueError, match="non-HLTV"):
        fetcher.get("https://example.com/matches/1/x")
    assert fetcher.url_for("results?offset=100") == "https://www.hltv.org/results?offset=100"


def test_context_manager_closes_owned_loader_only(hltv_settings: HLTVSettings, clock: FakeClock) -> None:
    injected = ScriptedLoader("<html></html>")
    with make_fetcher(hltv_settings, clock, injected):
        pass
    assert not injected.closed  # the caller owns injected loaders


# ------------------------------------------------------------------------------------------- http mode


@respx.mock
def test_http_mode_fetches_with_httpx(tmp_path: Path, clock: FakeClock) -> None:
    settings = HLTVSettings(fetch_mode="http", cache_dir=tmp_path / "c", min_delay_s=0, max_delay_s=0)
    route = respx.get("https://www.hltv.org/matches/2378010/x").mock(
        return_value=httpx.Response(200, text=UPCOMING_HTML)
    )
    respx.get("https://www.hltv.org/results").mock(return_value=httpx.Response(403, text=CHALLENGE_HTML))
    with HLTVFetcher(settings, sleep=clock.sleep, clock=clock.monotonic, now=clock.now) as fetcher:
        assert fetcher.get("/matches/2378010/x") == UPCOMING_HTML
        assert route.called
        sent = route.calls.last.request
        assert "text/html" in sent.headers["accept"]
        with pytest.raises(HLTVBlockedError):
            fetcher.get("/results")


# ------------------------------------------------------------------------------------------- browser helpers


class FakeChallengePage:
    def __init__(self, pages: list[str]) -> None:
        self.pages = pages
        self.waits: list[float] = []
        self.clock: FakeClock | None = None

    def content(self) -> str:
        return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]

    def wait_for_timeout(self, timeout: float) -> None:
        self.waits.append(timeout)
        if self.clock is not None:
            self.clock.advance(timeout / 1000.0)


def test_challenge_wait_returns_once_the_page_clears(clock: FakeClock) -> None:
    page = FakeChallengePage([CHALLENGE_HTML, CHALLENGE_HTML, UPCOMING_HTML])
    page.clock = clock
    html, cleared = wait_for_challenge_to_clear(
        page, CHALLENGE_HTML, timeout_s=20, poll_s=1, clock=clock.monotonic
    )
    assert cleared and html == UPCOMING_HTML and page.waits == [1000.0, 1000.0, 1000.0]


def test_challenge_wait_gives_up_after_the_timeout(clock: FakeClock) -> None:
    page = FakeChallengePage([CHALLENGE_HTML])
    page.clock = clock
    html, cleared = wait_for_challenge_to_clear(
        page, CHALLENGE_HTML, timeout_s=5, poll_s=1, clock=clock.monotonic
    )
    assert not cleared and detect_challenge(html) is not None and len(page.waits) == 5


def test_playwright_proxy_splits_credentials() -> None:
    assert playwright_proxy("http://user:p%40ss@proxy.local:8080") == {
        "server": "http://proxy.local:8080",
        "username": "user",
        "password": "p@ss",
    }
    assert playwright_proxy("socks5://1.2.3.4:1080") == {"server": "socks5://1.2.3.4:1080"}
    with pytest.raises(ValueError):
        playwright_proxy("not a url")


def test_browser_loader_uses_settings_for_launch(tmp_path: Path) -> None:
    from pydantic import SecretStr

    from cs2bot.ingest.hltv import BrowserPageLoader

    settings = HLTVSettings(
        cache_dir=tmp_path,
        chromium_executable="/custom/chrome",
        headless=False,
        proxy_url=SecretStr("http://u:pw@px:3128"),
        user_agent="Mozilla/5.0 test",
    )
    loader = BrowserPageLoader(settings)
    launch = loader._launch_kwargs()
    assert launch["executable_path"] == "/custom/chrome" and launch["headless"] is False
    assert launch["proxy"] == {"server": "http://px:3128", "username": "u", "password": "pw"}
    assert loader._context_kwargs()["user_agent"] == "Mozilla/5.0 test"
    assert "executable_path" not in BrowserPageLoader(HLTVSettings(cache_dir=tmp_path))._launch_kwargs()
    loader.close()  # never started: closing is a no-op


def test_cooldown_file_contents(hltv_settings: HLTVSettings, clock: FakeClock) -> None:
    fetcher = make_fetcher(hltv_settings, clock, ScriptedLoader(LoadedPage(CHALLENGE_HTML, 403)))
    with pytest.raises(HLTVBlockedError):
        fetcher.get("/matches/1/x")
    data = json.loads((Path(hltv_settings.cache_dir) / "_cooldown.json").read_text())
    assert data["consecutive_blocks"] == 1 and data["reason"].startswith("title:")
    assert fetcher.blocked_until == clock.now() + timedelta(seconds=FetchOptions().cooldown_base_s)


def test_unwritable_cache_still_returns_the_page(tmp_path: Path, clock: FakeClock) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    settings = HLTVSettings(cache_dir=blocker, min_delay_s=0, max_delay_s=0)
    fetcher = make_fetcher(settings, clock, ScriptedLoader(LoadedPage(UPCOMING_HTML, 200)))
    assert fetcher.get("/matches/1/x") == UPCOMING_HTML


def test_challenge_detection_has_no_false_positives_on_articles() -> None:
    article = load_fixture("news_article_43100.html").replace(
        "Natus Vincere bench jL, iM steps up to IGL | HLTV.org", "Attention required: NAVI changes | HLTV.org"
    )
    assert detect_challenge(article) is None  # only Cloudflare's exact interstitial titles count
    quoting = article.replace("We need a fresh start", "Sorry, you have been blocked, said the admin")
    real_size = quoting + "<!--" + "x" * 300_000 + "-->"  # real HLTV articles are ~300 KB
    assert detect_challenge(real_size) is None
    assert detect_challenge(quoting) == "cf-block"  # a few-KB page with that phrase is a block page


def test_cache_is_compressed_reads_legacy_entries_and_prunes(
    hltv_settings: HLTVSettings, clock: FakeClock
) -> None:
    import gzip

    loader = ScriptedLoader(LoadedPage(MATCH_HTML, 200))
    fetcher = make_fetcher(hltv_settings, clock, loader)
    fetcher.get("/matches/2378001/x")
    root = Path(hltv_settings.cache_dir)
    (gz_file,) = root.rglob("*.html.gz")
    assert gzip.decompress(gz_file.read_bytes()).decode() == MATCH_HTML
    assert gz_file.stat().st_size < len(MATCH_HTML.encode()) / 3
    # an entry in the older plain-HTML layout is still served
    key = gz_file.name.removesuffix(".html.gz")
    gz_file.unlink()
    (gz_file.parent / f"{key}.html").write_text(MATCH_HTML, encoding="utf-8")
    assert fetcher.get("/matches/2378001/x") == MATCH_HTML and len(loader.urls) == 1
    # pruning removes entries older than the threshold only
    clock.advance(3 * 86400)
    fetcher.get("/matches/2378010/x")
    assert fetcher.prune_cache(timedelta(days=1)) == 1
    assert fetcher.cached("/matches/2378001/x") is None and fetcher.cached("/matches/2378010/x") is not None
