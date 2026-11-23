# Final cross-cutting review

**Owned paths:** the whole repository (you run alone; you may edit any file).

**Tests:** the whole suite: `.venv/bin/python -m pytest tests -q -n 2`, `.venv/bin/ruff check src tests`, `.venv/bin/ruff format --check src tests`, `.venv/bin/mypy`; for the dashboard `npm run lint && npm test && npm run build` in dashboard/.

## Requirements

The system places real-money bets automatically. Review it END TO END across package boundaries (the per-package reviews already happened; look at the seams). Fix what you find, add a regression test for every bug, and write docs/dev/reports/final_review.md (findings by severity, what was fixed, what remains and what the owner must verify on their own PC before going live). Your lens is given in the additional instructions.

Lens A - money and safety path: follow one bet from odds snapshot to settled ledger entry through `odds.quotes` -> `services.prediction` -> `engine.pricing/ev/decision/bankroll` -> `services.betting` -> `executor` -> `services.settlement` -> `engine.ledger`. Hunt for: double placement, stale or mis-oriented odds (team swap!), wrong market mapped to a spec, tax errors, limits that can be bypassed (concurrency, restarts, dry-run/live mixing, timezone boundaries), halt checks missing on any path that can place a bet, UNCERTAIN handling, anything in the API that can change money-relevant state without the CSRF/Host checks, secrets in logs, and behaviour on Windows (paths, signals, file locks).

Lens B - data and model correctness: look-ahead leakage anywhere between ingestion, datasets, features, training and prediction; orientation consistency (team1/team2) across PandaScore, HLTV, demos, datasets and the simulator; time zones; CS2 rule correctness (MR12, overtime, economy); calibration and uncertainty actually reaching the decision rule; the backtest using the same code path as live; the Claude layer's bounds and prompt-injection handling; and whether README/docs (WINDOWS_SETUP, OPERATIONS, ARCHITECTURE, API, CLAUDE.md, .env.example) match the code and each other.
