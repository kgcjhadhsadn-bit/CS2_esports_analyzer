# cs2bot

Counter-Strike 2 Tier 1/2 pre-match analyzer: collects PandaScore / HLTV / demo data, models every
match (ratings, veto, round simulation, player props, ensemble, calibration, Claude context layer),
prices every Betclic.pl market and bets automatically when the calibrated probability is high and
the EV after the Polish 12% stake tax is positive. Runs locally on the owner's Windows PC
(PostgreSQL in Docker Desktop, the Python app natively via uv, dashboard on 127.0.0.1:8000).

## Where things are

- `PLAN.md` - scope, decisions and constraints (single source of truth; change it before changing direction).
- `docs/ARCHITECTURE.md` - package layout, module contracts, fixed CLI names.
- `docs/dev/` - `STATUS.md` (build status), `briefs/` (work-package requirements), `reports/` (each package's public API and limitations).
- `docs/WINDOWS_SETUP.md`, `docs/OPERATIONS.md`, `docs/API.md` - owner guides and the dashboard API.
- `src/cs2bot/` - the package; foundation = `config.py`, `domain/`, `db/`, `utils/`, `testing/synthetic.py`.
- `config/*.yaml` - Betclic market rules and selectors (edited on the owner's PC).
- `deploy/windows/` (PowerShell 5.1 scripts + `.cmd` launchers), `scripts/` (Linux/macOS), `docker-compose.yml`, `.github/workflows/ci.yml`.

## Conventions

- UTC-aware datetimes only (`cs2bot.utils.time.utcnow()`); DB columns reject naive datetimes. Warsaw time only for display and loss-limit day boundaries.
- `Decimal` for money and odds (`cs2bot.utils.money`), floats for probabilities.
- `team1`/`team2` always mean `matches.team1_id`/`team2_id`; bookmaker / source order may be swapped (`bookmaker_events.teams_swapped`).
- Map names via `cs2bot.utils.names.normalize_map_name`.
- No network in tests: saved fixtures, respx, fakes, local HTML. Browser tests carry `@pytest.mark.browser`, PostgreSQL tests `@pytest.mark.postgres` (need `CS2BOT_TEST_DATABASE_URL`).
- Never log secrets (settings secrets are `SecretStr`).
- Anything that can move money checks `db.state.is_trading_halted()` right before acting.
- Full type hints (mypy `disallow_untyped_defs`), ruff line length 110, idempotent DB writes, no look-ahead leakage in model code.
- `deploy/windows/*.ps1` must stay Windows PowerShell 5.1 compatible and pure ASCII.

## Commands

```bash
uv run pytest -q -n 4                   # tests
uv run ruff check src tests             # lint
uv run ruff format --check src tests    # formatting
uv run mypy                             # type check
scripts/test.sh                         # all of the above (CI equivalent); --pg adds PostgreSQL tests
```

Browser tests launch Chromium with `executable_path="/opt/pw-browsers/chromium"` (the default
Playwright launch fails on the dev machine); in production code the Chromium path is a setting
(`chromium_executable`), never hard-coded. They are skipped when that file does not exist.
