"""Browser-mode fetcher against a local HTTP server (real Chromium, no internet)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cs2bot.config import HLTVSettings
from cs2bot.ingest.hltv import HLTVBlockedError, HLTVFetcher, parse_match_page
from cs2bot.ingest.hltv.fetcher import FetchOptions
from tests.ingest.hltv.conftest import CHROMIUM, load_fixture

pytestmark = pytest.mark.browser

MATCH_HTML = load_fixture("match_bo3_finished.html")
# A challenge interstitial that "clears by itself": after ~1 s it navigates to the real page, like a
# passed Cloudflare check would. Nothing on the page is clicked or solved by the fetcher.
CLEARING_CHALLENGE = """<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>
<div id="challenge-error-text">Checking your browser</div>
<script>window._cf_chl_opt={cType:'managed'};setTimeout(function(){location.replace('/matches/2378001/real');},1000);</script>
</body></html>"""
STUCK_CHALLENGE = load_fixture("challenge_just_a_moment.html").replace(
    '<meta http-equiv="refresh" content="390">', ""
)


class _Handler(BaseHTTPRequestHandler):
    hits: dict[str, int]
    blocked_types: list[str]

    def do_GET(self) -> None:
        self.hits[self.path] = self.hits.get(self.path, 0) + 1
        if self.path.startswith("/matches/2378001/real") or self.path.startswith("/matches/2378001/direct"):
            body, status = MATCH_HTML, 200
        elif self.path.startswith("/matches/2378001/challenge"):
            body, status = CLEARING_CHALLENGE, 403
        elif self.path.startswith("/matches/1/stuck"):
            body, status = STUCK_CHALLENGE, 403
        elif self.path.endswith((".svg", ".png", ".gif", ".jpg")):
            self.blocked_types.append(self.path)
            body, status = "", 200
        else:
            body, status = "<html><body>not found</body></html>", 404
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def local_server() -> Iterator[tuple[str, dict[str, int], list[str]]]:
    hits: dict[str, int] = {}
    images: list[str] = []
    handler = type("Handler", (_Handler,), {"hits": hits, "blocked_types": images})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", hits, images
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def browser_settings(tmp_path: Path, local_server: tuple[str, dict[str, int], list[str]]) -> HLTVSettings:
    if not Path(CHROMIUM).exists():
        pytest.skip("Chromium not installed at /opt/pw-browsers/chromium")
    return HLTVSettings(
        base_url=local_server[0],
        fetch_mode="browser",
        chromium_executable=CHROMIUM,
        headless=True,
        cache_dir=tmp_path / "cache",
        min_delay_s=0.0,
        max_delay_s=0.0,
        navigation_timeout_s=30.0,
    )


def test_browser_fetches_and_reuses_one_context(
    browser_settings: HLTVSettings, local_server: tuple[str, dict[str, int], list[str]]
) -> None:
    _, hits, images = local_server
    with HLTVFetcher(browser_settings) as fetcher:
        html = fetcher.get("/matches/2378001/direct")
        page = parse_match_page(html, fetcher.url_for("/matches/2378001/direct"))
        assert page.team1 is not None and page.team1.name == "Vitality" and len(page.maps) == 3
        assert fetcher.get("/matches/2378001/direct") == html  # cache hit
        assert hits["/matches/2378001/direct"] == 1
        fetcher.get("/matches/2378001/direct", max_age_minutes=0)  # second navigation, same browser
        assert hits["/matches/2378001/direct"] == 2
    assert images == []  # images / fonts are not downloaded in browser mode


def test_browser_waits_for_a_challenge_to_clear_by_itself(
    browser_settings: HLTVSettings, local_server: tuple[str, dict[str, int], list[str]]
) -> None:
    _, hits, _ = local_server
    with HLTVFetcher(
        browser_settings, options=FetchOptions(challenge_wait_s=15.0, challenge_poll_s=0.5)
    ) as fetcher:
        html = fetcher.get("/matches/2378001/challenge")
    assert "Just a moment" not in html
    assert parse_match_page(html, "http://x/matches/2378001/real").team2 is not None
    assert hits.get("/matches/2378001/real") == 1


def test_browser_raises_when_the_challenge_persists(
    browser_settings: HLTVSettings, local_server: tuple[str, dict[str, int], list[str]]
) -> None:
    options = FetchOptions(challenge_wait_s=2.0, challenge_poll_s=0.5, cooldown_base_s=900.0)
    with HLTVFetcher(browser_settings, options=options) as fetcher:
        with pytest.raises(HLTVBlockedError) as exc:
            fetcher.get("/matches/1/stuck")
        assert exc.value.retry_after_s == 900.0
        with pytest.raises(HLTVBlockedError):  # cool-down: no further navigation
            fetcher.get("/matches/2378001/direct")
    assert local_server[1].get("/matches/2378001/direct") is None
