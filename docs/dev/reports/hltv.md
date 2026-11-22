# hltv: implementation report

> Written by the engineer who implemented the package. Reviewers update it when the public API changes.

**Tests:** `.venv/bin/python -m pytest tests/ingest/hltv -q` - 133 passed, 0 failed (lint/type clean: True)

## Summary

P2 (cs2bot.ingest.hltv) is built and passes every definition-of-done check.

- **Research:** hltv.org is blocked from this machine, so the page structure comes from gigobyte/HLTV, w1tte/hltv-scraper's 2026-02 notes on match, mapstats and results pages, 2026 /matches test data from RATING3PRO/HLTV-API and woq/hltv-bot, real saved news pages (f4bio/hltvapi.rs), and ranking parsers. Every selector, regex and Cloudflare marker lives in `selectors.py`.
- **Fetcher:** `HLTVFetcher` caches pages on disk, gzip-compressed with JSON metadata, plus a pruning method. Finished matches, mapstats, news articles, past rankings and past news-archive months use the long TTL. Only real network fetches go through the rate limiter (a subclass of `utils.http.RateLimiter`), never cache hits. It has a browser mode (Playwright, one reused browser/context, executable_path and proxy taken from settings, images/fonts not loaded) and an http mode (httpx). In browser mode it waits up to 20 s for a challenge to clear by itself. If the challenge persists it raises `HLTVBlockedError` and starts an exponential cool-down, saved on disk so it survives a restart. There is no challenge solving or stealth tooling. Challenge detection does not trigger on the Cloudflare bot script that normal pages carry, or on article text.
- **Parsers:** pure functions (HTML in, pydantic models out). They handle BO1/BO3/BO5, overtime (both page layouts), full and partial forfeits, postponed and deleted matches, TBA maps and stand-ins. A section that fails to parse becomes a warning, not a failure. Mapstats parsing covers scoreboards including per-side tables (the hidden eco-adjusted columns are ignored), side splits including overtime, and round history.
- **Sync:** every function is idempotent and runs inside savepoints. It links to PandaScore matches through `find_match` and keeps their orientation via `is_swapped`. Old backfilled matches do not move a player's current team, and stand-ins never do. If HLTV blocks mid-sync, it stops and returns a partial report with `blocked=True`, so the caller can still commit what was done.
- **Fixtures:** 21 HTML fixtures under `tests/ingest/hltv/fixtures/`, generated so that round histories agree with the half scores and final scores.

## Public API

Everything below is re-exported from `cs2bot.ingest.hltv`. HLTV ids are ints. All datetimes are UTC-aware.

## Fetcher
```python
HLTVFetcher(settings: HLTVSettings, *, page_loader: PageLoader | Callable[[str], LoadedPage | str] | None = None,
            rate_limiter: SupportsWait | None = None, sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic, now: Callable[[], datetime] = utcnow,
            options: FetchOptions | None = None, rng: random.Random | None = None)
  .get(path_or_url: str, *, max_age_minutes: int | None = None) -> str   # max_age 0 = force refresh
  .url_for(path_or_url: str) -> str          # raises ValueError for non-HLTV hosts
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
- `FetchOptions` defaults: `challenge_wait_s=20.0, challenge_poll_s=1.0, cooldown_base_s=900.0 (doubles per repeat block), cooldown_max_s=43200.0, transport_retries=1, transport_retry_delay_s=10.0, block_resources=True, browser_profile_dir=None, locale='en-US'`.
- `detect_challenge(html: str) -> str | None`.
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
sync_rankings(session, fetcher, ranking_date: date | None = None, *, options: HLTVSyncOptions | None = None) -> SyncReport
sync_news(session, fetcher, *, months: int = 1, now: datetime | None = None, options: HLTVSyncOptions | None = None,
          refresh: bool = False) -> SyncReport
hltv_round_records(session, map_played: MapPlayed) -> list[RoundRecord]   # fallback round data without demos
```
- **`PageSource`** is a Protocol requiring only `get(path_or_url, *, max_age_minutes=None) -> str`. `HLTVFetcher` satisfies it.
- **`SyncReport`** (dataclass): `job`, `created`, `updated`, `skipped`, `failed`, `fetched_pages`, `blocked`, `errors`, `warnings`, `match_ids`, `details`; plus `.ok`, `.items`, `.merge()`, `.as_dict()` (for `JobTracker.details`). Sync functions never raise for one bad page or row. If `report.blocked` is set, stop and commit the partial work.
- **`HLTVSyncOptions`** defaults: `listing_ttl_minutes=20, upcoming_match_ttl_minutes=120, near_start_window_minutes=120, near_start_ttl_minutes=10, match_link_window_hours=12.0, write_round_rows=False, max_news_articles=100, min_ranking_rows_for_cleanup=30, commit_every=None`.
- **What `sync_match` writes:**
  - Match, matched by `hltv_id`, else `find_match` within 12 h (keeping a PandaScore match's orientation and schedule).
  - Tournament (`hltv_event_id`, `is_lan`).
  - MapPlayed for played maps only: rounds, winner, start side, CT/T splits (including overtime, from mapstats), `overtime_rounds`, `picked_by_team_id`, `is_decider`, `hltv_mapstats_id`, `played_at`.
  - Veto rows, replaced once HLTV publishes a veto.
  - MatchLineup (source HLTV, `is_standin`; stale HLTV rows removed).
  - PlayerMapStats (source HLTV, `ct_kills`/`ct_deaths`/`t_kills`/`t_deaths`).
  - Demo rows (PENDING, one per `/download/demo/{id}`).
  - TeamRanking and `Team.hltv_rank` from the dated rank link on the match page.
  - `Match.in_scope` is OR-ed with the scope rule. It uses `cs2bot.ingest.scope.is_in_scope` when that module exists, else the fallback: tier S/A/B or a team in the top N.
- **Relevance** for results/upcoming: already in scope, or `stars >= 1`, or a ranked top-N team in the DB, or an in-scope match between the same teams nearby. Run `sync_rankings` first.
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
- `ingest rankings` → `sync_rankings`
- `ingest news` → `sync_news`
- `ingest match <hltv_id>` → `sync_match`
- Open the fetcher once per job: `with HLTVFetcher(settings.hltv) as f:`.

## Known limitations

- hltv.org cannot be reached from the build machine. The selectors and the 21 HTML fixtures are replicas built from open-source scrapers and 2026 page notes, not pages captured from HLTV. The first live run on the owner's PC should be smoke-tested; all selectors live in selectors.py.
- Headless Chromium sends a 'HeadlessChrome' user agent, which Cloudflare is more likely to challenge. On the owner's PC, set CS2BOT_HLTV__HEADLESS=false or CS2BOT_HLTV__USER_AGENT to the real browser UA, and consider FetchOptions(browser_profile_dir=...) so clearance cookies persist. By design the fetcher never solves challenges: it waits up to 20 s, then raises and cools down for 15 min doubling up to 12 h (persisted in <cache_dir>/_cooldown.json).
- Stand-in markup on match pages is not documented by any reference scraper. Detection is a conservative heuristic that only fires on explicit 'stand-in' class, title or text tokens.
- Linking a per-map demo download to its map is a best guess from the link text. DemoLink.map_name is parsed but not persisted, because the demos table has no map column.
- HLTV round history is stored in maps_played.extra['hltv']['rounds'] and exposed via hltv_round_records(). Writing it to the rounds table is opt-in (HLTVSyncOptions.write_round_rows=True) because that table has no source column and the demo pipeline (P3, not landed yet) owns it.
- The results page shows no team ids, so relevance uses DB aliases and ranks plus HLTV stars. Run sync_rankings before sync_results and sync_upcoming. A resumable backfill needs stop_when_nothing_new=False.
- 'Match deleted' (full forfeit or cancellation) maps to MatchStatus.CANCELED, with the forfeit winner in Match.extra. Forfeited 'Default' maps and unplayed or upcoming maps get no MapPlayed rows.
- For PandaScore-linked matches the PandaScore scheduled_at and format are kept. The HLTV time is kept in extra['hltv']['scheduled_at'].
- News mention matching is precision-first. Lowercase single-word team names (e.g. 'fnatic') are only detected through article links or uppercase aliases, and all-lowercase nicknames only when quoted.
- Performance and economy subpages are not fetched (no Rating 2.0 'impact', no per-round economy). Demo parsing covers economy.
- cs2bot.ingest.scope (P1) does not exist yet. It is imported when present, otherwise an equivalent fallback rule is used.

## Foundation change requests

- HLTVSettings: add browser_profile_dir (persistent Playwright profile so Cloudflare clearance cookies survive restarts), challenge_wait_s, cooldown_base_s/cooldown_max_s and block_resources. These currently live in cs2bot.ingest.hltv.fetcher.FetchOptions defaults.
- HLTVSettings: consider defaulting headless=False for the owner's desktop, or documenting CS2BOT_HLTV__USER_AGENT, because headless Chromium's 'HeadlessChrome' user agent invites Cloudflare challenges.
- utils.http.RateLimiter: accept injectable clock/sleep/rng. P2 subclasses it as InjectableRateLimiter for deterministic tests.
- rounds table: add a source column (DataSource.HLTV vs DEMO), so HLTV round history can live in rounds without colliding with demo parsing. HLTVSyncOptions.write_round_rows could then default to True.
- demos table: add map_number/map_name. HLTV now offers per-map demo downloads.
- A shared SyncReport type in the foundation, so P1 (PandaScore) and P2 (HLTV) return the same report to the scheduler and JobTracker. P2's is cs2bot.ingest.hltv.models.SyncReport.
- db.repo.resolve_player: add a move_team/seen_at option. It always overwrites current_team_id, which a newest-to-oldest backfill gets wrong. P2 works around it with players.extra['hltv_team_seen_at'].
- matches table (optional): first-class is_forfeit / forfeit_winner_team_id columns. Currently kept in Match.extra['hltv'].

## New dependency requests

- (none)
