# odds: implementation report

> Written by the engineer who implemented the package. Reviewers update it when the public API changes.

**Tests:** `.venv/bin/python -m pytest tests/odds -q` - 374 passed, 2 skipped (the 2 PostgreSQL tests; they pass against PostgreSQL 16 when `CS2BOT_TEST_DATABASE_URL` is set), 0 failed (lint/type clean: True). Numbers as of the independent review-round-1 pass.

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
- **Mapping:** 18 ordered rules in config/betclic_markets.yaml, Polish and English, converted to our team1/team2 orientation, with the handicap rule tested in both swap directions. Team or player selections that carry a line, score or combination stay unmapped rather than being stripped. On top of the rules every mapping passes safety guards (review round 1, see "Mapping" below): settlement wording vs `BetclicSettings`, unsupported/combined markets, three-way variants, explicit scope for kill lines, line bounds.
- **Linking:** scores team names in both orientations (deliberately not token_set_ratio, so "Spirit" does not equal "Spirit Academy"), time proximity and competition. It links AUTO only above min_event_link_confidence (dashboard overrides apply) and with a clear margin over the runner-up. CONFIRMED and REJECTED rows are never changed. BETCLIC aliases are stored for AUTO and CONFIRMED links.
- **Collector:** each event runs in its own savepoint (the event row is locked and re-read, so a manual link review committed meanwhile is never overwritten), markets seen are re-mapped on every pass, every stored market is re-mapped when the link / orientation changed or the API left `extra["remap_required"]`, and a snapshot is written only when odds or availability change. Prices are stamped with the capture time of their own payload (per market when known, else per event), not the run start, because a crawl takes minutes; older observations never overwrite newer ones. Markets listing several lines are split into deterministic rows. Live events are stored and flagged; their prices are not processed.

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
  - Fields: `started_at: datetime|None`, `finished_at: datetime|None`, `pages_visited: int`, `payloads_captured: int`, `raw_paths_by_event: dict[str, list[str]]`, `captured_at_by_event: dict[str, datetime]`, `captured_at_by_market: dict[str, dict[tuple[str, str], datetime]]` (event id -> (market raw_name, external_id) -> capture time of the freshest payload showing that market), `market_meta_by_event: dict[str, dict[tuple[str, str], dict[str, str]]]` (`{"page_name", "group"}` of markets whose raw_name the parser built as `"<group> - <title>"`), `partial_event_ids: set[str]`, `errors: list[str]`, `notes: dict[str, Any]`
  - Sources also fill `CollectedEvent.captured_at` (latest payload with markets) and `CollectedEvent.sport_raw` (the payload's own sport label); the collector prefers them over `captured_at_by_event`.
  - Method: `.summary() -> dict[str, Any]`
  - `partial_event_ids` are events seen only partly (on the listing page only, or - browser source - when one of the page's market categories could not be fetched). The collector does not mark their missing markets unavailable.
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
- `RawPayloadReplaySource(paths: Sequence[str | Path], *, cs2_pattern: str | None = None, keep_unknown_sport: bool = True, keys: PayloadKeys | None = None, schema: BetclicProtoSchema | None = None)` re-parses archived `*.json`/`*.html`/`*.bin` files. All replayed events are partial and keep their ORIGINAL capture time (`index.jsonl`, else the file mtime), never "now".
- `RawPayloadStore(root: Path | None, *, enabled: bool = True, retention_days: int | None = 14)`
  - `RawPayloadStore.from_settings(settings, *, enabled=True, retention_days=14)` uses `data_dir/raw/betclic`.
  - `.save(payload: CapturedPayload) -> Path | None`
  - `.cleanup(now: datetime | None = None) -> int`
- `BetclicSourceConfig` (frozen dataclass of crawl/API knobs)
  - `BetclicSourceConfig.load(settings)` reads the optional `source:` section of `settings.betclic.market_rules_file`.
  - `.from_yaml(path)`, `.from_mapping(dict)` (unknown keys and values of the wrong type are ignored with a warning)
- `cs2bot.odds.source_config.is_other_game(hints: Iterable[str | None], *, cs2_pattern=None, other_pattern=None) -> bool` and `url_section(url) -> str | None`: veto for events whose sport label / competition / URL section names another e-sport (League of Legends, Dota 2, Valorant ...) and not CS2. Used by `select_cs2` (sources) and `link_event`.
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
- `MarketMapper.from_yaml(path: str | Path, *, settlement: SettlementRules | None = None) -> MarketMapper` (relative paths: current directory, then project root); `MarketMapper.from_dict(data, *, source='<dict>', settlement=None)`
- `MarketMapper.with_settlement(*, totals_include_overtime: bool, h2h_tie_rule: str) -> MarketMapper` returns the mapper bound to the pricing engine's settlement assumptions (`self` when unchanged). `collect_once` and `remap_event(..., settings=...)` call it with `settings.betclic` on every pass, so callers normally never do.
- `SettlementRules(totals_include_overtime: bool = True, h2h_tie_rule: 'push'|'lose' = 'push')`, `MarketGuards` (compiled `guards:` section of the YAML; defaults in `cs2bot.odds.market_mapping.DEFAULT_*`).
- `MarketMapper.map(market_raw: str, selection_raw: str, *, event: CollectedEvent, swapped: bool, player_resolver: PlayerResolver | None = None, sibling_selections: Sequence[str] | None = None, draw_possible: bool | None = None) -> MappedSelection | None`
  - Returns `None` for unknown markets or selections; these are stored unmapped and never bet.
  - Handicap: `spec.line` is our team1's handicap. "Team2 -1.5" becomes line +1.5 with selection team2.
  - Head-to-head specs store the two player ids sorted ascending.
  - `sibling_selections` (all selection names of the market) is needed for the three-way guards; `draw_possible` is whether the linked series can end level (`True` only for a BO2, `False` other formats, `None` = unknown). A match-winner market listing a draw is mapped only with `draw_possible=True`.
  - Guards (a mis-mapped market is worse than an unmapped one):
    - names stating that overtime is excluded ("bez dogrywki", "regulaminowy czas", "czas regulaminowy", "nie wliczając dogrywki", "regular time") or included ("z dogrywką") are mapped only when that agrees with `totals_include_overtime` for the round-count family (total rounds, team total rounds, odd/even, round handicap); for every other market type a "regulation only" variant is never mapped;
    - draw-no-bet, double chance and combined markets are never mapped;
    - a two-way market type listing a draw ("Remis", "X") stays unmapped; a head-to-head listing a draw is mapped only with `h2h_tie_rule='lose'`; a match-winner market listing a draw is mapped only when `draw_possible` is `True` (known BO2); an over/under market that also lists "Dokładnie N" stays unmapped;
    - player kill lines and head-to-heads need an explicit scope in the name: a map ("mapa 1"), several maps ("mapy 1-2", "mapa 1+2") or the whole match ("- mecz", "w meczu");
    - a "player" that is exactly one of the event's teams is a team market (unmapped);
    - lines outside `guards.line_bounds` stay unmapped (map handicap |line| <= 2.5, total maps 1.5-4.5, round handicap |line| <= 12.5, total rounds 13-40, team total rounds 0.5-16.5, race to 1-16, player kills 0.5-150);
    - a bare "Handicap" or a "Handicap map" name carrying a map number is never a MAP handicap;
    - a name naming two different maps ("Mapa 1 i Mapa 2 - Zwycięzca", "Mapa 2 - Zwycięzca mapy 3") is never read as one map, and a rule that extracts maps never falls back to the whole match when the name has a numbered map it could not read; a digit right after "Mapa" is never a round or half number ("Mapa 1 runda 10", "Mapa 2 połowa"); a handicap selection carrying two lines stays unmapped.
- `MarketMapper.match_rule(market_raw: str) -> str | None` returns the id of the first rule whose patterns fit the name (diagnostics for `odds unmapped`; `None` when the name fails a guard).
- `MarketMapper.mapping_rule(market_raw, selection_raw, *, event, sibling_selections=None, draw_possible=None) -> str | None` returns the id of the rule that actually maps the selection.
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
  - Never links an event whose sport label (`event.extra["sport_raw"]`), competition or URL section names another e-sport and not CS2 (UNLINKED, reason "another e-sport ...").
  - An AUTO link whose match is `finished` is frozen (returned unchanged) instead of being wiped because the match is no longer a scheduled/live candidate.
- `LinkResult` (pydantic) fields: `event_id`, `match_id`, `status: LinkStatus`, `confidence: float|None`, `teams_swapped`, `changed`, `status_changed`, `candidates: list[LinkCandidate(match_id, confidence, teams_swapped, team_score, time_score, competition_score, in_scope, orientation_margin)]`, `reason`
  - AUTO also requires `orientation_margin` (team score of the chosen orientation minus the swapped one) >= `LinkConfig.min_orientation_margin` (0.10); otherwise PENDING_REVIEW with reason "team order unclear".
- `store_betclic_aliases(session, event: BookmakerEvent) -> int`
- `LinkConfig` (frozen dataclass; weights 0.7 team / 0.2 time / 0.1 competition, window 12 h, min_margin 0.05, min_orientation_margin 0.10)

## Collector
- `collect_once(session: Session, source: OddsSource, mapper: MarketMapper, *, settings: Settings, now: datetime | None = None, player_resolver_factory: Callable[[Session, int], PlayerResolver | None] | None = None, config: CollectorConfig | None = None) -> CollectReport`
  - The caller commits.
  - Exceptions from the source propagate; per-event errors are isolated in savepoints and reported.
  - `player_resolver_factory` defaults to `lineup_player_resolver`.
  - The mapper is re-bound to `settings.betclic.totals_include_overtime` / `h2h_tie_rule` on every pass.
  - Honours `event.extra["remap_required"]` (set by the API's link review): every stored market of a flagged event is re-mapped and the flag cleared - for events seen in the pass and for flagged events that are no longer listed (looked up among CONFIRMED/REJECTED events seen in the last `remap_lookback_days`).
  - Timestamps: `bookmaker_selections.last_seen_at` = last sighting (available or suspended); `last_odds_at` = "price last confirmed at" = last sighting as *available*; both are capture times of the payload. A selection that disappears keeps both and gets an unavailable snapshot.
  - `bookmaker_markets.extra`: `betclic_market_id`, `rule`, `split_signature` (split rows), `page_name` / `group` (parser-built names). `bookmaker_events.extra`: `sport_raw`, `last_collected_at`, `partial`, `raw_payloads`, `raw_payloads_at`.
- `CollectReport` (pydantic) fields: `started_at`, `finished_at`, `events_seen`/`new`/`failed`/`live`/`partial`/`relinked`/`remapped`, `link_status_counts`, `markets_seen`/`new`/`mapped`/`disappeared`, `selections_seen`/`new`/`mapped`/`marked_unavailable`/`stale_ignored`/`invalid`, `snapshots_written`, `unmapped_markets: list[str]`, `errors: list[str]`, `source: dict`, `.ok`
- `remap_event(session: Session, event_row: BookmakerEvent, mapper: MarketMapper, *, player_resolver: PlayerResolver | None = None, settings: Settings | None = None) -> int`. The API's `event_remapper` should be `lambda s, ev: remap_event(s, ev, mapper, settings=settings)`. It changes no prices and clears `extra["remap_required"]`.
- `series_draw_possible(session, match_id) -> bool | None` (`True` for a BO2, `False` for BO1/BO3/BO5, `None` unlinked / format unknown).
- `original_market_external_id(external_id: str) -> str` returns Betclic's id of a split market row (`'<id>~<hash>'`).
- `CollectorConfig(store_live_odds=False, max_raw_paths_per_event=20, max_unmapped_in_report=30, link=LinkConfig(), remap_lookback_days=14)`

## Quotes
- `latest_quotes(session: Session, match_id: int, *, max_age_s: float | None = None, now: datetime | None = None) -> list[OddsQuote]`
  - Only AUTO or CONFIRMED links, not live, kick-off in the future, market and selection mapped, selection available, `last_odds_at >= now - max_age_s` (default 600 s). `last_odds_at` is the capture time of the last payload that showed the selection available (`OddsQuote.captured_at`).
  - `OddsQuote.market_external_id` is Betclic's original id.
- `latest_quote(session: Session, selection_id: int, *, max_age_s: float | None = None, now: datetime | None = None) -> OddsQuote | None`
- `closing_odds(session: Session, selection_id: int, kickoff: datetime) -> Decimal | None` returns the last *available* snapshot strictly before kick-off.
- `odds_history(session: Session, selection_id: int) -> list[tuple[datetime, Decimal, bool]]` (oldest first)

## Known limitations

- Nothing was verified against live Betclic.pl (geo-blocked from here). The gRPC field numbers (BetclicProtoSchema), CS2 sport code ('counter_strike' in api_sport_codes), market category ids, Polish market labels in config/betclic_markets.yaml, the consent and tab selectors, and the REST application id are assumptions taken from 2025-26 open-source clients. Raw-payload archiving, RawPayloadReplaySource, proto_debug_tree and list_unmapped exist so these can be fitted on the owner's PC.
- Capturing gRPC-web relies on the page using fetch or XHR; WebSocket transport would be missed (ng-state and the HTML are still captured). If gRPC streams cannot be read, an event falls back to whatever the ng-state preloads (the 'Top' category).
- The crawl is sequential: about 40 events with 2.5-6 s pauses plus category fetches takes several minutes. The scheduler (P12) must not overlap runs. Prices are stamped with each event's capture time, so freshness and closing lines stay correct.
- Not mapped on purpose, so they stay unmapped and are never bet: kill handicaps in head-to-head markets, half-time handicaps and totals, first kill / first blood, match-level total rounds (no map number), the CS:GO-era round 16, bare ambiguous labels ('Zwycięzca', 'Wynik', 'Kto wygra?', 'Handicap'), and player kill lines / head-to-heads whose name states no scope ('Zabójstwa gracza', 'Liczba zabójstw - s1mple' without a map or 'mecz'). If Betclic turns out to name its whole-match kill markets that way, remove `scope` from the `requires` of the `player_kills` / `player_kills_h2h` rules in config/betclic_markets.yaml after checking the raw payloads. The owner can add rules after inspecting payloads.
- Markets whose name the parser builds from a map group ('Mapa 1' + 'Liczba rund' -> 'Mapa 1 - Liczba rund') carry a raw_name that is not shown on the page. The executor looks markets up by exact on-page text, so such quotes can be priced but placement fails safely ("market not found") until the executor uses `bookmaker_markets.extra["page_name"]` / `["group"]` (see foundation change requests).
- A handicap line given only in the market name (e.g. 'Handicap map (-1,5)' with team or 1/2 selections) is read as the bookmaker team1's handicap, the usual home convention. This is unverified for Betclic.
- Live events are stored and flagged but their prices are not processed (CollectorConfig.store_live_odds=False). An 'unavailable' snapshot carries the last known price.
- Unknown team abbreviations that are neither in the alias table nor similar enough to the team name end up PENDING_REVIEW until confirmed once on the dashboard (the BETCLIC alias is then stored).
- The default test run uses SQLite. tests/odds/test_odds_postgres.py (marker `postgres`, needs `CS2BOT_TEST_DATABASE_URL`) runs the collector, linking, re-mapping, quotes and the concurrent-review protection on PostgreSQL; it passed on PostgreSQL 16 during review round 1.

## Foundation change requests

- config.BetclicSettings: add real settings for the knobs that now live in the `source:` section of config/betclic_markets.yaml (cs2bot.odds.source_config.BetclicSourceConfig), at least max_events_per_run, min/max_page_delay_s, cs2_pattern, grpc_base_url, api_protocol, api_sport_codes, api_application, raw_retention_days, user_agent, fetch_categories. A nested `odds` section would also work.
- (done by the foundation, now used) `CollectedEvent.captured_at` / `sport_raw`, `BookmakerSelection.last_seen_at`, `BookmakerMarket.extra`.
- domain.odds.OddsQuote + executor: add `market_page_name: str | None` and `market_group: str | None` (filled from `bookmaker_markets.extra["page_name"]` / `["group"]`) and let the executor open the group/tab and match the market by `market_page_name` when it is set. Today `OddsQuote.market_raw_name` of a parser-prefixed market ('Mapa 1 - Liczba rund') is not the text on the page.
- domain.odds.CollectedMarket: add `page_name` / `group` (and optionally `captured_at`) so sources do not have to pass them through `FetchInfo.market_meta_by_event` / `captured_at_by_market`.
- ARCHITECTURE/P12: `cs2bot odds collect` should build BetclicApiSource when betclic.offering_api_base is set, else BetclicBrowserSource, and call collect_once(..., now=None). `cs2bot odds unmapped` should print list_unmapped() (plus `bookmaker_markets.extra["rule"]` / MarketMapper.match_rule for diagnostics). The API's `event_remapper` should be `lambda s, ev: cs2bot.odds.remap_event(s, ev, mapper, settings=settings)`. Collection passes must not overlap (one job at a time).
- ARCHITECTURE: the documented `BetclicSource(settings)` exists as an alias of BetclicBrowserSource. MarketMapper.map, link_event, collect_once and the quote functions take the keyword-only arguments listed in public_api, which are a superset of the signatures in docs/ARCHITECTURE.md.
- engine (information, no change needed if agreed): a 2-way half-winner market (no draw offered) is mapped onto HALF_WINNER, where a 6-6 half makes the team selections lose; if Betclic refunds such bets the EV is under-stated (conservative).

## New dependency requests

- (none)

## Review round 1

State found: the package had been partly reworked by an earlier, interrupted review pass (guards in the mapper, per-market capture times, `remap_required`, settlement binding) without tests; 2 tests failed, ruff reported 2 errors and 3 files were unformatted. Everything that existed was read, verified, kept where correct, completed and covered by tests. Now: 352 passed + 2 PostgreSQL tests (pass on PostgreSQL 16), ruff check / format clean, mypy clean, tests/foundation green, the API tests that drive `odds.remap_event` green.

### Verified (re-derived, not trusted)

- Handicap normalisation in all four slot / swap combinations (`spec.line` = our team1's handicap), score flipping, team-specific markets and "1" / "2" selections under `teams_swapped`.
- Mapper output against the pricing and settlement engine's conventions for every market type (scope of player markets, `draw` selections, pistol rounds 1 / 13, race-to line, head-to-head ids sorted, top-fragger team key).
- Snapshot-on-change, idempotency of a second identical pass, unavailable / available-again snapshots, closing line = last available snapshot strictly before kick-off, quote freshness on `last_odds_at`.
- Link scoring (weights, AUTO threshold with dashboard override, runner-up margin, academy teams, missing start time never AUTO, manual statuses preserved, alias storage with swap).
- Robustness: 9 000 mutated gRPC-web bodies and assorted malformed / cyclic / very deep JSON payloads never raise; per-event savepoints (also on PostgreSQL); raw-payload paths cannot escape the archive; proxy credentials and request headers are never logged or archived.
- Addendum: `CollectedEvent.captured_at` / `sport_raw`, `BookmakerSelection.last_seen_at`, `BookmakerMarket.extra`, the settlement-rule settings and `extra["remap_required"]` are all used and tested.

### Issues found

| # | Severity | Issue | Fixed |
|---|---|---|---|
| 1 | critical | "Handicap map 1" (a ROUND handicap on map 1) was mapped as a MAP handicap: "team +2.5 maps" looks like a certain winner to the model. Names with a map number are now never a map handicap (same for total maps and both-teams-win-a-map). | yes |
| 2 | critical | Other e-sports could be linked to a CS2 match: with no CS2-looking links the whole e-sport section is crawled, and a League of Legends "G2 - Fnatic" starting within a few hours of a CS2 "G2 - Fnatic" reached AUTO (identical team names give 0.70 of the 0.90 needed; time and an unknown competition supply the rest). Events whose sport label / competition / URL section names another game are now dropped by the sources and never linked. | yes |
| 3 | critical | Settlement wording (Addendum) was implemented without tests and missed common Polish forms ("czas regulaminowy", "nie wliczając dogrywki"); "Liczba rund w dogrywce" could fall into total rounds. Patterns extended (YAML + code defaults, kept in sync by a test), behaviour tested for both values of `totals_include_overtime` and `h2h_tie_rule`. | yes |
| 4 | major | A match-winner market listing a draw ("1 X 2") was mapped for any series format; in a BO1/BO3/BO5 that is a regulation-time market where the teams lose on overtime. Now mapped only when the linked match is a BO2 (`draw_possible`); over/under markets that also list "Dokładnie N" are unmapped as well. | yes |
| 5 | major | "Obie drużyny wygrają rundę pistoletową - mapa 1" was mapped as both-teams-win-a-map; the rule now needs "map" in the name and excludes rounds / pistol / halves / map numbers. | yes |
| 6 | major | Team kill markets ("Liczba zabójstw - Natus Vincere") were sent to the player resolver, which could fuzzy-match a nickname. A "player" that is exactly one of the event's teams is now unmapped. | yes |
| 7 | major | Lost update: a link review committed by the dashboard while a collection pass was storing the same event was overwritten by the collector's stale copy (manual CONFIRM / REJECT silently undone). The event row is now locked and re-read (`FOR UPDATE` + `populate_existing`); proven by a PostgreSQL test that fails without the fix. | yes |
| 8 | major | An AUTO link was wiped (UNLINKED, match_id NULL, all markets re-keyed) as soon as our match became `finished` while Betclic still listed the event, losing the link needed for closing-line / settlement analysis. AUTO links to finished matches are now frozen. | yes |
| 9 | major | Selections a pass did not see kept their old `selection_key` when the market's spec changed in that pass (edited rules, format now known), so a fresh-enough stale selection could be quoted under the wrong spec. They are now unmapped. | yes |
| 10 | major | The two existing tests for head-to-head markets contradicted the new guards (scope requirement, three-way with `h2h_tie_rule=push`) and failed; none of the interrupted pass's features had tests. 98 test cases added (guards, settlement binding, remap flag for listed and unlisted events, relink of unseen markets, per-market capture times, stale observations, `last_seen_at` vs `last_odds_at`, market extra, sources, linking, browser, PostgreSQL). | yes |
| 11 | major | Browser source: an event whose category requests failed was still reported as complete, so every market of the missing categories was marked unavailable and came back on the next pass (flapping snapshots). Such events are now partial. | yes |
| 12 | minor | `bookmaker_markets.extra["rule"]` named the first rule fitting the name, not the rule that mapped the market ("Liczba rund - mapa 1" -> `team_total_rounds`). New `MarketMapper.mapping_rule`. | yes |
| 13 | minor | Polish display dates ("05.11.2026 18:00") were read month-first and as UTC; now day-first, local Warsaw time. ISO timestamps unchanged. | yes |
| 14 | minor | Selections with odds outside (1, 1000] or without a name failed the whole event (numeric overflow on PostgreSQL); they are now dropped and counted (`selections_invalid`). | yes |
| 15 | minor | `categoryIds` given as a string was iterated character by character; source options of the wrong type in the YAML crashed a crawl half-way; duplicate events lost the later `captured_at`; `last_seen_at` of an event could move backwards on a replay. | yes |
| 16 | minor | Parser-built market names ("Mapa 1 - Liczba rund") are not the text on the page, which the executor needs. The page title and group are now stored in `bookmaker_markets.extra`; using them needs a change outside this package. | partly |

### Remaining concerns

- Nothing is verified against live Betclic.pl (field numbers, sport code, labels, selectors). The guards make wrong mappings unlikely, at the price of leaving unverified names unmapped; expect to tune config/betclic_markets.yaml on the owner's PC with `cs2bot odds unmapped`.
- Kill lines and head-to-heads without an explicit scope in their name are unmapped on purpose (see Known limitations).
- Parser-prefixed market names cannot be placed by the executor until it uses `extra["page_name"]` / `["group"]` (placement fails safely).
- The other-game veto is a list of known game / league names; an unknown game with no label at all in the payload, played by same-named teams at the same time, would still rely on the competition score alone.
- A market name with a line only in the market name ("Handicap map (-1,5)" with "1" / "2" selections) is read as the first team's handicap - unverified for Betclic.
- Collection passes must not overlap; the row lock protects manual reviews, not two collectors racing each other.

## Review round 1 - independent pass

A second, independent reviewer re-checked the package after the first round-1 pass (which is recorded above and was committed). State found: 352 tests passing, ruff / mypy clean. Everything below was re-derived from the code and probed with adversarial inputs (about 130 hand-written market/selection names in both orientations, see the new tests).

### Verified

- Handicap orientation (selection line, market-name line, `1`/`2` selections, both swap directions) against `cs2bot.domain.markets` and the pricing engine's `_handicap` convention (`margin_team1 + line`); score flipping; team-specific markets under swap.
- Settlement wording vs `BetclicSettings` (Addendum), `remap_required` handling (listed and unlisted events) and the API side that sets it (`api/routes/review.py`).
- Collector: snapshot only on change, `last_seen_at` vs `last_odds_at`, stale observations ignored, unavailable snapshots, partial events, per-event savepoints; quotes: freshness on `last_odds_at`, live / kick-off / link-status filters; closing line = last available snapshot strictly before kick-off.
- Linking scores, manual statuses preserved, other-e-sport veto, frozen AUTO links to finished matches.
- Secrets: proxy credentials only via `get_secret_value()` into the launch / client options, never logged; raw payload file names are sanitised.

### Issues found

| # | Severity | Issue | Fixed |
|---|---|---|---|
| 1 | critical | A name naming two maps was read as the first one: `"Mapa 1 i Mapa 2 - Zwycięzca"` / `"Mapa 1 / Mapa 3 - Zwycięzca"` -> map 1 winner, `"Mapa 2 - Zwycięzca mapy 3"` -> map 2, and `"Liczba zabójstw - s1mple - mapa 1 i mapa 2"` (a two-map kills line of 38.5) -> a single-map line, which the model prices as an almost certain "under". `extract_map_number` now returns `None` when more than one distinct map number appears, and a rule that extracts maps fails (instead of falling back to the whole match) when the name has a numbered map it could not read. | yes |
| 2 | major | The map number was read as the round: `"Mapa 1 runda 10"` -> pistol round 1 winner (a round-10 market), `"Mapa 2 runda pistoletowa"` -> round 13 (the "2" is the map). The pistol rule now excludes a map number directly followed by "runda" (config/betclic_markets.yaml). | yes |
| 3 | major | The map number was read as the half: `"Mapa 2 połowa 1"` -> half 2, `"Mapa 2 połowa"` -> half 2. `extract_half` removes map references first. | yes |
| 4 | major | A match-winner market listing a draw ("1 X 2") was mapped when the series format was unknown (`draw_possible=None`). In a BO1/BO3 that is a regulation-time market (team selections lose on overtime) and the engine falls back to inferred formats elsewhere, so the team side would be over-priced. Now mapped only for a known BO2 (`draw_possible=True`). | yes |
| 5 | major | `teams_swapped` was chosen by `swapped > straight` with no margin: with a lowered dashboard threshold an event whose names fit both orientations almost equally (near-identical team names, truncated Betclic spellings) could be linked AUTO with a guessed team order, putting every bet on the other team. AUTO now also needs `orientation_margin >= 0.10` (`LinkCandidate.orientation_margin`, `LinkConfig.min_orientation_margin`). | yes |
| 6 | minor | Browser source: the page-capture binding accepted bodies of any size from any script on the page (the limit was only enforced in the injected page script), so a third-party script could fill memory / the raw archive. The size limit is now enforced in Python too; non-string bodies are ignored. | yes |
| 7 | minor | A handicap selection carrying two lines (`"NAVI (-1,5) (1,5)"`) used the first one; now unmapped. | yes |

Tests added: 22 (ambiguous map / round / half names, two-line handicap selections, team names with digits, 1X2 with unknown format, orientation margin, capture-binding size limit). 374 passed, 2 skipped (PostgreSQL); ruff / format / mypy clean; tests/foundation and the API tests that drive `odds.remap_event` pass.

### Remaining concerns

- Unchanged from the first pass: nothing is verified against live Betclic.pl; tune config/betclic_markets.yaml with `cs2bot odds unmapped` on the owner's PC.
- Top-fragger markets without a scope in their name are priced as whole-match markets (no `scope` requirement, unlike kill lines). Parser-built names carry the map group ("Mapa 1 - Najwięcej zabójstw"), so this only matters if Betclic shows an un-prefixed map-level top-fragger market; add `scope` to the rule's `requires` if the raw payloads show that.
- Whole-match player kill lines ("... - mecz") are priced on the engine's "match" scope; whether Betclic voids them when not all maps are played (or counts only maps played) is unverified.
- Rules fall through to later rules when a selection cannot be parsed. The guards above close the known paths, but a new YAML rule should be tested with `MarketMapper.mapping_rule` against real names before it is relied on.
