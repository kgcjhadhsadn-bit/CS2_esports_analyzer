"""Demo archive download.

``download_demo`` is transport-agnostic: the integration layer passes a *downloader* callable (for HLTV a
browser-based one, because ``/download/demo/<id>`` sits behind the same protection as the pages), and
the default is a plain streaming ``httpx`` download for sources that allow it.

Downloader protocol: ``downloader(url, dest_dir) -> Path`` must save the file somewhere inside
``dest_dir`` and return its path. Whatever it returns is validated here (exists, non-empty, below the
size limit, not an HTML error/challenge page), so a misbehaving downloader fails loudly instead of
feeding garbage to the extractor.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from cs2bot.ingest.demos.errors import DemoDownloadBlockedError, DemoDownloadError
from cs2bot.utils.http import DEFAULT_USER_AGENT
from cs2bot.utils.logging import get_logger

log = get_logger(__name__)

Downloader = Callable[[str, Path], Path]

# A BO5 archive from HLTV is ~1.5-2 GB; anything far beyond that is not a demo archive.
MAX_ARCHIVE_BYTES = 6 * 1024**3
_CHUNK_BYTES = 1 << 20
_PART_SUFFIX = ".part"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
# Answers that mean "you are being rate-limited / challenged", not "this file does not exist".
BLOCK_STATUSES: frozenset[int] = frozenset({403, 429, 503})
_CHALLENGE_RE = re.compile(
    r"<title>\s*(just a moment|attention required|access denied|please wait)"
    r"|cf-chl-|challenge-platform/h/|cf_chl_opt|id=\"challenge-form\"|error code: 10\d\d",
    re.IGNORECASE,
)


def looks_like_challenge(html: str) -> bool:
    """True for Cloudflare-style interstitial / block pages (title and token markers only)."""
    return bool(html) and _CHALLENGE_RE.search(html[:200_000]) is not None


def safe_filename(name: str, *, default: str = "demo-archive") -> str:
    """Filesystem-safe basename (no directories, no odd characters, bounded length)."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = _SAFE_NAME_RE.sub("_", base).strip("._")
    return base[-150:] if base else default


def _filename_from_response(response: httpx.Response, url: str) -> str:
    """Content-Disposition filename, else the last path segment of the final URL."""
    disposition = response.headers.get("content-disposition")
    if disposition:
        message = Message()
        message["content-disposition"] = disposition
        filename = message.get_filename()
        if filename:
            return safe_filename(filename)
    for candidate in (str(response.url), url):
        segment = unquote(urlsplit(candidate).path.rsplit("/", 1)[-1])
        if segment:
            return safe_filename(segment)
    return "demo-archive"


def looks_like_html(path: Path) -> bool:
    """True when the file starts like an HTML document (error page / challenge instead of a demo)."""
    try:
        with path.open("rb") as fh:
            head = fh.read(512).lstrip().lower()
    except OSError:
        return False
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<?xml"))


def http_download(
    url: str,
    dest_dir: Path,
    *,
    timeout_s: float = 900.0,
    max_bytes: int = MAX_ARCHIVE_BYTES,
    client: httpx.Client | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
) -> Path:
    """Stream ``url`` into ``dest_dir`` (redirects followed, temp file + atomic rename).

    ``timeout_s`` is the budget for the *whole* transfer; individual socket operations time out
    after 60 s. The partial file is always removed on failure. HTTP 403 / 429 / 503 and challenge
    pages raise :class:`DemoDownloadBlockedError` (the demo job then stops its batch without using up
    an attempt); other failures raise :class:`DemoDownloadError`.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    owns_client = client is None
    http = client or httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(60.0, connect=30.0),
        headers={"User-Agent": user_agent},
    )
    part: Path | None = None
    deadline = time.monotonic() + timeout_s
    try:
        with http.stream("GET", url, follow_redirects=True) as response:
            if response.status_code in BLOCK_STATUSES:
                raise DemoDownloadBlockedError(
                    f"HTTP {response.status_code} for {url}: the source blocks plain HTTP downloads "
                    "(pass a browser downloader, e.g. cs2bot.ingest.demos.BrowserDownloader)"
                )
            if response.status_code >= 400:
                raise DemoDownloadError(f"HTTP {response.status_code} for {url}")
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" in content_type:
                head = response.read()[:200_000].decode("utf-8", errors="replace")
                if looks_like_challenge(head):
                    raise DemoDownloadBlockedError(f"challenge page instead of a demo archive from {url}")
                raise DemoDownloadError(f"got an HTML page instead of a demo archive from {url}")
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise DemoDownloadError(f"archive too large ({declared} bytes > {max_bytes}) at {url}")
            final = dest_dir / _filename_from_response(response, url)
            part = final.with_name(final.name + _PART_SUFFIX)
            written = 0
            with part.open("wb") as fh:
                for chunk in response.iter_bytes(_CHUNK_BYTES):
                    written += len(chunk)
                    if written > max_bytes:
                        raise DemoDownloadError(f"archive exceeds the size limit of {max_bytes} bytes: {url}")
                    if time.monotonic() > deadline:
                        raise DemoDownloadError(f"download exceeded {timeout_s:.0f}s: {url}")
                    fh.write(chunk)
            if written == 0:
                raise DemoDownloadError(f"empty response body from {url}")
            encoded = response.headers.get("content-encoding", "identity").lower() not in ("", "identity")
            if declared and declared.isdigit() and not encoded and int(declared) != written:
                raise DemoDownloadError(f"truncated download ({written} of {declared} bytes) from {url}")
            os.replace(part, final)
            part = None
            log.info("downloaded demo archive %s (%d bytes)", final.name, written)
            return final
    except httpx.HTTPError as exc:
        raise DemoDownloadError(f"{type(exc).__name__} while downloading {url}: {exc}") from exc
    finally:
        if part is not None:
            part.unlink(missing_ok=True)
        if owns_client:
            http.close()


def download_demo(
    url: str,
    dest_dir: Path,
    *,
    downloader: Downloader | None = None,
    timeout_s: float = 900.0,
    max_bytes: int = MAX_ARCHIVE_BYTES,
) -> Path:
    """Download one demo archive into ``dest_dir`` and return its path.

    ``downloader`` (``(url, dest_dir) -> Path``) replaces the default httpx transfer; exceptions it
    raises propagate unchanged so the caller can recognise source-specific ones (e.g. an HLTV block).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if downloader is not None:
        path = Path(downloader(url, dest_dir))
    else:
        path = http_download(url, dest_dir, timeout_s=timeout_s, max_bytes=max_bytes)
    if not path.is_file():
        raise DemoDownloadError(f"downloader returned {path}, which is not a file")
    size = path.stat().st_size
    if size == 0:
        path.unlink(missing_ok=True)
        raise DemoDownloadError(f"downloaded file {path.name} is empty")
    if size > max_bytes:
        path.unlink(missing_ok=True)
        raise DemoDownloadError(f"downloaded file {path.name} exceeds the size limit ({size} bytes)")
    if looks_like_html(path):
        path.unlink(missing_ok=True)
        raise DemoDownloadError(f"downloaded file {path.name} is an HTML page, not a demo archive")
    return path
