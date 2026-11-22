# sim: implementation report

> Written by the engineer who implemented the package. Reviewers update it when the public API changes.

**Tests:** `.venv/bin/python -m pytest tests/models/sim -q` - 70 passed, 0 failed (lint/type clean: True)

## Summary

P6 is complete: CS2 economy constants, round model, vectorised map simulator with strength calibration, empirical-Bayes player kill model and the match engine that builds a full MatchPrediction. Definition of done: 70/70 tests pass in about 15 s (no test is slow enough to need @pytest.mark.slow), ruff, ruff format and mypy are clean on my paths, and the 37 foundation tests still pass. I wrote only inside my owned paths.

How the CS2 rules were checked: values come from the CS2 game files (gamemode_competitive.cfg, gamemode_competitive_tmm.cfg and the convar dump in SteamDatabase/GameTracking-CS2), the Valve Major supplemental rulebook ("Premier defaults"), the CS:GO game-rules source and the July 2025 CS2 patch coverage. Every value has its source in a comment in economy.py.
- Regulation and overtime: MR12 first to 13; MR3 overtime (6 rounds, win 4-x, a new period at 3-3).
- Overtime sides: teams keep their side into each overtime period and swap at the overtime halftime. The source comment reads "don't switch teams, only switch at true halftimes".
- Overtime money: $10,000 (the Premier default used at Majors). Some organisers use $12,500, so it is configurable.
- Loss bonus: 1400 to 3400, the streak starts at 1 each half (the pistol loser gets $1,900) and a win steps the streak down by one.
- Current CS2 values: the plant bonus is $600, not the CS:GO $800. CTs get $50 each per Terrorist killed (July 2025). Ts alive at time expiry get no loss bonus.

Round model: logistic model with per-map CT bias, a pistol side adjustment, an antisymmetric 4x4 buy-matchup table and the strength difference d. It is fitted by penalised maximum likelihood with one nuisance strength term per match-map, so side and buy effects are not confounded with team strength. Priors come from public data (CT win rate 45-58% by map, full buy vs eco about 88%). On the synthetic universe it recovers the true buy effects (1.66 vs 1.70, 0.72 vs 0.75, 0.95 vs 0.95). With thin data it stays near the priors.

Map simulator:
- Speed: 20k simulations of one map take about 0.09 s.
- Each round models team-level money, the buy decision (pistol, full, force, eco, anti-eco), end reason, deaths and carried equipment.
- Common random numbers make calibration a smooth search: 1.4 simulations per calibration on average, 2 at most, and the result depends only on (input, target, sims, seed), so it is safely cached.
- Simulated outputs are reweighted so each map's win probability matches the ensemble's estimate exactly.
- Behaviour matches real CS2 patterns: the pistol loser ecos round 2 about 79% of the time, the pistol winner buys SMGs, and about 64% of rounds are full buys.
- At the true strength gap it reproduces the synthetic generator's map-win probabilities to within about 0.01.
- It produces about one round fewer per map, and fewer overtimes, than the synthetic generator, because the CS2-style economy has more momentum. The total-rounds test therefore uses loose tolerances.

Player kill model: kills per round shrunk toward a role prior, with map and opponent-defence adjustments, a negative-binomial dispersion per role, and a strict as_of cut-off (no look-ahead). Unknown players get the role or global prior with wider spread. Two documented additions beyond the spec, both optional: a centred score-skew term that ties kills to the simulated scoreline, and a lineup-based team normalisation that adjusts for stand-ins.

Match engine:
- Scenarios: central plus low/high scenarios shifted by one sigma in logit space, perfectly correlated across maps.
- Series math is exact: BO3 slot 3 is played with probability P(1-1), BO5 slots 4 and 5 are conditional, BO2 allows a draw.
- Every slot distribution is conditional on the slot being played.
- Player kill pmfs for map:k, maps:1,2 (plus maps:1,2,3 for BO5) and match.
- Top fragger comes from a joint series simulation with ties split evenly.
- Runtime: a BO3 whose veto distribution covers all 7 maps, with 3 scenarios, 20k sims and 10 players, takes about 2.3-4.7 s (the 20 s target is met).

Decisions you may want to revisit:
- Low/high use max(2000, n_sims // 2) simulations by default, which the spec allows if documented. Change it with scenario_n_sims.
- Maps with sigma = 0 reuse the central run in all scenarios, so derived markets get no spurious sigma.
- Results are reproducible with a seed. With seed=None a random seed is drawn and recorded in components["seed"].

## Public API

## Main entry point: `cs2bot.models.match_engine`

```python
from cs2bot.models.match_engine import build_match_prediction, PlayerInput, scenario_probabilities

build_match_prediction(*, match_id: int, fmt: MatchFormat, team1_id: int, team2_id: int,
    veto_distribution: list[MapSequenceProbability], map_estimates: dict[str, MapWinEstimate],
    round_model: RoundModel, economy: EconomyParams | None = None,
    start_sides: dict[str, Side | None] | None = None, players: list[PlayerInput] | None = None,
    kill_model: PlayerKillModel | None = None, logit_shift: float = 0.0,
    context_flags: Sequence[ContextFlag] = (), n_sims: int = 20000, seed: int | None = None,
    model_version: str, data_completeness: float = 0.0, veto_known: bool = False,
    created_at: datetime | None = None,
    # optional extras (keyword-only):
    as_of: datetime | None = None,             # data cut-off for player props (default created_at, default now)
    scenario_n_sims: int | None = None,        # sims for low/high (default max(2000, n_sims // 2))
    policy: BuyPolicy | None = None, flow: RoundFlowParams | None = None,
    race_targets: tuple[int, ...] = (3, 5, 7, 10, 13), tol: float = 0.003,
    top_fragger_samples: int | None = None,    # joint series sims (default n_sims)
    simulator: MapSimulator | None = None,     # default: shared cached DEFAULT_SIMULATOR (not thread-safe)
) -> MatchPrediction
```

What it expects:
- `veto_distribution`: sequences are renormalised; sequences longer than the format's map count are truncated (e.g. a BO2 decider is dropped); duplicate sequences are merged.
- `map_estimates`: one entry for every map that appears in the veto distribution, otherwise `ValueError`.
- Map names in `map_estimates` and `start_sides` are normalised with `normalize_map_name`.
- `start_sides`: missing or `None` means a 50/50 mixture of starting sides.
- `created_at` / `as_of` must be timezone-aware.

It raises `ValueError` on a missing map estimate, a sequence shorter than the format needs, a repeated map in a sequence, no positive probability, naive datetimes, `n_sims < 200`, or duplicate players.

Output conventions (all `PredictionCore`s: central, low, high):
- `sigma_team1_win = |high.p_team1_win - low.p_team1_win| / 2`.
- `map_score_pmf` keys look like `"2-1"`. In a BO2, `"1-1"` is the draw, `p_draw = P("1-1")` and `p_team1_win = P("2-0")`.
- `slots[k]` (`MapSlotPrediction`): every distribution is conditional on the slot being played.
  - `p_played` is exact (BO3 slot 3 = P(1-1); BO5 slot 4 = 1 − P(3-0) − P(0-3); slot 5 = P(2-2)).
  - `map_distribution` = P(map | played).
  - `score_pmf` includes overtime scores, so handicaps, totals, odd/even and team totals can all be derived from it.
  - `p_team1_pistol` keys are `"1"` and `"13"`.
  - `p_team1_half` / `p_half_draw`: half `"1"` = rounds 1-12; half `"2"` = rounds 13-24 actually played (overtime excluded).
  - `p_team1_race_to` contains only N ≤ 13, which is always decided, so P(team2) = 1 − p. For N > 13 use `MapSimOutput.p_neither_race_to`.
- `player_kills_by_scope` keys:
  - `"map:k"`: conditional on slot k being played (same objects as `slots[k].players`, keyed by `str(player_id)`).
  - `"maps:1,2"` for BO2/BO3/BO5, and `"maps:1,2,3"` for BO5: maps always played.
  - `"match"`: sum over the maps actually played.
- `top_fragger` uses the same scope keys plus team-restricted versions `"<scope>|team1"` and `"<scope>|team2"`. Every dict sums to 1 (ties split evenly).
- `components` holds: `seed`, `n_sims`, `scenario_map_p`, `map_strength_d` (per scenario and map), `sigma_logit`, `start_sides`, `veto`, `cache`, `timing_s`, `round_model` (fingerprint), `map_diagnostics`, `player_props`, `warnings`. Everything is JSON-serialisable.

`scenario_probabilities(estimate: MapWinEstimate, logit_shift: float = 0.0, max_sigma_logit: float = 2.5) -> tuple[central, low, high, sigma_logit]`, with `sigma_logit = sigma / (p (1 - p))` taken at the estimate's own p and capped at 2.5.

`PlayerInput` (pydantic, frozen; defined in `cs2bot.models.player_props` and re-exported here): `PlayerInput(player_id: int, team: TeamSlot, nickname: str | None = None, role: PlayerRole | None = None)`.

## `cs2bot.models.round_model`

- `RoundModel(*, ct_bias: Mapping[str, float] | None = None, pistol_ct_adj: float | None = None, buy_pairs: Sequence[float] | None = None, priors: RoundModelPriors | None = None)`
  - `RoundModel.default()` returns a model with prior parameters only.
- `.fit(rounds: Iterable[RoundRecord], *, as_of: datetime | None = None, half_life_days: float | None = 365.0, rounds_per_half: int = 12, max_iter: int = 2000) -> RoundModel` (returns self).
  - Only rounds strictly before `as_of` are used.
  - Missing buys are classified from equipment values; rounds whose buys stay unknown are skipped and counted in `fit_stats`.
- Probabilities:
  - `.round_win_prob(a_side: Side | Sequence[Side] | NDArray[bool], buy_a: BuyType | Sequence[BuyType] | NDArray[int], buy_b, map_name: str, d: float | ArrayLike = 0.0) -> NDArray[float64]` (vectorised).
  - `.round_win_prob_codes(a_is_ct, buy_a, buy_b, map_name, d=0.0)` takes buy codes 0..3 (PISTOL, ECO, FORCE, FULL).
- Parameters: `.ct_bias_for(map_name) -> float`, `.buy_effect(a: BuyType, b: BuyType) -> float`, `.buy_table` (4x4), `.logit_tables(map_name)`, `.fit_stats: dict`.
- `.fingerprint() -> str`. Treat a fitted model as immutable: the fingerprint is cached and only reset by `fit`.
- Persistence (JSON): `.save(path) -> Path`, `RoundModel.load(path)`, `.to_dict()`, `RoundModel.from_dict(d)`.
- Also exported: `RoundModelPriors` (frozen dataclass), `classify_buy(equipment_value: int | None, round_number: int, rounds_per_half: int = 12) -> BuyType | None`, `DEFAULT_CT_BIAS_PRIOR`, `BUY_PAIRS`.

## `cs2bot.models.map_sim`

- `MapSimInput(map_name: str, strength_diff: float = 0.0, team1_start_side: Side | None = None, economy: EconomyParams = DEFAULT_ECONOMY, round_model: RoundModel = RoundModel.default(), policy: BuyPolicy = DEFAULT_BUY_POLICY, flow: RoundFlowParams = DEFAULT_ROUND_FLOW, race_targets: tuple[int, ...] = (3, 5, 7, 10, 13))`
  - Frozen; `.with_strength(d) -> MapSimInput`.
- `MapSimulator(*, cache_size: int = 64, calibration_cache_size: int = 4096)`:
  - `.simulate(inp, n_sims: int = 20000, rng: np.random.Generator | int | None = None, *, record_rounds: bool = False) -> MapSimOutput`. An int seed makes the call cacheable.
  - `.calibrate_strength(target_p: float, inp, *, n_sims: int = 20000, rng_seed: int = 0, tol: float = 0.003, max_iter: int = 40) -> float`. Pure, memoised; targets clipped to [0.001, 0.999]; d bounded to [-8, 8].
  - `.calibrate_and_simulate(...same...) -> tuple[float, MapSimOutput]`.
  - `.stats: dict`, `.clear_caches()`.
- Module-level: `calibrate_strength(target_p, inp, *, n_sims=20000, rng_seed=0, tol=0.003) -> float`, `simulate_map(inp, n_sims=20000, rng=None)`, `DEFAULT_SIMULATOR`, `iid_map_win_prob(p_round, economy=DEFAULT_ECONOMY) -> float`, `iid_round_prob_for_map(target_p, economy=DEFAULT_ECONOMY) -> float`.
- `MapSimOutput` (frozen):
  - Identity: `map_name`, `strength_diff`, `team1_start_side`, `n_sims`, `rounds_per_half`, `race_targets`.
  - Summaries: `score_pmf: dict[str, float]` (`"13-9"`, overtime included), `p_team1_win`, `p_overtime`, `p_team1_pistol` (`{"1", "13"}`), `p_team1_half` / `p_half_draw` (`{"1", "2"}`), `p_team1_race_to` / `p_neither_race_to` (`{"N"}`), `total_rounds_pmf: dict[int, float]`.
  - Per-simulation arrays: `team1_rounds`, `team2_rounds`, `total_rounds` (int16), `team1_wins`, `records: PerSimRecords`.
  - `weights: NDArray | None`: when set, these normalised per-simulation weights must be used whenever the arrays are aggregated.
  - `mean_total_rounds`, `diagnostics` (buy shares, round-2 and round-3 rates), `.reweighted(target_p) -> MapSimOutput` (exact map p; distributions conditional on the winner are unchanged).

## `cs2bot.models.player_props`

- `PlayerKillModel(config: KillModelConfig | None = None)`:
  - `.fit(records: Iterable[PlayerMapRecord], *, half_life_days: float = 120.0) -> self`. Estimates are computed lazily per `as_of` from records strictly before it (cut-off rounded down to the minute).
  - `.project(player_id: int, team: TeamSlot, map_name: str, opponent_team_id: int | None, rounds_pmf: Mapping[int, float] | ArrayLike, *, as_of: datetime, nickname: str | None = None, role: PlayerRole | None = None, team_rounds: tuple[ArrayLike, ArrayLike] | None = None, weights: ArrayLike | None = None, kpr_scale: float = 1.0) -> PlayerProjection`. `team_rounds` = (own, opponent) rounds per simulation and enables the score skew.
  - `.rate(player_id, map_name, opponent_team_id, *, as_of, role=None, team=None) -> PlayerRate`.
  - `.lineup_rates(players: Sequence[PlayerInput], *, map_name, team1_id, team2_id, as_of, normalise: bool | None = None) -> dict[int, PlayerRate]`.
  - `.team_scales(players, *, team1_id, team2_id, as_of) -> tuple[float, float]`.
  - `.top_fragger_probs(players, map_name, team1_id, team2_id, team1_rounds, team2_rounds, *, as_of, rng=None, weights=None, n_samples=None, normalise=None) -> dict[str, float]`.
  - `.summary(as_of) -> dict`, `.n_records`, `.knows_player(id)`.
  - Persistence: `.save(path) -> Path` (`.npz`, no pickle), `PlayerKillModel.load(path)`.
- Functions:
  - `top_fragger_probs(kills: ArrayLike[n_samples, n_players], player_ids: Sequence[int], *, row_mask=None, row_weights=None) -> dict[str, float]` (ties split evenly).
  - `kills_mixture(rate, total_rounds, *, margin=None, weights=None, margin_center=None, score_skew=0.0, hist_margin_var=0.12) -> KillsMixture` (`.pmf(mask)`, `.distribution(mask)`, `.expected_rounds`).
  - `kills_distribution(...same...) -> KillsDistribution(pmf, mean, var)`.
  - `sample_kills(rate, own_rounds, opp_rounds, rng, *, margin_center=0.0, score_skew=0.0, hist_margin_var=0.12)`.
  - `effective_size(size, cv2, score_skew=0.0, hist_margin_var=0.0)`.
- Data classes:
  - `KillModelConfig` (frozen): `score_skew=0.3`, `normalise_teams=True`, `unknown_player_cv_mult=1.5`, `default_kpr=0.68`, shrinkage pseudo-counts, and more.
  - `PlayerRate` (frozen): `kpr`, `base_kpr`, `cv2`, `size`, `dpr`, `known`, `role`, `map_factor`, `opp_factor`, `team_scale`; `.scaled(f)`.

## `cs2bot.models.economy`

- `EconomyParams` (frozen dataclass, all fields configurable): `rounds_per_half=12`, `rounds_to_win=13`, `ot_rounds_per_half=3`, `ot_rounds_to_win=4`, `ot_switch_sides_at_start=False`, `max_overtimes=40` (simulation safety cap), `start_money=800`, `max_money=16000`, `ot_start_money=10000`, rewards (3250 elimination / 3250 time / 3500 defuse / 3500 detonation), loss bonus (`loss_bonus_base=1400`, `loss_bonus_step=500`, `loss_streak_max=4`, `starting_losses=1`, `loss_aversion=1`), `plant_bonus_team=600`, `planter_bonus=300`, `defuser_bonus=300`, `ct_team_kill_bonus=50`, `t_time_loss_survivor_bonus=False`, `kill_reward_by_buy=(300, 300, 450, 275)`.
  - `.loss_bonus(streak) -> int`, `.loss_bonus_ladder -> tuple[int, ...]`, `.regulation_rounds`, `.ot_rounds`.
- `BuyPolicy` and `RoundFlowParams` (frozen dataclasses of behaviour assumptions).
- Constants: `BUY_ORDER`, `BUY_CODE`, `PISTOL_CODE=0`, `ECO_CODE=1`, `FORCE_CODE=2`, `FULL_CODE=3`, `DEFAULT_ECONOMY`, `DEFAULT_BUY_POLICY`, `DEFAULT_ROUND_FLOW`.

## `cs2bot.models.sim_series` (exact series helpers, useful for pricing/backtest)

- `series_outcome(fmt: MatchFormat, p_maps: Sequence[float]) -> SeriesResult`, with `p_played: tuple[float, ...]`, `final: dict[(team1 maps, team2 maps), p]`, `.p_team1_win`, `.p_draw`, `.score_pmf()`.
- `series_sum_pmf(fmt, p_maps, pmf_if_team1_wins, pmf_if_team2_wins) -> NDArray`.
- `played_mask(fmt, team1_won[n_maps, n]) -> NDArray[bool]`.

`cs2bot.models.sim_stats` holds internal numerical helpers (NB pmf matrix, pmf utilities, `stable_seed`).

## Known limitations

- Only the round model is fitted from data. The BuyPolicy and RoundFlowParams settings (buy thresholds, force probabilities, end-reason mix, deaths, carried equipment) are documented heuristics. They could be estimated from demo data (P3) and tuned in backtests.
- The economy is team-level: money is the average per player, with no individual drops or AWP economy, and the $16k cap is applied to the average. Survivors' equipment is approximated.
- With default settings and equal teams the simulator gives about 22.3 rounds per map and about 14% overtime. That is somewhat more decisive than the synthetic generator (about 23.4 rounds, 18% overtime), because of the CS2 economy's momentum. Map-win probabilities still match the synthetic ground truth closely. Real-data calibration of the behaviour settings is recommended.
- When the starting side is unknown, the engine uses an exact 50/50 mixture. It does not infer who chooses the side (the non-picking team, or 'Team B' on the decider under Valve rules); pass start_sides once they are known. This matters most for pistol and half markets.
- The kills-score skew (gamma = 0.3) and the historical margin variance (0.12) are fixed, documented defaults. PlayerMapRecord carries no team rounds won, so they cannot be estimated in fit().
- Multi-map player-kill scopes assume maps are independent given the scenario, and treat the player's rate uncertainty as independent per map. This slightly understates the variance of multi-map totals for unknown players.
- Race-to-N for N > 13 (reachable only in overtime) is computed in MapSimOutput (with p_neither_race_to) but is not published in MapSlotPrediction, because the domain model has no 'neither' field.
- Map-level draws (Premier's single-overtime limit) are not supported: professional rules (unlimited overtime) are assumed. Overtime beyond 3 periods (about 0.4% of maps) is simulated as full buy against full buy.
- Low/high scenarios use max(2000, n_sims // 2) simulations by default (configurable via scenario_n_sims). They share common random numbers with each other but not with the central run.
- DEFAULT_SIMULATOR is a process-global cache (about 0.5 MB per cached 20k-sim output, 64 outputs by default) and is not thread-safe. Pass simulator=MapSimulator() per thread.
- Team normalisation needs at least 4 known lineup players per side. Unknown players' death rate defaults to the global mean.
- Overtime start money defaults to $10,000 (the Premier/Major setting). Events that use $12,500 need EconomyParams(ot_start_money=12500).

## Foundation change requests

- domain.predictions.MapSlotPrediction: add `p_neither_race_to: dict[str, float] = {}` (or `p_team2_race_to`) so race-to-N markets with N > 13 can be priced. The engine currently publishes only N ≤ 13, which is always decided.
- domain.match.LineupPlayer: add `role: PlayerRole | None = None`, so integration (P12) can build the engine's PlayerInput list (player_id, team slot, nickname, role) straight from MatchContext lineups. Alternatively, move PlayerInput into the domain.
- domain.match.MatchContext: add `start_sides: dict[str, Side | None]` (team1's starting side per map once the veto or side choice is announced), so services can pass start_sides to build_match_prediction. This matters for pistol and half markets.
- config.ModelSettings: add optional knobs currently passed as function arguments with documented defaults: `scenario_n_simulations` (default n//2), `ot_start_money` (10000; organiser-specific 12500), `round_model_half_life_days` (365), `kill_model_half_life_days` (120), `top_fragger_samples`.
- domain.records.PlayerMapRecord: add `team_rounds_won` and `opponent_rounds_won` (or have datasets supply the joined MapResultRecord). PlayerKillModel could then estimate the kills-score skew and remove score-explained dispersion from data instead of using fixed defaults.

## New dependency requests

- (none)
