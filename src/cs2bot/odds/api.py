"""Direct HTTP Betclic source (no browser), enabled when ``settings.betclic.offering_api_base`` is set.

Two protocols (``BetclicSourceConfig.api_protocol``, ``"auto"`` picks from the base URL):

* ``"grpc"`` - the current gRPC-web offering API (base ``https://offering.begmedia.com/web/
  offering.access.api``): ``GetMatchesBySportWithNotifications`` per configured sport code
  (paginated, 40 per page, total in the response), then ``GetMatchWithNotification`` per event
  for the default bundle and for every ``ca_*`` category found in it. Requests carry the headers
  Betclic's own front-end sends (``X-Bg-Regulation: PL`` ...). The CS2 sport code is unverified
  (``api_sport_codes``); the browser source's archived request bodies reveal the real one.
* ``"rest"`` - the historic JSON offering API (``.../api/pub/v4/events`` listing +
  ``.../v5/events/{id}`` details), URL templates in ``api_list_template`` / ``api_event_template``.

Server streams are read only up to their first complete frame. Requests are paced with a jittered
rate limiter and transient errors retried (:mod:`cs2bot.utils.http`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

import httpx

from cs2bot.config import Settings
from cs2bot.domain.odds import CollectedEvent
from cs2bot.odds.grpc_web import (
    MATCH_SERVICE,
    METHOD_GET_MATCH,
    METHOD_MATCHES_BY_SPORT,
    betclic_grpc_headers,
    body_frames,
    get_match_request,
    grpc_listing_total,
    grpc_web_frame,
    matches_by_sport_request,
)
from cs2bot.odds.payload_parser import ParsedEvent, find_match_categories
from cs2bot.odds.source_config import BetclicSourceConfig
from cs2bot.odds.sources import (
    CapturedPayload,
    FetchInfo,
    RawPayloadStore,
    parse_captured,
    parse_captured_full,
    payload_documents,
    select_cs2,
)
from cs2bot.odds.text import fold_text
from cs2bot.utils.http import RateLimiter, build_http_client, with_retries
from cs2bot.utils.time import utcnow

logger = logging.getLogger(__name__)

DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class BetclicApiError(RuntimeError):
    pass


class BetclicApiSource:
    """:class:`~cs2bot.odds.sources.OddsSource` talking to the offering API directly."""

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.Client | None = None,
        *,
        config: BetclicSourceConfig | None = None,
        store: RawPayloadStore | None = None,
        limiter: RateLimiter | None = None,
        retry_attempts: int = 3,
    ) -> None:
        self.settings = settings
        self.config = config or BetclicSourceConfig.load(settings)
        self.store = store or RawPayloadStore.from_settings(
            settings, enabled=self.config.save_raw, retention_days=self.config.raw_retention_days
        )
        self._client = http_client
        self._owns_client = http_client is None
        self._limiter = limiter or RateLimiter(self.config.api_min_delay_s, self.config.api_max_delay_s)
        self._retry_attempts = max(1, retry_attempts)
        self._cs2_re = re.compile(self.config.cs2_pattern)
        self._info = FetchInfo()

    # ---------------------------------------------------------------- plumbing
    @property
    def base(self) -> str:
        base = self.settings.betclic.offering_api_base
        if not base:
            raise BetclicApiError("settings.betclic.offering_api_base is not set")
        return base.rstrip("/")

    @property
    def protocol(self) -> str:
        if self.config.api_protocol != "auto":
            return self.config.api_protocol
        return (
            "grpc" if ("offering.access.api" in self.base or "offering.begmedia.com" in self.base) else "rest"
        )

    def _client_or_new(self) -> httpx.Client:
        if self._client is None:
            bs = self.settings.betclic
            proxy = bs.proxy_url
            self._client = build_http_client(
                timeout_s=self.config.api_timeout_s,
                proxy=proxy.get_secret_value() if proxy is not None else None,
                user_agent=self.config.user_agent or DEFAULT_BROWSER_UA,
                headers={"Accept-Language": f"{bs.language}-{bs.country_code.upper()},{bs.language};q=0.9"},
            )
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _call[T](self, fn: Callable[[], T]) -> T:
        self._limiter.wait()
        return with_retries(fn, attempts=self._retry_attempts, max_wait_s=20.0)

    def _keep(self, payload: CapturedPayload, payloads: list[CapturedPayload]) -> None:
        self.store.save(payload)
        payloads.append(payload)
        self._info.payloads_captured += 1

    def _get_json(self, url: str, payloads: list[CapturedPayload]) -> list[ParsedEvent]:
        def call() -> httpx.Response:
            resp = self._client_or_new().get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            return resp

        resp = self._call(call)
        payload = CapturedPayload(
            kind="json",
            data=resp.content,
            captured_at=utcnow(),
            url=url,
            content_type=resp.headers.get("content-type"),
        )
        self._keep(payload, payloads)
        records, _ = parse_captured([payload])
        return records

    def _post_grpc(self, method: str, message: bytes, payloads: list[CapturedPayload]) -> CapturedPayload:
        url = f"{self.base}/{MATCH_SERVICE}/{method}"
        body = grpc_web_frame(message)
        bs = self.settings.betclic
        headers = betclic_grpc_headers(bs.base_url, bs.country_code, bs.language)

        def call() -> tuple[bytes, str | None]:
            data = bytearray()
            with self._client_or_new().stream("POST", url, content=body, headers=headers) as resp:
                resp.raise_for_status()
                status = resp.headers.get("grpc-status")
                if status not in (None, "0"):
                    raise BetclicApiError(f"gRPC status {status}: {resp.headers.get('grpc-message', '')}")
                ctype = resp.headers.get("content-type")
                try:
                    for chunk in resp.iter_bytes():
                        data.extend(chunk)
                        # stop at the first complete frame (snapshot) or trailer: it is a stream
                        if body_frames(bytes(data), ctype) or len(data) > self.config.max_payload_bytes:
                            break
                except httpx.ReadTimeout:
                    if not data:
                        raise
            return bytes(data), ctype

        data, ctype = self._call(call)
        payload = CapturedPayload(
            kind="grpc", data=data, captured_at=utcnow(), url=f"{url}", content_type=ctype, request_body=body
        )
        self._keep(payload, payloads)
        return payload

    # ---------------------------------------------------------------- protocols
    def _fetch_rest(self, payloads: list[CapturedPayload]) -> tuple[list[str], set[str]]:
        bs = self.settings.betclic
        cfg = self.config
        listing: list[ParsedEvent] = []
        params = {
            "base": self.base,
            "application": cfg.api_application,
            "country": bs.country_code,
            "language": bs.language,
            "sitecode": bs.site_code,
            "limit": cfg.api_page_size,
        }
        for sport_id in cfg.api_sport_ids:
            for page in range(cfg.api_max_pages):
                url = cfg.api_list_template.format(
                    **params, sport_id=sport_id, offset=page * cfg.api_page_size
                )
                try:
                    found = self._get_json(url, payloads)
                except (httpx.HTTPError, BetclicApiError, ValueError) as exc:
                    self._info.errors.append(f"listing {url}: {type(exc).__name__}: {exc}")
                    break
                listing.extend(found)
                if len(found) < cfg.api_page_size:
                    break
        events = select_cs2(listing, pattern=self._cs2_re)
        ids = list(dict.fromkeys(e.external_id for e in events))[: cfg.max_events_per_run]
        failed: set[str] = set()
        for event_id in ids:
            url = cfg.api_event_template.format(**params, event_id=event_id)
            try:
                self._get_json(url, payloads)
            except (httpx.HTTPError, BetclicApiError, ValueError) as exc:
                self._info.errors.append(f"event {event_id}: {type(exc).__name__}: {exc}")
                failed.add(event_id)
        return ids, failed

    def _fetch_grpc(self, payloads: list[CapturedPayload]) -> tuple[list[str], set[str]]:
        bs = self.settings.betclic
        cfg = self.config
        ids: list[str] = []
        for code in cfg.api_sport_codes:
            code_is_cs2 = bool(self._cs2_re.search(fold_text(code)))
            offset = 0
            for _ in range(cfg.api_max_pages):
                try:
                    payload = self._post_grpc(
                        METHOD_MATCHES_BY_SPORT, matches_by_sport_request(code, bs.language, offset), payloads
                    )
                except (httpx.HTTPError, BetclicApiError) as exc:
                    self._info.errors.append(f"listing {code}@{offset}: {type(exc).__name__}: {exc}")
                    break
                records, _ = parse_captured([payload])
                kept = records if code_is_cs2 else select_cs2(records, pattern=self._cs2_re)
                ids.extend(r.external_id for r in kept if not r.is_live)
                total = grpc_listing_total(payload.data, payload.content_type)
                offset += cfg.api_page_size
                if not records or total is None or offset >= total:
                    break
        ids = list(dict.fromkeys(ids))[: cfg.max_events_per_run]
        failed: set[str] = set()
        for event_id in ids:
            if not event_id.isdigit():
                failed.add(event_id)  # only its listing markets are known: never "disappear" the rest
                continue
            try:
                first = self._post_grpc(
                    METHOD_GET_MATCH, get_match_request(int(event_id), bs.language), payloads
                )
            except (httpx.HTTPError, BetclicApiError) as exc:
                self._info.errors.append(f"event {event_id}: {type(exc).__name__}: {exc}")
                failed.add(event_id)
                continue
            categories = find_match_categories(payload_documents(first), event_id)[
                : cfg.max_categories_per_event
            ]
            for category in categories:
                try:
                    self._post_grpc(
                        METHOD_GET_MATCH, get_match_request(int(event_id), bs.language, category), payloads
                    )
                except (httpx.HTTPError, BetclicApiError) as exc:
                    self._info.errors.append(
                        f"event {event_id} category {category}: {type(exc).__name__}: {exc}"
                    )
                    failed.add(event_id)
        return ids, failed

    # ---------------------------------------------------------------- public API
    def fetch_cs2_events(self) -> list[CollectedEvent]:
        self._info = FetchInfo(started_at=utcnow())
        payloads: list[CapturedPayload] = []
        try:
            if self.protocol == "grpc":
                ids, failed = self._fetch_grpc(payloads)
            else:
                ids, failed = self._fetch_rest(payloads)
        finally:
            self.close()
        parsed = parse_captured_full(payloads)
        wanted = set(ids)
        kept = [r for r in parsed.records if r.external_id in wanted]
        parsed.fill_info(self._info, kept)
        self._info.partial_event_ids = failed
        self._info.notes |= {"protocol": self.protocol, "events_found": len(kept)}
        self._info.finished_at = utcnow()
        try:
            self.store.cleanup(self._info.finished_at)
        except OSError:
            logger.debug("raw payload cleanup failed", exc_info=True)
        return parsed.collected(kept)

    def last_fetch_info(self) -> FetchInfo:
        return self._info
