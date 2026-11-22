# P2 - HLTV fetcher, parsers and sync (cs2bot.ingest.hltv)

> Work-package brief: the requirements this package was built against. Keep it in sync when scope changes.

**Owned paths:** src/cs2bot/ingest/hltv/, tests/ingest/hltv/

**Tests:** tests/ingest/hltv/

## Requirements

Goal: HLTV is the richest source: results, map stats, player stat lines, round history, vetoes, lineups, rankings, news, demo links. It runs from the owner's home connection, so be gentle: aggressive caching, slow jittered rate limiting, a real browser. Never implement Cloudflare challenge solving, CAPTCHA solving, or fingerprint spoofing / stealth plugins: if a challenge page persists, back off and raise.

1. Research HLTV page structure from maintained open-source scrapers (the best available ground truth since hltv.org is blocked here): gigobyte/HLTV (TypeScript; read src/endpoints/getMatch.ts, getMatchMapStats.ts, getResults.ts, getMatches.ts, getTeamRanking.ts, getNews.ts and their helpers) via raw.githubusercontent.com, plus any Python HLTV scrapers you find. Keep the selectors you rely on in one module (selectors.py) so they are easy to update.
2. HLTVFetcher(settings: HLTVSettings, *, page_loader=None, sleep/clock injectable): get(path_or_url, *, max_age_minutes: int | None = None) -> str. Modes: "browser" (Playwright sync API, one browser/context reused, headless per settings, executable_path=settings.chromium_executable, proxy from settings.proxy_url, wait for DOM content) and "http" (httpx via utils.http.build_http_client). Disk cache under settings.cache_dir (hash of URL -> html + json meta with fetched_at); TTL settings.cache_ttl_minutes by default, settings.finished_match_cache_ttl_minutes for finished matches/mapstats. Rate limit with utils.http.RateLimiter(min_delay_s, max_delay_s) only for real network fetches (cache hits are free). Challenge detection ("Just a moment...", "cf-chl", "challenge-platform", "Attention Required"): in browser mode wait up to ~20 s for it to clear by itself; if not -> raise HLTVBlockedError and apply an exponential cool-down before the next fetch. Context manager / close().
3. Pure parsers (HTML string -> pydantic models; selectolax or BeautifulSoup):
   - parse_results_page(html) -> list[ResultRow] (hltv match id, url, team names/ids if present, scores, event, stars, date if available, format hints).
   - parse_matches_page(html) -> list[UpcomingRow] (upcoming/live list).
   - parse_match_page(html, url) -> MatchPage: hltv match id, team1/team2 (name, hltv id from /team/{id}/ links), scheduled time (data-unix ms), status (scheduled/live/finished/postponed/deleted), format (from "Best of N"), LAN/online, event (name + id), stage text, map list in series order with per-map scores, picked-by/decider and mapstats ids (/stats/matches/mapstatsid/{id}/...), veto lines ("1. Vitality removed Mirage", "3. NAVI picked Inferno", "7. Ancient was left over") -> veto entries with team names resolved to team1/team2, lineups (player names + hltv ids from /player/{id}/ links; stand-ins if marked), demo download link (/download/demo/{id}), forfeit/default win if visible.
   - parse_mapstats_page(html, url) -> MapStatsPage: map name, final score, side split per team (CT/T rounds; who started CT), player stat lines for both teams (kills, deaths, assists, headshots, ADR, KAST, rating, first kills/deaths and clutches if present) incl. per-side tables when present, and round history (per round: winner team, winning side, end reason from the outcome icons: elimination / bomb exploded / defused / time).
   - parse_ranking_page(html) -> list[RankingRow] (rank, team name, hltv team id, points, ranking date).
   - parse_news_list(html) -> list[NewsRow]; parse_news_article(html, url) -> NewsArticle (title, published_at, body text).
   Parsers must tolerate missing sections (upcoming matches have no maps/veto; forfeits; BO1 vs BO5; overtime scores like 16-14; deleted/postponed).
4. Sync into the DB (session-based, idempotent; db.repo.resolve_team/resolve_player/resolve_tournament with source=DataSource.HLTV):
   sync_match(session, fetcher, hltv_match_id, *, settings, with_mapstats=True) -> SyncReport: upsert Match by hltv_id or link to an existing PandaScore match via db.repo.find_match (respect the existing orientation via db.repo.is_swapped: swap scores, rounds, side splits, veto team refs, lineups' team ids), Tournament (hltv_event_id, LAN flag), MapPlayed rows (map_number, map_name, rounds, winner, team1_start_side & side splits from mapstats, picked_by_team_id/is_decider from the veto, hltv_mapstats_id, played_at), Veto rows (replace for the match), MatchLineup (source HLTV), PlayerMapStats (source HLTV, incl. side splits when available), and a Demo row (status PENDING, url, hltv_demo_id) when a demo link exists and none is recorded yet.
   sync_results(session, fetcher, *, settings, since: datetime, max_pages: int = 50, in_scope_only=True) -> SyncReport (walk /results pages by offset until older than since; for each relevant match call sync_match; relevant = in scope by tier/team rank (use cs2bot.ingest.scope.is_in_scope if importable - it is being built in parallel - else Team.hltv_rank <= settings.scope.hltv_top_n or stars >= 1); stop early when nothing new).
   sync_upcoming(session, fetcher, *, settings) -> SyncReport (upcoming list + match pages for in-scope matches; vetoes/lineups appear ~1 h before start, so re-fetch near start with a short cache TTL).
   sync_rankings(session, fetcher, ranking_date: date | None = None) -> SyncReport (TeamRanking rows + Team.hltv_rank / hltv_rank_updated_at).
   sync_news(session, fetcher, *, months: int = 1) -> SyncReport (NewsItem upsert by url; team_ids/player_ids mentioned, detected by matching known team aliases (utils.names) and player nicknames in title/body).
5. Tests: parser tests on HTML fixtures you build to mirror the real structure (finished BO3 with veto + 3 mapstats, BO1, upcoming without veto, postponed, overtime map, forfeit), mapstats parsing incl. round history and side splits, ranking/news parsing, fetcher cache TTL + rate limiter (injected sleep/clock), blocked-page detection, browser mode with a local page (mark browser) or a fake page loader, sync idempotency, linking to a PandaScore-created match in swapped orientation, a malformed page section doesn't abort the sync.

## Addendum (after the first build wave)

- Use the shared `cs2bot.domain.sync.SyncReport` (already aliased in `ingest/hltv/models.py`).
- `HLTVSettings` now has `browser_profile_dir`, `challenge_wait_s`, `cooldown_base_s`, `cooldown_max_s`, `block_resources`: build `FetchOptions` from settings (settings are the source of truth; keep FetchOptions for the rest).
- `utils.http.RateLimiter` now takes injectable `clock` / `sleep` / `rng`; drop the subclass if it is no longer needed.
- `rounds.source` exists: write HLTV round-history rows with `source=DataSource.HLTV` by default, never overwriting rows whose source is DEMO.
- `db.repo.resolve_player(..., update_current_team=False)` exists for backfills.
- Add `open_browser_for_manual_check(settings, *, url=None, wait_s=...)`: opens the fetcher's persistent profile in a VISIBLE window so the owner can pass a challenge by hand once (used by `cs2bot ingest hltv-browser`). No automated challenge solving.
