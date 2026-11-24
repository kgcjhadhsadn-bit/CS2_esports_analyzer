# engine: implementation report

> Written by the engineer who implemented the package. Reviewers update it when the public API changes.

**Tests:** `.venv/bin/python -m pytest tests/engine -q` - 567 passed, 0 failed, about 60 s single-process, 35 s with `-n 2` (lint/type clean: True)

## Summary

P8a (cs2bot.engine) prices every MarketType, applies the Polish tax / EV / Kelly model, runs the decision rules, computes bankroll state and stakes, keeps the ledger and settles bets from match results. All definition-of-done checks pass: the engine tests, ruff check, ruff format --check and mypy on `src/cs2bot/engine`, and tests/foundation. Only files under src/cs2bot/engine/ and tests/engine/ were touched.

**Tax research:**
- 10% winnings tax (art. 30 ust. 1 pkt 2 PIT): charged on the WHOLE winning. That is the full coupon payout (stake − 12%) × odds, returned stake included. It is not charged on net profit or on the excess over the threshold. (Re-checked in review round 1: the sources below agree; one site that says "winning minus stake" contradicts itself.)
- Threshold: it applies only when a single winning is above 2,280 PLN (art. 21 ust. 1 pkt 6a). The bookmaker withholds it as płatnik (art. 41 ust. 4).
- Rounding: base and tax are rounded to full złoty (art. 63 §1 Ordynacji podatkowej); `betclic.winnings_tax_rounding = "grosz"` switches to grosz precision.
- Worked example: stake 1000 at 3.00 gives 2640 gross, 264 tax, 2376 credited.
- A gov.pl document found during review round 1 mentions a proposal to raise the rate from 10% to 15%. It was not checked further and is not law; `betclic.big_win_tax_rate` covers it if it passes.

**Deliberate decisions:**
- **Sigma curvature guard:** the specified sigma = max(floor, |p_high − p_low| / 2) is kept, plus a guard: sigma = max(|a|, √2·|c|), where a = (p_high − p_low)/2 and c = (p_high + p_low − 2·p_central)/2. It only changes the result when the outcome peaks at the central strength (e.g. "map goes to overtime" in an even match).
- **Sigma in the decision rule:** the pricing sigma belongs to the unconditional p_win; the rule compares `sigma / (1 − p_void − p_push)` with `max_sigma`, the same scale as `p_win_given_settled`.
- **Kelly clipping:** Kelly is clipped to [−1, 1] so it stays monotone in the odds.
- **NaN safety:** decision comparisons are written so NaN fails them, with an `invalid_numbers` reason.
- **Winnings tax in staking:** size_stake keeps the stake at the largest amount whose winning stays tax-free. It goes above that only if the taxed stake has a higher expected log-growth.
- **Loss guards count ALL open stakes** (stricter than the brief's "open stakes placed today"): bets are placed up to 48 h ahead, so yesterday's open bets usually settle today. With this, realized loss of a day (week) can never exceed the limit, whatever loses. Limits are rounded DOWN to the grosz.
- **Loss-limit pause:** each limit pauses only once per day or week, so the owner's manual resume is not undone by the next scheduler tick (the guards still block new stakes until the period rolls over).
- **Balance and day-start bankroll:**
  - `balance` = ledger balance minus stakes of open bets not yet debited (`reserved_stakes`).
  - `day_start_bankroll` = bankroll_reference − realized P&L today. Deposits and withdrawals count as if made at the start of the period, so the first day's limit isn't 0.
- **Reconciliation is a safety control:** any non-zero difference is booked; a difference beyond the tolerance is flagged AND pauses trading (`pause_on_flag=True`), because every stake and limit is a fraction of the ledger balance. A shortfall that unconfirmed placements can explain is deferred instead (nothing booked, nothing flagged).
- **Settlement never guesses:** missing or inconsistent data gives `UNKNOWN`; a top-fragger tie is `UNKNOWN` too (dead-heat rules are a partial win, which `SettlementOutcome` cannot express).

**Integration helpers (beyond the spec):** price_quotes_report, ev.price_all, ev.min_odds_for_ev, decision.min_acceptable_odds / load_match_decision_info / load_already_bet_keys / bet_key, bankroll.size_decision / size_decisions / reserve_stake / loss_limit_status, ledger.apply_placement / settle_bet / bet_money_fields / record_deposit / record_withdrawal, settlement.resolve_bet / explain_outcome / spec_from_key / bet_status_for.

**Research sources:**
- [Bezprawnik: jak rozliczyć wygrane u legalnego bukmachera](https://bezprawnik.pl/jak-rozliczyc-wygrane-u-legalnego-bukmachera/)
- [PITax: podatek od wygranej](https://www.pitax.pl/wiedza/poradnik-rozliczenia/podatek-od-wygranej/)
- [Infor: wygrana w zakładach bukmacherskich a rozliczenie podatku](https://ksiegowosc.infor.pl/podatki/5572652,Wygrana-w-zakladach-bukmacherskich-a-rozliczenie-podatku.html)
- [Infor: opodatkowanie wygranej w zakładach bukmacherskich](https://ksiegowosc.infor.pl/podatki/podatki-osobiste/pit/748455,Opodatkowanie-wygranej-w-zakladach-bukmacherskich.html)
- [Art. 63 Ordynacji podatkowej (arslege)](https://arslege.pl/ordynacja-podatkowa/k38/a15954/)
- [podatki.gov.pl: stawki i limity](https://www.podatki.gov.pl/pozostale/gry-hazardowe/stawki-i-limity/)
- [money.pl: fiskus może zabrać 10 proc. wygranej](https://direct.money.pl/artykuly/porady/wygrana-nie-zawsze-trafia-w-calosci-do-zwyciezcy-fiskus-moze-zabrac-10-proc)

## Public API

All functions only `session.flush()`; they never commit (the caller owns the transaction). Import the submodules directly (`from cs2bot.engine import pricing, ev, decision, bankroll, ledger, settlement`). Nothing is re-exported in `__init__`.

`TaxSource = Settings | BetclicSettings | TaxModel` wherever a `settings` argument is described as a tax source.

## cs2bot.engine.pricing
- `class UnpriceableMarket(ValueError)`: the prediction cannot support this outcome. Never bet such outcomes.
- `@dataclass(frozen=True) class OutcomeSplit(p_win: float, p_push: float = 0.0, p_void: float = 0.0)` with property `.p_lose`
- `@dataclass(frozen=True) class PricingReport(priced: list[tuple[OutcomeProbability, OddsQuote]], skipped: list[tuple[OddsQuote, str]])`
- `market_rules(include_overtime_in_totals: bool | None = None, h2h_tie: str | None = None, betclic: BetclicSettings | None = None) -> tuple[bool, Literal["push", "lose"]]`
  - Resolves the bookmaker settlement rules shared by pricing and settlement. `None` falls back to `betclic` (default `get_settings().betclic`): `totals_include_overtime` and `h2h_tie_rule`.
  - Raises ValueError for an `h2h_tie` other than "push" / "lose".
- `price_outcome(spec: MarketSpec, selection: str, prediction: MatchPrediction, *, sigma_floor: float, include_overtime_in_totals: bool | None = None, h2h_tie: str | None = None, betclic: BetclicSettings | None = None) -> OutcomeProbability`
  - Probabilities are unconditional (p_win / p_push / p_void).
  - The two rule arguments default to the Betclic settings (see `market_rules`).
  - `include_overtime_in_totals=False` prices TOTAL_ROUNDS, TEAM_TOTAL_ROUNDS, TOTAL_ROUNDS_ODD_EVEN and ROUND_HANDICAP on regulation time (an overtime map counts as 12-12).
  - Sigma: with both low and high, `max(sigma_floor, |p_h-p_l|/2, |p_h+p_l-2p_c|/sqrt2)`. With one scenario, `|p_s - p_c|`. With none, the `fallback_sigma` delta method.
- `price_core(spec, selection, core: PredictionCore, *, fmt: MatchFormat, include_overtime_in_totals=None, h2h_tie=None, betclic=None) -> OutcomeSplit`: single scenario, no sigma (backtests).
- `price_quotes(prediction: MatchPrediction, quotes: Iterable[OddsQuote], *, sigma_floor: float, include_overtime_in_totals=None, h2h_tie=None, betclic=None) -> list[tuple[OutcomeProbability, OddsQuote]]`: skips unpriceable quotes and logs the reason at INFO. One broken quote never aborts the batch.
- `price_quotes_report(...same args...) -> PricingReport`: also returns the skipped quotes with reasons (for the dashboard).
- `fallback_sigma(p_win: float, prediction: MatchPrediction) -> float`
- Correct-score selections (`CORRECT_MAP_SCORE`, `MAP_CORRECT_SCORE`) must be a possible final score of the format / a possible final CS2 map score, even when a malformed pmf carries a key for them (settlement leaves such a bet UNKNOWN) **(review round 2)**.
- Shared with settlement: `compare_to_line(value: float, line: float) -> int` (+1 / 0 / −1), `rounds_value(team1_rounds: int, team2_rounds: int, *, include_overtime: bool, team: str | None = None) -> int`, `is_final_round_score(team1_rounds: int, team2_rounds: int) -> bool`, `final_map_scores(fmt: MatchFormat) -> set[tuple[int, int]]`.
- Constants: `REGULATION_ROUNDS = 24`, `ROUNDS_TO_WIN_REGULATION = 13`, `FIRST_OVERTIME_WIN = 16`, `PMF_TOLERANCE = 0.02` (pmfs further than this from summing to 1 are unpriceable; others are renormalised).

### Conventions P6 must produce (verified against the real `build_match_prediction` in review round 1)
- Slot distributions are conditional on that map being played.
- `player_kills_by_scope` keys:
  - `"map:k"`: the slot's `players` dict is used first. Conditional on map k being played.
  - `"maps:1,2"`: conditional on ALL listed maps being played. The bet is void with probability 1 − p_played(last listed map).
  - `"match"`: unconditional.
- `top_fragger[scope]` maps player id to probability, with dead heat already applied. A team-specific market reads `top_fragger["<scope>|team1"]` / `"<scope>|team2"`.
- `p_team1_race_to[str(N)]` = P(team1 is first to N). For N > 13, P(neither reaches N) comes from `p_neither_race_to[str(N)]` when present, else from the slot's `score_pmf` (both give the same number); that case is void.
- `p_team1_pistol` keys are "1" / "13". `p_team1_half` and `p_half_draw` keys are "1" / "2"; selecting team2 needs both. Half 2 = rounds 13-24 actually played.

## cs2bot.engine.ev
- `@dataclass(frozen=True) class TaxModel(stake_tax_rate: Decimal, big_win_threshold: Decimal, big_win_rate: Decimal, refund_stake_tax: bool = True, win_tax_rounding: Literal["zloty", "grosz"] = "zloty")`
  - `TaxModel.from_settings(settings: TaxSource) -> TaxModel`. The promo sets stake_tax_rate to 0; `refund_stake_tax` / `win_tax_rounding` come from `betclic.void_refunds_stake_tax` / `winnings_tax_rounding`.
  - Property `.refund_loss_rate`: share of the stake a refund costs (0 with full refunds).
- `@dataclass(frozen=True) class PayoutBreakdown(stake, stake_tax, stake_tax_rate, net_stake, odds, gross_win, win_tax, net_payout)`: all Decimal, with property `.profit`.
- `effective_odds(odds: Decimal, *, tax_rate: Decimal) -> Decimal`: `(1 - t) * odds`, unrounded.
- `stake_tax(stake: Decimal, settings: TaxSource) -> Decimal`: grosz-rounded; this is the value for `bets.tax`.
- `winnings_tax(gross_win: Decimal, settings: TaxSource) -> Decimal`: 10% of the whole winning if it is above 2280.
- `refund_amount(stake: Decimal, settings: TaxSource, *, tax: Decimal | None = None) -> Decimal`: what a void / push returns (the full stake, or stake − tax when `void_refunds_stake_tax` is off).
- `payout_breakdown(stake: Decimal, odds: Decimal, settings: TaxSource, *, tax: Decimal | None = None) -> PayoutBreakdown`
- `net_payout(stake: Decimal, odds: Decimal, settings: TaxSource) -> Decimal`: credited on a win, for a GROSS stake.
- `big_win_stake_limit(odds: Decimal, settings: TaxSource) -> Decimal | None`: largest stake whose winning stays ≤ 2280; None if the rate is 0.
- `expected_value(outcome: OutcomeProbability, odds: Decimal, settings: TaxSource, stake: Decimal | None = None) -> float`
  - Profit per 1 unit of gross stake: `p_w*(o_eff-1) - p_l - p_refund*c` (c = 0 with full refunds).
  - With `stake`: exact grosz rounding and the winnings tax.
- `min_odds_for_ev(outcome: OutcomeProbability, settings: TaxSource, *, min_ev: float = 0.0) -> Decimal | None` **(new in review round 1)**: lowest odds, rounded UP to 0.001, at which the per-unit EV is still ≥ min_ev. None if the outcome cannot win.
- `kelly_fraction(outcome: OutcomeProbability, odds: Decimal, settings: TaxSource) -> float`: `f* = (b p_w - p_l)/(b (p_w+p_l))`, `b = o_eff - 1`, clipped to [-1, 1]. Positive exactly when EV > 0.
- `kelly_for_net_odds(p_win: float, p_lose: float, b: float, *, p_refund: float = 0.0, refund_loss: float = 0.0) -> float`
- `expected_log_growth(p_win: float, p_lose: float, b: float, fraction: float, *, p_refund: float = 0.0, refund_loss: float = 0.0) -> float`
- `price(outcome: OutcomeProbability, quote: OddsQuote, settings: TaxSource) -> PricedOutcome`: effective_odds, per-unit ev, kelly_full.
- `price_all(pairs: Iterable[tuple[OutcomeProbability, OddsQuote]], settings: TaxSource) -> list[PricedOutcome]`
- Constant `ODDS_STEP = Decimal("0.001")`.

## cs2bot.engine.decision
- `class ReasonCode(StrEnum)`: MARKET_DISABLED, SELECTION_UNAVAILABLE, ODDS_OUT_OF_RANGE, ODDS_STALE, OUTSIDE_TIME_WINDOW, LOW_PROBABILITY, LOW_EV, HIGH_SIGMA, LOW_DATA_COMPLETENESS, WEAK_EVENT_LINK, CONTEXT_VETO, WAITING_FOR_VETO, DUPLICATE, PRICING_MISMATCH, INVALID_NUMBERS, MATCH_NOT_SCHEDULED.
  - Each reason string is `"<code>: <detail>"`. The waiting-for-veto detail contains "waiting for veto".
- `class MatchDecisionInfo(BaseModel)` (pydantic):
  - `match_id: int`
  - `scheduled_at: datetime | None`
  - `match_status: MatchStatus | None = None` (None = no status check; anything but SCHEDULED / UNKNOWN is rejected)
  - `data_completeness: float | None = None`
  - `link_confidence: float | None = None`
  - `link_status: LinkStatus = LinkStatus.UNLINKED`
  - `veto_known: bool = False`
  - `context_veto: bool = False`
  - `context_veto_reason: str | None = None`
  - `already_bet_keys: set[str] = set()`
- `evaluate(priced: PricedOutcome, *, match: MatchDecisionInfo, settings: DecisionSettings, betclic: BetclicSettings, now: datetime) -> BetDecision`
  - Lists ALL failing reasons; `accepted` only when there are none; stake 0. Raises ValueError for a naive `now`.
  - "Waiting for veto" applies to `spec.is_map_scoped`, and to any spec with `map_number` or `maps_scope` (player props on given maps).
- `evaluate_all(priced: Iterable[PricedOutcome], *, match, settings, betclic, now) -> list[BetDecision]`
- `min_acceptable_odds(priced: PricedOutcome, *, settings: DecisionSettings, betclic: BetclicSettings, odds_tolerance: float) -> Decimal` **(new in review round 1)**
  - Value for `BetOrder.min_acceptable_odds`: the highest of `quote odds × (1 − odds_tolerance)`, the odds at which the EV equals the market's `min_ev`, and `settings.min_odds`; rounded UP to 0.001.
  - Pass `settings.executor.odds_tolerance`. A price drop that would make the bet fail the EV rule is never accepted, whatever the tolerance.
- `bet_key(match_id: int, spec_key: str, selection_key: str) -> str`: returns `"<match_id>|<spec_key>#<selection_key>"`.
- `load_already_bet_keys(session: Session, match_id: int, *, include_dry_run: bool = True) -> set[str]`: all bets on the match except FAILED / CANCELLED. Pass `include_dry_run=False` when betting for real, otherwise earlier dry runs block the real bets.
- `load_match_decision_info(session: Session, *, match_id: int, bookmaker_event_id: int | None = None, prediction: MatchPrediction | None = None, now: datetime | None = None, include_dry_run_bets: bool = True) -> MatchDecisionInfo`
  - Start time: the earlier of `matches.scheduled_at` and the linked event's `starts_at`.
  - Match status from `matches.status`; LIVE as soon as the linked bookmaker event is flagged in-play (**review round 1**).
  - Completeness and veto_known come from the prediction if given, else from the DB.
  - The link counts only if `event.match_id == match_id`.
  - Context veto comes from active, unexpired `context_flags` rows plus the prediction's flags.
- `thresholds_for(market_type: MarketType, settings: DecisionSettings) -> Thresholds` (with `.min_probability`, `.min_ev`, `.max_sigma`)
- `settled_sigma(outcome: OutcomeProbability) -> float`, `is_slot_specific(spec: MarketSpec) -> bool`, `reason_code(reason: str) -> str`, `make_reason(code: ReasonCode, detail: str) -> str`
- Constants: `BETTABLE_LINK_STATUSES`, `BETTABLE_MATCH_STATUSES`, `NON_BLOCKING_BET_STATUSES`, `MAX_FUTURE_ODDS_SKEW_S = 300`.

## cs2bot.engine.bankroll
- `compute_state(session: Session, settings: Settings, *, now: datetime, bankroll_settings: BankrollSettings | None = None) -> BankrollState`
  - Initialises an empty ledger with a DEPOSIT of starting_bankroll (dashboard overrides honoured). Call `ledger.ensure_initialized(..., opening_balance=...)` first to start from the real Betclic balance.
  - Dry-run bets are excluded.
  - Days and weeks are Europe/Warsaw (`settings.timezone`) and DST-aware; `day_start` / `week_start` are returned as UTC instants.
  - `balance` = ledger balance minus open stakes not yet debited (`reserved_stakes`).
- `size_stake(decision: BetDecision, state: BankrollState, settings: BankrollSettings, betclic: BetclicSettings) -> tuple[Decimal, list[str]]`
  - A 0 stake comes with reasons, e.g. "halted: …", "bets_per_day: …", "no_edge: …", "no_bankroll: …", "match_exposure: nothing left…", "daily_loss_guard: …", "weekly_loss_guard: …", "balance: …", "below_min_stake: …", "not_accepted: …".
  - A positive stake comes with info notes "capped: <cap> -> X PLN (…)".
  - Pure function: the state must already contain every bet created so far.
- `apply_stake(decision: BetDecision, state: BankrollState, stake: Decimal, notes: list[str]) -> BetDecision`
  - Returns a copy with stake set and `accepted = accepted and stake > 0`.
  - A positive stake files the notes under `BetDecision.notes`; a zero stake appends them to `BetDecision.reasons`.
  - `kelly_fraction_used` = stake / bankroll_reference.
- `size_decision(decision, state, settings, betclic) -> BetDecision`: size_stake + apply_stake. A rejected decision is returned unchanged.
- `size_decisions(decisions: Iterable[BetDecision], state: BankrollState, settings: BankrollSettings, betclic: BetclicSettings) -> tuple[list[BetDecision], BankrollState]`: sizes a batch in order, reserving each stake before the next, so the batch as a whole respects every cap.
- `reserve_stake(state: BankrollState, match_id: int, stake: Decimal) -> BankrollState`: the state after a new PENDING bet (equals what compute_state returns once the row exists).
- `total_open_stakes(state: BankrollState) -> Decimal`: every stake at risk, whenever it was placed.
- `check_loss_limits(session: Session, state: BankrollState, settings: Settings | BankrollSettings, *, actor: str = "system", now: datetime | None = None) -> bool`
  - True while the daily or weekly realized-loss limit is reached.
  - Pauses through `ledger.pause_trading(...)` once per period; it never resumes. An existing pause keeps its reason and gets the limit appended **(review round 2)**.
- `loss_limit_status(state: BankrollState, settings: BankrollSettings) -> LossLimitStatus`. Fields: daily_limit, weekly_limit (rounded DOWN to the grosz), realized_loss_today, realized_loss_week, daily_allowance, weekly_allowance, daily_hit, weekly_hit, open_stakes.
- Constants: `OPEN_STATUSES`, `SETTLED_STATUSES`, `COUNTED_STATUSES`, `LOSS_LIMIT_STATE_KEY = "loss_limit_pauses"`.

## cs2bot.engine.ledger
- `class LedgerConflictError(ValueError)`: the requested entry contradicts the ledger (payout after refund, re-grading a settled bet, a placement result for a bet that is already placed). Nothing is written.
- `ensure_initialized(session: Session, settings: Settings | BankrollSettings, *, at: datetime | None = None, opening_balance: Decimal | None = None) -> BankrollLedger | None`: opening DEPOSIT of `opening_balance` (the balance read at Betclic; **new in review round 1**) or of `starting_bankroll`.
- `current_balance(session: Session) -> Decimal`: `balance_after` of the highest-id entry; 0.00 if the ledger is empty.
- `record_stake(session: Session, bet: Bet, *, at: datetime | None = None) -> BankrollLedger | None`: debits the gross stake; idempotent; dry runs return None.
- `record_payout(session: Session, bet: Bet, payout: Decimal, *, at: datetime | None = None) -> BankrollLedger | None`
  - Idempotent. A payout of 0 or a dry run returns None.
  - If the stake was never recorded, it records the stake first. Raises LedgerConflictError if the bet was refunded.
- `record_refund(session: Session, bet: Bet, *, amount: Decimal | None = None, at: datetime | None = None) -> BankrollLedger | None`: refunds the debited stake (or `amount`); idempotent; returns None if nothing was debited. Raises LedgerConflictError if the bet was paid out.
- `record_adjustment(session, amount: Decimal, note: str, *, at=None) -> BankrollLedger`
- `record_deposit(session, amount: Decimal, note: str = "deposit", *, at=None) -> BankrollLedger`
- `record_withdrawal(session, amount: Decimal, note: str = "withdrawal", *, at=None) -> BankrollLedger`
- `apply_placement(session: Session, bet: Bet, result: PlacementResult, settings: TaxSource) -> BetStatus` **(new in review round 1)**
  - Applies the executor's answer using `cs2bot.domain.betting.bet_status_after_placement`.
  - PLACED: sets placed_odds (reported odds, else requested), placed_at (`result.at`, or the attempt time for a bet confirmed later), external_bet_id, tax, potential_payout and `extra["stake_tax_rate"]`, then debits the stake. A row created as a dry run that was really placed becomes a real bet (logged as an error).
  - DRY_RUN: marks the row `dry_run`; the ledger is not touched.
  - PLACING (UNCERTAIN): nothing is debited; the stake stays reserved and counts as exposure. The attempt time is kept in `extra["placement_attempted_at"]`. Call again with a PLACED or FAILED result once the bet history has been checked. A dry-run row becomes a real one here (logged as an error): the executor answers UNCERTAIN in dry-run mode only when money may have moved (quick-bet click), and dry-run rows count for no exposure, guard, duplicate check or reconcile deferral **(review round 2)**.
  - FAILED (everything else): no money at risk; a stake that was already debited is refunded.
  - Only PENDING / PLACING bets take a result. Replaying the same result is a no-op; any other transition raises LedgerConflictError.
- `@dataclass(frozen=True) class ReconcileResult(observed_balance: Decimal, ledger_balance: Decimal, difference: Decimal, tolerance: Decimal, flagged: bool, entry_id: int | None, at: datetime, deferred: bool = False, paused: bool = False)`
- `reconcile(session: Session, observed_balance: Decimal, *, tolerance: Decimal, actor: str = "system", at: datetime | None = None, pause_on_flag: bool = True) -> ReconcileResult`
  - Writes a RECONCILIATION entry for any non-zero difference.
  - Flags when |difference| > tolerance: audit log, warning, and (**review round 1**) a trading pause with the numbers in the reason unless `pause_on_flag=False`. An existing pause stays and gets the mismatch appended to its reason (**review round 2**; it used to be dropped, so resuming after a loss-limit pause hid the mismatch). `paused` is True only when this call paused a running bot.
  - Deferred (**review round 1**): a shortfall of at most the stakes of un-debited PENDING / PLACING bets (+ tolerance) is neither booked nor flagged; resolve those bets and reconcile again.
  - Stores the result in `system_state["last_reconciliation"]`. Raises ValueError for a non-finite balance.
- `settle_amounts(bet: Bet, outcome: SettlementOutcome | Literal["won","lost","void","push"] | str, settings: TaxSource) -> tuple[Decimal, Decimal]`: returns (payout, pnl), consistent with ev.net_payout. Void and push refund the gross stake (or stake − tax when `void_refunds_stake_tax` is off). UNKNOWN raises ValueError.
- `settle_bet(session: Session, bet: Bet, outcome, settings: TaxSource, *, at: datetime | None = None) -> tuple[Decimal, Decimal]`
  - Sets status, payout, pnl, settled_at, `extra["settled_outcome"]` and writes the ledger entries in one step.
  - Real bets must be PLACED. Dry-run bets keep status DRY_RUN and never touch the ledger (paper P&L is stored on the row).
  - Idempotent for the same outcome (void and push count as the same refund); a different outcome raises LedgerConflictError.
- `pause_trading(session: Session, reason: str, *, actor: str = "system") -> bool` **(new in review round 2)**: pauses a running bot (returns True), or appends `reason` to the reason of an existing pause (returns False; a reason already present is not repeated; the text is capped at `MAX_PAUSE_REASON_CHARS = 2000`, keeping its start and the newest reason). Used by `reconcile` and `bankroll.check_loss_limits`, so safety controls never hide each other.
- `bet_money_fields(stake: Decimal, odds: Decimal, settings: TaxSource) -> dict[str, object]`: returns `{"tax", "potential_payout", "extra": {"stake_tax_rate": "0.12"}}` for a new Bet row.
- `bet_stake_tax(bet: Bet, settings: TaxSource) -> Decimal`: uses `extra["stake_tax_rate"]`, else a positive `bets.tax`, else settings.
- `is_dry_run(bet: Bet) -> bool`
- Constants: `STAKE_TAX_RATE_KEY`, `PLACEMENT_STATUS_KEY`, `PLACEMENT_ATTEMPTED_AT_KEY`, `SETTLED_OUTCOME_KEY`, `MAX_PAUSE_REASON_CHARS`.

## cs2bot.engine.settlement
- `resolve_outcome(spec: MarketSpec, selection: str, actuals: MatchActuals, *, include_overtime_in_totals: bool | None = None, h2h_tie: str | None = None, betclic: BetclicSettings | None = None, player_teams: Mapping[int, TeamSlot] | None = None) -> SettlementOutcome`
  - Every market type, with exactly the void / push conventions of `pricing.py`; the two rule arguments default to the Betclic settings like pricing.
  - Map not played → VOID. Result exactly on an integer line → PUSH. Forfeited / cancelled match → VOID.
  - UNKNOWN, never a guess, for: an unfinished or inconsistent series, an impossible CS2 map score, missing or inconsistent round history (pistol, half, race), missing kill statistics, a spec pricing would not price, a top-fragger tie, a team-restricted top-fragger market without `player_teams`.
- `explain_outcome(...same args...) -> Resolution`: the outcome plus the reason for a VOID / PUSH / UNKNOWN.
- `@dataclass(frozen=True) class Resolution(outcome: SettlementOutcome, reason: str = "")`
- `resolve_bet(bet: Bet, actuals: MatchActuals, *, include_overtime_in_totals=None, h2h_tie=None, betclic=None, player_teams=None) -> Resolution`: rebuilds the spec from `bets.spec_key`. A bet on another match or with an unreadable key is UNKNOWN.
- `spec_from_key(key: str) -> MarketSpec`: inverse of `MarketSpec.key()` (`player_name` is not part of the key).
- `bet_status_for(outcome: SettlementOutcome) -> BetStatus | None`: WON / LOST / VOID (push → VOID); None for UNKNOWN.
- Constants: `HALF_ROUNDS = 12`, `TEAM_SIZE = 5`.

## Bet lifecycle for the integration layer
1. **Go-live:** `ledger.ensure_initialized(session, settings, opening_balance=executor.get_balance())` so the ledger starts from the real account.
2. **Decide and size:** `bankroll.size_decisions(...)` for a batch, or recompute `compute_state` after each bet row. Use `load_already_bet_keys(include_dry_run=False)` when betting for real.
3. **Create the row:** status PENDING, `dry_run=` the executor's real dry-run state (**set it explicitly**: the column defaults to True, and a dry-run row is invisible to compute_state, so a real PENDING bet created with the default would not count as exposure until the executor answers), `min_acceptable_odds = decision.min_acceptable_odds(...)`, `**ledger.bet_money_fields(...)`. Commit before calling the executor.
4. **Place:** check `db.state.is_trading_halted`, call the executor, then `ledger.apply_placement(session, bet, result, settings)`.
5. **Unconfirmed placements:** look the bet up in the bookmaker's history and call `apply_placement` again with PLACED or FAILED. Never retry them.
6. **Settle:** `settlement.resolve_bet(bet, actuals)`; WON / LOST / VOID / PUSH go to `ledger.settle_bet`. UNKNOWN stays open and is settled from the bookmaker's history.
7. **Reconcile** after placements are applied and finished bets are settled: `ledger.reconcile(session, executor.get_balance(), tolerance=settings.executor.reconcile_tolerance)`. `deferred` means "resolve the unconfirmed bets first"; `paused` means the owner has to look.
8. **Every cycle:** `bankroll.check_loss_limits(session, state, settings)`.
9. **Serialization:** run compute_state → size → create bet one at a time, so the caps see the previous bets.

## Known limitations

- Void/push refunds assume Betclic returns the full gross stake, including the 12% stake tax. No published rule confirms this; set `betclic.void_refunds_stake_tax = False` if the first void bet shows otherwise (reconcile flags it).
- The 10% winnings tax is rounded to full złoty per art. 63 §1 Ordynacji podatkowej. If Betclic computes it to the grosz, set `betclic.winnings_tax_rounding = "grosz"`.
- Payouts are computed as (stake − grosz-rounded stake tax) × odds. With the default 0.10 PLN stake step the 12% tax is not a whole number of grosze, so a payout can differ from Betclic's by 1 grosz; reconcile absorbs it silently.
- Betclic's "gra bez podatku" promo is reported to have conditions (coupon type; one betting guide mentions a reduced 6% rate on singles instead of 0%). This was not verified. The owner must check that the promo applies to singles before setting `tax_free_promo`; a partial rate is set with `betclic.tax_rate`.
- kelly_fraction ignores the stake-dependent winnings tax. size_stake handles the 2280 PLN threshold by comparing two candidate stakes on expected log-growth (float approximation that ignores the złoty rounding).
- PLAYER_KILLS_H2H treats the two players' kill pmfs as independent. This ignores the positive correlation from the shared round and map count and the negative correlation from the score line.
- Top-fragger probabilities are used as given, so P6 must apply the dead-heat rule; whether Betclic settles ties by dead heat is unverified. Settlement leaves ties UNKNOWN.
- Race to N rounds with N > 13 is unpriceable today: the simulator publishes `p_team1_race_to` only for N ≤ 13. Pricing is ready for it (tested).
- Player markets on `maps:1,2,3` of a BO3 (a scope that is not always played) are unpriceable today: the simulator publishes multi-map scopes only for always-played maps.
- Team-restricted top-fragger markets settle locally only when the caller passes `player_teams` (`MapActual.player_kills` does not say which team a player is on).
- Regulation-only totals (`totals_include_overtime = False`) assume MR12: 24 regulation rounds and 12 per team when the map goes to overtime.
- The fallback sigma when low/high are missing is a delta-method approximation that is conservative for strength-insensitive markets.
- compute_state takes no locks. Concurrent betting jobs must be serialized by the caller, otherwise caps and guards can be exceeded.
- Ledger idempotency (one STAKE / PAYOUT / REFUND per bet) is enforced in code, not by a database constraint.
- reconcile books a payout the bookmaker has credited before the local settlement as a RECONCILIATION entry; the later local settlement then double-counts it until the next reconcile corrects it. Settle before reconciling.
- The loss guards count every open stake, so total open exposure can never exceed 5% of the bankroll (the daily limit). With the 2% per-bet cap that is at most two full-size bets plus a half-size one open at a time.

## Foundation change requests

- db.models.BankrollLedger: add a partial unique index on `(bet_id, kind)` for rows with a bet id, so the one-entry-per-bet-and-kind rule holds even if two sessions write at once.
- db.models.Bet.dry_run (review round 2): drop `default=True` so every insert has to state it. Dry-run rows are excluded from exposure, the loss guards and the reconcile deferral; a real PENDING row created with the default is invisible to `compute_state` until the executor answers (`apply_placement` then corrects it).
- config.BankrollSettings.stake_step: change the default from 0.10 to 0.50 (or 1.00). 12% of the stake is then a whole number of grosze, so the stake tax and the payout have no rounding ambiguity against Betclic.
- domain.results.MatchActuals: add `player_teams: dict[int, TeamSlot]` (filled by `datasets.load_match_actuals` from the lineups). Settlement already reads it when present, so team-restricted top-fragger markets settle locally without an extra argument.
- models.match_engine (P6): publish `p_team1_race_to` and `p_neither_race_to` for overtime targets (N > 13) in `MapSlotPrediction`; the field exists and pricing uses it. Optionally publish conditional multi-map kill scopes such as `maps:1,2,3` for BO3.
- docs/ARCHITECTURE.md (P8 section): add `engine.settlement` (`resolve_outcome`, `resolve_bet`), `ledger.apply_placement`, `ledger.settle_bet`, `decision.min_acceptable_odds` and the reconcile pause / deferral behaviour.
- api (P10): a "record deposit / withdrawal" action in the dashboard (calling `ledger.record_deposit` / `record_withdrawal`) would let the owner book transfers before the next reconciliation flags them and pauses the bot.

## New dependency requests

- (none)

## Review round 1

The round started from an interrupted review: `settlement.py` and the settings adoption existed but were untested, and this report was stale. Everything was re-read, re-derived and run.

**Verified (no change needed):**
- EV, Kelly and tax formulas, recomputed by hand and compared with the code: EV 0.1264 and f* 0.3098 for p = 0.8 at 1.60; the same f* with half the mass voided; 2000 PLN at 1.60 → 2816 gross, 282 tax, 2534 credited; tax-free limit 1619.32 PLN at 1.60; promo and net-refund variants.
- The 10% tax rule (whole winning above 2,280 PLN) against several Polish sources.
- Pricing of every market type against settlement, exactly (see below), in BO1 / BO2 / BO3 / BO5, with and without overtime in the totals.
- Pricing against the real `build_match_prediction` output: scope keys, conditional `map:k` and unconditional `match` kill pmfs, top-fragger keys and dead-heat sums, race-to keys, BO2 draw, low / high scenarios.
- 60 000 random malformed specs / selections: pricing only ever raises `UnpriceableMarket`, settlement never raises.
- Staking caps under a property test (400 random states) and a three-week simulation through the database (3 seeds in the suite, 120 more run once): no cap, guard or limit was exceeded, and the ledger matched independent bookkeeping after every step, across the spring DST change.
- The 2,280 PLN winnings-tax branch of `size_stake`, judged with the exact payout (grosz stake tax, złoty-rounded 10% tax) on 600 random large-bankroll cases: the stake never exceeds a cap, and its expected log-growth is positive and never below that of the tax-free limit. The branch only matters for bankrolls above roughly 8,600 PLN at the default caps.

**Issues found and fixed:**
1. *Major:* `settlement.py` had no tests and the pricing / settlement consistency test required by the addendum was missing. Added `test_engine_settlement.py` (189 tests): every market type on hand-built results, refusal to guess, spec-key round trips, and a consistency test in which the prediction is built from a sample of simulated series, so priced P(win) / P(push) / P(void) must equal the settled frequencies to 1e-9. Five deliberate mutations of pricing or settlement all make it fail.
2. *Major:* `bet_status_after_placement` (addendum) was not adopted anywhere. Added `ledger.apply_placement`, which maps the executor result to the bet status and the ledger in one step.
3. *Major:* a flagged balance mismatch was only logged and written to the audit log, while the ledger followed the observed balance and betting continued. A misread balance (for example ten times too high) would have multiplied every stake and limit. `reconcile` now pauses trading on a flagged mismatch and rejects non-finite balances.
4. *Major:* an unconfirmed placement that Betclic had accepted made `reconcile` book the missing stake as a mismatch, and the later confirmation debited it a second time. Such shortfalls are now deferred.
5. *Major:* nothing computed `BetOrder.min_acceptable_odds`, so an executor tolerance could accept a price at which the bet no longer passes the EV rule. Added `ev.min_odds_for_ev` and `decision.min_acceptable_odds`.
6. *Minor:* loss limits were rounded half-up to the grosz, so a day's loss could exceed 5% of the day-start bankroll by up to 0.005 PLN. They are rounded down now (found by the simulation).
7. *Minor:* a placement confirmed later would have been dated at the confirmation time; it now keeps the attempt time, so it counts for the day it was really placed (found by the simulation).
8. *Minor:* a linked Betclic event that is already in-play did not block betting while our match row still said "scheduled". `load_match_decision_info` now reports LIVE.
9. *Minor:* `settle_bet` refused "void" after "push" (and the reverse) as a re-grade although both are the same refund.
10. *Minor:* an unreadable `extra["stake_tax_rate"]` on a bet raised at settlement; it now falls back to `bets.tax` / the settings with a warning.
11. *Minor:* the ledger could only start from the configured `starting_bankroll`; `ensure_initialized(opening_balance=...)` starts it from the real balance.
12. *Minor:* this report's Public API section was stale (settlement, rule defaults, notes vs reasons, guards). Rewritten.

**Tests added:** test_engine_settlement.py, test_engine_pricing_match_engine.py, test_engine_lifecycle.py, engine_series.py (helper), plus new cases in the ledger, decision, EV, bankroll and pipeline test modules. 552 tests in total (304 before this round).

**Remaining concerns:**
- Betclic's real settlement rules are unverified: refund of the stake tax on void bets, rounding of the winnings tax, overtime in round totals, head-to-head ties, dead heats for top fragger, second-half definition. All are settings or documented assumptions; the first real settlements should be compared with them.
- The reconcile pause is new behaviour for the integration layer: the first reconciliation of an account whose balance differs from `starting_bankroll` pauses the bot unless the ledger is opened with `opening_balance`.
- Head-to-head kill markets assume independent players (see Known limitations). By a rough estimate the bias is small for single maps and conservative for favourites over several maps; it has not been measured on real data.
- `kelly_fraction_used` on a `BetDecision` is the fraction actually staked after caps, not the configured Kelly multiplier; the integration layer should store it under that meaning.

## Review round 2

Focus: the pricing <-> simulator contract end to end, settlement vs pricing for every market type, bankroll guards under adversarial sequences, rounding. Everything was re-read and run; round 1's work was not repeated.

**Verified (no change needed):**
- Settlement against the REAL map simulator, not a second hand-written one: `MapSimulator.simulate(..., record_rounds=True)` plays are rebuilt as finished maps (regulation rounds from the simulator's log, overtime order reconstructed to reproduce the simulator's own race records) and settled one by one. Pricing the slot the simulator published equals the settled frequencies to 1e-9 for every map-slot market type (map winner, round handicap, totals, team totals, odd/even, map correct score, overtime, pistols 1 / 13, halves 1 / 2 incl. draws, race to 1..16 incl. voids), with and without overtime in the totals, as simulated and after `reweighted` (the match engine's calibration). Two deliberate settlement mutations (half 2 including overtime, pistol round 13 read as round 1) make it fail.
- Coherence of the real `build_match_prediction` output that pricing relies on, BO1 / BO2 / BO3 / BO5, central / low / high: `p_team1_win` and `p_draw` equal the sums of `map_score_pmf`; slot `p_played` equals P(series lasts >= k maps); sum over slots of p_played x p_team1_win equals E[maps won by team1]; slot `p_team1_win` / `p_overtime` equal their score pmfs (all to 1e-15). `player_kills_by_scope["map:k"]` is the slot's `players` dict.
- Head-to-head kills, exactly: on populations where the two players' kill totals are exactly independent inside the scope (cross product of series x kill vectors), pricing equals settlement to 1e-9 for `map:k`, `maps:1,2`, `maps:1,2,3` and the BO2 match scope, handicaps -2.5 / -1 / 0 / +3, both tie rules. (Round 1 could only check this to +-0.07.)
- Top fragger, exactly: with kills `50 r + i` no two players can tie in any scope; pricing equals settlement to 1e-9 for every scope, team-restricted tables included, and the selections sum to 1 - void.
- Settlement fuzzed with 40,000 random malformed results (impossible scores, histories of the wrong length or order, negative kills, wrong map numbers, unfinished / forfeited / cancelled) and specs (NaN / inf lines, bad halves and rounds): never raises.
- Bankroll guards under adversarial sequences (new test, 2 seeds in the suite and 60 more run once): batches sized from one snapshot with `size_decisions` (most decisions on one match), refunds that keep the stake tax, tax-free promo days, "wins" at odds 1.07 / 1.12 that lose money, settlements dated back to any moment after placement (also across local midnight onto a finished day), deposits mid-day, the autumn DST change. No stake above 2 % of the bankroll, no match above 4 %, every stake a multiple of the step and >= the Betclic minimum, the batch within the free balance and within the daily / weekly guard, the ledger equal to the test's own cash book, no reconcile flag, and the realized net loss of every local day (week) <= 5 % (10 %) of the day-start (week-start) bankroll the engine saw last in that period. The worst day reaches 4.999 %.
- Derivation behind that invariant (with back-dating): a loss S back-dated into day D-1 lowers D's day-start bankroll by S and D's open stakes by S, so D's allowance changes by +0.95 S and the bound `loss_D <= 0.05 x day-start_D` still holds; open stakes carried into a day are <= 5 % of its day-start bankroll by the same argument.
- Rounding: stakes are rounded down and never exceed a cap; limits are rounded down; `min_acceptable_odds` rounds up; the stake-tax rounding difference against a Betclic that does not round the tax is at most 0.004 x odds per bet (under the 0.05 reconcile tolerance below odds 12.5); the złoty rounding of the winnings tax and the threshold comparison (2,280.00 exempt, 2,280.01 taxed) re-checked.

**Issues found and fixed:**
1. *Major:* an UNCERTAIN placement result on a dry-run row (the executor returns UNCERTAIN in dry-run mode when a selection click may have placed a quick bet) left the row as a dry run in PLACING. Dry-run rows are excluded from exposure, the loss guards, the real-betting duplicate check and the reconcile deferral, so a bet that may exist at Betclic was invisible to every limit, and the shortfall it caused was flagged instead of deferred. `apply_placement` now turns the row into a real open bet (logged as an error) until reconciliation resolves it. Test added.
2. *Major:* `reconcile` dropped a balance mismatch when the bot was already paused (e.g. by the daily loss limit): only the audit log recorded it, so the owner, resuming for the loss limit, never saw it - the misread-balance risk round 1 fixed came back through that path. Symmetrically, `check_loss_limits` overwrote the reason of an existing pause (e.g. a balance mismatch). New `ledger.pause_trading` appends to an existing pause reason (bounded length, no repeats); both use it. Tests added (the round-1 test asserting the old behaviour was rewritten).
3. *Minor:* pricing priced a correct-score selection that is not a possible final score whenever a malformed pmf carried a key for it, while settlement leaves such a bet UNKNOWN forever. Pricing now refuses it like settlement. Test added.
4. *Minor (tests):* the pricing <-> settlement consistency for head-to-head and top-fragger markets was only statistical / bounded, and settlement had never been checked against the real simulator's plays. Added `test_engine_sim_settlement.py`, `test_engine_settlement_props.py` and `test_engine_adversarial.py`.

**Tests added:** test_engine_sim_settlement.py (3), test_engine_settlement_props.py (6), test_engine_adversarial.py (2), plus new cases in the ledger, bankroll and pricing modules. 567 tests in total (552 before this round).

**Remaining concerns:**
- A payout Betclic credits before our own settlement (our match data lags) is booked by `reconcile` as a flagged mismatch and pauses the bot; the later local settlement then credits it again (corrected, and flagged, by the next reconcile). The integration layer must settle open bets from the bookmaker's bet history (`executor` bet history: WON / LOST / VOID with the payout) before every reconciliation, not only from `settlement.resolve_bet`.
- `MatchActuals.forfeited` voids every bet, also bets on maps that were completed before a forfeit; Betclic usually lets completed maps stand. The ledger follows Betclic through reconcile, but such a bet would be refunded locally and flagged. Settle partially played forfeits from the bookmaker's history.
- The `match` scope of head-to-head kills in a BO3 / BO5 is priced with independent totals although both grow with the number of maps played (documented limitation; conservative for favourites).
- The winnings-tax branch of `size_stake` may stake above the tax-free limit when that has a higher expected log-growth even if the EV after the 10 % tax falls below `min_ev`; it only matters for bankrolls above roughly 8,600 PLN.
