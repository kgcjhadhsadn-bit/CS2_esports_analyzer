# odds: implementation report

> Written by the engineer who implemented the package. Reviewers update it when the public API changes.

**Tests:** `.venv/bin/python -m pytest tests/odds -q` - 256 passed, 0 failed (lint/type clean: True)

## Summary

P4 (cs2bot.odds) is built and tested: Betclic odds collection, the generic payload parser, gRPC-web decoding, market mapping, event linking, the collector and quotes. Definition of done is met: 256 tests in tests/odds pass (none skipped, including 3 browser tests against local pages), ruff check and format are clean, mypy is clean, and the 37 foundation tests still pass. I changed only files in my owned paths.

Nothing could be checked against live Betclic.pl, so field numbers, the CS2 sport code, category ids, Polish market labels and page selectors are all assumptions until the collector runs on the owner's PC.

Research findings (written up in the docstrings of cs2bot/odds/sources.py and grpc_web.py):
- Betclic's current web apps, betclic.pl included, load odds as binary gRPC-web, not JSON. Requests go to offering.begmedia.com/web/offering.access.api/offering.access.api.MatchService/{GetMatchesBySportWithNotifications, GetMatchWithNotification}. These are server streams, so Playwright's response.body() never finishes for them. Headers include X-Bg-Regulation: PL and X-Bg-Ref-Brand: BETCLIC (a Polish open-source project uses exactly these). Field numbers come from three 2025-26 open-source clients. My request encoder reproduces byte-for-byte a request that one of those projects confirmed against the live server on 2026-09-19.
- Each page embeds Angular state in <script id="ng-state" type="application/json"> (matchId, matchDateUtc, categories with ca_* ids, selections as {id, name, odds, betslipMarketId}). The page also carries dozens of unrelated matches.
- Event pages preload only the "Top" market category. The other categories arrive one gRPC call per ca_* id.
- The historic REST API (offer.cdn.begmedia.com/api/pub/v4|v5/events) is reported to return 404 now. It is still supported through URL templates.

How it works:
- **Browser source:** non-persistent headless Chromium context (pl-PL, Europe/Warsaw), never the executor's profile. A page script tees fetch/XHR gRPC-web streams to Python, and page.on("response") picks up JSON. It reads ng-state, then requests every market category from inside the page the way the site does, with tab clicking as a fallback. It visits CS2 events soonest first with random pauses, and archives every payload under data_dir/raw/betclic/<UTC date>/ with an index.jsonl.
- **API source:** works in REST-template or gRPC-web mode, chosen automatically from offering_api_base.
- **Replay source:** re-parses the archived payloads, so the parser can be fitted offline on the owner's PC.
- **Parser:** recognises events, markets and selections by shape, with configurable key names. It parses Polish decimals and merges the same event across payloads.
- **Mapping:** 18 ordered rules in config/betclic_markets.yaml, Polish and English, converted to our team1/team2 orientation, with the handicap rule tested in both swap directions. Team or player selections that carry a line, score or combination stay unmapped rather than being stripped.
- **Linking:** scores team names in both orientations (deliberately not token_set_ratio, so "Spirit" does not equal "Spirit Academy"), time proximity and competition. It links AUTO only above min_event_link_confidence (dashboard overrides apply) and with a clear margin over the runner-up. CONFIRMED and REJECTED rows are never changed. BETCLIC aliases are stored for AUTO and CONFIRMED links.
- **Collector:** each event runs in its own savepoint, markets are re-mapped on every pass, and a snapshot is written only when odds or availability change. Prices are stamped with each event's capture time, not the run start, because a crawl takes minutes. Markets listing several lines are split into deterministic rows. Live events are stored and flagged; their prices are not processed.

## Public API

All names below are re-exported from `cs2bot.odds` except the grpc_web helpers (import from `cs2bot.odds.grpc_web`). Domain types come from `cs2bot.domain.odds`: CollectedEvent, MappedSelection, OddsQuote.

## Typical wiring (P12)
```python
from cs2bot.odds import MarketMapper, BetclicBrowserSource, BetclicApiSource, collect_once, latest_quotes, closing_odds, list_unmapped
mapper = MarketMapper.from_yaml(settings.betclic.market_rules_file)
source = BetclicApiSource(settings) if settings.betclic.offering_api_base else BetclicBrowserSource(settings)
with session_scope() as s:
    report = collect_once(s, source, mapper, settings=settings)          # leave now=None in production
    quotes = latest_quotes(s, match_id, max_age_s=settings.betclic.odds_max_age_s)
```

## Sources
- `class OddsSource(Protocol)`: `fetch_cs2_events(self) -> list[CollectedEvent]` (runtime_checkable)
- `class SupportsFetchInfo(Protocol)`: `last_fetch_info(self) -> FetchInfo`. This capability is optional; the collector uses it when present.
- `@dataclass FetchInfo`
  - Fields: `started_at: datetime|None`, `finished_at: datetime|None`, `pages_visited: int`, `payloads_captured: int`, `raw_paths_by_event: dict[str, list[str]]`, `captured_at_by_event: dict[str, datetime]`, `partial_event_ids: set[str]`, `errors: list[str]`, `notes: dict[str, Any]`
  - Method: `.summary() -> dict[str, Any]`
  - `partial_event_ids` are events seen only partly (e.g. on the listing page). The collector does not mark their missing markets unavailable.
- `BetclicBrowserSource(settings: Settings, *, config: BetclicSourceConfig | None = None, store: RawPayloadStore | None = None, context_hook: Callable[[playwright BrowserContext], Awaitable[None]] | None = None, sleep: Callable[[float], Awaitable[None]] | None = None)`
  - `.fetch_cs2_events() -> list[CollectedEvent]` blocks; it runs async Playwright in its own thread, so it is safe to call inside a running event loop.
  - It raises `BetclicSourceError` when the listing page fails and nothing was found. Playwright launch errors (e.g. Chromium missing) propagate.
  - `.last_fetch_info() -> FetchInfo`
  - Alias: `BetclicSource`.
  - Settings it uses: `betclic.headless`, `chromium_executable`, `proxy_url`, `base_url`, `cs2_listing_path`, `language`, `country_code`, `scope.max_hours_ahead`, `data_dir`, `timezone`.
- `BetclicApiSource(settings: Settings, http_client: httpx.Client | None = None, *, config: BetclicSourceConfig | None = None, store: RawPayloadStore | None = None, limiter: RateLimiter | None = None, retry_attempts: int = 3)`
  - `.fetch_cs2_events()`, `.last_fetch_info()`, `.protocol -> 'rest'|'grpc'`, `.close()`
  - Raises `BetclicApiError` if `settings.betclic.offering_api_base` is unset.
- `StaticOddsSource(events: Sequence[CollectedEvent], *, partial_event_ids: Iterable[str] = ())` for tests and integration.
- `RawPayloadReplaySource(paths: Sequence[str | Path], *, cs2_pattern: str | None = None, keep_unknown_sport: bool = True, keys: PayloadKeys | None = None, schema: BetclicProtoSchema | None = None)` re-parses archived `*.json`/`*.html`/`*.bin` files. All replayed events are partial.
- `RawPayloadStore(root: Path | None, *, enabled: bool = True, retention_days: int | None = 14)`
  - `RawPayloadStore.from_settings(settings, *, enabled=True, retention_days=14)` uses `data_dir/raw/betclic`.
  - `.save(payload: CapturedPayload) -> Path | None`
  - `.cleanup(now: datetime | None = None) -> int`
- `BetclicSourceConfig` (frozen dataclass of crawl/API knobs)
  - `BetclicSourceConfig.load(settings)` reads the optional `source:` section of `settings.betclic.market_rules_file`.
  - `.from_yaml(path)`, `.from_mapping(dict)`
- `run_coroutine_blocking[T](factory: Callable[[], Coroutine[Any, Any, T]]) -> T`

## Parser
- `extract_events(payloads: Iterable[Any], *, source_url: str | None = None, keys: PayloadKeys | None = None, sport_pattern: str | re.Pattern[str] | None = None, keep_unknown_sport: bool = True) -> list[CollectedEvent]`
  - Payloads may be dicts/lists, JSON text or bytes.
  - Duplicates of the same event are merged; later prices win.
- `extract_json_documents(html: str) -> list[Any]` returns ng-state / serverApp-state (including the old `&q;` escaping), JSON-LD and `window.__X__ = {...}` objects.
- `PayloadKeys` (frozen dataclass of key-name candidates)
- `cs2bot.odds.grpc_web.decode_betclic_grpc_body(body: bytes, *, content_type: str | None = None, schema: BetclicProtoSchema | None = None, max_frames: int | None = 1) -> dict` returns `{"matches": [...], "grpcStatus"?}`.
- `cs2bot.odds.grpc_web.proto_debug_tree(buf: bytes) -> dict | None` gives a readable view for fitting the decoder offline.

## Mapping
- `MarketMapper.from_yaml(path: str | Path) -> MarketMapper` (relative paths: current directory, then project root); `MarketMapper.from_dict(data, *, source='<dict>')`
- `MarketMapper.map(market_raw: str, selection_raw: str, *, event: CollectedEvent, swapped: bool, player_resolver: PlayerResolver | None = None, sibling_selections: Sequence[str] | None = None) -> MappedSelection | None`
  - Returns `None` for unknown markets or selections; these are stored unmapped and never bet.
  - Handicap: `spec.line` is our team1's handicap. "Team2 -1.5" becomes line +1.5 with selection team2.
  - Head-to-head specs store the two player ids sorted ascending.
- `MarketMapper.match_rule(market_raw: str) -> str | None` returns the id of the first applicable rule (diagnostics for `odds unmapped`).
- `MarketMapper.group_key(market_raw, selection_raw, *, event, sibling_selections=None) -> str | None`
- `PlayerResolver = Callable[[str], int | None]`
- `lineup_player_resolver(session: Session, match_id: int, *, fuzzy_threshold: float = 90.0) -> PlayerResolver` uses match lineups, falling back to both teams' current rosters.
- `make_name_resolver(index: Mapping[str, int | None], *, fuzzy_threshold: float = 90.0) -> PlayerResolver`
- `list_unmapped(session: Session, *, limit: int | None = None) -> list[tuple[str, int]]` returns raw market names never mapped, with row counts, most frequent first.
- `list_unmapped_selections(session: Session, *, limit: int | None = 200) -> list[tuple[str, str, int]]`

## Linking
- `link_event(session: Session, event_row: BookmakerEvent, *, settings: Settings, now: datetime | None = None, config: LinkConfig | None = None) -> LinkResult`
  - Sets `match_id`, `teams_swapped`, `link_confidence` and `link_status` (AUTO, PENDING_REVIEW or UNLINKED).
  - Never changes CONFIRMED or REJECTED rows.
- `LinkResult` (pydantic) fields: `event_id`, `match_id`, `status: LinkStatus`, `confidence: float|None`, `teams_swapped`, `changed`, `status_changed`, `candidates: list[LinkCandidate(match_id, confidence, teams_swapped, team_score, time_score, competition_score, in_scope)]`, `reason`
- `store_betclic_aliases(session, event: BookmakerEvent) -> int`
- `LinkConfig` (frozen dataclass; weights 0.7 team / 0.2 time / 0.1 competition, window 12 h, min_margin 0.05)

## Collector
- `collect_once(session: Session, source: OddsSource, mapper: MarketMapper, *, settings: Settings, now: datetime | None = None, player_resolver_factory: Callable[[Session, int], PlayerResolver | None] | None = None, config: CollectorConfig | None = None) -> CollectReport`
  - The caller commits.
  - Exceptions from the source propagate; per-event errors are isolated in savepoints and reported.
  - `player_resolver_factory` defaults to `lineup_player_resolver`.
- `CollectReport` (pydantic) fields: `started_at`, `finished_at`, `events_seen`/`new`/`failed`/`live`/`partial`/`relinked`, `link_status_counts`, `markets_seen`/`new`/`mapped`/`disappeared`, `selections_seen`/`new`/`mapped`/`marked_unavailable`, `snapshots_written`, `unmapped_markets: list[str]`, `errors: list[str]`, `source: dict`, `.ok`
- `remap_event(session: Session, event_row: BookmakerEvent, mapper: MarketMapper, *, player_resolver: PlayerResolver | None = None) -> int`. P10 should call this after a manual CONFIRM or REJECT, or after flipping teams_swapped, from the review queue. It changes no prices.
- `original_market_external_id(external_id: str) -> str` returns Betclic's id of a split market row (`'<id>~<hash>'`).
- `CollectorConfig(store_live_odds=False, max_raw_paths_per_event=20, max_unmapped_in_report=30, link=LinkConfig())`

## Quotes
- `latest_quotes(session: Session, match_id: int, *, max_age_s: float | None = None, now: datetime | None = None) -> list[OddsQuote]`
  - Only AUTO or CONFIRMED links, not live, kick-off in the future, market and selection mapped, selection available, `last_odds_at >= now - max_age_s` (default 600 s).
  - `OddsQuote.market_external_id` is Betclic's original id.
- `latest_quote(session: Session, selection_id: int, *, max_age_s: float | None = None, now: datetime | None = None) -> OddsQuote | None`
- `closing_odds(session: Session, selection_id: int, kickoff: datetime) -> Decimal | None` returns the last *available* snapshot strictly before kick-off.
- `odds_history(session: Session, selection_id: int) -> list[tuple[datetime, Decimal, bool]]` (oldest first)

## Known limitations

- Nothing was verified against live Betclic.pl (geo-blocked from here). The gRPC field numbers (BetclicProtoSchema), CS2 sport code ('counter_strike' in api_sport_codes), market category ids, Polish market labels in config/betclic_markets.yaml, the consent and tab selectors, and the REST application id are assumptions taken from 2025-26 open-source clients. Raw-payload archiving, RawPayloadReplaySource, proto_debug_tree and list_unmapped exist so these can be fitted on the owner's PC.
- Capturing gRPC-web relies on the page using fetch or XHR; WebSocket transport would be missed (ng-state and the HTML are still captured). If gRPC streams cannot be read, an event falls back to whatever the ng-state preloads (the 'Top' category).
- The crawl is sequential: about 40 events with 2.5-6 s pauses plus category fetches takes several minutes. The scheduler (P12) must not overlap runs. Prices are stamped with each event's capture time, so freshness and closing lines stay correct.
- Not mapped on purpose, so they stay unmapped and are never bet: kill handicaps in head-to-head markets, half-time handicaps and totals, first kill / first blood, match-level total rounds (no map number), the CS:GO-era round 16, and bare ambiguous labels ('Zwycięzca', 'Wynik', 'Kto wygra?'). The owner can add rules after inspecting payloads.
- A handicap line given only in the market name (e.g. 'Handicap map (-1,5)' with team or 1/2 selections) is read as the bookmaker team1's handicap, the usual home convention. This is unverified for Betclic.
- Live events are stored and flagged but their prices are not processed (CollectorConfig.store_live_odds=False). An 'unavailable' snapshot carries the last known price.
- Unknown team abbreviations that are neither in the alias table nor similar enough to the team name end up PENDING_REVIEW until confirmed once on the dashboard (the BETCLIC alias is then stored).
- Tests run on SQLite only. The queries are portable (no boolean aggregates), but no PostgreSQL-marked tests were added.

## Foundation change requests

- config.BetclicSettings: add real settings for the knobs that now live in the `source:` section of config/betclic_markets.yaml (cs2cs2bot.odds.source_config.BetclicSourceConfig), at least max_events_per_run, min/max_page_delay_s, cs2_pattern, grpc_base_url, api_protocol, api_sport_codes, api_application, raw_retention_days, user_agent, fetch_categories. A nested `odds` section would also work.
- domain.odds.CollectedEvent: add `captured_at: datetime | None` and `sport_raw: str | None`. I currently carry these through FetchInfo.captured_at_by_event and internal sport hints.
- db.models.BookmakerSelection: add `last_seen_at`. Today `last_odds_at` doubles as 'price confirmed at', refreshed on every sighting even without a price change.
- db.models.BookmakerMarket: add an `extra` JSON column for Betclic market code (mtc), category id and split-row metadata (today a split row's external_id is '<betclic id>~<hash>').
- ARCHITECTURE/P12: `cs2bot odds collect` should build BetclicApiSource when betclic.offering_api_base is set, else BetclicBrowserSource, and call collect_once(..., now=None). `cs2bot odds unmapped` should print list_unmapped() (plus MarketMapper.match_rule for diagnostics). P10's link review queue should call remap_event() after a manual CONFIRM or REJECT or a swap change.
- ARCHITECTURE: the documented `BetclicSource(settings)` exists as an alias of BetclicBrowserSource. MarketMapper.map, link_event, collect_once and the quote functions take the keyword-only arguments listed in public_api, which are a superset of the signatures in docs/ARCHITECTURE.md.

## New dependency requests

- (none)
