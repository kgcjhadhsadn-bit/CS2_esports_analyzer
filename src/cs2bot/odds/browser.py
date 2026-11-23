"""Playwright Betclic source: public pages only, no login, throw-away browser context.

Flow of :meth:`BetclicBrowserSource.fetch_cs2_events` (research notes: :mod:`cs2bot.odds.sources`):

1. launch Chromium (``settings.betclic.headless`` / ``chromium_executable`` / ``proxy_url``) with a
   fresh, non-persistent context (``pl-PL``, ``Europe/Warsaw``) - never the executor's logged-in
   profile;
2. install a page script that tees every ``fetch`` / XHR response going to the offering gRPC-web API
   and hands the first frame(s) to Python (``response.body()`` never resolves for those server
   streams), and listen to ``page.on("response")`` for JSON responses;
3. open ``base_url + cs2_listing_path``, collect event links (``...-m<id>``; filtered with the CS2
   pattern when the listing mixes e-sports); the soonest events are kept and visited LAST so
   that their prices are the freshest when the crawl ends;
4. visit each event page with polite random pauses; ask the page for every market category found
   in its state (``GetMatchWithNotification`` with each ``ca_*`` id - exactly what the site does
   when a tab is opened) and fall back to clicking category tabs;
5. archive every payload (HTML incl. ``ng-state``, JSON, gRPC) under
   ``<data_dir>/raw/betclic/<UTC date>/`` and parse everything with the generic walker.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import random
import re
import sys
import threading
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urljoin, urlsplit

from cs2bot.config import Settings
from cs2bot.domain.odds import CollectedEvent
from cs2bot.odds.grpc_web import (
    MATCH_SERVICE,
    METHOD_GET_MATCH,
    betclic_grpc_headers,
    get_match_request,
    grpc_web_frame,
)
from cs2bot.odds.payload_parser import ParsedEvent, find_match_categories, find_request_headers
from cs2bot.odds.source_config import BetclicSourceConfig
from cs2bot.odds.sources import (
    CapturedPayload,
    FetchInfo,
    PayloadKind,
    RawPayloadStore,
    classify_body,
    ids_from_links,
    parse_captured,
    parse_captured_full,
    payload_documents,
    select_cs2,
)
from cs2bot.odds.text import fold_text
from cs2bot.utils.time import utcnow

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Request, Response

logger = logging.getLogger(__name__)

ContextHook = Callable[["BrowserContext"], Awaitable[None]]
_STATIC_RESOURCES = frozenset({"image", "stylesheet", "font", "media", "script", "manifest", "texttrack"})


class BetclicSourceError(RuntimeError):
    """The Betclic listing page could not be loaded and nothing was collected."""


_SAFE_HEADER_RE = re.compile(r"^(?:x-bg-|appversion$|x-grpc|accept-language$)", re.IGNORECASE)

# Runs in every page before its own scripts. Tees fetch/XHR responses that go to the gRPC-web
# offering API and posts the first complete frame(s) to Python via the exposed binding.
_CAPTURE_SCRIPT = r"""
(() => {
  if (window.__cs2botHooked) { return; }
  window.__cs2botHooked = true;
  const MARKERS = %(markers)s;
  const MAX_BYTES = %(max_bytes)d;
  const MAX_FRAMES = %(max_frames)d;
  const interesting = (url, ctype) => {
    const u = String(url || '');
    return MARKERS.some((m) => u.indexOf(m) >= 0) || /grpc/i.test(ctype || '');
  };
  const toB64 = (bytes) => {
    let s = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(s);
  };
  const asBytes = (body) => {
    try {
      if (!body) { return null; }
      if (body instanceof Uint8Array) { return body; }
      if (body instanceof ArrayBuffer) { return new Uint8Array(body); }
      if (ArrayBuffer.isView(body)) { return new Uint8Array(body.buffer, body.byteOffset, body.byteLength); }
      if (typeof body === 'string') { return new TextEncoder().encode(body); }
    } catch (e) {}
    return null;
  };
  const framesDone = (buf, maxFrames) => {
    let pos = 0;
    let frames = 0;
    while (pos + 5 <= buf.length) {
      const len = ((buf[pos + 1] << 24) >>> 0) + (buf[pos + 2] << 16) + (buf[pos + 3] << 8) + buf[pos + 4];
      if (pos + 5 + len > buf.length) { return false; }
      const flags = buf[pos];
      pos += 5 + len;
      if (flags & 0x80) { return true; }
      frames += 1;
      if (frames >= maxFrames) { return true; }
    }
    return false;
  };
  const send = (url, bytes, ctype, req) => {
    try {
      if (window.__cs2botCapture) {
        window.__cs2botCapture({url: String(url), body: toB64(bytes), contentType: ctype || '',
                                request: req ? toB64(req) : ''});
      }
    } catch (e) {}
  };
  const readStream = async (reader, url, ctype, req) => {
    let buf = new Uint8Array(0);
    try {
      while (true) {
        const {value, done} = await reader.read();
        if (value) {
          const merged = new Uint8Array(buf.length + value.length);
          merged.set(buf);
          merged.set(value, buf.length);
          buf = merged;
        }
        if (done || buf.length > MAX_BYTES || framesDone(buf, MAX_FRAMES)) { break; }
      }
    } catch (e) {}
    try { reader.cancel(); } catch (e) {}
    if (buf.length) { send(url, buf, ctype, req); }
  };
  const originalFetch = window.fetch;
  window.__cs2botOrigFetch = originalFetch;
  window.__cs2botFramesDone = framesDone;
  window.__cs2botToB64 = toB64;
  if (originalFetch) {
    window.fetch = async function (input, init) {
      const resp = await originalFetch.apply(this, arguments);
      try {
        const url = (typeof input === 'string') ? input : ((input && input.url) || '');
        const ctype = resp.headers.get('content-type') || '';
        if (interesting(url, ctype) && resp.body && resp.clone) {
          readStream(resp.clone().body.getReader(), url, ctype, asBytes(init && init.body));
        }
      } catch (e) {}
      return resp;
    };
  }
  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__cs2botUrl = url;
    return originalOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    try {
      const xhr = this;
      const req = asBytes(body);
      xhr.addEventListener('load', () => {
        try {
          const ctype = xhr.getResponseHeader('content-type') || '';
          if (!interesting(xhr.__cs2botUrl, ctype)) { return; }
          let bytes = null;
          const textual = xhr.responseType === '' || xhr.responseType === 'text';
          if (xhr.responseType === 'arraybuffer' && xhr.response) { bytes = new Uint8Array(xhr.response); }
          else if (textual && typeof xhr.responseText === 'string') {
            bytes = new TextEncoder().encode(xhr.responseText);
          }
          if (bytes && bytes.length) { send(xhr.__cs2botUrl, bytes, ctype, req); }
        } catch (e) {}
      });
    } catch (e) {}
    return originalSend.apply(this, arguments);
  };
})();
"""

# Asks the offering API for one market category from inside the page (same network stack, cookies
# and IP as the site itself). Uses the un-hooked fetch so the response is not captured twice.
_CATEGORY_FETCH_JS = r"""
async ({url, body, headers, timeoutMs, maxBytes}) => {
  const doFetch = window.__cs2botOrigFetch || window.fetch;
  const toB64 = window.__cs2botToB64 || ((b) => btoa(String.fromCharCode.apply(null, b)));
  const done = window.__cs2botFramesDone || (() => false);
  const bytes = Uint8Array.from(atob(body), (c) => c.charCodeAt(0));
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const resp = await doFetch(url, {method: 'POST', body: bytes, headers, signal: ctrl.signal,
                                     credentials: 'omit', mode: 'cors'});
    const ctype = resp.headers.get('content-type') || '';
    let buf = new Uint8Array(0);
    if (resp.body) {
      const reader = resp.body.getReader();
      while (true) {
        const {value, done: finished} = await reader.read();
        if (value) {
          const merged = new Uint8Array(buf.length + value.length);
          merged.set(buf);
          merged.set(value, buf.length);
          buf = merged;
        }
        if (finished || buf.length > maxBytes || done(buf, 1)) { break; }
      }
      try { reader.cancel(); } catch (e) {}
    } else {
      buf = new Uint8Array(await resp.arrayBuffer());
    }
    return {status: resp.status, contentType: ctype, body: toB64(buf)};
  } catch (e) {
    return {error: String(e)};
  } finally {
    clearTimeout(timer);
  }
}
"""


def run_coroutine_blocking[T](factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run a coroutine to completion in a dedicated thread with its own event loop.

    Safe to call from synchronous code *and* from inside a running event loop (the API server /
    scheduler). On Windows a Proactor loop is used because Playwright needs subprocess support.
    """
    result: dict[str, Any] = {}

    def runner() -> None:
        if sys.platform == "win32":
            loop: asyncio.AbstractEventLoop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result["value"] = loop.run_until_complete(factory())
        except BaseException as exc:
            result["error"] = exc
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                asyncio.set_event_loop(None)
                loop.close()

    thread = threading.Thread(target=runner, name="cs2bot-betclic-browser", daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    value: T = result["value"]
    return value


def playwright_proxy(url: str) -> dict[str, str]:
    """``http://user:pass@host:port`` -> Playwright proxy settings (credentials kept separate)."""
    parts = urlsplit(url)
    server = f"{parts.scheme or 'http'}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    proxy = {"server": server}
    if parts.username:
        proxy["username"] = unquote(parts.username)
    if parts.password:
        proxy["password"] = unquote(parts.password)
    return proxy


class BetclicBrowserSource:
    """:class:`~cs2bot.odds.sources.OddsSource` driving a real (headless) Chromium."""

    def __init__(
        self,
        settings: Settings,
        *,
        config: BetclicSourceConfig | None = None,
        store: RawPayloadStore | None = None,
        context_hook: ContextHook | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.config = config or BetclicSourceConfig.load(settings)
        self.store = store or RawPayloadStore.from_settings(
            settings, enabled=self.config.save_raw, retention_days=self.config.raw_retention_days
        )
        self._context_hook = context_hook
        self._sleep = sleep or asyncio.sleep
        self._info = FetchInfo()
        self._cs2_re = re.compile(self.config.cs2_pattern)

    # ---------------------------------------------------------------- public API
    def fetch_cs2_events(self) -> list[CollectedEvent]:
        return run_coroutine_blocking(self._fetch)

    def last_fetch_info(self) -> FetchInfo:
        return self._info

    # ---------------------------------------------------------------- launch options
    def _launch_kwargs(self) -> dict[str, Any]:
        bs = self.settings.betclic
        kwargs: dict[str, Any] = {"headless": bs.headless}
        if bs.chromium_executable:
            kwargs["executable_path"] = bs.chromium_executable
        if bs.proxy_url is not None:
            kwargs["proxy"] = playwright_proxy(bs.proxy_url.get_secret_value())
        return kwargs

    def _context_kwargs(self) -> dict[str, Any]:
        cfg = self.config
        kwargs: dict[str, Any] = {
            "locale": "pl-PL",
            "timezone_id": self.settings.timezone or "Europe/Warsaw",
            "viewport": {"width": cfg.viewport_width, "height": cfg.viewport_height},
            "extra_http_headers": {"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.6"},
        }
        if cfg.user_agent:
            kwargs["user_agent"] = cfg.user_agent
        return kwargs

    # ---------------------------------------------------------------- the crawl
    async def _fetch(self) -> list[CollectedEvent]:
        from playwright.async_api import async_playwright

        run = _CrawlRun(self)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(**self._launch_kwargs())
            try:
                context = await browser.new_context(**self._context_kwargs())
                try:
                    if self._context_hook is not None:
                        await self._context_hook(context)
                    await context.expose_binding("__cs2botCapture", run.on_capture)
                    await context.add_init_script(
                        _CAPTURE_SCRIPT
                        % {
                            "markers": json.dumps(list(self.config.grpc_url_markers)),
                            "max_bytes": self.config.max_payload_bytes,
                            "max_frames": max(1, self.config.grpc_max_frames),
                        }
                    )
                    page = await context.new_page()
                    page.set_default_navigation_timeout(self.config.navigation_timeout_s * 1000)
                    page.on("response", run.on_response)
                    page.on("request", run.on_request)
                    await run.crawl(page)
                finally:
                    await context.close()
            finally:
                await browser.close()
        return run.finish()


class _CrawlRun:
    """State of one crawl (lives in the worker thread's event loop)."""

    def __init__(self, source: BetclicBrowserSource) -> None:
        self.source = source
        self.cfg = source.config
        self.settings = source.settings
        self.info = FetchInfo(started_at=utcnow())
        self.captured: list[CapturedPayload] = []
        self.tasks: set[asyncio.Task[None]] = set()
        self.grpc_headers: dict[str, str] = {}
        self.visited_ok: set[str] = set()
        # visited, but some market categories could not be fetched: the market list is incomplete
        self.incomplete: set[str] = set()
        self.link_ids: dict[str, str] = {}
        self._seen_bodies: set[int] = set()
        self._hooked: list[tuple[str, bytes]] = []  # (url, first frames) captured by the page hook
        self._grpc_fallback: list[CapturedPayload] = []  # complete gRPC bodies seen by page.on("response")
        self._consent_done = False
        self.listing_failed = False

    # ---------------------------------------------------------------- capture
    def _add(self, payload: CapturedPayload) -> None:
        key = hash((payload.kind, payload.url, payload.data))
        if key in self._seen_bodies:
            return
        self._seen_bodies.add(key)
        if payload.kind != "html" or self.cfg.save_html:
            self.source.store.save(payload)
        self.captured.append(payload)
        self.info.payloads_captured += 1

    def _ignored(self, url: str) -> bool:
        low = url.lower()
        return any(marker in low for marker in self.cfg.ignore_url_patterns)

    def on_capture(self, source: Any, payload: Any) -> None:
        """Binding called by the page script with ``{url, body, contentType, request}`` (base64)."""
        try:
            data = base64.b64decode(payload.get("body") or "")
            request = base64.b64decode(payload.get("request") or "") or None
            ctype = payload.get("contentType") or None
            kind = classify_body(data, ctype) or "grpc"
            page_url = getattr(getattr(source, "page", None), "url", None)
            url = str(payload.get("url") or "")
            self._hooked.append((url, data))
            self._add(
                CapturedPayload(
                    kind=kind,
                    data=data,
                    captured_at=utcnow(),
                    url=url,
                    page_url=page_url,
                    content_type=ctype,
                    request_body=request,
                )
            )
        except Exception:
            logger.debug("bad payload from page capture hook", exc_info=True)

    def on_request(self, request: Request) -> None:
        url = request.url
        if any(m in url for m in self.cfg.grpc_url_markers) and not self.grpc_headers:
            self.grpc_headers = {k: v for k, v in request.headers.items() if _SAFE_HEADER_RE.match(k)}

    def on_response(self, response: Response) -> None:
        if response.request.resource_type in _STATIC_RESOURCES:
            return
        task = asyncio.get_running_loop().create_task(self._read_response(response))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _read_response(self, response: Response) -> None:
        try:
            url = response.url
            if self._ignored(url) or response.status >= 400:
                return
            ctype = (await response.header_value("content-type")) or ""
            kind: PayloadKind | None = None
            if "json" in ctype.lower():
                kind = "json"
            elif "grpc" in ctype.lower():
                kind = "grpc"
            if kind is None:
                return
            length = await response.header_value("content-length")
            if length and length.isdigit() and int(length) > self.cfg.max_payload_bytes:
                return
            timeout = self.cfg.body_timeout_s if kind == "json" else min(2.0, self.cfg.body_timeout_s)
            body = await asyncio.wait_for(response.body(), timeout=timeout)
            if len(body) > self.cfg.max_payload_bytes:
                return
            payload = CapturedPayload(
                kind=kind,
                data=body,
                captured_at=utcnow(),
                url=url,
                page_url=response.frame.page.url if response.frame else None,
                content_type=ctype,
            )
            if kind == "grpc":
                # Prefer the page hook's copy (it carries the request body); keep this one only as
                # a fallback when the hook missed the stream - decided in _drain().
                self._grpc_fallback.append(payload)
            else:
                self._add(payload)
        except (TimeoutError, asyncio.CancelledError):
            return  # streaming gRPC bodies never finish - the page hook captures those
        except Exception:
            logger.debug("could not read response body", exc_info=True)

    async def _drain(self) -> None:
        if self.tasks:
            _, pending = await asyncio.wait(set(self.tasks), timeout=self.cfg.body_timeout_s + 2)
            for task in pending:
                task.cancel()
        fallback, self._grpc_fallback = self._grpc_fallback, []
        for payload in fallback:
            url = payload.url or ""
            if not any(url == h_url and payload.data.startswith(h_data) for h_url, h_data in self._hooked):
                self._add(payload)

    # ---------------------------------------------------------------- navigation
    async def _pause(self, lo: float, hi: float) -> None:
        delay = random.uniform(lo, max(lo, hi))
        if delay > 0:
            await self.source._sleep(delay)

    async def _visit(self, page: Page, url: str) -> bool:
        from playwright.async_api import Error as PlaywrightError

        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            self.info.errors.append(f"navigation to {url} failed: {type(exc).__name__}: {str(exc)[:200]}")
            return False
        await asyncio.sleep(self.cfg.settle_s)  # real wait: lets in-page captures reach Python
        with contextlib.suppress(PlaywrightError):  # gRPC streams keep the network busy: expected
            await page.wait_for_load_state("networkidle", timeout=3000)
        if not self._consent_done:
            await self._accept_consent(page)
        await self._drain()
        try:
            html = await page.content()
        except PlaywrightError:
            html = ""
        if html:
            self._add(
                CapturedPayload(
                    kind="html",
                    data=html.encode("utf-8"),
                    captured_at=utcnow(),
                    url=page.url,
                    page_url=page.url,
                    content_type="text/html",
                )
            )
        self.info.pages_visited += 1
        if response is not None and response.status >= 400:
            self.info.errors.append(f"{url} answered HTTP {response.status}")
            return False
        return True

    async def _accept_consent(self, page: Page) -> None:
        from playwright.async_api import Error as PlaywrightError

        self._consent_done = True
        for selector in self.cfg.consent_selectors:
            try:
                button = page.locator(selector).first
                if await button.is_visible(timeout=500):
                    await button.click(timeout=2000)
                    await self.source._sleep(0.5)
                    return
            except PlaywrightError:
                continue

    async def _event_links(self, page: Page) -> dict[str, str]:
        from playwright.async_api import Error as PlaywrightError

        try:
            hrefs: list[str] = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        except PlaywrightError:
            hrefs = []
        links = ids_from_links(hrefs, self.cfg.event_link_pattern)
        cs2 = {eid: url for eid, url in links.items() if self._is_cs2_text(url)}
        if cs2:
            return cs2
        # No CS2-looking URL: assume a CS2-only listing whose slugs omit the game name, but stay
        # inside the listing's own section (skips football "top events" sidebars and the like).
        section = self.settings.betclic.cs2_listing_path.rstrip("/").lower()
        same_section = {
            eid: url for eid, url in links.items() if section and section in urlsplit(url).path.lower()
        }
        if same_section or links:
            self.info.notes["link_filter"] = "no CS2-looking event links; using the listing section's links"
        return same_section

    def _is_cs2_text(self, text: str | None) -> bool:
        return bool(text) and bool(self.source._cs2_re.search(fold_text(text or "")))

    def _event_url(self, event_id: str, record: ParsedEvent | None) -> str:
        if record is not None and record.url:
            return record.url
        bs = self.settings.betclic
        return self.cfg.event_url_template.format(
            base_url=bs.base_url.rstrip("/"), listing_path=bs.cs2_listing_path, event_id=event_id
        )

    def _plan(self, links: dict[str, str], listing_records: list[ParsedEvent]) -> list[tuple[str, str]]:
        """Event pages to visit: CS2 events inside the look-ahead window, the soonest ones LAST.

        When more events are listed than ``max_events_per_run`` the soonest ones are kept. They
        are visited at the END of the crawl: a crawl takes minutes and its prices are stored when
        it finishes, so whatever is visited first is the stalest by then - and the matches about
        to start are the ones whose prices must still be inside ``betclic.odds_max_age_s`` when
        the decision job runs. Events without a known start time are visited first.
        """
        now = utcnow()
        horizon = now + timedelta(hours=self.settings.scope.max_hours_ahead)
        records = {r.external_id: r for r in listing_records}
        candidates: dict[str, str] = dict(links)
        for rec in select_cs2(listing_records, pattern=self.source._cs2_re, allowed_ids=set(links)):
            candidates.setdefault(rec.external_id, self._event_url(rec.external_id, rec))
        planned: list[tuple[datetime | None, int, str, str]] = []
        for index, (event_id, url) in enumerate(candidates.items()):
            known = records.get(event_id)
            starts = known.starts_at if known is not None else None
            if known is not None and known.is_live:
                continue  # pre-match only: no need to open live events
            if starts is not None and (starts > horizon or starts < now - timedelta(hours=1)):
                continue
            planned.append((starts, index, event_id, url))
        far_future = now + timedelta(days=3650)
        planned.sort(key=lambda p: (p[0] or far_future, p[1]))
        soonest = planned[: self.cfg.max_events_per_run]
        return [(eid, url) for _, _, eid, url in reversed(soonest)]

    async def _fetch_categories(self, page: Page, event_id: str, docs: list[Any]) -> int:
        """Fetch every market category of an event; returns how many were captured.

        When a category that the page announces cannot be fetched the event is recorded in
        ``self.incomplete``: its market list is then known to be partial and the collector must
        not mark the markets it did not see as unavailable.
        """
        if not event_id.isdigit():
            return 0
        found = find_match_categories(docs, event_id)
        categories = found[: self.cfg.max_categories_per_event]
        if len(found) > len(categories):
            self.incomplete.add(event_id)
        if not categories:
            return 0
        bs = self.settings.betclic
        url = f"{self.cfg.grpc_base_url.rstrip('/')}/{MATCH_SERVICE}/{METHOD_GET_MATCH}"
        headers = betclic_grpc_headers(bs.base_url, bs.country_code, bs.language, browser=True)
        headers |= {k: v for k, v in find_request_headers(docs).items() if _SAFE_HEADER_RE.match(k)}
        headers |= self.grpc_headers
        fetched = 0
        for category in categories:
            body = grpc_web_frame(get_match_request(int(event_id), bs.language, category))
            try:
                result = await page.evaluate(
                    _CATEGORY_FETCH_JS,
                    {
                        "url": url,
                        "body": base64.b64encode(body).decode("ascii"),
                        "headers": headers,
                        "timeoutMs": int(self.cfg.grpc_read_timeout_s * 1000),
                        "maxBytes": self.cfg.max_payload_bytes,
                    },
                )
            except Exception as exc:
                self.info.errors.append(f"category {category} of {event_id}: {type(exc).__name__}")
                self.incomplete.add(event_id)
                break
            if not isinstance(result, dict) or result.get("error") or int(result.get("status") or 0) >= 400:
                detail = result.get("error") or result.get("status") if isinstance(result, dict) else result
                self.info.errors.append(f"category {category} of {event_id}: {detail}")
                self.incomplete.add(event_id)
                continue
            data = base64.b64decode(result.get("body") or "")
            if not data:
                self.incomplete.add(event_id)
            if data:
                # page.on("response") sees this request too: register it so that copy is dropped
                self._hooked.append((url, data))
                self._add(
                    CapturedPayload(
                        kind="grpc",
                        data=data,
                        captured_at=utcnow(),
                        url=f"{url}#{category}",
                        page_url=page.url,
                        content_type=result.get("contentType") or "application/grpc-web+proto",
                        request_body=body,
                    )
                )
                fetched += 1
            await self._pause(self.cfg.min_category_delay_s, self.cfg.max_category_delay_s)
        return fetched

    async def _click_tabs(self, page: Page) -> int:
        from playwright.async_api import Error as PlaywrightError

        start_url = page.url
        clicks = 0
        for selector in self.cfg.category_tab_selectors:
            try:
                tabs = page.locator(selector)
                count = min(await tabs.count(), self.cfg.max_tab_clicks - clicks)
            except PlaywrightError:
                continue
            for i in range(count):
                try:
                    await tabs.nth(i).click(timeout=2000)
                    clicks += 1
                    await asyncio.sleep(max(0.3, self.cfg.settle_s / 2))
                    if page.url != start_url and "-m" not in page.url:
                        await page.go_back()
                except PlaywrightError:
                    continue
            if clicks >= self.cfg.max_tab_clicks:
                break
        await self._drain()
        return clicks

    async def crawl(self, page: Page) -> None:
        bs = self.settings.betclic
        listing_url = urljoin(bs.base_url.rstrip("/") + "/", bs.cs2_listing_path.lstrip("/"))
        start = len(self.captured)
        if not await self._visit(page, listing_url):
            self.listing_failed = True
            self.info.errors.append("listing page could not be loaded")
        self.link_ids = await self._event_links(page)
        listing_records, _ = parse_captured(self.captured[start:])
        plan = self._plan(self.link_ids, listing_records)
        self.info.notes["events_planned"] = len(plan)
        for event_id, url in plan:
            await self._pause(self.cfg.min_page_delay_s, self.cfg.max_page_delay_s)
            before = len(self.captured)
            if not await self._visit(page, url):
                continue
            new_payloads = self.captured[before:]
            docs = [doc for p in new_payloads for doc in payload_documents(p)]
            fetched = await self._fetch_categories(page, event_id, docs) if self.cfg.fetch_categories else 0
            if fetched == 0 and self.cfg.click_category_tabs:
                await self._click_tabs(page)
            await self._drain()
            self.visited_ok.add(event_id)

    def finish(self) -> list[CollectedEvent]:
        parsed = parse_captured_full(self.captured)
        allowed = set(self.link_ids) | self.visited_ok
        kept = select_cs2(parsed.records, pattern=self.source._cs2_re, allowed_ids=allowed)
        parsed.fill_info(self.info, kept)
        self.info.partial_event_ids = {
            r.external_id
            for r in kept
            if r.external_id not in self.visited_ok or r.external_id in self.incomplete
        }
        if self.incomplete:
            self.info.notes["events_incomplete"] = len(self.incomplete)
        self.info.finished_at = utcnow()
        self.info.notes["events_found"] = len(kept)
        self.source._info = self.info
        if self.listing_failed and not kept:
            raise BetclicSourceError("; ".join(self.info.errors[:3]) or "listing page could not be loaded")
        try:
            removed = self.source.store.cleanup(self.info.finished_at)
            if removed:
                self.info.notes["raw_days_removed"] = removed
        except OSError:
            logger.debug("raw payload cleanup failed", exc_info=True)
        return parsed.collected(kept)


# Name used in docs/ARCHITECTURE.md
BetclicSource = BetclicBrowserSource
