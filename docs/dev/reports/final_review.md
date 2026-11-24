# final_review: implementation report

**Tests:** `.venv/bin/python -m pytest tests -q -n 2` - 3000 passed, 20 skipped (browser / PostgreSQL), 0 failed (after Lens B; Lens A: 2988 passed); Lens A regression tests: `tests/odds/test_odds_final_review.py` (4), `tests/services/test_services_final_review.py` (8), `tests/services/test_services_final_review_db.py` (3); Lens B regression tests: `tests/learning/test_learning_final_review.py` (5), `tests/services/test_services_final_review_lens_b.py` (7) - 12 passed, 0 failed; `ruff check src tests`, `ruff format --check src tests` and `mypy` clean; `tests/foundation` 37 passed; dashboard (unchanged) `npm run lint` clean, `npm test` 79 passed, `npm run build` OK.

## Summary

Lens A (money and safety path). I followed one bet across every package boundary - Betclic payload ->
`odds.collector` / `event_matching` / `market_mapping` -> `odds.quotes` -> `services.prediction` ->
`engine.pricing` / `ev` / `decision` / `bankroll` -> `services.betting` -> `executor` (thread wrapper, journal,
profile lock) -> `services.settlement` -> `engine.ledger` - and looked at the API, the CLI, `cs2bot up`
and the Windows scripts wherever they can change money-relevant state. Every bug below has a regression
test that fails without the fix (checked by disabling each fix with a small pytest plugin).

### Findings, by severity

**Critical - fixed**

1. **A CONFIRMED event link inverted every selection when Betclic changed the order of the teams.**
   `bookmaker_events.teams_swapped` is positional ("bookmaker team1 is our team2") and the market mapper
   places a selection by matching its text against the CURRENT `team1_raw` / `team2_raw`, which the
   collector overwrote on every pass. AUTO links are re-scored on every pass, but a CONFIRMED link (the
   owner's review decision) is never touched, so a later payload listing "FaZe - NaVi" instead of "NaVi -
   FaZe" (another page or API, a re-published event) re-keyed "Natus Vincere" as our team2: the model's
   80 % for team1 was priced against the odds of the other team - exactly the "looks like a sure thing"
   bet the system would take. Reproduced with the collector before fixing.
   *Fix* (`odds/collector.py::_follow_team_order`): for a linked event whose stored names come back clearly
   flipped (same geometric-mean name score and orientation margin as `event_matching`), `teams_swapped` is
   flipped with them and every stored market is re-mapped; a CONFIRMED link whose names changed beyond
   recognition (both teams renamed, order unclear) goes back to `PENDING_REVIEW` (never bet until confirmed
   again or re-linked automatically with a clear orientation). One renamed team keeps the link.
   Tests: `tests/odds/test_odds_final_review.py`.

**High - fixed**

2. **Bets priced from a prediction that no longer describes the match.** The betting cycle took the
   latest `match_analyses` row for up to 8 h without checking it against the match row, while the quotes
   are mapped in the orientation of the match row *now*. (a) A source replacing a participant (PandaScore
   `_replace_teams`, HLTV re-pointing a match it alone knows) or changing the format left the old
   prediction bettable against odds for other teams / another series; (b) when the re-prediction failed
   (exception) or was skipped (teams / lineups gone) the old analysis simply stayed "latest";
   (c) a match that left the Tier 1/2 scope kept being bet (PLAN.md 4.1).
   *Fix*: `services.betting.prediction_mismatch` (prediction match id, team ids and format must equal the
   match row's, no stale flag) is checked before pricing; `services.prediction` flags the stored prediction
   `matches.extra["prediction"]["stale"]` when a match cannot be re-predicted (skip with an existing
   analysis, or an error - written in a fresh session) and clears it on the next valid prediction or
   unchanged fingerprint; `candidate_analyses` requires `in_scope`. Tests:
   `tests/services/test_services_final_review.py` (5 tests).
3. **Repeated UNCERTAIN placements were never paused.** One UNCERTAIN answer stops the cycle without
   pausing (by design: the next cycle tries again). If the confirmation step itself is broken (a changed
   receipt layout, a stuck page), every cycle (3 min) clicked one more real bet that nobody could confirm,
   each blocking only its own outcome. *Fix* (`services.betting._repeated_uncertain_reason`): a new
   UNCERTAIN while another real placement is still PLACING pauses trading with an explicit reason, so at
   most two unconfirmed real bets exist before the owner looks. Tests: 2 in
   `tests/services/test_services_final_review.py`.

**Medium - fixed**

4. **One stake / payout / refund per bet was enforced in code only** (read-then-write checks in
   `engine.ledger`); two writers racing on one bet (`cs2bot settle` next to `cs2bot up`, the cycle applying
   a late executor result while settlement confirms the same PLACING bet from the history) could both book
   it. *Fix*: unique index `uq_bankroll_ledger_bet_kind` on `bankroll_ledger(bet_id, kind)` (model + Alembic
   migration `0002`; NULL `bet_id` rows - deposits, adjustments, reconciliations - never conflict). The
   migration refuses to run over a ledger that already holds duplicates and lists them. An
   `IntegrityError` lands in the existing "result could not be recorded" paths (bet stays PLACING, cycle
   stops / settlement records an error). This was the integration report's open foundation request.
   Tests: `tests/services/test_services_final_review_db.py`.
5. **Local settlement of a bet whose match teams changed after placement** would resolve "team1" against
   the new team. *Fix*: bets record `extra["teams_at_placement"]`; `services.settlement` answers UNKNOWN
   (alert, bet stays open) when they differ from the match row - Betclic's own history still settles it.
6. **Database restore could re-stake bets placed after the backup** (they are missing from the restored
   database; a definite-failure journal record of an earlier attempt does not block a re-used key).
   *Fix*: `deploy/windows/restore-db.ps1` switches the kill switch ON after `cs2bot db upgrade` (ASCII, PS
   5.1) and tells the owner to compare with "Moje zakłady" and run `cs2bot settle` first; OPERATIONS.md
   updated.

**Verified, no change needed** (seams checked end to end): quotes are only handed out for AUTO /
CONFIRMED, non-live, not-started events with fresh (`last_odds_at`) available mapped selections; the
executor addresses market and selection by Betclic's raw names (orientation-free), checks the event id in
the landed URL, re-reads odds before every click and never accepts less than `min_acceptable_odds` (which
keeps the decision's EV floor); the 12 % stake tax, the 10 % winnings tax above 2,280 PLN (whole winning,
złoty rounding), refunds, EV / Kelly and the min-odds floor agree between `engine.ev`, `engine.ledger`,
the decision and the executor's potential-win check; halt checks sit at cycle start, in sizing, in a fresh
session before PENDING -> PLACING, and twice in the executor (before acting and right before the final
click); the only code path that calls `executor.place` is `services.betting._place_one`; dry-run vs live
(key prefixes, `include_dry_run`, `compute_state` ignoring paper bets, reconciliation live-only, both
`order.dry_run` and `settings.executor.dry_run` gate the click); idempotency keys + compare-and-swap +
placement journal + profile lock across processes and restarts; Warsaw day / week boundaries (DST-aware);
API mutations are all POST/PUT behind the Host allow-list, the `X-Requested-With: cs2bot` CSRF header and
an Origin check, config overrides are range-checked (NaN rejected); no secret reaches a log (SecretStr
everywhere; proxy credentials split out; the DB URL is never printed); Windows: lock file with
`O_CREAT|O_EXCL`, PID check via `OpenProcess`, SIGBREAK handled, the scripts set the working directory
(so `.env` and relative data paths resolve to the repo).

### Lens B (data and model correctness, documentation)

I followed the data from the sources to the decision - PandaScore / HLTV / demos -> `datasets` ->
`features` -> ratings / veto / round model / kill model / ensemble + calibration -> `match_engine` ->
`engine.pricing` -> `engine.decision`, plus the Claude layer and the backtest - looking for look-ahead
leakage, team1/team2 orientation slips, time-zone errors, CS2 rule errors, calibration and sigma that
do not reach the decision, and differences between the backtest and the live path. Two experiments
drove most findings: predicting the same match with the teams listed both ways through the live
service, and predicting one match through `services.prediction` and `backtest.PipelinePredictor` with
the same models. Every fix has a regression test that fails without it (checked by reverting each
fix); new tests: `tests/learning/test_learning_final_review.py` (5) and
`tests/services/test_services_final_review_lens_b.py` (7).

**High - fixed**

1. **Ensemble probabilities depended on which team is "team1".** Mirrored training rows do not make
   LightGBM antisymmetric (trees split on `t1_x` and `t2_x` separately). Through the live service, an
   even synthetic match got P(team A wins) = 0.454 when listed "A vs B" and 0.393 when listed "B vs A"
   (map level 0.44 vs 0.39; the two answers summed to 1.076). Which team is team1 is an accident of the
   source (PandaScore's opponent order), so this was a random +-3 point error on near-even matches -
   the size of the EV threshold - and it created spurious edges in both directions. The rating-only
   path and the simulator are exactly symmetric (checked).
   *Fix* (`models/ensemble.py`): `MapWinEnsemble.predict` from a `MatchContext` also predicts
   `mirror_context(ctx)` (teams, lineups, veto slots, start sides, flags swapped) and averages the two
   calibrated answers in logit space; half the gap between them is added to `sigma_model` (it is model
   uncertainty). `EnsembleConfig.symmetric` (default on; old bundles load with it on). Now
   p(A,B) + p(B,A) = 1 to 1e-12 for every map, picker and sequence mixture. Tests: 4 in
   `test_learning_final_review.py`; `test_ensemble_model.py::test_feature_row_and_context_paths_agree`
   adapted (the raw-row path is the forward half).

**Medium - fixed**

2. **Stored Claude flags kept pushing a pairing they were not written for.** `context_flags.logit_shift`
   is stored from the perspective of the team that was team1 when the flag was written. When a source
   replaced a participant (PandaScore `_replace_teams`, HLTV re-pointing a match only it knows), (a) a
   flag about the removed team still shifted the new pairing (with `team=None`), and (b) a team that
   stayed but moved to the other slot (PandaScore lists the new pairing in its own order) got its shift
   with the wrong sign. Bounded by `max_total_logit_shift` (0.35 logit, about +-8 points at p = 0.5),
   but silent, and it persists while the analyzer is off or failing. *Fix*: `llm.load_active_flags`
   ignores the shift of a flag about a team no longer in the match (its veto still blocks);
   `llm.deactivate_flags_after_team_change` (called by both syncs on a participant change) switches
   plain flags off and keeps vetoes with shift 0; manual flags are untouched. The LLM fingerprint
   includes the team ids, so the next analysis writes flags for the new pairing. Tests: 3.
3. **The backtest did not measure the model that bets.** (a) `PipelinePredictor` simulated with the
   built-in overtime money, not `settings.models.ot_start_money` (round markets priced differently from
   live as soon as the owner configures 12 500); (b) `cs2bot backtest` ran without the ensemble and with
   the default rating configuration, while the live bot trains the ensemble and uses the `models tune`
   result; (c) DB backtest contexts carried the starting sides of the played maps - the live bot never
   knows them before a match (map rows exist only once a map is played) and they reveal which maps were
   played (a decider). *Fixes*: economy from settings in `backtest/pipeline.py`; CLI default
   `--ensemble` + tuned rating configuration (with a report warning that the tuning holdout may overlap
   the window); `backtest.db.load_backtest_matches` drops `start_sides`. A parity test now checks that
   `PipelinePredictor` and `services.prediction` give identical match, map, round-score and overtime
   probabilities and sigma (1e-12) for the same models. Tests: 3.
4. **Uncalibrated probabilities were invisible.** PLAN.md 5.8 / 6 bets on a *calibrated* p, but with
   fewer than `EnsembleConfig.min_rows` labelled maps, a failed LightGBM fit (silent fallback) or a tiny
   holdout (identity calibrator), the bot bets on the rating model's own probabilities and nothing told
   the owner. *Fix*: `cs2bot doctor` ("models") warns with the reason, read from the newest bundle's
   manifest. The decision rule itself is unchanged (owner's call; see limitations). Test: 1.

**Low - fixed**

5. `MapWinEnsemble.predict_for_sequences` skipped maps when a sequence's `pickers` list was shorter than
   its maps (`zip(strict=False)`), so the match engine failed with "no map estimate"; it now pads with
   unknown pickers like the rating model. Test: 1.
6. **Docs out of step with the code:** OPERATIONS listed a non-existent "slow" lane (real lanes: fast /
   sync / hltv / train), omitted the rankings (12 h) and executor-health (15 min) jobs, described the
   betting job's trigger and the model-artefact retention (the newest 5 are kept automatically)
   inaccurately, and did not mention the demo job's aggregate / role rebuild or the backtest's defaults
   and caveats; ARCHITECTURE updated for the changes above; `config.py` notes that
   `models.rating_half_life_days` / `min_maps_for_rating` are not read by any model.

**Verified, no change needed**

* *Leakage:* training is cut at `as_of` (`since <= t < until` on the map time); the rating model's
  historical views use games strictly before `as_of`; training rows are computed as of the match start
  with the match excluded (map 2 never sees map 1); the feature store cuts every query; the ensemble
  refuses `as_of <= data_end`; the backtest asserts every training set and the predictor's
  `trained_through`; scope uses the rank history for old matches; the live rating refresh applies only
  maps played before `now`.
* *Orientation:* PandaScore (`is_swapped`, scores / sides / round history swapped), HLTV (`_Orientation`
  incl. side splits and halves), demos (roster decided by players, clan names, start side or score -
  raises rather than guesses), datasets (team1 = `matches.team1_id`), veto (`first_veto=None` = 50/50),
  rating model + simulator + series maths (p(A,B) + p(B,A) = 1 exactly), market mapping (Lens A).
* *Time zones:* HLTV `data-unix` milliseconds -> UTC, PandaScore ISO "Z", Betclic naive times = Warsaw,
  date-only values = UTC midnight, the DB rejects naive datetimes, Warsaw only for display and limits.
* *CS2 rules:* MR12 (13 wins), MR3 overtime (first to 4, sides kept into overtime), $800 / $16 000,
  loss bonus 1400 + 500 x streak up to 3400 with a one-step reduction after a win, round rewards
  3250 / 3500, overtime money $10 000 by default (the `mp_overtime_startmoney` default; configurable);
  a finished map needs a winner with >= 13 rounds; pricing and settlement share
  `betclic.totals_include_overtime`.
* *Calibration and uncertainty reach the decision:* isotonic / Platt on a time-ordered holdout, sigma
  from the rating deviation and the bootstrap models (plus the new orientation gap), match-level sigma
  from the +-1 sigma scenarios, the decision uses p(win | settled) and the settled-scale sigma, data
  completeness from the prediction, context vetoes from the flags.
* *Claude layer:* news is HTML-escaped inside `<news_item>` tags declared untrusted, evidence must cite
  supplied items, each flag is clipped to 0.20 and the total to 0.35 logit (confidence-weighted),
  adverse flags cannot help the affected team, a veto needs confidence >= 0.6, the model never outputs
  a probability.
* *Docs (script-checked):* every `CS2BOT_*` variable in the docs and `.env.example` is a real setting;
  every `cs2bot ...` command and option in the docs exists; every endpoint in API.md exists and every
  `/api` endpoint is documented.

## Public API

New or changed callables other packages / the owner use (everything else is unchanged):

```python
# cs2bot.services.betting
prediction_mismatch(prediction: MatchPrediction, match: Match) -> str | None
    # None = the stored prediction may be bet for this match row; else the reason (teams, format, stale)
# run_betting_cycle(...) - signature unchanged; new behaviour: skips mismatched / stale predictions and
#   out-of-scope matches (report.notes "match N not bet: ..."), pauses on a second open UNCERTAIN,
#   writes bets.extra["teams_at_placement"] = [team1_id, team2_id]

# cs2bot.services.prediction
STALE_KEY = "stale"                      # inside matches.extra["prediction"]
mark_prediction_stale(session: Session, match_id: int, reason: str, *, now: datetime) -> bool  # flushes
stale_reason(match: Match) -> str | None
# predict_upcoming(...) - unchanged signature; flags / clears the stale marker as described above

# cs2bot.services.settlement
TEAMS_AT_PLACEMENT_KEY = "teams_at_placement"   # bets.extra; local resolution -> UNKNOWN on a mismatch

# cs2bot.odds.collector.collect_once(...) - unchanged signature; a linked event whose Betclic team order
#   flips keeps its orientation (teams_swapped follows, markets re-mapped, extra["team_order_flipped_at"]);
#   a CONFIRMED link whose team names changed beyond recognition -> PENDING_REVIEW (extra["review_reason"])

# cs2bot.db.models.BankrollLedger: unique Index("uq_bankroll_ledger_bet_kind", "bet_id", "kind")
# Alembic revision "0002" (down_revision "0001"): `cs2bot db upgrade` applies it (start.ps1 / update.ps1 do)
```

Wiring example (unchanged entry points; the new checks run inside them):

```python
from cs2bot.services.betting import prediction_mismatch, run_betting_cycle
from cs2bot.services.prediction import mark_prediction_stale, stale_reason

report = run_betting_cycle(session_factory, settings, executor, bundle)  # skips stale / mismatched matches
with session_factory() as s:
    mark_prediction_stale(s, match_id, "lineup changed, re-predict first", now=utcnow())  # manual block
    s.commit()
```

Owner-facing: `deploy\windows\restore-db.cmd` now ends with the kill switch ON
(`uv run cs2bot control kill-switch off`, or the dashboard, after checking Betclic).

### Lens B: new or changed callables

```python
# cs2bot.models.ensemble
mirror_context(ctx: MatchContext) -> MatchContext      # team1 <-> team2 (lineups, veto slots, sides, flags)
EnsembleConfig.symmetric: bool = True                   # saved with the bundle; old bundles load with True
MapWinEnsemble.predict(ctx: MatchContext, map_name, *, as_of, store, rating_provider, picked_by="auto")
    # unchanged signature; orientation-free when given a context; components gain
    # "p_forward", "p_mirrored", "orientation_gap"; a raw feature row (Mapping) is predicted as given
MapWinEnsemble.predict_for_sequences(...)               # unchanged; short pickers lists = unknown pickers

# cs2bot.llm (cs2bot.llm.store)
deactivate_flags_after_team_change(session: Session, match_id: int) -> int   # flushes; rows changed
load_active_flags(session, match_id, *, now) -> list[ContextFlag]           # unchanged signature; a flag
    # about a team no longer in the match keeps its veto but gets logit_shift = 0

# cs2bot.services.doctor
check_models(settings: Settings) -> Check   # "warn" + reason when the newest bundle is not calibrated

# cs2bot.backtest
load_backtest_matches(session, *, settings, ...)  # contexts without start_sides (unchanged signature)
PipelinePredictor.predict(query)                  # economy = economy_from_settings(settings.models)
```

CLI: `cs2bot backtest --from --to [--sims N] [--ensemble/--no-ensemble]` - the default is now
`--ensemble` and the tuned rating configuration (`data/models/rating_config.json`) is used when present
(`model_runs.params["rating_config"]` records which).

Wiring example (callers are unchanged; the syncs call the flag helper themselves):

```python
from cs2bot.llm import deactivate_flags_after_team_change
from cs2bot.models.ensemble import mirror_context

est_ab = bundle.ensemble.predict(ctx, "mirage", as_of=now, store=store, rating_provider=bundle.ratings)
est_ba = bundle.ensemble.predict(mirror_context(ctx), "mirage", as_of=now, store=store,
                                 rating_provider=bundle.ratings)
assert abs(est_ab.p_team1_win + est_ba.p_team1_win - 1.0) < 1e-9

with session_factory() as s:       # after replacing a participant of match 42 by hand
    deactivate_flags_after_team_change(s, 42)
    s.commit()
```

## Known limitations

Remaining risks found in this review but not changed (with why):

- **Executor that cannot start** (Chromium missing, profile locked by another process) answers FAILED for
  every order: no money moves, but each cycle writes FAILED rows for every accepted outcome until the
  `executor_health` alert is fixed. INSUFFICIENT_BALANCE likewise stops a cycle without pausing.
  A structured "executor unavailable" status needs a domain change (`PlacementStatus`).
- **Market names built by the parser** ("Mapa 1 - Liczba rund") are not the text on the page; the
  executor finds such markets only by the exact name, so they end MARKET_UNAVAILABLE / SELECTOR_ERROR (no
  money at risk, missed bets) until `OddsQuote` carries `page_name` / `group` (stored in
  `bookmaker_markets.extra`) and the executor opens map tabs.
- **History matching** (unconfirmed placements without a Betclic bet id) uses stake + selection + market +
  time, not the event name: two unconfirmed bets with the same selection name and stake inside the grace
  period could be attributed to each other (money stays right, the match attribution / CLV not).
- **Dashboard overrides** may set `min_event_link_confidence` or `min_data_completeness` to 0 - the owner's
  call, but it disables a PLAN.md safety check (link status AUTO / CONFIRMED is still required).
- **Screenshots and HTML dumps** (`data\screenshots\`) show the logged-in Betclic page (balance, name):
  local files, never logged, but not to be shared.
- **Windows Ctrl+C** reaches every process of the console, Playwright's driver and Chromium included: a
  placement in flight at that moment ends UNCERTAIN (safe: reconciled from the history). `stop.cmd`
  (graceful) is the better way to stop.
- An outcome blocked by `unconfirmed_not_found` can only be released by editing `bets.extra` (no API
  action yet).
- The PostgreSQL paths (row locks, migration 0002 on PostgreSQL) were not exercised here (no
  `CS2BOT_TEST_DATABASE_URL`); the migration was run on SQLite. Nothing ran against live Betclic.

Lens B (data and model correctness), remaining:

- **The decision rule does not require calibration.** With the ensemble absent or fallen back the bot
  bets on the rating model's probabilities; `cs2bot doctor` now warns, but a "calibrated" gate in the
  decision rule (or stricter thresholds while uncalibrated) is an owner decision. Calibration is per
  map; series, round and player-prop probabilities are derived from calibrated map probabilities, not
  calibrated themselves.
- **Backtest optimism that remains:** the final veto is treated as known at decision time by default
  (no veto publication time is stored); no Claude flags and data completeness 1.0; the tuned rating
  configuration may have been tuned on a holdout that overlaps the backtest window (the report says
  so); `--from` / `--to` days are UTC.
- **Player roles** (`players.primary_role`) are the latest inference from all demos, so the kill-model
  training rows and backtests see today's roles for old matches (mild look-ahead; roles are stable).
- **Ensemble holdout metrics** in `FitReport` are of the one-sided predictions (the calibrator was
  fitted on both orientations); live predictions are the symmetric average, typically a little better.
- **Stored analyses made before this update** keep their one-sided probabilities until re-simulated (at
  most 6 h, or the next retrain); `uv run cs2bot predict upcoming --force` re-simulates at once.
- HLTV gives all maps of a match the page's start time; maps of a series share one timestamp (ordered
  by map number), which is what every consumer assumes.
- No real HLTV / PandaScore / Betclic data was available: all findings come from code reading and
  synthetic data; PostgreSQL paths were not exercised.

### What the owner must verify on the Windows PC before going live

1. `deploy\windows\start.cmd` (or `uv run cs2bot db upgrade`) applies migration 0002; `uv run cs2bot doctor`
   shows migrations at head. If the upgrade reports duplicate ledger entries, fix them before betting.
2. Log in to Betclic once (`betclic-login.cmd`) **before** the first live start, so the ledger opens from
   the real balance; check that Betclic's **quick-bet / one-click mode is OFF**.
3. Dry-run smoke test (`CS2BOT_EXECUTOR__DRY_RUN=true`): for several events and market types the
   screenshot shows the right event, market (incl. map number / line) and selection in the slip;
   `uv run cs2bot odds unmapped` and the dashboard's market table show the mapped markets you expect.
4. First live bets with the minimum stake: the result is PLACED (not UNCERTAIN) with a Betclic bet id;
   after settlement the dashboard bankroll equals the Betclic balance; a void / won bet's payout matches
   the tax settings (`void_refunds_stake_tax`, `winnings_tax_rounding`) - a `payout_mismatch` alert means
   they need adjusting.
5. Review page: every CONFIRMED link shows the right `teams_swapped` (Betclic's first team vs our team1).
6. PC clock synchronised (Windows Time): odds freshness and the minutes-before-kick-off checks use it.
7. Never delete `data\browser\betclic` (placement journal + session) while bets are open; after a database
   restore keep the kill switch on until open bets were compared with "Moje zakłady".

8. *(Lens B)* `uv run cs2bot doctor`: the "models" line must be **ok**. A warning "NOT calibrated"
   means the bot would bet on uncalibrated rating-model probabilities: backfill more history and
   retrain (`uv run cs2bot models train`), or raise `decision.min_probability` / `min_ev` meanwhile.
9. *(Lens B)* After the backfill, run `uv run cs2bot backtest --from ... --to ...` (the live model) and
   look at calibration (ECE, reliability) and CLV per market before trusting a market type; compare with
   a run on prices before the veto was known if map-level markets look too good.
10. *(Lens B)* Right after installing this update: `uv run cs2bot predict upcoming --force` (or restart
    and wait for the next 6-hourly re-simulation), so no bet uses a one-sided ensemble prediction.
11. *(Lens B)* Check the overtime money of the events you bet on (`CS2BOT_MODELS__OT_START_MONEY`,
    default 10 000) and whether Betclic counts overtime in round totals / handicaps
    (`CS2BOT_BETCLIC__TOTALS_INCLUDE_OVERTIME`); review the entries marked unverified in
    `config\patches.yaml` (map-pool calendar).

## Foundation change requests

None open. Applied in this review (I could edit any file): `src/cs2bot/db/models.py` (unique ledger
index) + migration `0002_ledger_one_entry_per_bet.py`; docs (`docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`,
`docs/dev/STATUS.md`); `deploy/windows/restore-db.ps1`.

Suggested for later (not needed for safety now): `PlacementStatus.EXECUTOR_UNAVAILABLE` (stop / pause the
cycle instead of FAILED rows); `OddsQuote.market_page_name` / `market_group` + executor support for map
tabs; an API action that clears `extra["unconfirmed_not_found"]` after a manual check;
`BookmakerBetRecord.placed_at` resolution (integration request).

Lens B: none open. Applied (I could edit any file): a comment in `src/cs2bot/config.py` (unused model
settings; no behaviour change); code in `models/ensemble.py`, `llm/store.py` (+ export in
`llm/__init__.py`), `ingest/pandascore/sync.py`, `ingest/hltv/sync/matches.py`, `backtest/pipeline.py`,
`backtest/db.py`, `services/doctor.py`, `cli.py`; docs (`docs/OPERATIONS.md`, `docs/ARCHITECTURE.md`,
`docs/dev/STATUS.md`). Suggested for later: store the team1 id (or an orientation-free sign) with each
`context_flags` row (migration) so a stored shift can be re-oriented instead of dropped; a role history
for players (time-aware roles); the veto publication time (to replay veto timing in the backtest); a
`calibrated` marker on `MatchPrediction` so `engine.decision` can require calibration if the owner wants it.

## New dependency requests

None.

Lens B: none.
