# final_review: implementation report

**Tests:** `.venv/bin/python -m pytest tests -n 2` - **3241 passed, 21 skipped (browser / PostgreSQL), 0 failed** (after Lens C; Lens B: 3000 passed; Lens A: 2988 passed); Lens C regression tests: `tests/engine/test_engine_final_review_lens_c.py` (42), `tests/services/test_services_final_review_lens_c.py` (12), `tests/api/test_api_final_review_lens_c.py` (10), `tests/learning/test_learning_final_review_lens_c.py` (4), `tests/services/test_telegram_service.py::test_watchdog_reports_a_stale_or_stuck_money_path_once_per_cooldown` (1) - 69 passed, 0 failed; Lens A regression tests: `tests/odds/test_odds_final_review.py` (4), `tests/services/test_services_final_review.py` (8), `tests/services/test_services_final_review_db.py` (3); Lens B regression tests: `tests/learning/test_learning_final_review.py` (5), `tests/services/test_services_final_review_lens_b.py` (7); `ruff check src tests`, `ruff format --check src tests` and `mypy` (203 files) clean; `tests/foundation` 37 passed; dashboard `npm run lint` clean, `npm test` 86 passed (2 new), `npm run build` OK; `docker compose -f docker-compose.cloud.yml config` renders; `bash -n` on the changed script.

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

### Lens C (cloud autonomy, VPS deployment, deep data, the confidence rule)

I followed the unattended loop on the VPS end to end - container start (entrypoint, Xvfb, migrations,
`cs2bot up`), the first training, the scheduler lanes, odds -> prediction -> guard -> decision -> tier
stake -> executor -> settlement, pauses / auto-resume / circuit breaker, auto-login, Telegram, the
dashboard behind Caddy, backups - and checked that deep_data's features and the trusted-calibration
verdict actually reach the decision. The user's own words set the rule: "I do not want anything
'value', even a 1.28 wins!" (confidence only, odds ignored; PLAN.md section 0) - the computed task's
"break-even guard" is the superseded variant. Every fix has a regression test; new tests:
`tests/engine/test_engine_final_review_lens_c.py` (42), `tests/services/test_services_final_review_lens_c.py`
(12), `tests/api/test_api_final_review_lens_c.py` (10), `tests/learning/test_learning_final_review_lens_c.py`
(4), 1 in `tests/services/test_telegram_service.py`, 2 vitest tests; existing tests that encoded the old
behaviour were adapted (Kelly-specific suites now pin `staking_mode="kelly"`).

**High - fixed**

1. **The owner's rule did not work end to end: stakes were still fractional Kelly.** The decision
   accepted a 75%+ pick at any price, but `engine.bankroll.size_stake` staked 0 (`no_edge`) whenever the
   EV after the 12% tax was not positive - a 1.28 favourite at 80% (EV -0.10) was accepted and never bet.
   The confidence tiers required by the brief's addendum were configured but not implemented.
   *Fix:* `size_stake` sizes by `bankroll.staking_mode`: `confidence_tiers` (default) stakes the highest
   tier met by p(win | settled) - 75% -> 1%, 85% -> 1.5%, 92% -> 2% of the bankroll reference, odds
   ignored - then applies every existing cap (max stake, per-match exposure, daily / weekly loss guards,
   bets per day, free balance, the 2,280 PLN winnings-tax limit, stake step rounding down, Betclic
   minimum); `kelly` keeps the old sizing. Tiers are validated (finite, strictly ascending, fractions
   positive, non-decreasing, <= `max_stake_fraction`) for `.env` and every dashboard edit
   (`config.confidence_tiers_problem`), deliberately not inside `BankrollSettings` (an override stored
   before this update, e.g. a 1.5% max stake, must not make every cycle raise; the max-stake cap binds
   anyway). Accepted outcomes of one match are sized most-confident first (EV order preferred long
   prices). The backtest uses the same staker (the report's `settings_summary` records the mode and
   tiers); dashboard, API, `.env.example`, OPERATIONS / VPS_SETUP / WINDOWS_SETUP / README / PLAN updated.
2. **A "win" below odds 1.137 loses money even when it wins.** With the 12% stake tax a winning bet pays
   `0.88 x odds` per 1 PLN staked: at 1.10 that is 0.968, so the bet loses whether it wins or loses. The
   decision accepted p 0.89 at 1.10 (an existing test asserted exactly that). This is not a value rule -
   the user wants a 1.28 win to be a win, and it is. *Fix:* `decision.require_profitable_win` (default
   on) rejects odds below `engine.ev.min_profitable_odds` (1.137; 1.001 in a tax-free promo) with
   `unprofitable_win`, and `min_acceptable_odds` never lets the executor accept a drop below it.
   `false` ignores the odds completely. Recorded in PLAN.md section 0 for the owner to confirm.
3. **deep_data's features never reached the model.** `services.registry.train_models` built the
   ensemble's feature store without `DeepData` (match metadata - stage, venue, schedule -, demo duels and
   clutches, coach changes, Betclic market history) and without the demo team stats, while the live
   store (`FeatureStore.from_session`) had both. Every deep column was all-NaN in training (never split
   on, so the 70+ new factors were dead weight live) and the demo-stat columns were a train / live skew.
   The backtest pipeline had the same gap. *Fix:* one builder, `services.registry.build_feature_store`,
   for training and live prediction (incl. the optional sharp feed); `HistoricalData` / `TrainingSet`
   carry `team_demo_stats` and `deep` (cut at every training cutoff, leakage-asserted) into
   `PipelineTrainer`.
4. **The high-confidence guard was not applied, and nothing on the VPS would ever write it.**
   cloud_autonomy left `guard.adjust` as a TODO; only a manual `cs2bot backtest` wrote
   `confidence_guard.json`, which nobody runs on an unattended server. *Fix:* `engine.confidence`
   applies the guard before pricing (`decision.use_confidence_guard`, default on): the 75% check, the
   stake tier, the EV shown and the recorded decision use the adjusted p(win | settled) (refund mass
   kept; never raises a probability; a failing guard yields NaN, i.e. no bet). The guard file is read
   once per cycle; without one the no-history prior applies (half of the excess over 75% is kept, so a
   0.75 pick stays bettable and only the tier is more cautious); a broken file falls back to the prior.
   New scheduler job `confidence_guard_refresh` (train lane, daily slot, runs when the file is
   `models.guard_refresh_days` (7) old and at least 40 finished matches have stored prices; attempts
   remembered so an empty window is not retried daily) re-runs the walk-forward backtest over 180 days
   (`services.backtesting`, shared with `cs2bot backtest`; the file is written atomically). `cs2bot doctor`
   reports the guard; decisions carry a "confidence guard: p 0.950 -> 0.850" note; Telegram bet messages
   show "confidence X% -> stake Y% of the bankroll".

**Medium - fixed**

5. **Guard: empty top bins escaped an over-confident history.** The shrinker's monotone running minimum
   only lets higher bins cap lower ones; an empty 97.5-100% bin used the milder no-history prior, so with
   97% picks winning 70% a 1.00 pick still counted as 0.87 and was bet at the 1.5% tier. *Fix*
   (`HighConfidenceShrinker.fit`): a thin bin (< `min_bin_predictions`) above an over-confident bin
   inherits at least that miscalibration; well-populated bins keep their own evidence.
6. **An unusable executor produced one FAILED bet per accepted outcome every cycle.** Chromium missing or
   the profile locked by another process returned FAILED; the cycle went on to the next outcome (another
   browser start) and repeated every 3 minutes; the `executor_unavailable` pause code existed but had no
   producer. *Fix:* such results carry `executor.base.EXECUTOR_UNAVAILABLE_PREFIX`; the cycle stops at the
   first one and, with autonomy, pauses with `executor_unavailable` (lifted by the auto-resume service
   after a passing health check; the circuit breaker bounds repeats). Without autonomy (the PC) it only
   stops the cycle, as before.
7. **The first training blocked the whole start.** It ran before the scheduler and the API: with a
   backfilled history it can exceed the 15-minute healthcheck start period (container unhealthy,
   `deploy.sh` timing out), no odds were collected meanwhile, and a `docker stop` during it hit a process
   without signal handlers. The scheduled retrain (never run -> due at once) then trained the same data a
   second time. *Fix:* the first training runs in a daemon thread (`run_up(train_in_background=True)`);
   the retrain job uses `ModelRegistry.retrain_unless_fresh` (skips a bundle younger than 2 h, checked
   inside the train lock); healthcheck start period 5 minutes.
8. **Behind Caddy the login rate limit acted per proxy.** uvicorn ran with `proxy_headers=False`, so every
   request came from Caddy's address: 5 wrong passwords from anyone on the internet locked the owner out
   for 5 minutes, the audit log showed Caddy, and the session cookie was never `Secure`.
   *Fix:* `APISettings.forwarded_allow_ips` (empty by default) adds uvicorn's `ProxyHeadersMiddleware`
   trusting only those peers; `docker-compose.cloud.yml` pins Caddy to a fixed address
   (`CADDY_IPV4` on `FRONTEND_SUBNET`) and sets `CS2BOT_API__FORWARDED_ALLOW_IPS` to it. Forwarding
   headers from any other peer are ignored; Host is never rewritten.
9. **Silent stops reached nobody.** Telegram reported failing jobs only; a lane that stopped running or a
   job hanging in RUNNING (a stuck browser call blocks the whole fast lane: no odds, bets or settlement)
   was visible only on a dashboard nobody watches. *Fix:* the notifier's watchdog uses the dashboard's
   `job_health` and alerts once per cooldown when a money-path job (odds, predict, bet, settle) has not
   succeeded for twice its interval + 10 min, or any job runs longer than 45 min.

**Low - fixed**

10. Docs out of step: PLAN.md still listed "¼ Kelly" and the EV row of section 6 as current; README,
    CLAUDE.md (VPS files missing from "Where things are"), OPERATIONS (said tiers were not wired yet),
    VPS_SETUP (first training blocks the API), WINDOWS_SETUP, ARCHITECTURE (no cloud deployment
    paragraph, no deep_data / backtesting / guard), API.md and `.env.example` (no VPS block) updated.

**Verified, no change needed**

* *Docker start:* the entrypoint starts a supervised Xvfb on `:99`, retries `cs2bot db upgrade`, then
  `cs2bot up` = scheduler + Betclic executor (headful on Xvfb through the `cs2bot-chromium` wrapper,
  `effective_headless` cannot fall back because `DISPLAY` is set) + API on 0.0.0.0:8000 (password and
  session secret enforced by compose, entrypoint and `Settings`; Host allow-list and CORS origin derived
  from `CS2BOT_PUBLIC_HOST`); SIGTERM / SIGINT forwarded, Xvfb death -> exit 70 -> Docker restart;
  `restart: unless-stopped` on every service; PostgreSQL only on the internal network; only Caddy's
  80/443 published; VNC on the host's loopback only; `.env` never in the build context.
* *Restart / crash recovery:* stale PENDING rows cancelled, PLACING never re-sent, placement journal +
  profile lock, settlement reconciles from the Betclic history; auto-login blocks survive restarts
  except "no credentials"; the Telegram update offset is persisted (no replayed `/go`).
* *Auto-resume / circuit breaker:* never the kill switch or a manual pause; loss limits lift at the next
  Warsaw day / week; pause writers lock the row; breaker 3 per 24 h per code, window restarts at the
  owner's resume. The new `executor_unavailable` producer goes through the same `pause_trading` path
  (audit -> Telegram).
* *Secrets:* `SecretStr` everywhere; the Telegram token redacted from errors; credentials scrubbed from
  browser errors; `.env` chmod 600 and excluded from the image; nothing new logs a secret.
* *Backups:* daily verified `pg_dump`, retention, optional rclone; `restore.sh` ends with a manual pause.
* *Trusted calibration:* `require_trusted_calibration` + the guard's per-market verdict reach
  `engine.decision` (cloud_autonomy), fail safe when unknown; now read from the same per-cycle guard
  object as the adjustment.

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

### Lens C: new or changed callables

```python
# cs2bot.config
DecisionSettings.require_profitable_win: bool = True     # reject odds where a win pays back < stake
DecisionSettings.use_confidence_guard: bool = True       # apply the high-confidence guard before deciding
BankrollSettings.staking_mode: Literal["confidence_tiers", "kelly"] = "confidence_tiers"
BankrollSettings.confidence_tiers: list[tuple[float, float]] = [(0.75, .01), (0.85, .015), (0.92, .02)]
confidence_tiers_problem(bankroll: BankrollSettings) -> str | None   # None = usable (Settings + dashboard)
ModelSettings.guard_refresh_days: float = 7.0            # 0 = no scheduled guard refresh
ModelSettings.guard_backtest_days: int = 180; guard_backtest_sims: int = 1000
ModelSettings.guard_backtest_retrain_days: int = 14; guard_min_priced_matches: int = 40
SchedulerSettings.guard_refresh_cron: str = "30 6 * * *" # daily slot (runs only when due)
APISettings.forwarded_allow_ips: list[str] = []          # trusted reverse proxies (Caddy on the VPS)

# cs2bot.engine
bankroll.size_stake(decision, state, settings, betclic) -> (Decimal, list[str])   # tiers or Kelly + caps;
    # notes start with "tier: ..." / "capped: ...", reasons with "below_tier: ..." etc.
bankroll.confidence_tier(p_settled: float, tiers) -> tuple[float, float] | None
ev.min_profitable_odds(settings: Settings | BetclicSettings | TaxModel) -> Decimal  # 1.137 at 12 %
decision.ReasonCode.UNPROFITABLE_WIN            # "unprofitable_win"
decision.min_acceptable_odds(...)               # also floored at min_profitable_odds (when enabled)
confidence.ProbabilityAdjuster = Callable[[str, float], float]        # (market type, p) -> p'
confidence.guarded_outcome(outcome, market_type, adjuster) -> OutcomeProbability
confidence.guard_pairs(pairs, adjuster) -> (pairs, {(spec_key, selection): (p_model, p_adjusted)})
confidence.guard_note(p_model, p_adjusted) -> str

# cs2bot.models.confidence
HighConfidenceShrinker.fit(...)   # thin bins above an over-confident bin inherit its miscalibration

# cs2bot.services
registry.build_feature_store(session, settings, *, until, map_results=None, player_maps=None,
                             rounds=None, vetoes=None) -> FeatureStore   # training AND live
registry.ModelBundle.settings: Settings | None    # runtime only; set by train_models / the registry
registry.ModelRegistry.retrain_unless_fresh(session_factory, *, max_age: timedelta, keep=5)
    -> TrainReport | None
backtesting.run_backtest_with_guard(session_factory, settings, *, since, until, sims=2000,
    ensemble=True, retrain_every=timedelta(days=7), params=None) -> GuardRefreshReport
backtesting.refresh_confidence_guard(session_factory, settings, *, now=None, force=False, runner=None)
    -> GuardRefreshReport          # .ran .written .skipped .path .n_predictions .trusted .as_dict()
backtesting.save_guard_from_report(settings, report) -> (Path | None, list[str], int)
backtesting.priced_finished_matches(session, *, since, until) -> int
backtesting.GUARD_STATE_KEY = "confidence_guard"   # system_state: last refresh attempt
betting.trusted_markets(bundle, settings=None, *, guard=None) -> set[str] | None
betting.run_betting_cycle(...)     # unchanged signature; guard -> decision -> tier sizing, most
    # confident first; bets.extra gains "p_settled" and "stake_fraction"; an unusable executor stops
    # the cycle (+ pause executor_unavailable with autonomy)
doctor.check_confidence_guard(settings) -> Check
telegram.WATCHED_JOB_KINDS, telegram.STUCK_JOB_AFTER   # watchdog alerts in TelegramNotifier.scan_once

# cs2bot.executor.base
EXECUTOR_UNAVAILABLE_PREFIX = "executor unavailable: "; is_executor_unavailable(message) -> bool

# cs2bot.flows
jobs: "confidence_guard_refresh" (train lane), RETRAIN_SKIP_IF_NEWER_THAN = timedelta(hours=2)
supervisor.run_up(..., train_in_background: bool = True, ...)

# cs2bot.backtest
HistoricalData(..., team_demo_stats=(), deep=None); TrainingSet(..., team_demo_stats=(), deep=None)
load_historical_data(session, *, ..., with_deep=False, sharp=None)   # run_db_backtest: with the ensemble
```

API: `GET/PUT /api/control/config` expose and validate `staking_mode`, `confidence_tiers`,
`require_profitable_win`, `use_confidence_guard`, `require_trusted_calibration` (docs/API.md).
Compose / `.env`: `CADDY_IPV4` (172.30.250.10), `FRONTEND_SUBNET` (172.30.250.0/24),
`CS2BOT_API__FORWARDED_ALLOW_IPS` (set by the compose file).

Wiring example (entry points unchanged; the scheduler runs the new job by itself):

```python
from cs2bot.services.backtesting import refresh_confidence_guard
from cs2bot.services.betting import run_betting_cycle

report = run_betting_cycle(session_factory, settings, executor, bundle)  # guard + tiers inside
guard = refresh_confidence_guard(session_factory, settings, force=True)  # = `cs2bot backtest` + guard
print(guard.as_dict())   # {"written": True, "trusted": [...], ...} or {"skipped": "only 12 finished ..."}
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

### Lens C (cloud runtime, deep data, the confidence rule), remaining

- **Until the guard has history, the top tier is out of reach.** The no-history prior keeps half of the
  excess over 75%: raw 0.75-0.95 -> the 1% tier, raw >= 0.95 -> 1.5%, and 92%+ (2%) is impossible. The
  scheduled refresh needs >= 40 finished matches with stored Betclic prices (a few weeks after the first
  start). Deliberate (a confident wrong probability is the costliest error); `decision.use_confidence_guard
  = false` stakes on the raw probabilities instead.
- **The backtest does not apply the guard** (it is fitted on the backtest's own predictions - applying it
  there would be look-ahead), so backtest stakes use raw probabilities while live stakes are more
  cautious. The backtest's feature store still stays at the last retrain cutoff between retrains (the
  live store refreshes every 30 min); match metadata is treated as static pre-match information.
- **Not run here:** the Docker image (no daemon; the CI `docker` job builds it and runs the smoke test,
  which now also exercises the pinned Caddy address and the `Secure` cookie), a real VPS, Let's Encrypt,
  live Betclic / HLTV / Telegram. `docker compose config` renders the new network / env settings.
- **The 2,280 PLN winnings-tax rule in tier mode** compares log-growth with the (possibly negative-EV)
  odds, so a tier stake that would push a win above 2,280 PLN is usually held at the tax-free limit -
  relevant only above a ~100 000 PLN bankroll.
- **The global login limit** (20 failures / 5 min) can still be exhausted by a distributed attacker
  (dashboard locked for 5 min; Telegram unaffected). The per-IP limit now works behind Caddy.
- **CLV** is still measured against Betclic's own close (deep_data's `closing_line_reference` with a
  sharp feed is not wired into settlement; no sharp feed is configured).
- **History backfill is manual** (side container, VPS_SETUP section 9); no scheduler job.
- **Chromium flags** still come from the `cs2bot-chromium` wrapper (cloud_deploy FCR 2, not needed for
  correctness).
- `telegram.enabled` / `autonomy.auto_login` without a token / credentials do not stop the start-up
  (doctor fails, the dashboard alerts, Telegram stays silent - by design the bot keeps working).
- The default backup omits `data/browser` (Betclic session + placement journal); losing the disk loses
  the journal - keep `BACKUP_INCLUDE_DATA=true` + `RCLONE_REMOTE` (checklist below).

### What the owner must verify on the VPS before going live (Lens C)

1. `deploy/linux/deploy.sh --selftest` passes; `deploy/linux/status.sh --doctor` shows no FAIL. The
   "confidence guard" line is a warning until the first weekly backtest - expected.
2. The dashboard opens over HTTPS and logs in. After five wrong passwords from your phone's mobile
   network (not from the PC) the PC can still log in (the rate limit sees real clients). If `deploy.sh`
   fails with "Pool overlaps with other one on this address space", set `FRONTEND_SUBNET` / `CADDY_IPV4`
   in `.env`, `docker compose down` (never `-v`), `deploy.sh`.
3. Dashboard > Settings shows the bet rule: confidence only, odds 1.137 or more, the three stake tiers.
   Decide whether you keep `CS2BOT_DECISION__REQUIRE_PROFITABLE_WIN=true` (recommended: below 1.137 a
   win pays back less than the stake) and `CS2BOT_DECISION__USE_CONFIDENCE_GUARD=true` (recommended:
   stakes start at 1% / 1.5% until the backtest shows the model deserves more).
4. First start on an empty model: the dashboard answers at once; the log shows "models: trained ..."
   before any prediction / bet. After the backfill: `docker compose exec app cs2bot models train`, then
   `docker compose restart app`.
5. Telegram: `docker compose exec app cs2bot telegram test`; send `/status`. During the dry-run days you
   receive "DRY RUN bet ... confidence X% -> stake Y% of the bankroll" messages; a "has not succeeded for
   too long" / "stuck?" message means a lane stopped - restart the app and look at the logs.
6. Stop Chromium once on purpose (e.g. `docker compose exec app pkill -f chrome` during a dry run): the
   next placement may fail, the bot pauses with `executor_unavailable` (Telegram) and resumes itself
   after the next health check.
7. After a few weeks of stored prices: Telegram / `cs2bot doctor` shows the guard written (trusted market
   types listed). Optionally then `CS2BOT_DECISION__REQUIRE_TRUSTED_CALIBRATION=true`.
8. Backups: `BACKUP_INCLUDE_DATA=true` and an off-site `RCLONE_REMOTE`; run `deploy/linux/backup.sh` once
   and check `logs/backup.log`. A restore ends with a manual pause - compare "Moje zakłady" first.
9. Everything from the Windows checklist that is not PC-specific still applies (dry-run screenshots,
   quick-bet mode off, minimum-stake live bets, CONFIRMED links' `teams_swapped`, the server clock - NTP
   is set by `provision.sh`).

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

Lens C: none open (I could edit any file). Applied: `src/cs2bot/config.py` (decision / bankroll / model /
scheduler / API fields, `confidence_tiers_problem`), `src/cs2bot/executor/base.py` + `betclic.py` +
`threaded.py` (unavailable prefix), `src/cs2bot/models/confidence.py`, `src/cs2bot/backtest/*`,
`src/cs2bot/api/main.py` + `configedit.py`, services / flows / CLI, the dashboard, `Dockerfile`,
`docker-compose.cloud.yml`, `deploy/linux/deploy.sh`, docs (PLAN.md, README.md, CLAUDE.md, ARCHITECTURE,
OPERATIONS, VPS_SETUP, WINDOWS_SETUP, API, `.env.example`, STATUS). cloud_deploy FCR 1 (proxy headers),
3 (`.env.example` VPS block) and 4 (docs) and deep_data FCR 1-3 (training store, guard in the decision,
guard written by a job) are done; still suggested: cloud_deploy FCR 2 (`chromium_args` instead of the
wrapper), FCR 5 (a resumable backfill job), deep_data FCR 4 (CLV against `closing_line_reference`).

## New dependency requests

None.

Lens B: none.

Lens C: none (uvicorn's `ProxyHeadersMiddleware` is part of the existing uvicorn dependency).
