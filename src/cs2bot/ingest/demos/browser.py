"""Browser-based demo downloader (Playwright / Chromium).

HLTV's ``/download/demo/<id>`` links sit behind the same protection as its pages, so a plain HTTP
client usually receives an HTML interstitial instead of the archive. :class:`BrowserDownloader` is a
``(url, dest_dir) -> Path`` callable (the ``downloader`` protocol of
:func:`cs2bot.ingest.demos.download.download_demo`) that lets a real Chromium fetch the file, exactly
as if the owner had clicked the link.

It does **not** solve challenges or disguise the browser. When the site answers with a challenge /
block page instead of a file it waits a little (an interstitial that clears by itself then starts the
download) and otherwise raises :class:`DemoDownloadBlockedError`, which makes the demo job stop its
batch and try again later. With a persistent ``profile_dir`` the cookies of a challenge the owner
passed by hand (``cs2bot ingest hltv-browser``) are reused.

Assumptions (HLTV cannot be reached from the build machine; verified against a local HTTP server):

* the demo URL answers with ``Content-Disposition: attachment`` (possibly after redirects), which
  Chromium turns into a download and Playwright reports as a ``download`` event;
* one Chromium profile can only be open in one browser at a time: do not run this downloader while an
  ``HLTVFetcher`` holds the same profile directory open (run the jobs one after the other, as the
  scheduler lane does), or pass another ``profile_dir`` / an existing Playwright ``context``.

Playwright's sync API must stay in the thread that created it: create, use and close the downloader
in the same thread.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from urllib.parse import unquote, urlsplit

from cs2bot.config import Settings
from cs2bot.ingest.demos.download import BLOCK_STATUSES, looks_like_challenge, safe_filename
from cs2bot.ingest.demos.errors import DemoDownloadBlockedError, DemoDownloadError
from cs2bot.utils.logging import get_logger
from cs2bot.utils.paths import resolve_path

log = get_logger(__name__)

_POLL_MS = 250


def _proxy_dict(proxy_url: str) -> dict[str, str]:
    """``http://user:pass@host:port`` -> Playwright proxy settings (credentials passed separately)."""
    parts = urlsplit(proxy_url)
    if not parts.hostname:
        raise ValueError("proxy URL has no host")
    proxy = {
        "server": f"{parts.scheme or 'http'}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    }
    if parts.username:
        proxy["username"] = unquote(parts.username)
    if parts.password:
        proxy["password"] = unquote(parts.password)
    return proxy


class BrowserDownloader:
    """Download files through Chromium; usable as the ``downloader`` of the demo pipeline.

    ``context`` reuses an already open Playwright ``BrowserContext`` (it is then never closed here);
    otherwise a browser is started lazily on the first download: a persistent one when
    ``profile_dir`` is given, a throw-away one if not. ``chromium_executable`` is only needed when
    Playwright's own Chromium is not installed. ``start_timeout_s`` is how long to wait for the
    download to *begin* (this covers an interstitial that clears by itself); ``timeout_s`` bounds the
    whole transfer.
    """

    def __init__(
        self,
        *,
        context: Any = None,
        profile_dir: Path | str | None = None,
        headless: bool = True,
        chromium_executable: str | None = None,
        proxy_url: str | None = None,
        user_agent: str | None = None,
        referer: str | None = None,
        downloads_dir: Path | str | None = None,
        start_timeout_s: float = 45.0,
        timeout_s: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.profile_dir = Path(profile_dir) if profile_dir is not None else None
        self.headless = headless
        self.chromium_executable = chromium_executable
        self.proxy_url = proxy_url
        self.user_agent = user_agent
        self.referer = referer
        self.downloads_dir = Path(downloads_dir) if downloads_dir is not None else None
        self.start_timeout_s = start_timeout_s
        self.timeout_s = timeout_s
        self._clock = clock
        self._external_context = context
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = context
        self._page: Any = None

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> BrowserDownloader:
        """Downloader configured like the HLTV fetcher (same profile, proxy, user agent, executable)."""
        hltv = settings.hltv
        kwargs: dict[str, Any] = {
            "profile_dir": resolve_path(hltv.browser_profile_dir) if hltv.browser_profile_dir else None,
            "headless": hltv.headless,
            "chromium_executable": hltv.chromium_executable,
            "proxy_url": hltv.proxy_url.get_secret_value() if hltv.proxy_url is not None else None,
            "user_agent": hltv.user_agent,
            "referer": hltv.base_url.rstrip("/") + "/",
            "downloads_dir": resolve_path(settings.demos.storage_dir) / "_browser_downloads",
            "timeout_s": settings.demos.download_timeout_s,
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    # ----------------------------------------------------------------------------------- lifecycle

    def launch_kwargs(self) -> dict[str, Any]:
        """Arguments for ``chromium.launch`` / ``launch_persistent_context`` (no secrets are logged)."""
        kwargs: dict[str, Any] = {"headless": self.headless}
        if self.chromium_executable:
            kwargs["executable_path"] = self.chromium_executable
        if self.proxy_url:
            kwargs["proxy"] = _proxy_dict(self.proxy_url)
        if self.downloads_dir is not None:
            kwargs["downloads_path"] = str(self.downloads_dir)
        return kwargs

    def _ensure_page(self) -> Any:
        if self._page is not None and not self._page.is_closed():
            return self._page
        if self._context is None:
            from playwright.sync_api import sync_playwright

            try:
                if self.downloads_dir is not None:
                    self.downloads_dir.mkdir(parents=True, exist_ok=True)
                self._playwright = sync_playwright().start()
                chromium = self._playwright.chromium
                context_kwargs: dict[str, Any] = {"accept_downloads": True}
                if self.user_agent:
                    context_kwargs["user_agent"] = self.user_agent
                if self.profile_dir is not None:
                    self.profile_dir.mkdir(parents=True, exist_ok=True)
                    self._context = chromium.launch_persistent_context(
                        str(self.profile_dir), **self.launch_kwargs(), **context_kwargs
                    )
                else:
                    self._browser = chromium.launch(**self.launch_kwargs())
                    self._context = self._browser.new_context(**context_kwargs)
            except BaseException:
                self.close()  # never leak a half-started browser
                raise
        self._page = self._context.new_page()
        return self._page

    def close(self) -> None:
        """Close what this object opened (an injected ``context`` stays open)."""
        page, self._page = self._page, None
        if page is not None:
            try:
                page.close()
            except Exception as exc:
                log.debug("closing the download page failed: %s", exc)
        if self._external_context is None:
            for name in ("_context", "_browser"):
                obj = getattr(self, name)
                setattr(self, name, None)
                if obj is not None:
                    try:
                        obj.close()
                    except Exception as exc:
                        log.debug("closing playwright %s failed: %s", name, exc)
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception as exc:
                    log.debug("stopping playwright failed: %s", exc)
                self._playwright = None
            if self.downloads_dir is not None:
                with contextlib.suppress(OSError):
                    self.downloads_dir.rmdir()  # only succeeds when the browser left nothing behind

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    # ------------------------------------------------------------------------------------ download

    def __call__(self, url: str, dest_dir: Path) -> Path:
        """Download ``url`` into ``dest_dir`` and return the file's path.

        Raises :class:`DemoDownloadBlockedError` when a challenge / block page is shown instead of a
        file and :class:`DemoDownloadError` for every other failure.
        """
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        page = self._ensure_page()
        downloads: list[Any] = []

        def on_download(download: Any) -> None:
            downloads.append(download)

        page.on("download", on_download)
        status: int | None = None
        try:
            try:
                goto_kwargs: dict[str, Any] = {
                    "wait_until": "commit",
                    "timeout": self.start_timeout_s * 1000.0,
                }
                if self.referer:
                    goto_kwargs["referer"] = self.referer
                response = page.goto(url, **goto_kwargs)
                status = response.status if response is not None else None
            except Exception as exc:
                # Chromium aborts the navigation when the response turns out to be a download.
                message = str(exc)
                if "Download is starting" not in message and "net::ERR_ABORTED" not in message:
                    self._discard_page()
                    raise DemoDownloadError(
                        f"browser could not open {url}: {type(exc).__name__}: {exc}"
                    ) from exc
            deadline = self._clock() + self.start_timeout_s
            while not downloads and self._clock() < deadline:
                if status is not None and status >= 400 and status not in BLOCK_STATUSES:
                    break  # a plain 404 / 410 / 500 will not turn into a download
                page.wait_for_timeout(_POLL_MS)
            if not downloads:
                html = self._page_html(page)
                if status in BLOCK_STATUSES or looks_like_challenge(html):
                    raise DemoDownloadBlockedError(
                        f"challenge / block page instead of a download at {url} (HTTP {status})"
                    )
                raise DemoDownloadError(f"no download started at {url} (HTTP {status})")
            return self._save(downloads[0], dest_dir, url)
        finally:
            try:
                page.remove_listener("download", on_download)
            except Exception as exc:  # the page is gone
                log.debug("removing the download listener failed: %s", exc)

    def _save(self, download: Any, dest_dir: Path, url: str) -> Path:
        name = safe_filename(download.suggested_filename or "", default="demo-archive")
        target = dest_dir / name
        deadline = self._clock() + self.timeout_s
        try:
            is_over = self._transfer_probe(download)
            if is_over is not None:
                page = download.page
                while not is_over():
                    if self._clock() > deadline:
                        download.cancel()
                        raise DemoDownloadError(f"download exceeded {self.timeout_s:.0f}s: {url}")
                    page.wait_for_timeout(_POLL_MS)
            failure = download.failure()  # blocks until the transfer is over (it already is, normally)
            if failure:
                raise DemoDownloadError(f"download of {url} failed: {failure}")
            download.save_as(str(target))
        except DemoDownloadError:
            target.unlink(missing_ok=True)
            raise
        except Exception as exc:
            target.unlink(missing_ok=True)
            raise DemoDownloadError(f"download of {url} failed: {type(exc).__name__}: {exc}") from exc
        finally:
            try:
                download.delete()  # the browser's temporary copy
            except Exception as exc:
                log.debug("deleting the temporary download failed: %s", exc)
        log.info("browser downloaded %s (%d bytes)", target.name, target.stat().st_size)
        return target

    @staticmethod
    def _transfer_probe(download: Any) -> Callable[[], bool] | None:
        """A non-blocking "has the transfer ended?" check, or ``None`` when it cannot be built.

        Playwright's sync API only has blocking accessors (``path()``, ``failure()``, ``save_as()``),
        which would make ``timeout_s`` unenforceable: a transfer that stalls would block the job for
        ever. The same question is therefore asked on Playwright's own event loop without waiting for
        the answer; the loop runs whenever the page waits (``wait_for_timeout``). This touches private
        attributes, so every failure falls back to the blocking call (no total timeout then).
        """
        try:
            impl = download._impl_obj
            task = impl._loop.create_task(impl._artifact.failure())
        except Exception as exc:
            log.debug("no non-blocking download probe available (%s); waiting without a timeout", exc)
            return None

        def is_over() -> bool:
            if not task.done():
                return False
            if not task.cancelled():
                task.exception()  # mark a failure as seen; ``download.failure()`` reports it properly
            return True

        return is_over

    @staticmethod
    def _page_html(page: Any) -> str:
        try:
            return str(page.content())
        except Exception:  # the page navigated away / was closed
            return ""

    def _discard_page(self) -> None:
        page, self._page = self._page, None
        if page is not None:
            try:
                page.close()
            except Exception as exc:
                log.debug("closing the download page failed: %s", exc)
