# hltv: implementation report

> Written by the engineer who implemented the package. Reviewers update it when the public API changes.

**Tests:** `.venv/bin/python -m pytest tests/ingest/hltv -q` - 154 passed, 0 failed (lint/type clean: True; after review round 1)

## Summary

P2 (cs2bot.ingest.hltv) is built and passes every definition-of-done check.

- **Research:** hltv.org is blocked from this machine, so the page structure comes from gigobyte/HLTV, w1tte/hltv-scraper's 2026-02 notes on match, mapstats and results pages, 2026 /matches test data from RATING3PRO/HLTV-API and woq/hltv-bot, real saved news pages (f4bio/hltvapi.rs), and ranking parsers. Every selector, regex and Cloudflare marker lives in `selectors.py`.
- **Fetcher:** `HLTVFetcher` caches pages on disk, gzip-compressed with JSON metadata, plus a pruning method. Finished matches, mapstats, news articles, past rankings and past news-archive months use the long TTL. Only real network fetches go through the rate limiter (`utils.http.RateLimiter` with the injected clock/sleep/rng), never cache hits. It has a browser mode (Playwright, one reused browser/context, executable_path and proxy taken from settings, images/fonts not loaded) and an http mode (httpx). In browser mode it waits up to 20 s for a challenge to clear by itself. If the challenge persists it raises `HLTVBlockedError` and starts an exponential cool-down, saved on disk so it survives a restart. There is no challenge solving or stealth tooling. Challenge detection does not trigger on the Cloudflare bot script that normal pages carry, or on article text.
- **Parsers:** pure functions (HTML in, pydantic models out). They handle BO1/BO3/BO5, overtime (both page layouts), full and partial forfeits, postponed and deleted matches, TBA maps and stand-ins. A section that fails to parse becomes a warning, not a failure. Mapstats parsing covers scoreboards including per-side tables (the hidden eco-adjusted columns are ignored), side splits including overtime, and round history.
- **Manual check:** `open_browser_for_manual_check` opens the fetcher's persistent profile in a visible window so the owner can pass a challenge by hand (`cs2bot ingest hltv-browser`); nothing is clicked or solved.
- **Sync:** every function is idempotent and runs inside savepoints. It links to PandaScore matches through `find_match` and keeps their orientation via `is_swapped`. Old backfilled matches do not move a player's current team, and stand-ins never do. If HLTV blocks mid-sync, it stops and returns a partial report with `blocked=True`, so the caller can still commit what was done.
- **Fixtures:** 21 HTML fixtures under `tests/ingest/hltv/fixtures/`, generated so that round histories agree with the half scores and final scores.

## Public API

Everything below is re-exported from `cs2bot.ingest.hltv`. HLTV ids are ints. All datetimes are UTC-aware.

## Fetcher
```python
HLTVFetcher(settings: HLTVSettings, *, page_loader: PageLoader | Callable[[str], LoadedPage | str] | None = None,
            rate_limiter: SupportsWait | None = None, sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic, now: Callable[[], datetime] = utcnow,
            options: FetchOptions | None = None,   # None -> FetchOptions.from_settings(settings)
            rng: random.Random | None = None)
  .get(path_or_url: str, *, max_age_minutes: int | None = None) -> str   # max_age 0 = force refresh
  .url_for(path_or_url: str) -> str          # = hltv_url(base_url, ...); ValueError for non-HLTV hosts
  .ttl_minutes(url: str, cached_html: str | None = None) -> int
  .cached(path_or_url: str) -> CachedPage | None   # .invalidate(path_or_url) -> None
  .prune_cache(older_than: timedelta | None = None) -> int   # maintenance job; default = long TTL
  .blocked_until -> datetime | None ; .stats: FetchStats ; .base_url: str
  .close() ; context manager (closes only a loader it created itself)
```
- The loader is chosen by `settings.fetch_mode` and created lazily: `BrowserPageLoader(settings, options)` or `HttpPageLoader(settings)`.
- Errors it raises:
  - `HLTVBlockedError(reason, retry_after_s)` when a challenge does not clear, on HTTP 403/429, or while a cool-down is active (`reason="cooldown"`).
  - `HLTVNotFoundError` on 404/410.
  - `HLTVFetchError` on other failures. Connection errors are retried once.
  - All three subclass `HLTVError`. `HLTVParseError` (also `ValueError`) is raised by the parsers.
- **Settings are the source of truth**: `FetchOptions.from_settings(settings, **overrides)` takes `challenge_wait_s`, `cooldown_base_s` (doubles per repeat block), `cooldown_max_s`, `block_resources` and `browser_profile_dir` (default `data/browser/hltv`, a persistent Chromium profile) from `HLTVSettings`; the remaining knobs are `challenge_poll_s=1.0, transport_retries=1, transport_retry_delay_s=10.0, locale='en-US'`. A directly constructed `FetchOptions()` (tests) has the same defaults but `browser_profile_dir=None`.
- `detect_challenge(html: str) -> str | None`; `hltv_url(base_url, path_or_url) -> str` (same-site absolute URLs incl. `hltv.org` vs `www.hltv.org` are rewritten onto `base_url`, other hosts raise `ValueError`).
- `browser_launch_kwargs(settings, *, headless)` / `browser_context_kwargs(settings, *, locale)` (shared by the fetcher and the manual check).

## Manual challenge pass (`cs2bot ingest hltv-browser`)
```python
open_browser_for_manual_check(settings: HLTVSettings | Settings, *, url: str | None = None,
                              wait_s: float = 600.0, poll_s: float = 1.0, clear_cooldown: bool = True,
                              start_playwright: Callable[[], Any] | None = None,
                              clock: Callable[[], float] = time.monotonic) -> ManualCheckResult
ManualCheckResult(url, profile_dir, cleared, window_closed, waited_s, cooldown_cleared, last_challenge)
```
- Opens `settings.browser_profile_dir` with `launch_persistent_context(headless=False)` (executable path and proxy from settings), navigates once to `url` (default: the front page; non-HLTV URLs raise `ValueError`) and then only watches the page until the owner closes the window or `wait_s` runs out. No clicking, no solving.
- `cleared` = the last page seen was not a challenge; then the fetcher's persisted cool-down (`<cache_dir>/_cooldown.json`) is removed so the bot resumes at once.
- Raises `ValueError` when `browser_profile_dir` is None. The bot must be stopped first (one profile = one browser). Cloudflare binds clearance to the user agent: set `CS2BOT_HLTV__USER_AGENT` (or run headful) so the headless bot can reuse the cookie.
- Playwright's sync API is not thread-safe: use one fetcher per thread, never inside a running asyncio loop (FastAPI/scheduler should call it from a worker thread).

## Parsers (pure functions)
```python
parse_results_page(html: str, *, base_url: str = HLTV_BASE_URL) -> list[ResultRow]
parse_results_pagination(html: str, *, base_url: str = HLTV_BASE_URL) -> Pagination | None
parse_matches_page(html: str, *, base_url: str = HLTV_BASE_URL) -> list[UpcomingRow]
parse_match_page(html: str, url: str, *, base_url: str = HLTV_BASE_URL) -> MatchPage
parse_mapstats_page(html: str, url: str) -> MapStatsPage
parse_ranking_page(html: str, *, url: str | None = None, ranking_date: date | None = None) -> list[RankingRow]
parse_news_list(html: str, *, base_url: str = HLTV_BASE_URL, now: datetime | None = None) -> list[NewsRow]
parse_news_article(html: str, url: str) -> NewsArticle
```
- Orientation inside the models is HLTV's page orientation (team1 = left).
- **`ResultRow`**: `hltv_match_id`, `url`, `team1`/`team2: TeamRef(name, hltv_id)`, `team1_score`/`team2_score`, `winner: TeamSlot`, `event`, `stars`, `played_at`, `format`, `map_name` (BO1 only), `is_forfeit`, `format_hint`.
- **`UpcomingRow`**: `hltv_match_id`, `url`, `team1`/`team2` (None if TBD), `scheduled_at`, `format`, `event: EventRef(name, hltv_id)`, `stars`, `is_live`, `is_lan`, `title`, `map_names`, `has_teams`.
- **`MatchPage`**:
  - Header: `team1`/`team2`, `team1_rank`/`team2_rank`, `rank_date`, `scheduled_at`, `status` (`HLTVMatchStatus`: scheduled/live/finished/postponed/deleted; `.to_match_status()`; deleted maps to CANCELED), `status_text`, `best_of`, `format`, `is_lan`, `event`, `stage`, `notes`.
  - Result: `team1_score`/`team2_score`, `winner`, `is_forfeit`, `forfeit_note`, `warnings`.
  - `maps: list[MapResult]`: `map_number`, `map_name`, `raw_name`, `state` (played/live/unplayed/forfeit), rounds, `winner`, `halves: list[HalfScore]`, `team1_start_side`, `mapstats_id`, `mapstats_url`, `picked_by`, `is_decider`, `overtime_rounds`, `regulation_side_rounds()`.
  - `vetoes: list[VetoEntry(step, action, team: TeamSlot | None, team_name, map_name, raw)]`.
  - Lineups: `team1_lineup`/`team2_lineup: list[LineupPlayerRow(nickname, hltv_id, is_standin, full_name, real_name, country)]`.
  - `demos: list[DemoLink(hltv_demo_id, url, map_name)]`, `demo_link`, `played_maps`.
- **`MapStatsPage`**:
  - `mapstats_id`, `hltv_match_id`, `map_name`, `played_at`, `event`, `rating_version`, `warnings`.
  - `team1`/`team2: TeamMapStats`: `team`, `rounds`, `won`, `start_side`, `ct_rounds`, `t_rounds`, `overtime_rounds`, `team_rating`, `first_kills`, `clutches_won`, and `players` / `ct_players` / `t_players`.
  - Each `PlayerStatLine`: `nickname`, `hltv_id`, `country`, `kills`, `headshots`, `assists`, `flash_assists`, `deaths`, `traded_deaths`, `kast` (0..100), `adr`, `rating`, `first_kills`, `first_deaths`, `kd_diff`, `fk_diff`, `multi_kill_rounds`, `clutches_won`, `round_swing`.
  - `rounds: list[RoundOutcome(round_number, winner, winner_side, team1_side, end_reason, outcome, team1_score, team2_score, is_overtime)]`.
  - Flags: `rounds_complete`, `side_split_complete`.
- **`RankingRow`**: `rank`, `team_name`, `hltv_team_id`, `points`, `ranking_date`, `change`, `is_new`, `player_hltv_ids`.
- **`NewsRow`**: `url`, `hltv_news_id`, `title`, `published_at`, `comments`, `country`.
- **`NewsArticle`**: `url`, `hltv_news_id`, `title`, `published_at`, `author`, `lead`, `body`, `team_hltv_ids`, `player_hltv_ids`.

## Sync (session in, flushes, never commits unless `commit_every` is set)
```python
sync_match(session, fetcher: PageSource, hltv_match_id: int, *, settings: Settings, with_mapstats: bool = True,
           max_age_minutes: int | None = None, options: HLTVSyncOptions | None = None, url_hint: str | None = None,
           hints: Mapping[str, Any] | None = None, expect_finished: bool = False) -> SyncReport
sync_mapstats(session, fetcher, mapstats_id: int, *, settings: Settings | None = None, match: Match | None = None,
              map_number: int | None = None, options: HLTVSyncOptions | None = None) -> SyncReport
sync_results(session, fetcher, *, settings: Settings, since: datetime, max_pages: int = 50, in_scope_only: bool = True,
             stop_when_nothing_new: bool = True, query: Mapping[str, str | int] | None = None,
             options: HLTVSyncOptions | None = None, force: bool = False) -> SyncReport
sync_upcoming(session, fetcher, *, settings: Settings, options: HLTVSyncOptions | None = None,
              now: datetime | None = None, include_live: bool = False) -> SyncReport
sync_rankings(session, fetcher, ranking_date: date | None = None, *, options: HLTVSyncOptions | None = None,
              settings: Settings | None = None) -> SyncReport   # settings -> ingest.scope.refresh_upcoming_scope
sync_news(session, fetcher, *, months: int = 1, now: datetime | None = None, options: HLTVSyncOptions | None = None,
          refresh: bool = False) -> SyncReport
hltv_round_records(session, map_played: MapPlayed) -> list[RoundRecord]   # fallback round data without demos
```
- **`PageSource`** is a Protocol requiring only `get(path_or_url, *, max_age_minutes=None) -> str`. `HLTVFetcher` satisfies it.
- **`SyncReport`** (dataclass): `job`, `created`, `updated`, `skipped`, `failed`, `fetched_pages`, `blocked`, `errors`, `warnings`, `match_ids`, `details`; plus `.ok`, `.items`, `.merge()`, `.as_dict()` (for `JobTracker.details`). Sync functions never raise for one bad page or row. If `report.blocked` is set, stop and commit the partial work.
- **`HLTVSyncOptions`** defaults: `listing_ttl_minutes=20, upcoming_match_ttl_minutes=120, near_start_window_minutes=120, near_start_ttl_minutes=10, match_link_window_hours=12.0, write_round_rows=True, max_news_articles=100, min_ranking_rows_for_cleanup=30, commit_every=None`.
- **What `sync_match` writes:**
  - Match, matched by `hltv_id`, else `find_match` within 12 h (keeping a PandaScore match's orientation and schedule).
  - Tournament (`hltv_event_id`, `is_lan`).
  - MapPlayed for played maps only: rounds, winner, start side, CT/T splits (including overtime, from mapstats), `overtime_rounds`, `picked_by_team_id`, `is_decider`, `hltv_mapstats_id`, `played_at`.
  - Veto rows, replaced once HLTV publishes a veto.
  - MatchLineup (source HLTV, `is_standin`; stale HLTV rows removed, but only for a team whose lineup the page shows).
  - Rounds (`source=DataSource.HLTV`) from a complete map stats round history; upserted by round number, never written when the map already has rows from another source (DEMO).
  - PlayerMapStats (source HLTV, `ct_kills`/`ct_deaths`/`t_kills`/`t_deaths`).
  - Demo rows (PENDING, one per `/download/demo/{id}`).
  - TeamRanking and `Team.hltv_rank` from the dated rank link on the match page.
  - `Match.in_scope` is OR-ed with the scope rule. It uses `cs2bot.ingest.scope.is_in_scope` when that module exists, else the fallback: tier S/A/B or a team in the top N.
- **Relevance** for results/upcoming: already in scope, or `stars >= 1`, or a top-N team *at match time* (`ingest.scope.team_rank_as_of`: current rank for recent matches, ranking history for older ones), or an in-scope match between the same teams nearby. Run `sync_rankings` first; a backfill needs dated rankings (match pages record them) for old unstarred matches.
- **`sync_upcoming`** skips live and TBD matches and anything beyond `settings.scope.max_hours_ahead`. Matches starting within 120 min are re-fetched with a 10-minute TTL.
- **`sync_results`** stops at rows older than `since`, after `max_pages`, or at the first page holding only already-complete matches. Use `stop_when_nothing_new=False` (and a large `max_pages`) for the backfill CLI. `query` lets the CLI filter, e.g. `{"startDate": ..., "endDate": ...}`.
- **`sync_news`** upserts `NewsItem(source="hltv")` by URL. `team_ids`/`player_ids` come from article links plus conservative name matching (`MentionIndex`).
- **`Match.extra['hltv']`**: `status`, `status_text`, `best_of`, `event_id`, `event_name`, `scheduled_at`, `team1_rank`/`team2_rank` (our orientation), `rank_date`, `forfeit`, `forfeit_note`, `forfeit_winner_team_id`, `forfeited_maps`, `notes`, `has_veto`, `has_lineups`, `stars`, `complete`, `synced_at`.
- **`MapPlayed.extra['hltv']`**: `raw_name`, `halves` (`[{team1, team2, team1_side, overtime}]`), `mapstats_url`, `mapstats_id`, `rating_version`, `rounds_complete`, `team1_rating`/`team2_rating`, `team1_first_kills`/`team2_first_kills`, `team1_clutches`/`team2_clutches`, and `rounds` (`[{round, winner: 'team1'|'team2', winner_side, team1_side, end_reason, score: [t1, t2]}]`, our orientation).
- **`PlayerMapStats.extra`**: `hltv_player_id`, `rating_version`, `round_swing`, `kd_diff`, `fk_diff`, `ct`/`t` per-side dicts.
- **`Player.extra['hltv_team_seen_at']`**.

## URL helpers
`match_path(hltv_match_id, slug='-')`, `mapstats_path(mapstats_id, slug='-')`, `matches_path()`, `results_path(offset=0, query=None)`, `ranking_path(ranking_date=None)` (snaps to that week's Monday), `news_archive_path(year, month)`, `HLTV_BASE_URL`.

## CLI wiring for P12
- `ingest upcoming` → `sync_upcoming`
- `ingest results` → `sync_results(since=now - 2 days)`
- `ingest backfill --since` → `sync_results(since=..., max_pages=10_000, stop_when_nothing_new=False, options=HLTVSyncOptions(commit_every=20))`
- `ingest rankings` → `sync_rankings(..., settings=settings)`
- `ingest news` → `sync_news`
- `ingest match <hltv_id>` → `sync_match`
- `ingest hltv-browser` → `open_browser_for_manual_check(settings)` (bot stopped)
- Open the fetcher once per job: `with HLTVFetcher(settings.hltv) as f:`.

## Known limitations

- hltv.org cannot be reached from the build machine. The selectors and the 21 HTML fixtures are replicas built from open-source scrapers and 2026 page notes, not pages captured from HLTV. The first live run on the owner's PC should be smoke-tested; all selectors live in selectors.py.
- Headless Chromium sends a 'HeadlessChrome' user agent, which Cloudflare is more likely to challenge. On the owner's PC, set CS2BOT_HLTV__HEADLESS=false or CS2BOT_HLTV__USER_AGENT to the real browser UA. Clearance cookies persist in the browser profile (`HLTVSettings.browser_profile_dir`, default `data/browser/hltv`). By design the fetcher never solves challenges: it waits up to 20 s, then raises and cools down for 15 min doubling up to 12 h (persisted in <cache_dir>/_cooldown.json; all three values are HLTVSettings fields).
- Stand-in markup on match pages is not documented by any reference scraper. Detection is a conservative heuristic that only fires on explicit 'stand-in' class, title or text tokens.
- Linking a per-map demo download to its map is a best guess from the link text. DemoLink.map_name is parsed but not persisted, because the demos table has no map column.
- HLTV round history is stored in maps_played.extra['hltv']['rounds'] (exposed via hltv_round_records()) and, when complete, in the rounds table with source=HLTV. Demo rounds are never overwritten; the demo pipeline deletes a map's rounds before writing DEMO rows.
- The results page shows no team ids, so relevance uses DB aliases and ranks plus HLTV stars. Run sync_rankings before sync_results and sync_upcoming. A resumable backfill needs stop_when_nothing_new=False.
- 'Match deleted' (full forfeit or cancellation) maps to MatchStatus.CANCELED, with the forfeit winner in Match.extra. Forfeited 'Default' maps and unplayed or upcoming maps get no MapPlayed rows.
- For PandaScore-linked matches the PandaScore scheduled_at and format are kept. The HLTV time is kept in extra['hltv']['scheduled_at'].
- News mention matching is precision-first. Lowercase single-word team names (e.g. 'fnatic') are only detected through article links or uppercase aliases, and all-lowercase nicknames only when quoted.
- Performance and economy subpages are not fetched (no Rating 2.0 'impact', no per-round economy). Demo parsing covers economy.
- cs2bot.ingest.scope is imported lazily (is_in_scope, team_rank_as_of, refresh_upcoming_scope); an equivalent fallback rule is used if it is missing.

## Foundation change requests

Done in the foundation (and adopted here in review round 1): HLTVSettings `browser_profile_dir` / `challenge_wait_s` / `cooldown_base_s` / `cooldown_max_s` / `block_resources`; injectable `utils.http.RateLimiter` (the `InjectableRateLimiter` subclass is gone); `rounds.source`; shared `domain.sync.SyncReport`; `resolve_player(update_current_team=...)`.

Still open:
- HLTVSettings: consider defaulting headless=False for the owner's desktop, or documenting CS2BOT_HLTV__USER_AGENT, because headless Chromium's 'HeadlessChrome' user agent invites Cloudflare challenges (and a clearance cookie earned in the visible manual-check window is bound to the visible browser's user agent).
- demos table: add map_number/map_name. HLTV now offers per-map demo downloads.
- matches table (optional): first-class is_forfeit / forfeit_winner_team_id columns. Currently kept in Match.extra['hltv'].

## New dependency requests

- (none)

## Review round 1

Independent review against docs/dev/briefs/hltv.md incl. the Addendum. Checks re-run: `pytest tests/ingest/hltv` (154 passed, ~10 s with `-n 2`, browser tests included), ruff check/format and mypy clean, `tests/foundation` passing; `tests/ingest/pandascore` and `tests/ingest/demos` (which use this package) still pass.

**Verified (re-derived, no change needed):** cache key canonicalisation and TTL policy (long TTL only for mapstats / articles / past rankings and archive months / match pages that say "Match over"/"Match deleted"); cache hits bypass the rate limiter; challenge detection ignores Cloudflare's normal-page bot script; exponential cool-down persisted on disk and reset on success; HLTV page orientation -> our orientation for scores, rounds, side splits (regulation halves and round history), round winners and team1 side, veto/pick team ids (`is_swapped`); CS2 MR12 overtime arithmetic (`max(0, total - 24)`); side split from round history vs breakdown; KAST stored 0..100 like the PandaScore package; parsers fuzzed with random truncations / corrupted numbers of every fixture (only `HLTVParseError` ever escapes); UTC-aware datetimes everywhere.

**Addendum items (implemented in this round):**
1. `FetchOptions.from_settings()`; `HLTVFetcher` and `BrowserPageLoader` build their options from `HLTVSettings` when none are passed, so the settings' challenge wait, cool-down, resource blocking and persistent browser profile now actually apply (before, the settings fields were ignored and the profile was never used). Browser tests now use a temp profile dir (the default would have written into the repo's `data/`).
2. `InjectableRateLimiter` removed; the foundation `RateLimiter(clock=, sleep=, rng=)` is used.
3. Round history is written to `rounds` with `source=DataSource.HLTV` by default (`write_round_rows=True`), upserted by round number (idempotent, repairs changed rows, drops surplus HLTV rows) and never written for a map that has DEMO (or any non-HLTV) rows.
4. `resolve_player(update_current_team=...)` is used for new-player resolution (stand-ins never move a known player's team).
5. `open_browser_for_manual_check` + `ManualCheckResult` (new module `manual_check.py`), with fake-Playwright tests (visible launch, profile, proxy credentials passed to Playwright only, cool-down lifted only when the page is no longer a challenge, no browser started on invalid input, browser closed on launch failure).
6. Shared `SyncReport` was already aliased.

**Issues found and fixed:**
- (major) A scraped link to another host (e.g. a news-list item pointing off-site) made the real fetcher raise `ValueError`, which `fetch_page` did not catch: one bad link aborted the whole `sync_news` / `sync_results` run. Now recorded as a failure in the report. Test with the real `HLTVFetcher`.
- (major) Relevance and `Match.in_scope` used *today's* `Team.hltv_rank` for every match, so a backfill pulled in all unstarred old matches of teams that are only good today (and missed nothing the other way). Now uses `ingest.scope.team_rank_as_of` (current rank for recent matches, ranking history for old ones) with an injectable `now` (`sync_upcoming` passes its `now`). An existing test that depended on the real current date now seeds ranking history.
- (major) Lineup reconciliation deleted a team's stored HLTV lineup rows whenever that team's lineup block was missing/unparsable on a re-fetch (only the both-empty case was guarded). Now only teams whose lineup the page shows are reconciled. Test added.
- (minor) `url_for` treated `hltv.org` and `www.hltv.org` as different hosts and accepted scheme-relative `//other.host/...` paths by gluing them onto the base URL. Now `hltv_url()` normalises same-site URLs onto the configured base and refuses other hosts (incl. `user@host` tricks).
- (minor) `sync_rankings(settings=...)` now re-evaluates the scope of not-yet-finished matches (`ingest.scope.refresh_upcoming_scope`) after the latest ranking, as that module asks; without `settings` nothing changes.

**Remaining concerns:**
- Selectors and fixtures are still replicas built from open-source scrapers; the first live run on the owner's PC must be smoke-tested (`selectors.py`).
- Results rows without `data-zonedgrouping-entry-unix` fall back to the sublist date at 00:00 UTC; with such rows a `since` inside that day could stop the walk a few hours early.
- The browser profile can only be open in one browser: the HLTV fetcher, the demo `BrowserDownloader` and the manual check must not run at the same time on the same profile (the scheduler lane must serialise them).
- `open_browser_for_manual_check` has no real-browser test (needs a display); it is covered with a fake Playwright only.
