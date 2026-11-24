# final_review: implementation report

**Tests:** `.venv/bin/python -m pytest tests -n 2` - 2988 passed, 20 skipped (browser / PostgreSQL), 0 failed; new regression tests: `tests/odds/test_odds_final_review.py` (4), `tests/services/test_services_final_review.py` (8), `tests/services/test_services_final_review_db.py` (3) - 15 passed, 0 failed; `ruff check src tests`, `ruff format --check src tests` and `mypy` clean; `tests/foundation` 37 passed; dashboard (unchanged) `npm run lint` clean, `npm test` 79 passed, `npm run build` OK.

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

## Foundation change requests

None open. Applied in this review (I could edit any file): `src/cs2bot/db/models.py` (unique ledger
index) + migration `0002_ledger_one_entry_per_bet.py`; docs (`docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`,
`docs/dev/STATUS.md`); `deploy/windows/restore-db.ps1`.

Suggested for later (not needed for safety now): `PlacementStatus.EXECUTOR_UNAVAILABLE` (stop / pause the
cycle instead of FAILED rows); `OddsQuote.market_page_name` / `market_group` + executor support for map
tabs; an API action that clears `extra["unconfirmed_not_found"]` after a manual check;
`BookmakerBetRecord.placed_at` resolution (integration request).

## New dependency requests

None.
