# P1 - PandaScore client, sync and scope filter (cs2bot.ingest.pandascore, cs2bot.ingest.scope)

> Work-package brief: the requirements this package was built against. Keep it in sync when scope changes.

**Owned paths:** src/cs2bot/ingest/pandascore/ (package), src/cs2bot/ingest/scope.py, tests/ingest/pandascore/

**Tests:** tests/ingest/pandascore/

## Requirements

Goal: PandaScore is the stable backbone for schedules, teams, rosters, tournaments (with tiers) and results. The owner will buy the highest PandaScore tier; the API token is settings.pandascore.api_token.

1. Research the PandaScore REST API (developers.pandascore.co: reference + pagination, rate limits, filtering and sorting). CS2 is served under the /csgo/ prefix (e.g. /csgo/matches/upcoming, /csgo/matches/running, /csgo/matches/past, /csgo/matches/{id}, /csgo/teams/{id}, /csgo/tournaments/{id}, /csgo/games/{id}, /csgo/series, /csgo/players). Auth via "Authorization: Bearer <token>". Pagination via page[number]/page[size] (max 100) with X-Total / Link headers. Filtering via filter[...], range[...], search[...], sort=. Tournament objects carry "tier" (s, a, b, c, d, unranked). Verify field names from the docs/examples; where unsure, be tolerant.
2. Pydantic models for the payloads you use (extra="ignore", nearly everything optional): match (id, name, begin_at, scheduled_at, end_at, status, match_type, number_of_games, opponents[], results[], winner_id, games[], tournament, serie, league, forfeit, draw, rescheduled, modified_at, detailed_stats), game/map (id, position, status, winner, map name if present, begin_at), team (id, name, acronym, slug, location, players[] with id/name/first_name/last_name/nationality/role), tournament (id, name, slug, tier, begin_at, end_at, serie, league, prizepool, region/country and any LAN/online indicator if present).
3. PandaScoreClient(settings: PandaScoreSettings, *, http_client: httpx.Client | None = None): httpx client with bearer auth, utils.http.RateLimiter.per_hour(settings.max_requests_per_hour), retries for 429/5xx honouring Retry-After, timeouts, a generic paginated iterator, and methods upcoming_matches(hours_ahead: int | None = None), running_matches(), past_matches(since: datetime, until: datetime | None = None), match(match_id), team(team_id), tournament(tournament_id), games(match_id) if needed. Missing token -> clear error (PandaScoreAuthError) without leaking anything.
4. Mapping helpers: status -> MatchStatus (not_started->scheduled, running->live, finished->finished, canceled->canceled, postponed->postponed), match_type/number_of_games -> MatchFormat (best_of 1/3/5, BO2 when 2 games are all played, unknown -> None), tier string -> Tier, scores/winner, map names via normalize_map_name.
5. scope.is_in_scope(tier: Tier, team1_rank: int | None, team2_rank: int | None, settings: ScopeSettings) -> bool: in scope if tier.value in settings.pandascore_tiers OR either team's HLTV rank <= settings.hltv_top_n. Also scope.refresh_match_scope(session, match, settings) using Team.hltv_rank.
6. sync_upcoming(session, client, settings: Settings) -> SyncReport, sync_past(session, client, settings, since: datetime, until: datetime | None = None) -> SyncReport, sync_match(session, client, settings, pandascore_match_id) -> SyncReport. Upsert via db.repo.resolve_tournament / resolve_team / resolve_player (source=DataSource.PANDASCORE). Match upsert: by Match.pandascore_id; otherwise db.repo.find_match(team_a, team_b, scheduled_at) to link a match HLTV already created (set pandascore_id) - respect the EXISTING orientation (db.repo.is_swapped) when writing scores/maps/winner; otherwise create with PandaScore's opponent order. Update status/format/scores/winner/tier/stage/scheduled_at changes (rescheduling!), maps (MapPlayed rows by map_number with map names/winners when available), lineups (MatchLineup source=PANDASCORE when roster info is available; never delete HLTV-sourced lineups), Match.in_scope + Match.tier, last_synced_at. Skip matches with TBD opponents (count them) but update them once teams are known. SyncReport (pydantic): created/updated/skipped counts, errors: list[str]. One bad match must not abort the sync.
7. Idempotency: running the same sync twice yields no changes the second time (test it).
8. Tests (respx, realistic JSON fixtures written from the docs): pagination over several pages, 429 + Retry-After retry (inject a no-op sleep), auth error, mapping tables, scope rule, upsert create/update/reschedule, linking to a pre-existing HLTV match in SWAPPED orientation (scores/winner/maps written in our orientation), malformed match skipped with error recorded while others succeed, idempotent re-run.

## Addendum (after the first build wave)

- Return the shared `cs2bot.domain.sync.SyncReport` (dataclass; the HLTV sync returns the same type) instead of defining your own.
- `db.repo.resolve_player(..., update_current_team=False)` exists for old matches (backfills must not move players back to former teams).
- HLTV sync is already implemented (docs/dev/reports/hltv.md): read how it creates/links matches so both sources converge on ONE match row whichever runs first.
