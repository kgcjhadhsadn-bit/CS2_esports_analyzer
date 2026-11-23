"""``BrowserDownloader``: configuration (no browser) and real Chromium downloads from a local server."""

from __future__ import annotations

import threading
import time
import zipfile
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.config import DemoSettings, HLTVSettings, Settings
from cs2bot.db.base import Base
from cs2bot.db.models import Demo, Round
from cs2bot.db.session import make_engine
from cs2bot.domain.enums import DemoStatus
from cs2bot.ingest.demos.browser import BrowserDownloader
from cs2bot.ingest.demos.download import looks_like_challenge
from cs2bot.ingest.demos.errors import DemoDownloadBlockedError, DemoDownloadError
from cs2bot.ingest.demos.pipeline import PipelineOptions, process_pending_demos
from tests.ingest.demos.demos_builders import factory_for, standard_match
from tests.ingest.demos.demos_db import seed_match

CHROMIUM = "/opt/pw-browsers/chromium"
ARCHIVE = b"Rar!\x1a\x07\x01\x00" + bytes(range(256)) * 1200  # ~300 kB


def demo_zip() -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("alpha-vs-bravo-m1-mirage.dem", b"PBDEMS2\x00" + b"\x00" * 2000)
    return buffer.getvalue()


ZIP = demo_zip()


# ------------------------------------------------------------------------------- no browser needed


def test_from_settings_mirrors_the_hltv_fetcher_configuration(tmp_path: Path) -> None:
    settings = Settings(
        hltv=HLTVSettings(
            browser_profile_dir=tmp_path / "profile",
            headless=False,
            chromium_executable="C:/chrome/chrome.exe",
            proxy_url=SecretStr("http://user:p%40ss@proxy.example.org:8080"),
            user_agent="Mozilla/5.0 Test",
        ),
        demos=DemoSettings(storage_dir=tmp_path / "demos", download_timeout_s=123.0),
    )
    downloader = BrowserDownloader.from_settings(settings)

    assert downloader.profile_dir == tmp_path / "profile"
    assert downloader.timeout_s == 123.0
    assert downloader.referer == "https://www.hltv.org/"
    assert downloader.user_agent == "Mozilla/5.0 Test"
    assert downloader.downloads_dir == tmp_path / "demos" / "_browser_downloads"
    assert downloader.launch_kwargs() == {
        "headless": False,
        "executable_path": "C:/chrome/chrome.exe",
        "proxy": {"server": "http://proxy.example.org:8080", "username": "user", "password": "p@ss"},
        "downloads_path": str(tmp_path / "demos" / "_browser_downloads"),
    }
    # nothing is started until the first download, and closing an unused downloader is harmless
    downloader.close()
    assert not (tmp_path / "profile").exists()


def test_from_settings_defaults_and_overrides(tmp_path: Path) -> None:
    settings = Settings(hltv=HLTVSettings(browser_profile_dir=None), demos=DemoSettings(storage_dir=tmp_path))
    downloader = BrowserDownloader.from_settings(settings, headless=False, start_timeout_s=5.0)
    assert downloader.profile_dir is None
    assert downloader.headless is False and downloader.start_timeout_s == 5.0
    kwargs = downloader.launch_kwargs()
    assert "executable_path" not in kwargs and "proxy" not in kwargs  # never hard-coded


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<html><head><title>Just a moment...</title></head></html>", True),
        ("<html><head><TITLE>Attention Required! | Cloudflare</TITLE></head></html>", True),
        ('<form id="challenge-form" action="/x?__cf_chl_f_tk=1">', True),
        ("<script>window._cf_chl_opt={cvId:'3'}</script>", True),
        ("<html><title>Page not found | HLTV.org</title><body>404</body></html>", False),
        ("<html><title>NAVI vs FaZe | HLTV.org</title></html>", False),
        ("", False),
    ],
)
def test_looks_like_challenge(html: str, expected: bool) -> None:
    assert looks_like_challenge(html) is expected


# ----------------------------------------------------------------------------------- real Chromium


class _Handler(BaseHTTPRequestHandler):
    hits: list[str]

    def log_message(self, *_args: Any) -> None:  # keep the test output quiet
        return

    def _send(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.hits.append(self.path)
        attachment = {"Content-Type": "application/octet-stream"}
        if self.path == "/download/demo/1":  # like HLTV: a redirect to the file server
            self.send_response(302)
            self.send_header("Location", "/files/alpha-vs-bravo.rar")
            self.end_headers()
        elif self.path == "/files/alpha-vs-bravo.rar":
            self._send(
                200,
                ARCHIVE,
                {**attachment, "Content-Disposition": 'attachment; filename="alpha-vs-bravo.rar"'},
            )
        elif self.path == "/download/demo/2":
            self._send(
                200, ZIP, {**attachment, "Content-Disposition": 'attachment; filename="alpha-vs-bravo.zip"'}
            )
        elif self.path == "/challenge":
            page = (
                b"<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
            )
            self._send(403, page, {"Content-Type": "text/html"})
        elif self.path == "/soft-challenge":  # block page served with status 200
            page = b"<html><head><title>Attention Required! | Cloudflare</title></head><body></body></html>"
            self._send(200, page, {"Content-Type": "text/html"})
        elif self.path == "/stalled":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="stalled.rar"')
            self.send_header("Content-Length", str(len(ARCHIVE)))
            self.end_headers()
            self.wfile.write(ARCHIVE[:2000])
            self.wfile.flush()
            time.sleep(15)  # never finishes within the test's timeout
        else:
            self._send(
                404,
                b"<html><title>Page not found</title><body>404</body></html>",
                {"Content-Type": "text/html"},
            )


@pytest.fixture
def server() -> Iterator[tuple[str, list[str]]]:
    hits: list[str] = []
    handler = type("Handler", (_Handler,), {"hits": hits})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", hits
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def downloader(tmp_path: Path) -> Iterator[BrowserDownloader]:
    if not Path(CHROMIUM).exists():
        pytest.skip(f"Chromium not installed at {CHROMIUM}")
    with BrowserDownloader(
        chromium_executable=CHROMIUM,
        headless=True,
        downloads_dir=tmp_path / "browser-tmp",
        start_timeout_s=2.0,
        timeout_s=30.0,
    ) as instance:
        yield instance


@pytest.mark.browser
def test_browser_download_follows_redirects_and_reuses_the_browser(
    downloader: BrowserDownloader, server: tuple[str, list[str]], tmp_path: Path
) -> None:
    base, hits = server
    first = downloader(f"{base}/download/demo/1", tmp_path / "one")
    assert first == tmp_path / "one" / "alpha-vs-bravo.rar"
    assert first.read_bytes() == ARCHIVE
    assert hits == ["/download/demo/1", "/files/alpha-vs-bravo.rar"]
    page = downloader._page

    second = downloader(f"{base}/download/demo/2", tmp_path / "two")
    assert second.name == "alpha-vs-bravo.zip" and second.read_bytes() == ZIP
    assert downloader._page is page  # one page for all downloads
    assert [p.name for p in (tmp_path / "one").iterdir()] == ["alpha-vs-bravo.rar"]


@pytest.mark.browser
def test_browser_download_reports_blocks_and_missing_files(
    downloader: BrowserDownloader, server: tuple[str, list[str]], tmp_path: Path
) -> None:
    base, _hits = server
    dest = tmp_path / "out"
    with pytest.raises(DemoDownloadBlockedError, match="HTTP 403"):
        downloader(f"{base}/challenge", dest)
    with pytest.raises(DemoDownloadBlockedError):
        downloader(f"{base}/soft-challenge", dest)
    started = time.monotonic()
    with pytest.raises(DemoDownloadError, match="HTTP 404") as info:
        downloader(f"{base}/download/demo/999", dest)
    assert not isinstance(info.value, DemoDownloadBlockedError)
    assert time.monotonic() - started < 1.5  # a plain 404 is not waited on
    with pytest.raises(DemoDownloadError, match="could not open"):
        downloader("http://127.0.0.1:9/unreachable", dest)
    assert list(dest.iterdir()) == []
    # the downloader keeps working afterwards
    assert downloader(f"{base}/download/demo/1", dest).read_bytes() == ARCHIVE


@pytest.mark.browser
def test_browser_download_total_timeout_cancels_a_stalled_transfer(
    server: tuple[str, list[str]], tmp_path: Path
) -> None:
    if not Path(CHROMIUM).exists():
        pytest.skip(f"Chromium not installed at {CHROMIUM}")
    base, _hits = server
    with BrowserDownloader(chromium_executable=CHROMIUM, start_timeout_s=5.0, timeout_s=1.5) as downloader:
        started = time.monotonic()
        with pytest.raises(DemoDownloadError, match="exceeded 2s"):
            downloader(f"{base}/stalled", tmp_path / "out")
        assert time.monotonic() - started < 8.0
        assert list((tmp_path / "out").iterdir()) == []


@pytest.mark.browser
def test_browser_download_with_a_persistent_profile(server: tuple[str, list[str]], tmp_path: Path) -> None:
    if not Path(CHROMIUM).exists():
        pytest.skip(f"Chromium not installed at {CHROMIUM}")
    base, _hits = server
    profile = tmp_path / "profile"
    with BrowserDownloader(
        chromium_executable=CHROMIUM, profile_dir=profile, start_timeout_s=5.0
    ) as downloader:
        path = downloader(f"{base}/download/demo/1", tmp_path / "out")
    assert path.read_bytes() == ARCHIVE
    assert profile.is_dir() and any(profile.iterdir())  # cookies etc. survive for the next run


@pytest.mark.browser
def test_pipeline_with_the_browser_downloader(
    downloader: BrowserDownloader, server: tuple[str, list[str]], tmp_path: Path
) -> None:
    base, _hits = server
    engine = make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        with factory() as session:
            seeded = seed_match(session)
            good = Demo(match_id=seeded.match_id, url=f"{base}/download/demo/2")
            session.add(good)
            session.commit()
            good_id = good.id
        settings = Settings(demos=DemoSettings(storage_dir=tmp_path / "demos"))
        report = process_pending_demos(
            factory,
            settings,
            downloader=downloader,
            parser_factory=factory_for(standard_match().build()),
            options=PipelineOptions(min_free_bytes=0),
        )
        assert (report.parsed, report.failed, report.rounds_persisted) == (1, 0, 18)
        with factory() as session:
            assert session.get(Demo, good_id).status is DemoStatus.PARSED
            assert session.scalar(select(func.count()).select_from(Round)) == 18

        # a challenge page stops the batch and leaves the demo pending
        with factory() as session:
            blocked = Demo(match_id=seeded.match_id, url=f"{base}/challenge")
            session.add(blocked)
            session.commit()
            blocked_id = blocked.id
        report = process_pending_demos(
            factory,
            settings,
            downloader=downloader,
            parser_factory=factory_for(standard_match().build()),
            options=PipelineOptions(min_free_bytes=0),
        )
        assert report.blocked and report.failed == 0
        with factory() as session:
            demo = session.get(Demo, blocked_id)
            assert (demo.status, demo.attempts) == (DemoStatus.PENDING, 0)
    finally:
        engine.dispose()
