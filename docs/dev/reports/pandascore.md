# pandascore: implementation report

**Tests:** `.venv/bin/python -m pytest tests/ingest/pandascore -q` - 147 passed, 0 failed (ruff check / ruff format --check / mypy clean on `src/cs2bot/ingest/pandascore`, `src/cs2bot/ingest/scope.py`; `tests/foundation` still passes)

## Summary

P1 (`cs2bot.ingest.pandascore` + `cs2bot.ingest.scope`) is built and meets the definition of done. The package had been left half-finished (client, models, mapping, entity resolution and sync written, no tests). I verified it against the PandaScore docs, wrote the fixtures and the test suite, and fixed what the tests and review found.

- **Research:** field names, enums, pagination, filters and plan limits were checked against developers.pandascore.co (pagination, filtering-and-sorting, rate limits, the CS match / games references and the Counter-Strike 2 migration guide). No token was available, so nothing has been run against the live API.
  - CS2 is served under `/csgo/`; the CS2 title slug is `cs-2` (id 13) and `filter[videogame_title]` accepts it.
  - Single teams and tournaments only exist on the generic `/teams/{id}` and `/tournaments/{id}`.
  - `/csgo/matches/{id}` and `/csgo/matches/{id}/games` need a historical or real-time plan.
- **Client:** bearer auth, even request spacing from `max_requests_per_hour`, retries for 429/5xx/connection errors honouring `Retry-After` (seconds or HTTP date), lazy pagination, typed pydantic results, typed errors. The token is never put in a URL, log line, exception or `repr`.
- **Sync:** idempotent upsert of tournaments, teams, players, matches, maps, lineups and scope. Each match runs in its own savepoint; API and data problems end up in the `SyncReport`, never as exceptions.
- **One match row with HLTV, whichever source runs first:** tested in both orders against the HLTV package's own fixtures (same real match). PandaScore owns `scheduled_at`, `format`, `tier`, `stage`, `tournament_id`; HLTV owns the final result, the map rows it wrote, vetoes and its lineup rows.
- **Scope:** `is_in_scope` is the single rule (tier in `pandascore_tiers` or an HLTV top-N team); the HLTV sync already imports it.

Changes made to the code that was found:

- `sync_upcoming` computed its time window from the client's clock instead of the run's `now`.
- Players' "last seen" stamp changed on every run for upcoming matches, so a re-run was not a no-op; it is now stable within a UTC day.
- A player HLTV marked as a stand-in for a match is no longer moved to that team by PandaScore's stat lines; roster entries flagged inactive no longer move a player either.
- `client.match()` falls back to `/csgo/matches?filter[id]=` when the single-match endpoint returns 403, so reconciliation works on the entry plan.
- A missing or rejected token now stops the run after the first error.
- `sync_past` walks long ranges in 30-day windows and reports `completed_until`.
- Tracking of touched match ids was quadratic in the number of matches (it matters for the backfill).

## Public API

Everything below is importable from `cs2bot.ingest.pandascore` unless a module is named. All datetimes are UTC-aware.

### Client

```python
PandaScoreClient(settings: PandaScoreSettings, *, http_client: httpx.Client | None = None,
                 rate_limiter: SupportsWait | None = None, sleep: Callable[[float], None] = time.sleep,
                 max_attempts: int = 5, backoff_base_s: float = 2.0, backoff_max_s: float = 60.0,
                 max_retry_after_s: float = 300.0, now: Callable[[], datetime] = utcnow)
  .upcoming_matches(hours_ahead: int | None = None, *, videogame_title: str | None = None) -> list[PSMatch]
  .running_matches(*, videogame_title: str | None = None) -> list[PSMatch]
  .past_matches(since: datetime, until: datetime | None = None, *, videogame_title: str | None = None) -> list[PSMatch]
  .match(match_id: int | str) -> PSMatch
  .games(match_id: int | str) -> list[PSGame]          # maps, rounds, player lines; sorted by position
  .game(game_id: int) -> PSGame
  .team(team_id: int | str) -> PSTeam                  # GET /teams/{id}
  .tournament(tournament_id: int | str) -> PSTournament  # GET /tournaments/{id}
  .tournament_rosters(tournament_id: int | str) -> PSTournamentRosters
  .match_opponents(match_id: int | str) -> PSMatchOpponents
  # lazy variants (one request per page, constant memory)
  .iter_upcoming_matches(hours_ahead=None, *, videogame_title=None, max_pages=None, on_invalid=None, now=None) -> Iterator[PSMatch]
  .iter_past_matches(since, until=None, *, videogame_title=None, max_pages=None, on_invalid=None) -> Iterator[PSMatch]
  .iter_modified_matches(since, until=None, *, videogame_title=None, max_pages=None, on_invalid=None) -> Iterator[PSMatch]
  .iter_matches(kind: str = "", params=None, *, videogame_title=None, max_pages=None, on_invalid=None) -> Iterator[PSMatch]
  # low level
  .paginate(path: str, params=None, *, page_size: int | None = None, max_pages: int | None = None, start_page: int = 1) -> Iterator[dict[str, Any]]
  .request(path: str, params=None) -> httpx.Response ; .get_json(path, params=None) -> Any
  .has_token -> bool ; .ensure_token() -> None ; .stats: ClientStats ; .close() ; context manager
```

- `ClientStats`: `requests`, `retries`, `pages`, `invalid_items`, `rate_limit_remaining` (last `X-Rate-Limit-Remaining`).
- Default limiter: `utils.http.RateLimiter` with a gap of `3600 / settings.max_requests_per_hour` seconds between requests (3.6 s at the default 1000/h). Set `CS2BOT_PANDASCORE__MAX_REQUESTS_PER_HOUR=10000` on the historical / real-time plans.
- A malformed list item is skipped and passed to `on_invalid(raw, exc)` (or logged); it never aborts the list.
  This includes out-of-range numeric timestamps and non-hashable ids. `paginate` stops when a
  page only repeats rows already yielded (a server ignoring `page[number]` cannot loop it forever).
- `format_timestamp(dt) -> str` gives the `2026-11-21T00:00:00Z` form used in `range[...]`.
- Errors, all subclasses of `PandaScoreError`:
  - `PandaScoreAuthError`: no token configured (raised before any request) or HTTP 401.
  - `PandaScorePlanError`: HTTP 403, endpoint not in the plan.
  - `PandaScoreNotFoundError`: HTTP 404.
  - `PandaScoreRateLimitError(retry_after_s)`: 429 that did not clear, or a `Retry-After` longer than `max_retry_after_s` (not slept through).
  - `PandaScoreHTTPError(status_code)`, `PandaScoreTransportError`, `PandaScoreResponseError` (not JSON / wrong shape).

### Payload models (pydantic, `extra="ignore"`, nearly everything optional)

`PSMatch`, `PSGame`, `PSTeam`, `PSPlayer`, `PSTournament`, `PSSerie`, `PSLeague`, `PSOpponent`, `PSResult`, `PSMap`, `PSRound`, `PSRoundScore`, `PSGamePlayer`, `PSTournamentRosters`, `PSMatchOpponents`.

- `PSMatch`: `.kickoff` (scheduled_at, else begin_at, else original_scheduled_at), `.team_opponents()` (fewer than two = TBD), `.score_for(team_id)`, `.is_team_match`.
- `PSGame`: `.winner_team_id`, `.rounds_for(team_id)`.
- `PSPlayer.real_name`; `PSGamePlayer.pandascore_player_id`.

### Mapping helpers (pure)

```python
map_match_status(status: str | None) -> MatchStatus      # not_started->SCHEDULED, running->LIVE, finished, canceled, postponed, else UNKNOWN
map_game_status(status: str | None) -> MatchStatus       # not_played -> CANCELED
map_match_format(match_type: str | None, number_of_games: int | None) -> MatchFormat | None
map_tier(tier: str | None) -> Tier                       # unknown / missing -> UNRANKED
map_is_lan(tournament_type: str | None) -> bool | None   # offline True, online False, mixed None
map_round_outcome(outcome: str | None) -> RoundEndReason
game_map_name(game: PSGame) -> str | None                # normalize_map_name; placeholders -> None
parse_prizepool_usd(prizepool: str | None) -> int | None # USD only
stage_label(tournament_name, match_name) -> str | None
overtime_rounds(rounds_a, rounds_b, regulation=24) -> int
```

`map_match_format`: `best_of` 1/2/3/5, `first_to` 1/2/3 (BO1/BO3/BO5), `all_games_played` 2 (BO2); anything else is `None` and the stored format is kept.

### Sync (session in; flushes, never commits unless `commit_every` is set; returns `cs2bot.domain.sync.SyncReport`)

```python
sync_upcoming(session, client, settings: Settings, *, options: PandaScoreSyncOptions | None = None, now: datetime | None = None) -> SyncReport
sync_past(session, client, settings: Settings, since: datetime, until: datetime | None = None, *, options=None, now=None) -> SyncReport
sync_match(session, client, settings: Settings, pandascore_match_id: int, *, options=None, now=None) -> SyncReport
sync_modified(session, client, settings: Settings, since: datetime, until: datetime | None = None, *, options=None, now=None) -> SyncReport
pandascore_round_records(session, map_played: MapPlayed) -> list[RoundRecord]   # round history fallback without a demo
```

- Status merge with HLTV: PandaScore's status wins, except that it never re-opens a match HLTV saw
  finished, nor one HLTV deleted (`extra['hltv']['status'] == "deleted"`, stored CANCELED) unless
  PandaScore reports it finished.
- `sync_upcoming`: not-started matches of the next `settings.scope.max_hours_ahead` hours plus running matches. Open matches we store that vanished from both lists are re-fetched one by one (at most 40 per run); that is how results, cancellations and far-away reschedules arrive. Run it on the scheduler's `upcoming_sync_min` interval.
- `sync_past`: matches that began in `[since, until]`, oldest first, in 30-day windows. It is both the results job (`since = now - 2 days`) and the backfill (`since = settings.scope.backfill_start`, with `commit_every`).
- `sync_match`: one match with game details and rosters whatever its scope.
- `sync_modified`: every match PandaScore modified in a range; an optional cheap catch-all.
- `report.details` counters: `unchanged`, `api_requests`, `tbd`, `linked`, `rescheduled`, `reconciled`, `out_of_scope`, `other_title`, `game_calls`, `roster_calls`, `ambiguous_rosters`, `lineups_pruned`, `maps_without_name`, `completed_until` (sync_past).
- `report.blocked` is set when the rate limit is exhausted: commit what was done and let the next scheduled run continue.

`PandaScoreSyncOptions` (dataclass, all defaults are safe):

| Field | Default | Meaning |
|---|---|---|
| `videogame_title` | `"cs-2"` | `filter[videogame_title]` on the lists; payloads of another title are skipped |
| `match_link_window_hours` | `12.0` | `db.repo.find_match` window for linking a match HLTV created |
| `store_out_of_scope` | `True` | keep out-of-scope matches too (no extra API calls); `False` skips them |
| `game_details` | `"in_scope"` | `/csgo/matches/{id}/games` for finished matches: `"in_scope"`, `"all"`, `"never"` |
| `rosters` | `"in_scope"` | `/tournaments/{id}/rosters` for lineups: same three modes |
| `lineup_size` | `5` | a roster becomes the expected lineup only with exactly this many (active) players |
| `store_round_history` | `True` | keep round history in `maps_played.extra['pandascore']['rounds']` |
| `write_player_stats` | `False` | also write `player_map_stats` rows with source PANDASCORE |
| `include_running` | `True` | `sync_upcoming` also reads `/matches/running` |
| `reconcile_missing`, `reconcile_lookback_hours`, `max_reconcile` | `True`, `72.0`, `40` | re-fetch of vanished open matches |
| `current_team_window_days` | `30` | older matches never move `Player.current_team_id` |
| `games_final_after_hours` | `48.0` | incomplete game stats are re-fetched until the match is this old |
| `past_window_days` | `30` | window size of `sync_past` (`None` = one request series) |
| `max_pages`, `commit_every` | `None`, `None` | page cap per list; commit after every N matches |

What a match upsert writes:

- **Match:** found by `pandascore_id`, else linked through `find_match` (existing orientation kept via `is_swapped`), else created in PandaScore's opponent order. Fields: status, `scheduled_at`, format, tier, stage, `tournament_id`, `is_lan`, series score, winner, `in_scope`, `last_synced_at`.
- **Tournament:** one row per PandaScore tournament (= stage), named `"<league> <serie> - <stage>"`, with tier, `is_lan`, prize pool (USD), dates, `pandascore_serie_id`.
- **Teams / players:** by PandaScore id, then exact alias, then a strict spelling-variant match; academy and female rosters are never merged into the main team. Players are matched to rows HLTV created (same nickname on the team or in the match's lineup).
- **MapPlayed:** by `map_number` for played maps: name, rounds, winner, `played_at`, overtime rounds, start side and CT/T splits (from the round history). Rows carrying HLTV data only get empty columns filled.
- **MatchLineup (source PANDASCORE):** the players with stat lines for finished matches, else the tournament roster when it lists exactly five. A team that already has rows from another source gets none; rows of other sources are never deleted.
- **`Match.extra['pandascore']`:** `name`, `slug`, `status`, `match_type`, `number_of_games`, `modified_at`, `begin_at`, `end_at`, `original_scheduled_at`, `previous_scheduled_at`, `rescheduled`, `forfeit`, `forfeit_winner_team_id`, `draw`, `detailed_stats`, `league_id`, `serie_id`, `tournament_id`, `games` (per-map status and winner), `games_synced_for`, `deleted`.
- **`MapPlayed.extra['pandascore']`:** `game_id`, `status`, `length_s`, `complete`, `detailed_stats`, `end_at`, `rounds` (`[{round, team1_side, winner, end_reason, bomb_planted}]`, our orientation), `rounds_complete`.
- **Other extras:** `Team.extra['pandascore']['acronym']`, `Player.extra['pandascore']['role']`, `Player.extra['pandascore_team_seen_at']`, `Tournament.extra['pandascore']` (league, serie, stage, type, region, country, prizepool, tier).

### Scope (`cs2bot.ingest.scope`)

```python
is_in_scope(tier: Tier | str | None, team1_rank: int | None, team2_rank: int | None, settings: ScopeSettings) -> bool
refresh_match_scope(session, match: Match, settings: ScopeSettings | Settings, *, sticky: bool = True, now: datetime | None = None) -> bool
refresh_upcoming_scope(session, settings: ScopeSettings | Settings, *, statuses=(SCHEDULED, POSTPONED, LIVE), sticky: bool = True, now=None) -> int
team_rank_as_of(session, team: Team | None, when: datetime | None, *, now=None, max_age_days: int = 45) -> int | None
match_tier(session, match: Match) -> Tier
```

- `refresh_match_scope` sets `match.in_scope` from the match (or tournament) tier and `Team.hltv_rank`. It is sticky by default: an in-scope match stays in scope.
- Matches older than 45 days use the `team_rankings` history of their day instead of today's rank.
- Call `refresh_upcoming_scope` after `hltv.sync_rankings`: a team entering the top N pulls its open matches into scope. It returns the number of flags changed.

### Wiring example (P12)

```python
from cs2bot.config import get_settings
from cs2bot.db.session import session_scope
from cs2bot.ingest.pandascore import PandaScoreClient, PandaScoreSyncOptions, sync_past, sync_upcoming
from cs2bot.ingest.scope import refresh_upcoming_scope
from cs2bot.utils.time import utcnow
from datetime import UTC, datetime, time, timedelta

settings = get_settings()
with PandaScoreClient(settings.pandascore) as client:          # one client per job
    with session_scope() as session:                           # `ingest upcoming`
        report = sync_upcoming(session, client, settings)
    with session_scope() as session:                           # `ingest results`
        report = sync_past(session, client, settings, utcnow() - timedelta(days=2))
    with session_scope() as session:                           # `ingest backfill --since ...`
        since = datetime.combine(settings.scope.backfill_start, time(), tzinfo=UTC)
        report = sync_past(session, client, settings, since, options=PandaScoreSyncOptions(commit_every=50))
with session_scope() as session:                               # after `ingest rankings`
    refresh_upcoming_scope(session, settings)
# JobTracker details: report.as_dict(); job status: FAILED if not report.ok and report.items == 0, PARTIAL if not report.ok
```

Suggested job order: PandaScore `sync_upcoming`, then HLTV `sync_rankings` / `sync_upcoming`, then `refresh_upcoming_scope`. Either order of the two sources ends with one match row.

## Known limitations

- Nothing was run against the live API. The payload models and the seven JSON fixtures are written from the public reference; the first run with the owner's token should be checked (`report.errors`, `report.warnings`, `client.stats.invalid_items`).
- PandaScore's docs do not say whether a 429 carries `Retry-After`. Without it the client backs off 2/4/8/16 s and then gives up with `report.blocked`; the hourly quota then needs the next scheduled run.
- Requests are spaced evenly (3.6 s at 1000/h), so a backfill is slow at the default setting: roughly one request per in-scope finished match plus the list pages. Raise `max_requests_per_hour` to the plan's real limit.
- Map names, round history and player lines need the historical or real-time plan. On a smaller plan the sync still stores schedules, results and tiers and reports one warning per run; maps then come from HLTV only.
- Game details are fetched only for matches that are in scope at that moment. A match promoted later (a team entering the top N) gets its PandaScore maps only if `sync_past` or `sync_match` covers it again.
- A full-match forfeit that PandaScore reports as `finished` is stored as FINISHED with the walkover winner and `extra['pandascore']['forfeit'] = True` (`datasets.actuals` treats it as void). HLTV stores the same case as CANCELED; when both sources have the match the status ends as FINISHED.
- When the two sources disagree on a value only one of them owns after the other filled it first (for example a map's start side written by PandaScore before HLTV), the first writer's value stays. Result, map name and rounds are not affected: HLTV always wins those.
- If HLTV lists a stand-in after PandaScore wrote a five-man lineup, the PandaScore rows of that team are removed and the lineup is incomplete until HLTV's next sync of the match re-adds its players.
- Tournament rows are per PandaScore stage ("Playoffs", "Group A"). An HLTV event row for the same event stays a separate tournament row; the match points at the PandaScore one.
- Prize pools in currencies other than USD are not converted (`prize_pool_usd` stays empty; the raw text is in `Tournament.extra`).
- `player_map_stats` rows from PandaScore are off by default. They are safe for `cs2bot.datasets` (it prefers DEMO > HLTV > PANDASCORE per player), but a player the two sources could not match would appear twice on a map.
- The savepoint-per-match isolation and `commit_every` were only exercised on SQLite. Rolling back a failed match's savepoint works there, but with the foundation's SQLite engine a released savepoint is not undone by a later outer rollback, so "uncommitted work is lost on rollback" could not be tested. PostgreSQL behaviour is untested here.

## Foundation change requests

- `docs/ARCHITECTURE.md` (P1 section): the client's `team(id)` / `tournament(id)` use the generic `/teams/{id}` and `/tournaments/{id}` (there is no `/csgo/teams/{id}`); add `sync_match`, `sync_modified`, `PandaScoreSyncOptions` and `scope.refresh_match_scope` / `refresh_upcoming_scope` to the listed interface.
- `src/cs2bot/config.py`, `PandaScoreSettings`: add `videogame_title: str = "cs-2"` and the sync knobs that are deployment choices (`store_out_of_scope`, `game_details`, `rosters`, `write_player_stats`). They currently live in `PandaScoreSyncOptions` defaults.
- `src/cs2bot/config.py`, `PandaScoreSettings.max_requests_per_hour`: document that the default 1000 is the entry plan and the owner's plan allows 10000 (or change the default).
- `src/cs2bot/db/repo.py`, `fuzzy_find_team` / `resolve_team`: the fuzzy fallback uses `token_set_ratio`, which scores 100 when one name's words are a subset of the other's ("MOUZ NXT" vs "MOUZ", "Spirit Academy" vs "Spirit"). P1 works around it with its own strict matcher (`ingest/pandascore/entities.py`); other callers (HLTV, Betclic linking) are still exposed.
- `src/cs2bot/db/session.py`, `make_engine` for SQLite: add SQLAlchemy's pysqlite savepoint workaround (`isolation_level = None` on connect plus an explicit `BEGIN` on the `begin` event). Without it a released savepoint behaves like a commit in the SQLite tests, so rollback-after-savepoint cannot be tested.
- `src/cs2bot/db/models.py`, `matches`: first-class `is_forfeit` / `forfeit_winner_team_id` columns (also requested by HLTV); both sources keep them in `Match.extra` today.

## New dependency requests

- (none)

## Review round 1

Independent review of the whole package against the brief (including the Addendum), the foundation
contracts and the HLTV package it converges with. Checks re-run: 147 tests pass (`-n 2`, ~6 s);
ruff check / format and mypy clean on the owned paths; `tests/foundation` passes.

**Verified (no change needed):** every brief item and the Addendum (shared `SyncReport`,
`resolve_player(update_current_team=False)` via the 30-day window, one match row whichever source runs
first); status / format / tier / LAN / prize-pool mapping tables; BO2 / first_to handling; overtime
(MR12, 24 regulation rounds) and side splits from the round history; orientation of scores, rounds,
sides and winners on a linked match HLTV created in swapped order; field ownership between the two
sources (`scheduled_at`, `format`, `is_lan`, `stage`, `tournament_id`, scores, map rows) - no field is
written back and forth by the two syncs; reschedule handling and reconciliation of vanished matches;
idempotent re-runs; token never in URLs, logs, exceptions or `repr` (httpx also drops the
`Authorization` header on cross-origin redirects); retry / `Retry-After` logic incl. HTTP-date form;
scope rule and the as-of-date rank history (no look-ahead from today's rank for old matches).

**Issues found and fixed:**

1. (major) An out-of-range numeric timestamp in one list item (e.g. `"end_at": 1e20`) raised
   `OSError` / `OverflowError` from `datetime.fromtimestamp` inside a pydantic validator. Pydantic only
   converts `ValueError`, so the error escaped `_typed`, ended the whole list and propagated out of
   `sync_upcoming` / `sync_past` as an exception. `models._to_datetime` now turns it into a validation
   error (booleans are rejected too), `client._typed` also treats `TypeError` / `ArithmeticError` /
   `OSError` as a malformed item, and `sync._consume` records any unexpected iterator exception in the
   report instead of raising. Tests: `test_absurd_timestamps_are_validation_errors`,
   `test_absurd_timestamp_and_unhashable_id_do_not_abort_the_sync`.
2. (major) A list item with a non-hashable `id` (list / object) crashed `paginate`'s de-duplication
   with `TypeError`, aborting the batch. Only int / str ids are de-duplicated now; the item reaches
   validation and is reported as malformed. Test: `test_unhashable_ids_are_passed_on_not_fatal`.
3. (major) A server that ignores `page[number]` but keeps sending `rel="next"` made `paginate` loop
   forever (de-duplication hid the repeats, so the loop never ended). It now stops when a page holds
   only rows already yielded. Test: `test_pagination_stops_when_the_server_ignores_page_numbers`.
4. (major) Cross-source status flip-flop: for a linked match HLTV marked "Match deleted" (CANCELED)
   while PandaScore still listed it `not_started`, PandaScore set SCHEDULED and the next HLTV sync set
   CANCELED again (its merge never moves CANCELED back), on every run - the betting pipeline would see
   a cancelled match as open every other cycle. PandaScore now keeps CANCELED in that case unless it
   reports the match finished (then FINISHED wins, as both syncs agree). Tests:
   `test_hltv_deleted_match_is_not_reopened_by_pandascore`,
   `test_pandascore_finished_still_overrides_an_hltv_deletion`.
5. (minor) `store_out_of_scope=False` decided with today's `Team.hltv_rank`, so a backfill stored old
   matches of teams that only became good later (and could drop ones of teams that were top-N then).
   It now uses `scope.team_rank_as_of` like `refresh_match_scope`. Test:
   `test_store_out_of_scope_false_uses_the_rank_as_of_the_match`.
6. (minor) The running-matches list ignored `options.max_pages`; malformed-item error messages echoed
   an untrusted id of unbounded length (now truncated to 40 characters).

**Remaining concerns (not fixed):**

- If HLTV deletes a match because it was rescheduled under a NEW HLTV id, the PandaScore-linked row now
  stays CANCELED and HLTV creates a second, HLTV-only row for the new date (`find_match` will not reuse
  a row carrying another `hltv_id`). Before this review the same case produced a flip-flopping row plus
  that duplicate. A proper fix needs the HLTV sync to relink (`hltv_id` replacement) when the linked
  row is a deleted match of the same teams - an HLTV package change.
- An existing PandaScore match whose teams changed while it is HLTV-linked is reported as an error on
  every run (by design: it never rewrites a row HLTV owns); this needs a human look when it appears.
- `refresh_match_scope` is sticky, and recent matches (< 45 days) use today's rank: a mild selection
  effect for datasets, documented in `scope.py`.
- Everything is still unverified against the live API (no token); see "Known limitations".
