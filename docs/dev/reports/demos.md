# demos: implementation report

**Tests:** `.venv/bin/python -m pytest tests/ingest/demos -q` - 313 passed, 0 failed, 14 skipped (after review round 1, second pass) (the 14 are the opt-in real-demo checks, which pass when the demos are downloaded: 14 passed) (lint/type clean: True)

## Summary

P3 (`cs2bot.ingest.demos`) is built and meets every definition-of-done check. The package was found half-written (all source modules drafted, no tests, one module broken); everything was verified, kept where correct and fixed where not.

- **Verified against real demos.** The brief assumed no real demo was available, but the public fixtures of the awpy project are downloadable from this machine. The parser was run with the real `demoparser2` 0.42.2 on six demos: Valve matchmaking, FACEIT (knife round, overtime 16-14), ESEA (knife round) and three HLTV demos of professional matches, one with a round replayed after a backup restore. Scores, round counts and every published per-player figure (kills, deaths, ADR, KAST, headshots, multi-kill rounds, enemies flashed, flash assists) match exactly. The one exception is a csstats.gg ADR for one matchmaking player (34.7 published, 37.7 from the game events); awpy documents the same residual. The whole pipeline (zip -> header-based map matching -> parse -> steam-id linking -> persist -> aggregates) was also run end to end on a real HLTV demo, and the resulting rows load cleanly through `cs2bot.datasets`.
- **Bugs that real data exposed and that are now fixed** (each has a regression test in the fake-parser suite):
  - GOTV demos (every HLTV demo) contain no `player_blind` event, so `enemies_flashed` was always 0. Blinds are now rebuilt from `flash_duration` rising on the tick of a `flashbang_detonate`.
  - A backup restore "kills" all ten players on the tick the replayed round starts. Those were stored as ten deaths and wiped that round's damage. Nothing during freeze time counts any more.
  - Round-end reasons arriving as floats (`9.0`) were mapped to `other`.
  - KAST counted pure flash assists; HLTV does not (one player came out at 78.3 % against HLTV's 73.9 %).
  - Round 1 lost its start tick because real demos open with a winner-less `round_end`.
- **Other fixes to the inherited code:**
  - `aggregates.py` referenced an undefined helper and did not run.
  - RAR extraction through `rarfile`'s pipes is broken with the `bsdtar` backend (rarfile 4.5 passes `-f --`), which is exactly the fallback Windows' built-in `tar.exe` would have used. Extraction now runs the tools directly and tries each installed one: on this machine `bsdtar` cannot read solid RAR3 and `unar` cannot read solid RAR5, but each reads the other.
  - Position aggregates ignored deaths whose attacker has no `players` row.
- **Added beyond the brief:**
  - `BrowserDownloader` (Playwright). The HLTV fetcher has no download method, and a plain HTTP client gets a challenge page for `/download/demo/<id>`, so without this no HLTV demo could be fetched.
  - HTTP 403/429/503 and challenge pages count as "blocked": the batch stops, the demo stays `PENDING` and no attempt is used up.
  - `persist_parsed_demo` refuses a demo of another map.
  - An opt-in test module that repeats the real-demo validation.

## Public API

Everything below is importable from `cs2bot.ingest.demos`.

### Job entry point

```python
process_pending_demos(session_factory: Callable[[], Session], settings: Settings, *,
                      downloader: Downloader | None = None, parser_factory=DemoParser, limit: int = 5,
                      options: PipelineOptions | None = None, now: datetime | None = None) -> ProcessReport
select_pending_demo_ids(session, *, limit: int, now=None, options=None) -> list[int]
PipelineOptions(max_attempts=3, stale_after=timedelta(hours=3), min_free_bytes=3 * 1024**3, max_error_chars=2000)
JOB_NAME = "demos.process"
```

- **Selection:** `PENDING` first, then `FAILED` with `attempts < 3`, then `DOWNLOADING`/`DOWNLOADED` rows older than 3 h (left by a crash); newest match first.
- **Per demo:** `DOWNLOADING` (attempts + 1) -> download -> `DOWNLOADED` -> extract -> match files to the match's `MapPlayed` rows by demo header -> per map `parse_demo` + `resolve_steamids` + `persist_parsed_demo` in one transaction -> `PARSED` (`parser_version`, `parsed_at`, `dem_files`), or `FAILED` with `error`. A demo without URL becomes `UNAVAILABLE`.
- **Clean-up:** raw files are deleted afterwards, also after a failure, unless `settings.demos.keep_archives` / `keep_dem_files` are set. Work directory: `<settings.demos.storage_dir>/match_<id>/demo_<id>/`.
- **Blocked source:** an exception class named `HLTVBlockedError` or `DemoDownloadBlockedError` stops the batch, puts the row back to `PENDING` without using an attempt and sets `report.blocked`.
- **Never raises for one bad demo.** It stops early (with a warning) when less than `min_free_bytes` is free on the demo volume.
- **`ProcessReport`:** `job, selected, parsed, failed, unavailable, maps_persisted, rounds_persisted, blocked, errors, warnings, demo_ids, match_ids`, plus `.items`, `.ok`, `.as_dict()` (for `JobTracker.details`).
- Relative demo URLs are resolved against `settings.hltv.base_url`, protocol-relative ones (`//host/...`) get `https:`. Any other non-`http(s)` URL (`file:`, `javascript:`, ...) is never handed to a downloader: the row becomes `UNAVAILABLE` without using an attempt.
- Clean-up only ever deletes files inside the demo's work directory: a downloader that returns a file kept elsewhere (a cache) keeps it.
- A blocked download restores the row's previous status, attempt count and `error`. `session_factory` is a `sessionmaker` (sessions are opened one after another, never nested; no threads).

Wiring for `cs2bot demos process --limit N` and the scheduler's demo job (slow lane; do not run it while an `HLTVFetcher` holds the same browser profile open):

```python
from cs2bot.db.jobs import track_job
from cs2bot.features import update_player_roles
from cs2bot.ingest.demos import (JOB_NAME, BrowserDownloader, process_pending_demos,
                                 rebuild_duel_aggregates, rebuild_position_aggregates)

with track_job(JOB_NAME, session_factory) as job, BrowserDownloader.from_settings(settings) as downloader:
    report = process_pending_demos(session_factory, settings, downloader=downloader, limit=limit)
    job.add(report.items)
    job.details = report.as_dict()
    if not report.ok:
        job.partial_error = "; ".join(report.errors[:3]) or "download source is blocking"
if report.parsed:                                   # report.match_ids lists the matches that got new data
    with session_factory() as session:
        rebuild_position_aggregates(session)        # window_end = today (UTC), 120 days, 20 s sample
        rebuild_duel_aggregates(session)
        update_player_roles(session)
        session.commit()
```

### Download

```python
Downloader = Callable[[str, Path], Path]            # (url, dest_dir) -> path of the saved file
download_demo(url: str, dest_dir: Path, *, downloader: Downloader | None = None,
              timeout_s: float = 900.0, max_bytes: int = 6 * 1024**3) -> Path
http_download(url, dest_dir, *, timeout_s=900.0, max_bytes=..., client: httpx.Client | None = None,
              user_agent=DEFAULT_USER_AGENT) -> Path
BrowserDownloader(*, context=None, profile_dir=None, headless=True, chromium_executable=None, proxy_url=None,
                  user_agent=None, referer=None, downloads_dir=None, start_timeout_s=45.0, timeout_s=900.0)
BrowserDownloader.from_settings(settings: Settings, **overrides) -> BrowserDownloader
downloader(url, dest_dir) -> Path;  downloader.close();  usable as a context manager
```

- `download_demo` validates whatever the downloader returns (a file, not empty, below the size limit, not an HTML page). Exceptions raised by an injected downloader propagate unchanged.
- The default transfer is streaming httpx: redirects followed, `.part` file then atomic rename, size limit, total timeout, truncation check.
- `BrowserDownloader.from_settings` uses the HLTV fetcher's settings (`settings.hltv.browser_profile_dir`, `headless`, `chromium_executable`, `proxy_url`, `user_agent`) and `settings.demos.download_timeout_s`. The executable path is never hard-coded. Pass `context=<Playwright BrowserContext>` to reuse an open browser instead (it is then not closed). It solves no challenges: a challenge or block page raises `DemoDownloadBlockedError`.
- Playwright's sync API is thread-bound: create, use and close the downloader in one thread.

### Archives

```python
extract_archive(path: Path, dest_dir: Path, *, max_member_bytes: int = 4 * 1024**3) -> list[Path]
match_dem_files_to_maps(dem_paths: Sequence[Path], maps: Sequence[tuple[int, str]],
                        header_reader: Callable[[Path], str | None] = read_demo_map_name) -> dict[int, Path]
read_demo_map_name(path) -> str | None              # demo header, "de_mirage" -> "mirage"
detect_archive_kind(path) -> "rar" | "zip" | "zst" | "gz" | "bz2" | "dem" | "unknown"   # magic bytes, then extension
find_rar_tools() -> list[RarTool]                   # empty list = no RAR tool on this machine
ensure_rar_backend() -> list[RarTool]               # raises ArchiveToolMissingError with install advice
```

- Only `.dem` members are extracted, flattened into `dest_dir` under sanitised names (no path traversal, per-file size limit). A plain `.dem` is returned as-is.
- RAR: members are listed with `rarfile`, then extracted by the first tool that works: `unrar`, `7z`/`7zz`, `unar`, `bsdtar`, also in the default Windows folders of WinRAR and 7-Zip and Windows' own `tar.exe`.
- `cs2bot doctor` should call `find_rar_tools()` and tell the owner to install 7-Zip when the list is empty.
- `match_dem_files_to_maps`: the header map name decides; filename hints (`-m2-`, map name) break ties or stand in for an unreadable header; nothing is assigned without evidence except one file for one map; several files for one map -> the largest.

### Parsing

```python
parse_demo(path: Path | str, settings: DemoSettings, *, parser_factory=DemoParser,
           options: ParseOptions | None = None) -> ParsedDemo        # raises DemoParseError
ParseOptions(tick_rate=64, tick_search_window=4, min_blind_duration_s=1.0, min_match_rounds=13)
classify_buy_type(avg_equipment_value: float | None, round_number: int) -> BuyType | None
map_round_end_reason(raw) -> RoundEndReason;  demoparser_version() -> str | None
ECO_MAX_AVG_EQUIPMENT = 1500.0;  FORCE_MAX_AVG_EQUIPMENT = 3800.0;  PISTOL_ROUNDS = frozenset({1, 13})
```

`ParsedDemo` fields: `map_name` (normalised), `header`, `tick_rate`, `parser_version`, `path`, `roster_names` / `roster_scores` (keys `"a"` = the five that were CT in the first parsed round, `"b"` = the others), `dropped_rounds` (`warmup`, `replayed`, `knife`, `restarted`), `warnings`, `n_rounds`, `steamids`, and five DataFrames. Sides are `"ct"`/`"t"`, enums are their values, steam ids are exact Python ints.

- `players`: `steamid, name, team_name, roster, start_side, rounds_played`
- `rounds`: `round_number, start_tick, freeze_end_tick, end_tick, official_end_tick, winner_side, end_reason, reason_raw, ct_equipment_value, t_equipment_value, ct_money_start, t_money_start, ct_buy_type, t_buy_type, bomb_planted, bomb_site, ct_survivors, t_survivors, duration_s, ct_players, t_players, ct_roster, is_overtime`
- `kills`: `round_number, tick, seconds_into_round, attacker_steamid, victim_steamid, assister_steamid, attacker_side, victim_side, weapon, headshot, penetrated, through_smoke, noscope, attacker_blind, flash_assisted, attacker_place, victim_place, attacker_x/y/z, victim_x/y/z, distance, is_opening, is_trade, was_traded, is_enemy_kill`
- `player_rounds`: `round_number, steamid, name, side, roster, kills, deaths, assists, damage, utility_damage, enemies_flashed, flashes_thrown, smokes_thrown, he_thrown, molotovs_thrown, equipment_value, money_start, survived, kast, opening_kill, opening_death, clutch_situation, clutch_won, primary_weapon`
- `positions`: `round_number, steamid, side, seconds_into_round, place, x, y, z, is_alive`

Definitions (the full list with reasons is in the module docstring of `parse.py`):

- **Real rounds only:** warmup, knife round, rounds before the final match restart and rounds replayed after a backup restore are dropped. Numbers come from `total_rounds_played`, so pistol rounds (1, 13) and overtime (25+, no pistol round) are right even when a demo starts late.
- **Buy type:** per-player average equipment value at freeze end; below 1500 eco, below 3800 force, else full (the same thresholds as `models.round_model.classify_buy`).
- **Trade:** the killer dies to a teammate of his victim within `settings.trade_window_s` (inclusive); kills on the same tick count in either event order.
- **Bomb blast / world deaths** (`NON_KILL_WEAPONS`: `planted_c4`, `c4`, `world`, `worldspawn`, `trigger_hurt`) are deaths but nobody's kill (no kill, opening or trade credit), whoever the event names as attacker. Their `kills` row names no attacker (`attacker_steamid`, `attacker_side`, attacker place / coordinates and `distance` are empty), because every consumer of the `kills` table counts a row with differing sides as a kill.
- **Knife round:** only the first round of a (re)started match (`total_rounds_played == 0`) can be one; a late-starting demo whose first round is decided by a knife kill keeps it.
- **Damage:** capped by the victim's remaining health; no team or self damage. Utility damage is HE plus fire.
- **Assists** include flash assists. **KAST** = kill, non-flash assist, survived or traded (HLTV's definition).
- **Clutch:** `1vN` at the moment a player becomes the last of his side; won when his side wins the round.
- **Position samples:** living players at each `settings.position_sample_seconds` offset after freeze end.

### Persistence

```python
resolve_steamids(session, match_id: int, parsed: ParsedDemo) -> dict[int, int]            # steamid -> players.id
persist_parsed_demo(session, map_played_id: int, parsed: ParsedDemo, steamid_to_player_id: dict[int, int]) -> int
```

- `resolve_steamids` only looks at the match's own players (`match_lineups`, plus players with a stat line). Order: stored `Player.extra["steamid"]` / `["steamids"]`, exact nickname, unambiguous fuzzy nickname, elimination within a roster. Learned ids are stored in `Player.extra` (new dict, as strings). It never raises; unknown players are simply absent.
- `persist_parsed_demo` replaces the map's `Round` rows of any source (written with `source=DataSource.DEMO`), `Kill`, `PlayerRoundStats` and `PlayerPositionSample` rows, flushes and returns the number of rounds. It is idempotent.
- Which demo roster is `team1` is decided from the resolved players' teams, then clan names, then `team1_start_side`, then the known score. It raises `DemoPersistError` before deleting anything when that cannot be decided, when the map row is missing, or when the demo is of another map.
- Unknown steam ids give `NULL` player ids on kills and no per-player rows.
- A summary goes to `maps_played.extra["demo"]`: `parser_version, rounds, score, score_matches_result, orientation, team1_roster, unresolved_steamids, dropped_rounds, warnings, persisted_at`.

### Aggregates

```python
rebuild_position_aggregates(session, *, window_days=120, sample_second=20, as_of: date | None = None,
                            prune_before: date | None = None) -> int
rebuild_duel_aggregates(session, *, window_days=120, as_of: date | None = None, prune_before: date | None = None) -> int
ALL_MAPS = "*"
```

- Both cover maps played in `[as_of - window_days, as_of)`; the `as_of` day itself is excluded, so rows with `window_end = D` are safe to use for matches on day `D`.
- Rows of the same `window_end` are replaced; other `window_end` values are kept (walk-forward backtests) unless `prune_before` is given. Both flush and return the row count.
- `positions_agg`: per player / map / side / place - `rounds_sampled` (the denominator), `presence_share`, enemy kills made from and deaths suffered in that place, opening kills and deaths. Samples without a place name count in `rounds_sampled` but in no place, so the shares of a player / map / side can sum to less than 1.
- `duels_agg`: one row per ordered pair per map plus `map_name == "*"`.

### Errors

`DemoError` > `DemoDownloadError` > `DemoDownloadBlockedError`; `DemoError` > `ArchiveError` > `ArchiveToolMissingError`; `DemoParseError`; `DemoPersistError`.

### Real-demo validation (optional, for the owner or CI with a cache)

`tests/ingest/demos/test_demos_real_fixtures.py` runs when `CS2BOT_REAL_DEMO_DIR` points at the six awpy fixture demos (2.4 GB in total; the download commands are in the module docstring).

## Known limitations

- HLTV itself was never reached. `BrowserDownloader` was exercised against a local HTTP server only (redirect to an attachment, challenge page, 404, stalled transfer, persistent profile); whether HLTV's download link behaves like that must be confirmed on the owner's PC.
- No real HLTV `.rar` was available. RAR extraction was checked with real RAR3 / RAR5 / solid sample archives and both tools installed here (`unar`, `bsdtar`); `unrar` and 7-Zip command lines are built from their documentation and not run. Old Windows 10 builds ship a `tar.exe` without RAR5 support, so 7-Zip should be installed.
- `BrowserDownloader` enforces its total timeout by asking Playwright's event loop whether the transfer has ended, which touches private Playwright attributes. If a Playwright upgrade removes them it falls back to waiting without a total timeout.
- The tick rate is assumed to be 64 (true for the six real demos; not read from the demo).
- Equipment value is read at freeze end. Purchases made afterwards are missed: in 95 real rounds the buy class differed from the value 15 s later in 3.
- When two flashbangs pop within four ticks of each other, a blinded player is credited to the first thrower.
- `enemies_flashed` needs at least 1.0 s of blindness (`ParseOptions.min_blind_duration_s`); other sites count differently.
- A demo with one unparseable map is `FAILED` as a whole and downloaded again on the next attempt; the maps that did parse stay stored. After three attempts a row stays `FAILED` until someone resets `attempts`.
- An archive that lacks a map only produces a warning in the report; the demo still counts as `PARSED`.
- A map whose demo is split into parts (server crash) uses the largest part only, and a demo whose last `round_end` is missing loses that round. Both show up as `score_matches_result: false` in `maps_played.extra["demo"]` but are still stored.
- Demo players that cannot be linked to a `players` row get no `player_round_stats` / position rows (those columns are not nullable); their steam ids are listed in the summary.
- No `PlayerMapStats(source=DEMO)` rows are written and `maps_played` columns (start side, side splits) are not filled from the demo.
- Downloads are sequential; `settings.demos.max_concurrent_downloads` is not used.

## Foundation change requests

- `docs/ARCHITECTURE.md`, section P3: the signature is `download_demo(url, dest_dir, *, downloader=None, timeout_s=...)`, not `download_demo(fetcher_or_client, url, dest_dir)`; `rebuild_*_aggregates` take keyword arguments; add `BrowserDownloader`.
- `src/cs2bot/config.py`, `DemoSettings`: add `max_attempts: int = 3`, `stale_after_hours: float = 3.0`, `min_free_disk_gb: float = 3.0`, `min_blind_duration_s: float = 1.0`. They live in `PipelineOptions` / `ParseOptions` today and cannot be set from `.env`.
- `src/cs2bot/db/models.py`, `Demo`: add `map_number: int | None`. HLTV offers one download per map and the pipeline cannot tell which map a row is for until the file is downloaded (also requested by the hltv package).
- `src/cs2bot/db/models.py`: add `steamid` (BigInteger, nullable) to `player_round_stats` and `player_position_samples` and make `player_id` nullable there, so rows of not-yet-linked demo players can be stored and linked later instead of being dropped.
- `cs2bot.ingest.hltv`: the fetcher has no demo download. Either keep using `cs2bot.ingest.demos.BrowserDownloader`, or expose the fetcher's Playwright context so `BrowserDownloader(context=...)` can share one browser.
- ops / `cs2bot doctor`: check `find_rar_tools()` and document installing 7-Zip in `docs/WINDOWS_SETUP.md`.

## New dependency requests

None. (`demoparser2`, `rarfile`, `zstandard`, `httpx`, `playwright`, `rapidfuzz` and `pandas` are already installed; `awpy` is not needed.)

## Review round 1

Independent review against the brief (including the Addendum) and the foundation contracts. All
definition-of-done checks pass: `pytest tests/ingest/demos` 311 passed / 14 skipped (the opt-in
real-demo module), ruff check + format clean, mypy clean, `pytest tests/foundation` 37 passed.

**Verified (re-derived, not trusted):** round cleaning (warmup, knife, final restart, backup-restore
replacement, pseudo round ends, numbering from `total_rounds_played`), reason-code and winner tables
against demoparser2's `ROUND_WIN_REASON`, buy classification incl. pistol rounds 1/13 and no OT
pistol, damage capped by remaining health (never above 100 per victim per round, team/self/world
damage excluded), KAST (non-flash assist), clutch detection, position sampling (alive, not dead
before the sample tick, sample tick <= round end), trade window boundary, steam ids kept as exact
ints, persistence orientation (lineup -> clan name -> start side -> score, raising before any
delete), idempotent replace of `Round` rows of any source written with `source=DEMO` (Addendum),
`Player.extra` assigned as a new dict, pipeline status flow / attempts / retries / blocked handling /
keep flags, aggregates' half-open window `[as_of - window_days, as_of)` (no look-ahead), replace
per `window_end`. Archive extraction is zip-slip safe (flattened, sanitised names, size limits) and
RAR tools are called with an argument list (no shell).

**Issues found and fixed** (each has a regression test in `tests/ingest/demos/test_demos_review1.py`):

1. *major* - A demo that starts recording late and whose first round is decided by knife kills only
   had that round dropped as a "knife round", and every later round was renumbered one too low
   (round 13 lost its pistol flag, OT detection shifted). Only a round with `total_rounds_played == 0`
   can now be a knife round.
2. *minor* - Trades between kills on the same tick were only found when the demo listed the earlier
   kill first; both orders are now checked (affects `is_trade`, `was_traded`, KAST).
3. *minor* - A bomb-blast death whose event names the planter as attacker would have been credited
   as a kill (and opening kill / trade). Weapons in `NON_KILL_WEAPONS` never give a kill.
4. *minor (safety)* - `_cleanup` deleted whatever path an injected downloader returned, even outside
   the work directory; now restricted to files inside it.
5. *minor (safety)* - Scraped demo URLs with any scheme were handed to the downloader (a `file:` URL
   would have been opened by Chromium); only `http(s)` URLs are used now, protocol-relative URLs get
   `https:`, others mark the row `UNAVAILABLE`.
6. *minor* - A blocked download cleared the row's previous `error`; it is restored now.

**Remaining concerns (not fixed):**

- Clutches: when a 1vN turns into a 1v1 both last players get a clutch situation (`1vN` and `1v1`).
  HLTV's exact counting rule could not be checked from this machine.
- Whether CS2 names the planter or the world as attacker of a bomb-blast death was not confirmed
  on a real demo; the code is correct either way now.
- `resolve_steamids` step 5 loads every `players` row outside the lineup when some demo player is
  unresolved (a full-table scan per map); fine for thousands of players, worth a JSON index or a
  `player_steamids` table later.
- (Resolved since: HLTV sync now writes its round rows with `source=DataSource.HLTV`; checked in the
  second pass below.)
- The real-demo validation module was not re-run in this review (the demos are not cached here).

### Second pass (independent re-review, same round)

The first pass above was found committed; this pass re-verified it rather than trusting it. All
definition-of-done checks pass: `pytest tests/ingest/demos` 313 passed / 14 skipped (opt-in
real-demo module), ruff check + format clean, mypy clean, `pytest tests/foundation` 37 passed; the
consumer tests that read demo tables (`tests/learning -k "demo or roles"`) still pass.

**Re-verified:** brief items 1-8 and the Addendum point by point; round cleaning and numbering
(restart segments, backup-restore replacement, knife round only at `total_rounds_played == 0`);
reason / winner code tables against CS's `RoundEndReason` (1, 7, 8, 9, 12, 17, 18); buy thresholds
identical to `models.round_model.classify_buy`; trade window inclusive and same-tick symmetric;
damage capping by remaining health; KAST without pure flash assists; persistence orientation chain
and idempotent replace (any source, written as DEMO); `Player.extra` reassigned as a new dict;
pipeline status flow, attempts, blocked handling, URL scheme guard, clean-up limited to the work
directory; aggregates' half-open window (no look-ahead). Adversarial run: event frames with NaN
ticks, non-numeric / zero steam ids and a bogus winner-less `round_end` parse to the same result;
an empty `position_sample_seconds` yields no samples without error.

**Issues found and fixed** (regression tests in `tests/ingest/demos/test_demos_review1.py`):

7. *major* - The first pass stopped crediting bomb-blast deaths to the planter inside
   `player_rounds`, but the stored `kills` row still carried the planter as attacker with side `t`
   against a `ct` victim. Every consumer of the `kills` table (`datasets.demo` team kill / trade
   counts, `features.roles`, `rebuild_position_aggregates`, `rebuild_duel_aggregates`) counts a row
   with differing sides as a kill, so the planter would have been credited there. Kill rows of
   `NON_KILL_WEAPONS` deaths now carry no attacker id, side, place, coordinates or distance
   (`worldspawn` added to the set). Tests: parse output and persist + both aggregates.
8. *minor (doc)* - `aggregates.py` claimed the presence shares of a player / map / side always add
   up to 1; samples without a place name count in the denominator only. Docstring and report fixed.

**Remaining concerns (not fixed):**

- Clutch counting when a 1vN turns into a 1v1 (both last players flagged) - unchanged from the first
  pass; HLTV's exact rule could not be checked.
- A round without a `round_freeze_end` (surrender vote during freeze time) is timed from its
  `round_start`; if that same round were also a backup-restore replay, the restore's pseudo deaths on
  the start tick would count. Not observed in real demos; very unlikely combination.
- Deterministic failures (orientation undecidable, no file matches a map) are retried up to three
  times, each re-downloading the archive (1-2 GB). Bounded, but a `DemoPersistError` could mark the
  row FAILED with `attempts = max_attempts` directly; left as is because a later HLTV sync may add
  the lineup / score that makes the orientation decidable.
- The real-demo validation module was not re-run (the 2.4 GB fixtures are not cached here).
