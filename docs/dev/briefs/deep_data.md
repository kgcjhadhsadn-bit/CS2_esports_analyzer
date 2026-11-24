# Deep data: more match factors, odds-movement signal, HLTV fallback, PandaScore optional

**Owned paths:** `src/cs2bot/datasets/`, `src/cs2bot/features/`, `src/cs2bot/models/` (ensemble, ratings, veto, sim, props), `src/cs2bot/backtest/`, `src/cs2bot/ingest/` (hltv, pandascore, demos, scope), `src/cs2bot/odds/` (movement features only - do not change mapping/linking behaviour), `config/patches.yaml`, matching tests, and `docs/dev/reports/deep_data.md`. Two other engineers work at the same time: cloud_autonomy (executor, services, flows, api, dashboard, cli) and cloud_deploy (Docker/VPS files). In `src/cs2bot/config.py` only ADD fields to existing sections or a new section with the Edit tool (never rewrite the file); never edit services/flows/api/executor/cli - list needed wiring in your report.

**Tests:** next to each package you change (prefix new test files `test_deep_`).

## Context

The owner wants the deepest possible analysis of everything that affects a CS2 result, and now bets on confidence (>= 75% calibrated probability, break-even guard only - see PLAN.md section 0). So calibration and coverage of rare-but-decisive situations matter more than ever: a confident wrong prediction is the costliest error. Read docs/dev/reports/{datasets_features,ratings_veto,sim,ensemble_backtest,hltv,pandascore,odds,final_review}.md first.

## Requirements

1. **New leakage-safe features** (FeatureStore / build_training_rows / features_for_match_map), each tested for no look-ahead:
   - schedule load and fatigue: maps/matches in the last 3/7/14 days, days of rest, back-to-back days, playing a second series the same day;
   - travel and time zones: event location vs each team's home region (country/region from HLTV team pages or a curated table), time-zone shift, first match after travel, online vs LAN;
   - stakes: elimination / decider / grand final / qualification match (from the HLTV stage text and bracket position), "already qualified/eliminated" dead rubbers where detectable;
   - format specialists: BO1 vs BO3 performance gaps, decider-map performance, map-pick win rate vs map-ban behaviour;
   - economy and clutch: pistol win rate per side, conversion after winning a pistol, anti-eco and force-buy win rates, eco-upset rate conceded, clutch win rates (1vX), retake and post-plant win rates (from rounds / player_round_stats when demo data exists);
   - player match-ups: AWPer vs AWPer and entry vs anchor duel records by map and map area (duels_agg / positions_agg), opening-duel win rate per player per side, trade efficiency;
   - roster & meta: days since roster change, stand-in counts, coach change, patch / map-pool change recency (config/patches.yaml; keep it current: the active pool is reported as [ancient, anubis, cache, dust2, inferno, mirage, nuke] - verify and update DEFAULT_ACTIVE_MAP_POOL in config.py), form trend and change points;
   - **market signal**: Betclic line movement per selection (opening vs current price, steam moves, time since last move) from odds_snapshots, and an optional sharp-bookmaker benchmark (`SharpOddsSource` protocol + a disabled-by-default adapter; no paid feed is configured) - used as a feature and as the closing-line reference for CLV.
2. **Calibration for a confidence rule:** report reliability in the 0.75-1.0 range specifically (bins of 2.5 percentage points), add a conservative adjustment so probabilities above 0.75 are shrunk when the high-confidence bins are under-populated or over-confident, and expose a per-market "trusted" flag (enough history + calibration within tolerance) that the decision layer can require. Document it; the integration wiring is done by cloud_autonomy's reviewer or the final review.
3. **HLTV fallback on a VPS:** when the fetcher is blocked (challenge page), automatically retry through `settings.hltv.proxy_url` if configured, with its own cool-down; never solve challenges. Config field for "proxy only after a block" vs "always proxy".
4. **PandaScore optional:** every path (ingestion, scope, datasets, completeness) works with no token: HLTV supplies schedules, teams, tiers (derive tier from HLTV event stars / ranking of the teams). No crash, no repeated error spam; a single "PandaScore disabled" note.
5. Retrain the ensemble on the synthetic universe with the new features and show (in the report) that calibration and log-loss do not get worse; feature importance table.
6. Definition of done: whole suite (`.venv/bin/python -m pytest tests -q -n 2`), ruff, ruff format --check, mypy clean. Write docs/dev/reports/deep_data.md.
