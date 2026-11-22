# engine: implementation report

> Written by the engineer who implemented the package. Reviewers update it when the public API changes.

**Tests:** `.venv/bin/python -m pytest tests/engine -q` - 249 passed, 0 failed (lint/type clean: True)

## Summary

P8a (cs2bot.engine) is implemented and tested: pricing for every MarketType, the Polish tax / EV / Kelly model, the decision rules, bankroll state and stake sizing, and the ledger. All four definition-of-done checks pass: 249 engine tests pass in about 5 s, ruff check, ruff format --check and mypy are clean on my paths, and tests/foundation still passes. I only touched files under src/cs2bot/engine/ and tests/engine/.

**Tax research:**
- 10% winnings tax (art. 30 ust. 1 pkt 2 PIT): charged on the WHOLE winning. That is the full coupon payout (stake − 12%) × odds, returned stake included. It is not charged on net profit or on the excess over the threshold.
- Threshold: it applies only when a single winning is above 2,280 PLN (art. 21 ust. 1 pkt 6a). The bookmaker withholds it as płatnik (art. 41 ust. 4).
- Rounding: base and tax are rounded to full złoty (art. 63 §1 Ordynacji podatkowej).
- Worked example: stake 1000 at 3.00 gives 2640 gross, 264 tax, 2376 credited.

**Deliberate decisions:**
- **Sigma curvature guard:** the specified sigma = max(floor, |p_high − p_low| / 2) is kept, plus a guard: sigma = max(|a|, √2·|c|), where a = (p_high − p_low)/2 and c = (p_high + p_low − 2·p_central)/2. It only changes the result when the outcome peaks at the central strength (e.g. "map goes to overtime" in an even match). There both scenarios lower the probability, so the specified formula would give almost zero.
- **Kelly clipping:** Kelly is clipped to [−1, 1] so it stays monotone in the odds.
- **NaN safety:** decision comparisons are written so NaN fails them, with an `invalid_numbers` reason.
- **Winnings tax in staking:** size_stake keeps the stake at the largest amount whose winning stays tax-free. It goes above that only if the taxed stake has a higher expected log-growth.
- **Loss-limit pause:** each limit pauses only once per day or week, so the owner's manual resume is not undone by the next scheduler tick.
- **Balance and day-start bankroll:**
  - `balance` = ledger balance minus stakes of open bets not yet debited. This keeps bankroll_reference = balance + open stakes correct whenever record_stake is called.
  - `day_start_bankroll` = bankroll_reference − realized P&L today. Deposits and withdrawals count as if made at the start of the period, so the first day's limit isn't 0.
- **Reconciliation:** reconcile writes a RECONCILIATION entry for any non-zero difference and flags only when it exceeds the tolerance.

**Integration helpers (beyond the spec):** price_quotes_report, ev.price_all, decision.load_match_decision_info / load_already_bet_keys / bet_key, bankroll.size_decision / loss_limit_status, ledger.bet_money_fields / record_deposit / record_withdrawal.

**Research sources:**
- [Bezprawnik: jak rozliczyć wygrane u legalnego bukmachera](https://bezprawnik.pl/jak-rozliczyc-wygrane-u-legalnego-bukmachera/)
- [PITax: podatek od wygranej](https://www.pitax.pl/wiedza/poradnik-rozliczenia/podatek-od-wygranej/)
- [Infor: wygrana w zakładach bukmacherskich a rozliczenie podatku](https://ksiegowosc.infor.pl/podatki/5572652,Wygrana-w-zakladach-bukmacherskich-a-rozliczenie-podatku.html)
- [Infor: opodatkowanie wygranej w zakładach bukmacherskich](https://ksiegowosc.infor.pl/podatki/podatki-osobiste/pit/748455,Opodatkowanie-wygranej-w-zakladach-bukmacherskich.html)
- [Art. 63 Ordynacji podatkowej (arslege)](https://arslege.pl/ordynacja-podatkowa/k38/a15954/)
- [podatki.gov.pl: stawki i limity](https://www.podatki.gov.pl/pozostale/gry-hazardowe/stawki-i-limity/)

## Public API

All functions only `session.flush()`; they never commit (the caller owns the transaction). Import the submodules directly (`from cs2bot.engine import pricing, ev, decision, bankroll, ledger`). Nothing is re-exported in `__init__`.

## cs2bot.engine.pricing
- `class UnpriceableMarket(ValueError)`: the prediction cannot support this outcome. Never bet such outcomes.
- `@dataclass(frozen=True) class OutcomeSplit(p_win: float, p_push: float = 0.0, p_void: float = 0.0)` with property `.p_lose`
- `@dataclass(frozen=True) class PricingReport(priced: list[tuple[OutcomeProbability, OddsQuote]], skipped: list[tuple[OddsQuote, str]])`
- `price_outcome(spec: MarketSpec, selection: str, prediction: MatchPrediction, *, sigma_floor: float, include_overtime_in_totals: bool = True, h2h_tie: str = "push") -> OutcomeProbability`
  - Probabilities are unconditional (p_win / p_push / p_void).
  - `h2h_tie` is "push" or "lose"; anything else raises ValueError.
  - Sigma: with both low and high, `max(sigma_floor, |p_h-p_l|/2, |p_h+p_l-2p_c|/sqrt2)`. With one scenario, `|p_s - p_c|`. With none, the `fallback_sigma` delta method.
- `price_quotes(prediction: MatchPrediction, quotes: Iterable[OddsQuote], *, sigma_floor: float, include_overtime_in_totals: bool = True, h2h_tie: str = "push") -> list[tuple[OutcomeProbability, OddsQuote]]`: skips unpriceable quotes and logs the reason at INFO.
- `price_quotes_report(...same args...) -> PricingReport`: also returns the skipped quotes with reasons (for the dashboard).
- `price_core(spec: MarketSpec, selection: str, core: PredictionCore, *, fmt: MatchFormat, include_overtime_in_totals: bool = True, h2h_tie: str = "push") -> OutcomeSplit`: single scenario, no sigma (backtests).
- `fallback_sigma(p_win: float, prediction: MatchPrediction) -> float`
- `is_final_round_score(team1_rounds: int, team2_rounds: int) -> bool`
- `final_map_scores(fmt: MatchFormat) -> set[tuple[int, int]]`
- Constants: `REGULATION_ROUNDS = 24`, `PMF_TOLERANCE = 0.02` (pmfs further than this from summing to 1 are unpriceable; others are renormalised).

### Conventions P6 must produce
- Slot distributions are conditional on that map being played.
- `player_kills_by_scope` keys:
  - `"map:k"`: the slot's `players` dict is used first. Conditional on map k being played.
  - `"maps:1,2"`: conditional on ALL listed maps being played. The bet is void with probability 1 − p_played(last listed map).
  - `"match"`: unconditional.
- `top_fragger[scope]` maps player id to probability, with dead heat already applied. A team-specific market reads `top_fragger["<scope>|team1"]` / `"<scope>|team2"`.
- `p_team1_race_to[str(N)]` = P(team1 is first to N). For N > 13 the slot's `score_pmf` must exist: P(neither team reaches N) = P(max score < N), and that case is void.
- `p_team1_pistol` keys are "1" / "13". `p_team1_half` and `p_half_draw` keys are "1" / "2"; selecting team2 needs both.

## cs2bot.engine.ev
- `TaxSource = Settings | BetclicSettings | TaxModel`
- `@dataclass(frozen=True) class TaxModel(stake_tax_rate: Decimal, big_win_threshold: Decimal, big_win_rate: Decimal)`
  - `TaxModel.from_settings(settings: TaxSource) -> TaxModel`. The promo sets stake_tax_rate to 0.
- `@dataclass(frozen=True) class PayoutBreakdown(stake, stake_tax, stake_tax_rate, net_stake, odds, gross_win, win_tax, net_payout)`: all Decimal, with property `.profit`.
- `effective_odds(odds: Decimal, *, tax_rate: Decimal) -> Decimal`: `(1 - t) * odds`, unrounded.
- `stake_tax(stake: Decimal, settings: TaxSource) -> Decimal`: grosz-rounded; this is the value for `bets.tax`.
- `winnings_tax(gross_win: Decimal, settings: TaxSource) -> Decimal`: 10% of the whole winning if it is above 2280; base and tax rounded to full złoty.
- `payout_breakdown(stake: Decimal, odds: Decimal, settings: TaxSource, *, tax: Decimal | None = None) -> PayoutBreakdown`
- `net_payout(stake: Decimal, odds: Decimal, settings: TaxSource) -> Decimal`: credited on a win, for a GROSS stake.
- `big_win_stake_limit(odds: Decimal, settings: TaxSource) -> Decimal | None`: largest stake whose winning stays ≤ 2280; None if the rate is 0.
- `expected_value(outcome: OutcomeProbability, odds: Decimal, settings: TaxSource, stake: Decimal | None = None) -> float`
  - Profit per 1 unit of gross stake. Refunds are worth 0.
  - Without `stake`: `p_w*(o_eff-1) - p_l`. With `stake`: exact grosz rounding and the winnings tax.
- `kelly_fraction(outcome: OutcomeProbability, odds: Decimal, settings: TaxSource) -> float`: `f* = (b p_w - p_l)/(b (p_w+p_l))`, `b = o_eff - 1`, clipped to [-1, 1]. Positive exactly when EV > 0.
- `kelly_for_net_odds(p_win: float, p_lose: float, b: float) -> float`
- `expected_log_growth(p_win: float, p_lose: float, b: float, fraction: float) -> float`
- `price(outcome: OutcomeProbability, quote: OddsQuote, settings: TaxSource) -> PricedOutcome`: effective_odds, per-unit ev, kelly_full.
- `price_all(pairs: Iterable[tuple[OutcomeProbability, OddsQuote]], settings: TaxSource) -> list[PricedOutcome]`

## cs2bot.engine.decision
- `class ReasonCode(StrEnum)`: MARKET_DISABLED, SELECTION_UNAVAILABLE, ODDS_OUT_OF_RANGE, ODDS_STALE, OUTSIDE_TIME_WINDOW, LOW_PROBABILITY, LOW_EV, HIGH_SIGMA, LOW_DATA_COMPLETENESS, WEAK_EVENT_LINK, CONTEXT_VETO, WAITING_FOR_VETO, DUPLICATE, PRICING_MISMATCH, INVALID_NUMBERS.
  - Each reason string is `"<code>: <detail>"`. The waiting-for-veto detail contains "waiting for veto".
- `class MatchDecisionInfo(BaseModel)` (pydantic):
  - `match_id: int`
  - `scheduled_at: datetime | None`
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
- `evaluate_all(priced: Iterable[PricedOutcome], *, match: MatchDecisionInfo, settings: DecisionSettings, betclic: BetclicSettings, now: datetime) -> list[BetDecision]`
- `bet_key(match_id: int, spec_key: str, selection_key: str) -> str`: returns `"<match_id>|<spec_key>#<selection_key>"`.
- `load_already_bet_keys(session: Session, match_id: int) -> set[str]`: all bets on the match except FAILED / CANCELLED. DRY_RUN bets count, so dry runs are not repeated.
- `load_match_decision_info(session: Session, *, match_id: int, bookmaker_event_id: int | None = None, prediction: MatchPrediction | None = None, now: datetime | None = None) -> MatchDecisionInfo`
  - Completeness and veto_known come from the prediction if given, else from the DB.
  - The link counts only if `event.match_id == match_id`.
  - Context veto comes from active, unexpired `context_flags` rows plus the prediction's flags.
- `thresholds_for(market_type: MarketType, settings: DecisionSettings) -> Thresholds` (with `.min_probability`, `.min_ev`, `.max_sigma`)
- `is_slot_specific(spec: MarketSpec) -> bool`
- `reason_code(reason: str) -> str`
- `make_reason(code: ReasonCode, detail: str) -> str`

## cs2bot.engine.bankroll
- `compute_state(session: Session, settings: Settings, *, now: datetime, bankroll_settings: BankrollSettings | None = None) -> BankrollState`
  - Initialises an empty ledger with a DEPOSIT of starting_bankroll (dashboard overrides honoured).
  - Dry-run bets are excluded.
  - Days and weeks are Europe/Warsaw (`settings.timezone`) and DST-aware.
  - `balance` = ledger balance minus open stakes not yet debited.
- `size_stake(decision: BetDecision, state: BankrollState, settings: BankrollSettings, betclic: BetclicSettings) -> tuple[Decimal, list[str]]`
  - A 0 stake comes with reasons, e.g. "halted: …", "bets_per_day: …", "no_edge: …", "match_exposure: nothing left…", "daily_loss_guard: …", "below_min_stake: …", "not_accepted: …".
  - A positive stake comes with info notes "capped: <cap> -> X PLN (…)".
- `apply_stake(decision: BetDecision, state: BankrollState, stake: Decimal, notes: list[str]) -> BetDecision`
  - Returns a copy with stake set and `accepted = accepted and stake > 0`; the notes are appended to reasons.
  - `kelly_fraction_used` = stake / bankroll_reference.
- `size_decision(decision: BetDecision, state: BankrollState, settings: BankrollSettings, betclic: BetclicSettings) -> BetDecision`: size_stake + apply_stake.
- `check_loss_limits(session: Session, state: BankrollState, settings: Settings | BankrollSettings, *, actor: str = "system", now: datetime | None = None) -> bool`
  - True if the daily or weekly realized-loss limit is reached.
  - Calls `db.state.set_paused(True, reason=…)` once per period; it never resumes.
- `loss_limit_status(state: BankrollState, settings: BankrollSettings) -> LossLimitStatus`. Fields: daily_limit, weekly_limit, realized_loss_today, realized_loss_week, daily_allowance, weekly_allowance, daily_hit, weekly_hit.
- Constants: `OPEN_STATUSES`, `SETTLED_STATUSES`, `COUNTED_STATUSES`, `LOSS_LIMIT_STATE_KEY = "loss_limit_pauses"`.

## cs2bot.engine.ledger
- `ensure_initialized(session: Session, settings: Settings | BankrollSettings, *, at: datetime | None = None) -> BankrollLedger | None`
- `current_balance(session: Session) -> Decimal`: `balance_after` of the highest-id entry; 0.00 if the ledger is empty.
- `record_stake(session: Session, bet: Bet, *, at: datetime | None = None) -> BankrollLedger | None`: debits the gross stake; idempotent; dry runs return None.
- `record_payout(session: Session, bet: Bet, payout: Decimal, *, at: datetime | None = None) -> BankrollLedger | None`
  - Idempotent. A payout of 0 or a dry run returns None.
  - If the stake was never recorded, it records the stake first.
- `record_refund(session: Session, bet: Bet, *, at: datetime | None = None) -> BankrollLedger | None`: refunds the debited stake; idempotent; returns None if nothing was debited.
- `record_adjustment(session: Session, amount: Decimal, note: str, *, at: datetime | None = None) -> BankrollLedger`
- `record_deposit(session: Session, amount: Decimal, note: str = "deposit", *, at: datetime | None = None) -> BankrollLedger`
- `record_withdrawal(session: Session, amount: Decimal, note: str = "withdrawal", *, at: datetime | None = None) -> BankrollLedger`
- `@dataclass(frozen=True) class ReconcileResult(observed_balance: Decimal, ledger_balance: Decimal, difference: Decimal, tolerance: Decimal, flagged: bool, entry_id: int | None, at: datetime)`
- `reconcile(session: Session, observed_balance: Decimal, *, tolerance: Decimal, actor: str = "system", at: datetime | None = None) -> ReconcileResult`
  - Writes a RECONCILIATION entry for any non-zero difference.
  - Flags only when |difference| > tolerance; a flag is also written to the audit log.
  - Stores the result in `system_state["last_reconciliation"]`.
- `settle_amounts(bet: Bet, outcome: Literal["won","lost","void","push"] | str, settings: TaxSource) -> tuple[Decimal, Decimal]`: returns (payout, pnl), consistent with ev.net_payout. Void and push refund the full gross stake.
- `bet_money_fields(stake: Decimal, odds: Decimal, settings: TaxSource) -> dict[str, object]`: returns `{"tax", "potential_payout", "extra": {"stake_tax_rate": "0.12"}}` for a new Bet row.
- `bet_stake_tax(bet: Bet, settings: TaxSource) -> Decimal`: uses `extra["stake_tax_rate"]`, else a positive `bets.tax`, else settings.
- `is_dry_run(bet: Bet) -> bool`
- `STAKE_TAX_RATE_KEY = "stake_tax_rate"`

## Bet lifecycle for the integration layer
1. **PLACED:** call `record_stake`.
2. **FAILED / CANCELLED after a stake was recorded:** call `record_refund`.
3. **WON:** `settle_amounts(bet, "won")`, then `record_payout`.
4. **LOST:** `settle_amounts(bet, "lost")`; no ledger entry.
5. **VOID / push:** set status VOID, call `record_refund`, with payout = stake and pnl = 0.
6. **Always:** set `bets.status`, `payout`, `pnl` and `settled_at` (compute_state reads them). Run `check_loss_limits` after settlement and before betting.
7. **Serialization:** run compute_state → size → create bet one at a time, so the caps see the previous bets.

## Known limitations

- Void/push refunds assume Betclic returns the full gross stake, including the 12% stake tax. No published rule confirms this; ledger.reconcile will flag the difference after the first void bet if it is wrong.
- The 10% winnings tax is rounded to full złoty per art. 63 §1 Ordynacji podatkowej. If Betclic computes it to the grosz, payouts above 2280 PLN can differ by up to 0.50 PLN; reconcile syncs this and flags it if above the tolerance.
- kelly_fraction ignores the stake-dependent winnings tax. size_stake handles the 2280 PLN threshold by comparing two candidate stakes on expected log-growth (float approximation that ignores the złoty rounding).
- PLAYER_KILLS_H2H treats the two players' kill pmfs as independent. This ignores the positive correlation from the shared round count and the negative correlation from mutual duels.
- Top-fragger probabilities are used as given, so P6 must apply the dead-heat rule. Team-specific tables use the convention I chose: key '<scope>|team1' / '<scope>|team2'.
- 'maps:a,b' player scopes are assumed to be conditional on every listed map being played (void otherwise). Per-map pmfs are deliberately not convolved when a scope is missing, so that variance is not understated; such outcomes are unpriceable.
- Regulation-only totals (include_overtime_in_totals=False) assume MR12: 24 regulation rounds and 12 per team when the map goes to overtime. The flag applies to TOTAL_ROUNDS, TEAM_TOTAL_ROUNDS and TOTAL_ROUNDS_ODD_EVEN. Round handicaps always include overtime.
- Sigma follows the spec'd |p_high - p_low|/2, plus a second-order curvature guard (√2·|c|) for outcomes that peak at the central strength. The fallback when low/high are missing is a delta-method approximation that is conservative for strength-insensitive markets.
- The daily guard follows the spec: it counts only open stakes placed today. Bets placed earlier that lose today can push today's realized loss past the limit; check_loss_limits then pauses the bot.
- compute_state takes no locks. Concurrent betting jobs must be serialized by the caller, otherwise caps and guards can be exceeded.
- BetDecision.kelly_fraction_used is set to stake / bankroll_reference (the fraction actually staked after caps), not to the configured Kelly multiplier.
- reconcile writes a RECONCILIATION entry for any non-zero difference, including differences within tolerance (to stop drift building up), and flags only beyond the tolerance.

## Foundation change requests

- config.BetclicSettings: add `void_refunds_stake_tax: bool = True` and `winnings_tax_rounding: Literal['zloty','grosz'] = 'zloty'`, so the refund and rounding assumptions can be adjusted without code changes once real Betclic settlements are observed.
- domain.predictions: document the conventions the pricing engine relies on so P6 produces them: top_fragger team key '<scope>|team1' / '<scope>|team2'; 'maps:a,b' pmfs conditional on all listed maps being played; p_team1_race_to[N] for N > 13 excludes the 'neither team reaches N' case; top-fragger values already dead-heat adjusted.
- domain.betting.BankrollState: add `day_start` / `week_start` instants and `reserved_stakes`. check_loss_limits currently has to recompute the period from `now`.
- domain.betting.BetDecision: add a separate `notes: list[str]` for informational sizing notes (binding caps), so `reasons` holds only rejection reasons.
- config.DecisionSettings (or config/betclic_markets.yaml): per-market pricing rules `h2h_tie` (push/lose) and `include_overtime_in_totals`, so Betclic's settlement rules drive the pricing kwargs instead of call-site constants.

## New dependency requests

- (none)
