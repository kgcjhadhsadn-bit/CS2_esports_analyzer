# Build status

Legend: `done` = finished; `wip` = partly written, not finished; `-` = not started.
Test counts are the package's own suite as reported in `docs/dev/reports/<package>.md`.

| Package | Implemented | Tests | Reviewed | Notes |
|---|---|---|---|---|
| foundation (config, domain, db, utils, synthetic, scheduler) | done | 46 | by author | integration applied small changes (see reports/integration.md) |
| odds (Betclic collection, mapping, linking) | done | 374 | round 1 | unverified against live Betclic.pl |
| hltv | done | 166 | round 1 | parsers built from open-source scrapers, not live pages |
| engine (pricing, EV/Kelly, decision, bankroll, ledger, settlement) | done | 567 | rounds 1-2 | |
| sim (round model, map simulator, kill props, match engine) | done | 116 | round 1 | |
| executor (Betclic placement) | done | 390 | rounds 1-2 | selectors unverified against the live site |
| api (local dashboard backend) | done | 224 | round 1 | |
| ratings_veto | done | 183 | round 1 | |
| datasets_features | done | 71 | round 1 | |
| ensemble_backtest | done | 144 | round 1 | |
| pandascore | done | 168 | round 1 | no token available: fixtures from the documentation |
| demos | done | 313 | round 1 | |
| llm (Claude context layer) | done | 233 | round 1 | |
| dashboard (React) | done | 79 (vitest) | round 1 | |
| ops (Windows install, CI, docs) | done | 37 | round 1 | PowerShell scripts never run on Windows |
| integration (services, jobs, `cs2bot up`, CLI, end-to-end test) | done | 40 | - | whole suite 2955 passed |
| final cross-cutting review | - | | - | |
