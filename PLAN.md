# CS2 Esports Analyzer & Betclic Auto-Bettor — Project Plan

> Status: **planning** · Last updated: 2026-11-22 (runtime moved to owner's PC)
> This document is the single source of truth for scope, architecture and build order.
> Change it before changing direction in code.

---

## 0. Decisions so far

| Topic | Decision |
|---|---|
| Data sources | PandaScore API (**highest available tier**) + HLTV scraping from the owner's PC (incl. demo files) |
| Match scope | **Tier 1 / Tier 2 only** |
| Markets | **Every pre-match CS2 market Betclic offers**, analyzed in full detail |
| Live (in-play) betting | **No**, pre-match only |
| AI role | Statistical / simulation model computes probabilities; Claude adds news context and writes rationales |
| Bet rule | **Confidence + edge filter**: high calibrated probability AND positive EV after tax |
| Staking | **¼ Kelly + hard caps**, loss limits, kill switch |
| Execution | **Fully automatic** bet placement on the **owner's own Betclic.pl account** |
| Runtime | **Owner's PC in Poland, everything local**: no server, no VPS |
| Interface | **Web dashboard** |
| Budget | **High** (500+ PLN / month) |
| Go-live gate | **None**: go live as soon as the system is built (backtest still runs, because calibration needs it) |

---

## 1. Goal & honest constraints

**Goal:** the most detailed, science-based CS2 pre-match model the available data allows. The system analyzes every Tier 1/2 match down to players, roles, positions, duels, maps, vetoes, economy, rounds and kills. It detects meta shifts and trends, and **places a bet only when the model is highly confident AND the Betclic price is profitable after tax.**

Constraints the design must accept:

- **Zero losing bets is impossible.** CS2 has real variance: clutches, eco-round upsets, force-buys, overtime, players having off days. Even a true 90% outcome loses 1 time in 10. The target is *calibrated* probabilities and long-run profit, not a perfect record. Success is measured with Brier score, log-loss, ROI, drawdown and **closing-line value (CLV)**.
- **The bookmaker's edge is real.** Betclic esports margins are typically 6–10%, and Poland takes **12% tax off every stake**. A bet is only worth placing if `p × 0.88 × odds > 1` by a safety margin. "Bet on everything" is a guaranteed loss, so the system analyzes everything but bets selectively.
- **Betclic's Terms forbid bots and automated betting.** If Betclic detects automation, it can restrict or close the account and withhold the balance. The site will also change and break the selectors. The owner has accepted this risk. Betting with a licensed operator is legal in Poland; automation is a contractual issue with Betclic (this is not legal advice). The account holder must be 18+.
- **The bot uses the owner's main Betclic account.** If Betclic restricts it, that is the account and balance at risk.
- **IP/geo:** everything runs on the owner's PC in Poland, so Betclic sees a normal Polish home connection. That's the best case for not being flagged. Keep pacing human-like and bet frequency low.
- **The PC must be on.** The bot only works while the PC is running and awake. Pre-match bets must be placed before each match starts, so the PC needs to be on in the hours before Tier 1/2 matches (sleep disabled, or scheduled wake). The scheduler catches up on missed data jobs after a restart.
- **HLTV has no official API.** Scraping is against its ToS and protected by Cloudflare, and it will run from the owner's home IP. Too many requests can get that IP blocked by HLTV/Cloudflare (which would also affect normal browsing of HLTV). Mitigations: aggressive caching, slow polite rate limits, a real browser (Playwright) and, for the large one-off backfill, an optional residential proxy so the home IP isn't hammered. Expect maintenance work.

---

## 2. Architecture

Python 3.12 monorepo, running **entirely on the owner's PC** (Windows assumed):
- **Docker Desktop** runs PostgreSQL (and MinIO, if used).
- The Python app (scrapers, models, engines, executor, API) runs **natively** via `uv`, so Playwright can open a visible browser for the Betclic login.
- The dashboard is served at `http://localhost`, reachable only from this PC.
- One start command (`cs2bot up`) launches everything; an optional Windows Task Scheduler entry starts it at login.

```
[PandaScore API]  [HLTV scraper]  [HLTV demos]  [News sources]   [Betclic odds collector]
        \               |              |              |                    |
         +------> Ingestion workers (Prefect flows) ----------------------+
                               |
                      PostgreSQL (+ object storage for demos)
                               |
         Feature store -> Models (ratings, veto, round sim, props) -> Calibration
                               |                         ^
                     Claude context layer (news flags, rationale)
                               |
         Pricing engine (all markets) -> Decision engine (confidence+EV) -> Bankroll (1/4 Kelly + caps)
                               |
                     Betclic executor (Playwright) -> Bet ledger
                               |
                FastAPI  <->  React dashboard (incl. kill switch)
```

**Stack**

| Concern | Choice |
|---|---|
| Packaging | `uv`, `pyproject.toml` |
| Database | PostgreSQL 16, SQLAlchemy 2 + Alembic |
| Scheduling | Prefect (flows, retries, observability) |
| HTTP / scraping | `httpx`, Playwright (Chromium) |
| Demo parsing | `demoparser2` (primary), `awpy` (helpers, map areas) |
| Data / ML | polars, numpy, scipy, scikit-learn, LightGBM, PyMC |
| LLM | Anthropic SDK: Opus for deep match context, Haiku for bulk news triage |
| API | FastAPI |
| Dashboard | React + Vite + Recharts |
| Demo storage | Local disk: raw demos deleted after parsing, parsed data kept as Parquet/Postgres |
| Quality | ruff, mypy, pytest, GitHub Actions CI |

---

## 3. Repository layout

```
PLAN.md  README.md  pyproject.toml  docker-compose.yml  .env.example
src/cs2bot/
  config.py                 # pydantic-settings; all thresholds & caps live here
  db/                       # models.py, migrations/ (Alembic)
  ingest/
    pandascore.py
    hltv/                   # client.py, parsers.py, rankings.py, vetoes.py
    demos/                  # download.py, parse.py
    news.py
  odds/                     # betclic_collector.py, event_matching.py
  features/                 # team.py, player.py, map.py, economy.py, meta.py
  models/                   # ratings.py, veto.py, round_model.py, map_sim.py,
                            # player_props.py, calibration.py
  llm/                      # context.py, prompts/
  engine/                   # markets.py, pricing.py, decision.py, bankroll.py
  executor/                 # betclic.py, session.py, safety.py
  backtest/                 # runner.py, metrics.py
  flows/                    # schedules.py (Prefect deployments)
  api/                      # main.py, routes/
dashboard/                  # React app
tests/
```

---

## 4. Data layer

### 4.1 Scope filter
A match is in scope if its PandaScore tournament tier is **S, A or B**, *or* either team is in the **HLTV top 50**. Both values are configurable.

### 4.2 Sources
- **PandaScore (highest available tier):** fixtures, tournaments, teams, rosters, results, per-game stats. The stable backbone of IDs and schedules.
- **HLTV (scraped from the owner's PC):** results, map stats, player ratings, rankings, veto sequences, lineups, stand-ins, LAN/online flag, demo download links.
- **HLTV demos:** downloaded for every in-scope match and parsed with `demoparser2`. This yields every kill, player position (x/y/z + named map area), duel, utility throw, equipment value / economy state, and round result. **It is the source of the "which player holds which position against which opponent" detail.**
- **News:** HLTV news, Liquipedia roster changes, team/player socials. Triaged and classified by Claude.
- **Betclic odds collector:** polls the JSON endpoints Betclic's own frontend uses for every CS2 event and market every few minutes, and stores timestamped snapshots. **Build this first.** Historical Betclic odds can't be downloaded, so every day it isn't running is lost data. For backtests before that, use historical closing odds scraped from odds-comparison sites.

### 4.3 Backfill
From the CS2 release (**September 2023**) to today, for all in-scope matches.

**Disk:** raw HLTV demo archives are large (hundreds of MB per match). They are downloaded, parsed, then **deleted**; only parsed data is kept. Plan for roughly **100–200 GB free** (parsed data, database, temporary demo files). The backfill runs slowly over several days to stay within polite HLTV rate limits.

### 4.4 Entity matching
PandaScore, HLTV and Betclic use different names and IDs. `event_matching.py` links them by normalized team names, an alias table, start-time window and tournament. Low-confidence matches go to a review queue in the dashboard and are **never bet on automatically**.

### 4.5 Core tables
`teams, players, roster_periods, tournaments, matches, maps_played, vetoes, rounds, kills, damage_events, utility_events, player_map_stats, positions_agg, duels_agg, news_items, context_flags, odds_snapshots, predictions, bets, bankroll_ledger, system_state`

---

## 5. Modeling: the detail layer

Every model is trained and evaluated **walk-forward** (only data available before the match), so there is no leakage.

1. **Team / map strength.** Glicko-2 rating per team **per map**, with:
   - time decay (older results count less),
   - roster-continuity decay (a lineup change partly resets the rating, in proportion to the players changed),
   - LAN vs online and stand-in adjustments.
2. **Veto model.** Each team's pick/ban probabilities per map, from veto history with recency weighting (multinomial model). Output: a probability distribution over the map sequences for BO1 / BO3 / BO5.
3. **Round model.** Probability of winning a round, per side (CT/T), per map, conditioned on:
   - team map ratings and recent player form,
   - economy state (pistol, eco, force, full buy, for both teams),
   - side-specific site performance from demos (site retakes, executes, post-plant win %),
   - **role matchups**: AWPer vs AWPer, entry vs anchor, lurker impact, and duel win rates **by map area** for the actual players expected to meet there.
4. **Map simulator.** Monte Carlo (≥ 10,000 sims per map), round by round, including the economy loop (loss bonus, buys) and **MR12 + overtime rules**. Outputs: map-winner probability, round-handicap distribution, total rounds, overtime probability, exact-score distribution.
5. **Match aggregator.** Veto distribution × per-map simulations → match winner, correct map score, map handicap ±1.5, total maps, and any other match-level market Betclic lists.
6. **Player props.** Kills per round by **role, map and opponent defensive profile** × the simulated round-count distribution → negative-binomial kill distribution for over/under, top fragger, and similar markets.
7. **Meta & trends.**
   - patch notes and map-pool rotation detection,
   - rolling-window form,
   - regime breaks (change-point detection on team ratings and round-win rates),
   - coach / IGL changes, roster-move effects.
8. **Ensemble + calibration.** LightGBM on engineered features, blended with the structural simulations. **Isotonic calibration** on a time-ordered holdout. Each prediction outputs a **calibrated probability `p`** plus an **uncertainty `σ`** (bootstrap / posterior std).
9. **Claude context layer.**
   - Haiku triages incoming news; Opus reads the relevant items and match context for each upcoming match.
   - Output = **structured flags** (stand-in, illness, visa issue, bootcamp, travel/jet lag, elimination or "dead rubber" stakes, internal drama).
   - Each flag applies a **bounded** probability adjustment, or a hard **"no bet"** veto. The LLM never outputs the probability itself.
   - Claude also writes the human-readable **rationale** shown for every bet in the dashboard.

---

## 6. Pricing → decision → staking

**Pricing.** Every Betclic outcome is mapped to a model probability `p` with uncertainty `σ`.

**Tax.** Effective decimal odds `o_eff = 0.88 × o`. The tax factor is configurable, for Betclic "no tax" promos.

**Bet only if ALL of these hold** (every threshold is configurable per market type):

| Check | Default |
|---|---|
| Calibrated probability | `p ≥ 0.75` |
| Expected value after tax | `EV = p × o_eff − 1 ≥ 0.03` |
| Model uncertainty | `σ ≤ 0.05` |
| Data completeness for the match | `≥ 95%` (lineups confirmed, demos/stats present) |
| Claude context flags | no unresolved veto flag |
| Entity matching | high-confidence link between Betclic event and model match |

At `p = 0.75` this needs odds of about **1.52** or higher, so expect **few bets**. That is by design.

**Staking: ¼ Kelly with hard caps.**
- `b = o_eff − 1`, `f* = (b·p − (1 − p)) / b`, stake = `0.25 × f* × bankroll`
- Max **2%** of bankroll per bet
- Max **4%** total exposure per match (markets on the same match are correlated)
- Max **N bets per day** (configurable)
- **Daily loss limit 5%**, **weekly loss limit 10%**: hitting either automatically **pauses** the bot until it is manually resumed
- Global **kill switch** in the dashboard

---

## 7. Betclic executor (owner's PC)

- **Playwright** with a **dedicated, persistent Chromium profile** on the owner's PC, separate from the owner's everyday browser.
- **First login is done by the owner, by hand**, in that browser window (including SMS / 2FA). The session is then reused; when it expires, the bot pauses and the dashboard asks the owner to log in again.
- Betclic credentials (if stored at all) live only in the local `.env` / Windows Credential Manager on this PC. They are **never committed, never sent anywhere, and never pasted into chat**.
- **Bet flow:** open event → locate market and outcome → **re-read live odds** (abort if they moved beyond tolerance or the edge disappeared) → enter stake → confirm → capture Betclic bet ID + screenshot → write to `bets` / `bankroll_ledger`.
- **Safety:**
  - kill switch (DB flag, checked before every action),
  - `DRY_RUN` mode, used only for the first smoke test of the selectors,
  - randomized human-like delays and low request rate,
  - selector health checks with dashboard alerts when the page layout changes,
  - balance reconciliation after every bet and daily,
  - idempotency: a bet is never placed twice for the same prediction.

---

## 8. Dashboard (FastAPI + React)

- Upcoming Tier 1/2 matches with data-completeness status
- **Model vs Betclic odds** for every market, with EV and decision reason ("bet" / "skipped because …")
- Per-bet analysis page: Claude rationale, veto distribution, map-sim histograms, key duels and positions, player kill projections
- Bet history, bankroll curve, ROI, CLV, drawdown
- Calibration plots (reliability diagram, Brier over time)
- Pipeline health (scrapers, demo parser, odds collector, executor)
- **Kill switch, pause / resume**, threshold & cap editor
- Review queue for low-confidence entity matches
- Local only (`localhost`), single user

---

## 9. Build phases

| # | Phase | Output |
|---|---|---|
| 0 | Scaffold | Repo structure, Docker Compose (Postgres) for the PC, CI (ruff, mypy, pytest), `.env.example`, Windows setup guide, **Betclic odds collector running ASAP** |
| 1 | Ingestion | PandaScore + HLTV clients, DB schema, backfill from Sept 2023 |
| 2 | Demo pipeline | Download + parse demos; kill/round/economy/position tables; duel & position aggregates |
| 3 | Core models | Map ratings, veto model, round model, map simulator, match aggregator |
| 4 | Detail models | Player props, meta/trend features, LightGBM ensemble, calibration |
| 5 | Backtest | Walk-forward harness with historical odds; Brier, log-loss, ROI, drawdown per market |
| 6 | Claude layer | News triage, context flags, bounded adjustments, rationales |
| 7 | Engines | Pricing for all markets, decision rules, bankroll / Kelly / limits |
| 8 | Executor | Betclic Playwright executor + safety controls |
| 9 | Dashboard | FastAPI + React dashboard |
| 10 | Install & go live | Install on the owner's PC, auto-start, health alerts in the dashboard, first `DRY_RUN` selector smoke test, then live |

---

## 10. Open questions

- Starting bankroll (PLN) and final values for caps / limits / bets-per-day.
- PC details: Windows version, RAM, free disk space, and which hours it is usually on.
- Betclic 2FA method (SMS / app), so the re-login flow matches it.
- PandaScore: the owner signs up for the top plan and puts the API key in the local `.env` (not in chat).
- Anthropic API key (local `.env`).
- Optional residential proxy for the HLTV backfill, to protect the home IP.
- Which CS2 prop markets Betclic actually offers (the odds collector will reveal this).
