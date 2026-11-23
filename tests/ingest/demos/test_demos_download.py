"""``download_demo``: the default httpx transfer (mocked with respx) and injected downloaders."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from cs2bot.ingest.demos import download as download_module
from cs2bot.ingest.demos.download import download_demo, http_download, looks_like_html, safe_filename
from cs2bot.ingest.demos.errors import DemoDownloadBlockedError, DemoDownloadError

URL = "https://demos.example.org/download/demo/98765"
RAR_BYTES = b"Rar!\x1a\x07\x01\x00" + bytes(range(256)) * 40


@respx.mock
def test_default_download_streams_to_a_file(tmp_path: Path) -> None:
    route = respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            content=RAR_BYTES,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": 'attachment; filename="alpha-vs-bravo-iem-katowice.rar"',
            },
        )
    )
    dest = tmp_path / "match_1" / "demo_7"  # does not exist yet
    path = download_demo(URL, dest, timeout_s=30.0)

    assert route.called
    assert path == dest / "alpha-vs-bravo-iem-katowice.rar"
    assert path.read_bytes() == RAR_BYTES
    assert [p.name for p in dest.iterdir()] == ["alpha-vs-bravo-iem-katowice.rar"]  # no .part left


@respx.mock
def test_redirects_are_followed_and_name_the_file(tmp_path: Path) -> None:
    final = "https://cdn.example.org/files/2026/navi-vs-faze-m1-mirage.dem.zst?token=abc"
    respx.get(URL).mock(return_value=httpx.Response(302, headers={"Location": final}))
    respx.get(final).mock(return_value=httpx.Response(200, content=b"\x28\xb5\x2f\xfd" + b"z" * 100))
    path = download_demo(URL, tmp_path)
    assert path.name == "navi-vs-faze-m1-mirage.dem.zst"
    assert path.stat().st_size == 104


@respx.mock
def test_url_without_a_filename_gets_the_last_path_segment(tmp_path: Path) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=RAR_BYTES))
    assert download_demo(URL, tmp_path).name == "98765"


@respx.mock
@pytest.mark.parametrize("status", [403, 404, 429, 500])
def test_http_errors(tmp_path: Path, status: int) -> None:
    respx.get(URL).mock(return_value=httpx.Response(status, content=b"nope"))
    with pytest.raises(DemoDownloadError, match=f"HTTP {status}"):
        download_demo(URL, tmp_path)
    assert list(tmp_path.iterdir()) == []


@respx.mock
@pytest.mark.parametrize("status", [403, 429, 503])
def test_block_statuses_raise_the_blocked_error(tmp_path: Path, status: int) -> None:
    """The demo job must back off instead of burning its three attempts on a protected source."""
    respx.get(URL).mock(return_value=httpx.Response(status, content=b"denied"))
    with pytest.raises(DemoDownloadBlockedError, match="browser downloader"):
        download_demo(URL, tmp_path)
    respx.get(URL).mock(return_value=httpx.Response(404, content=b"gone"))
    with pytest.raises(DemoDownloadError) as info:
        download_demo(URL, tmp_path)
    assert not isinstance(info.value, DemoDownloadBlockedError)


@respx.mock
def test_challenge_page_with_status_200_is_a_block(tmp_path: Path) -> None:
    page = b"<!DOCTYPE html><html><head><title>Just a moment...</title></head><body></body></html>"
    respx.get(URL).mock(return_value=httpx.Response(200, content=page, headers={"Content-Type": "text/html"}))
    with pytest.raises(DemoDownloadBlockedError, match="challenge page"):
        download_demo(URL, tmp_path)
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_html_instead_of_an_archive_is_rejected(tmp_path: Path) -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            content=b"<!DOCTYPE html><title>Demo not found</title>",
            headers={"Content-Type": "text/html; charset=UTF-8"},
        )
    )
    with pytest.raises(DemoDownloadError, match="HTML page"):
        download_demo(URL, tmp_path)
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_html_with_a_misleading_content_type_is_rejected_after_the_download(tmp_path: Path) -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            content=b"\n  <html><body>Access denied</body></html>",
            headers={"Content-Type": "application/octet-stream"},
        )
    )
    with pytest.raises(DemoDownloadError, match="HTML page"):
        download_demo(URL, tmp_path)
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_size_limit_from_the_header_and_while_streaming(tmp_path: Path) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=RAR_BYTES))
    with pytest.raises(DemoDownloadError, match="too large"):
        download_demo(URL, tmp_path, max_bytes=1000)
    assert list(tmp_path.iterdir()) == []

    def chunks() -> httpx.SyncByteStream:
        class Stream(httpx.SyncByteStream):
            def __iter__(self):  # type: ignore[no-untyped-def]
                for _ in range(50):
                    yield b"x" * 1000

        return Stream()

    # chunked transfer: no Content-Length, the limit must hit while writing
    respx.get(URL).mock(return_value=httpx.Response(200, stream=chunks()))
    with pytest.raises(DemoDownloadError, match="exceeds the size limit"):
        download_demo(URL, tmp_path, max_bytes=10_000)
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_truncated_and_empty_downloads(tmp_path: Path) -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, content=RAR_BYTES[:500], headers={"Content-Length": str(len(RAR_BYTES))}
        )
    )
    with pytest.raises(DemoDownloadError, match="truncated"):
        download_demo(URL, tmp_path)
    respx.get(URL).mock(return_value=httpx.Response(200, content=b""))
    with pytest.raises(DemoDownloadError, match="empty"):
        download_demo(URL, tmp_path)
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_transport_errors_are_wrapped(tmp_path: Path) -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("connection refused"))
    with pytest.raises(DemoDownloadError, match="ConnectError"):
        download_demo(URL, tmp_path)


@respx.mock
def test_total_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=RAR_BYTES))
    clock = iter([0.0, 5000.0, 5000.0, 5000.0])
    monkeypatch.setattr(download_module.time, "monotonic", lambda: next(clock))
    with pytest.raises(DemoDownloadError, match="exceeded 60s"):
        download_demo(URL, tmp_path, timeout_s=60.0)
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_http_download_with_a_caller_supplied_client(tmp_path: Path) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=RAR_BYTES))
    with httpx.Client(headers={"User-Agent": "test"}) as client:
        path = http_download(URL, tmp_path, client=client)
        assert not client.is_closed  # the caller's client is left open
    assert path.read_bytes() == RAR_BYTES


# -------------------------------------------------------------------------- injected downloader


def test_injected_downloader_is_used_and_validated(tmp_path: Path) -> None:
    calls: list[tuple[str, Path]] = []

    def browser_download(url: str, dest_dir: Path) -> Path:
        calls.append((url, dest_dir))
        target = dest_dir / "from-browser.rar"
        target.write_bytes(RAR_BYTES)
        return target

    dest = tmp_path / "work"
    path = download_demo(URL, dest, downloader=browser_download)
    assert calls == [(URL, dest)]
    assert dest.is_dir() and path == dest / "from-browser.rar"


def test_injected_downloader_results_are_checked(tmp_path: Path) -> None:
    def missing(_url: str, dest_dir: Path) -> Path:
        return dest_dir / "never-written.rar"

    def empty(_url: str, dest_dir: Path) -> Path:
        (dest_dir / "empty.rar").write_bytes(b"")
        return dest_dir / "empty.rar"

    def challenge(_url: str, dest_dir: Path) -> Path:
        (dest_dir / "demo.rar").write_text("<!doctype html><html>Checking your browser</html>")
        return dest_dir / "demo.rar"

    def huge(_url: str, dest_dir: Path) -> Path:
        (dest_dir / "huge.rar").write_bytes(b"Rar!" + b"\x00" * 5000)
        return dest_dir / "huge.rar"

    with pytest.raises(DemoDownloadError, match="not a file"):
        download_demo(URL, tmp_path, downloader=missing)
    with pytest.raises(DemoDownloadError, match="empty"):
        download_demo(URL, tmp_path, downloader=empty)
    with pytest.raises(DemoDownloadError, match="HTML page"):
        download_demo(URL, tmp_path, downloader=challenge)
    with pytest.raises(DemoDownloadError, match="size limit"):
        download_demo(URL, tmp_path, downloader=huge, max_bytes=1000)
    assert list(tmp_path.iterdir()) == []  # rejected files are removed


def test_injected_downloader_exceptions_propagate_unchanged(tmp_path: Path) -> None:
    class HLTVBlockedError(Exception):
        pass

    def blocked(_url: str, _dest: Path) -> Path:
        raise HLTVBlockedError("cloudflare challenge")

    with pytest.raises(HLTVBlockedError):
        download_demo(URL, tmp_path, downloader=blocked)


# ---------------------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("navi-vs-faze.rar", "navi-vs-faze.rar"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\demo.rar", "demo.rar"),
        ("dé mo (1).rar", "d_mo_1_.rar"),
        ("", "demo-archive"),
        ("...", "demo-archive"),
        ("a" * 400 + ".rar", "a" * 146 + ".rar"),
    ],
)
def test_safe_filename(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected


def test_looks_like_html(tmp_path: Path) -> None:
    page = tmp_path / "page"
    page.write_bytes(b"\r\n <!DOCTYPE HTML><html>")
    archive = tmp_path / "archive"
    archive.write_bytes(RAR_BYTES)
    assert looks_like_html(page) and not looks_like_html(archive)
    assert not looks_like_html(tmp_path / "missing")
