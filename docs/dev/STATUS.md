# Build status

Legend: `done` = finished and committed; `wip` = partly written, not finished; `-` = not started.

| Package | Implemented | Tests | Reviewed | Notes |
|---|---|---|---|---|
| foundation (config, domain, db, utils, synthetic, scheduler) | done | 46 | by author | |
| odds (Betclic collection, mapping, linking) | done | 256 | - | unverified against live Betclic.pl |
| hltv | done | 133 | - | parsers built from open-source scrapers, not live pages |
| engine (pricing, EV/Kelly, decision, bankroll, ledger) | done | 249 | - | settlement.py to be added in review |
| sim (round model, map simulator, kill props, match engine) | done | 70 | - | |
| executor (Betclic placement) | done | 222 | - | selectors unverified against the live site |
| api (local dashboard backend) | done | 185 | - | |
| ratings_veto | wip | 0 | - | code written, no tests yet |
| datasets_features | - | | - | |
| ensemble_backtest | - | | - | |
| pandascore | - | | - | |
| demos | - | | - | |
| llm (Claude context layer) | - | | - | |
| dashboard (React) | - | | - | |
| ops (Windows install, CI, docs) | - | | - | |
| integration (services, jobs, `cs2bot up`, CLI, end-to-end test) | - | | - | |
| final cross-cutting review | - | | - | |
