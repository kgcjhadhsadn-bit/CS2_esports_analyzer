# demos: implementation report

**Tests:** `.venv/bin/python -m pytest tests/ingest/demos -q` - 301 passed, 0 failed, 14 skipped (the 14 are the opt-in real-demo checks, which pass when the demos are downloaded: 14 passed) (lint/type clean: True)

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
- Relative demo URLs are resolved against `settings.hltv.base_url`. `session_factory` is a `sessionmaker` (sessions are opened one after another, never nested; no threads).

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
- **Trade:** the killer dies to a teammate of his victim within `settings.trade_window_s` (inclusive).
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
- `positions_agg`: per player / map / side / place - `rounds_sampled` (the denominator), `presence_share`, enemy kills made from and deaths suffered in that place, opening kills and deaths.
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
