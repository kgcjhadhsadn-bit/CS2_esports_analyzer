# ratings_veto: implementation report

**Tests:** `.venv/bin/python -m pytest tests/models/ratings_veto -q` - 183 passed, 0 failed (about 45 s with `-n 2` on the shared machine; lint/type clean: True; 174 before review round 1)

## Summary

P5 is complete: a hierarchical online Glicko-2 rating model per team and per (team, map), and a veto model that turns pick/ban history into a distribution over the maps a series is played on. Definition of done is met: 174/174 tests pass, `ruff check`, `ruff format --check` and `mypy` are clean on my paths, and the 37 foundation tests still pass. I wrote only inside my owned paths.

State I found: `ratings.py`, `ratings_glicko.py`, `veto.py` and a test suite of 159 tests already existed and passed. I re-verified them with independent experiments (walk-forward scoring against the generator's true probabilities, config sensitivity, a brute-force veto enumeration, a fuzz run with malformed records) and kept them. What I changed or added:

- **Veto `top_k` truncation was distorting map probabilities; fixed.** The 50 most likely sequences hold only 45-75% of a BO3's probability and 10-35% of a BO5's. Cutting and rescaling them moved the probability that a map is played by up to 0.26 (BO3, first veto unknown) and 0.51 (BO5). The default truncation now keeps every slot's map distribution exact (new `veto_truncation.py`), so map markets are priced from the same numbers as with the full list.
- **`WalkForwardRatings`**: a provider with the `predict` signature that answers for any past `as_of` without look-ahead, for ensemble training rows and backtests.
- `VetoModel.next_step` no longer offers a map that the known steps assign to a later step.
- The volatility update now uses the pre-inflation rating variance, as in the paper (negligible numerical effect).
- `predict_sequences` treats an empty `map_pool` as the default Active Duty pool instead of raising.
- 15 new tests (truncation, the provider, `next_step`, the truncated list through the match engine).

Measured on synthetic universes (16 teams, 600 matches; holdout = last 40% of matches, predicted walk-forward):

| Check | Result |
|---|---|
| Spearman, fitted overall rating vs true strength | 0.94 (seeds 3 and 4) |
| Log-loss, seed 3 / seed 4 | 0.449 / 0.585 (coin flip 0.693; true probabilities 0.398 / 0.523) |
| Expected calibration error (5 bins) | 0.02-0.06 |
| Error of p against the true probability | RMSE 0.10-0.15, with the reported `sigma` averaging 0.11-0.13 |
| Map offsets vs overall-only, universe with large map offsets | log-loss lower by more than 0.03 |
| Veto next-step top-1, ban / pick | 54-64% / 40-66% (chance about 22%) |
| Veto with the rating advantage term | log-likelihood per step better by 0.003-0.05 |

Design in brief:
- **Ratings.** Each team has an overall Glicko-2 rating plus a Gaussian offset per map (and optionally per LAN). One game updates all components with Kalman gains; with a single component this is exactly Glicko-2 (tested against the paper's worked example). The map offset is shrunk toward zero with weight `w ≈ n / (n + k)`, `k = 4 / map_offset_sd²` (about 11 games by default), reported as `map_weight`.
- **Time, roster, margin.** Deviation inflates by `sqrt(phi² + sigma² · periods)` with 7-day periods, capped at the initial value. Replaced players reset a fraction of the certainty (1 change 12%, 3 changes 70%, 5 changes 100%) and regress the rating halfway to the mean in proportion. The actual score is `0.9 · win + 0.1 · F(round share)`, always above 0.5 for a win.
- **Prediction.** `p_team1_win` is the logistic averaged over the rating uncertainty and `sigma` its standard deviation. `predict`, `rating` and `map_advantage` raise `ValueError` unless `as_of` is after the last processed game.
- **Veto.** Each step is a softmax over the remaining maps with utility `a · own preference + b · rating advantage + c · opponent's pick preference`. Preferences are recency-weighted (90-day half-life) and smoothed toward league rates; `a, b, c` per action are fitted by penalised maximum likelihood on walk-forward features. Sequences are enumerated exactly with a dynamic programme.

Defaults were tuned on synthetic data and are near the optimum of a coarse grid there. Margin of victory and the map-pick advantage did not help on synthetic data in a way that would transfer, so they stay mild / off; re-tune on real results with `tune`.

## Public API

### `cs2bot.models.ratings`

```python
class RatingConfig(BaseModel):          # frozen, extra="forbid"; Glicko-2 scale (1.0 = 173.7 display points)
    initial_mu: float = 0.0
    initial_phi: float = 2.0
    initial_sigma: float = 0.06
    tau: float = 0.5
    period_days: float = 7.0
    min_phi: float = 0.0
    min_sigma: float = 0.005
    max_sigma: float = 0.5
    volatility_eps: float = 1e-6
    map_offset_sd: float = 0.6          # 0 = overall-only Glicko-2
    map_offset_drift: float = 0.02
    mov_weight: float = 0.1             # margin of victory, 0 = plain win/loss
    lan_scale: float = 0.0              # LAN adjustment: strength differences scaled by 1 + lan_scale
    lan_offset_sd: float = 0.0          # optional per-team LAN offsets
    lan_offset_drift: float = 0.0
    pick_advantage: float = 0.0         # logits added to the team that picked the map
    tier_weights: dict[str, float] = {} # e.g. {"s": 1.5}; keys are Tier values
    roster_reset_fractions: tuple[float, ...] = (0.0, 0.12, 0.35, 0.7, 0.9, 1.0)
    roster_mu_regression: float = 0.5
    roster_offset_scale: float = 0.5
    equivalent_k_shrink: float          # property

class RatingSnapshot(BaseModel):        # frozen
    team_id: int; map_name: str | None; mu: float; phi: float; sigma: float; n_games: int
    last_played: datetime | None; r: float; rd: float       # r = 1500 + 173.7178 mu
    map_offset: float; map_offset_sd: float; map_weight: float; is_prior: bool

class MapRatingModel:
    def __init__(self, config: RatingConfig | None = None) -> None
    def fit(self, records: Iterable[MapResultRecord], *, record_history: bool = False) -> MapRatingModel
    def fit_until(self, records: Iterable[MapResultRecord], as_of: datetime, *, record_history: bool = False) -> MapRatingModel
    def update(self, record: MapResultRecord) -> bool       # False = duplicate / no winner / negative rounds; ValueError if out of order
    def predict(self, team1_id: int, team2_id: int, map_name: str, as_of: datetime, *, is_lan: bool = False,
                team1_lineup: Sequence[int] = (), team2_lineup: Sequence[int] = (),
                picked_by: TeamSlot | None = None) -> MapWinEstimate
    def predict_for_sequences(self, team1_id: int, team2_id: int, sequences: Sequence[MapSequenceProbability],
                              as_of: datetime, *, is_lan: bool = False, team1_lineup: Sequence[int] = (),
                              team2_lineup: Sequence[int] = ()) -> dict[str, MapWinEstimate]
    def map_advantage(self, team_id: int, opponent_id: int, map_name: str, as_of: datetime, *, is_lan: bool = False) -> float
    def rating(self, team_id: int, map_name: str | None = None, as_of: datetime | None = None) -> RatingSnapshot
    def ratings_table(self, map_name: str | None = None, as_of: datetime | None = None) -> list[RatingSnapshot]
    def copy(self) -> MapRatingModel
    def reset(self, *, record_history: bool = False) -> None
    def has_processed_match(self, match_id: int) -> bool
    # history (only on a model fitted with record_history=True; not saved)
    def snapshot_before(self, as_of: datetime) -> MapRatingModel
    def historical_predict(self, team1_id, team2_id, map_name, as_of, *, is_lan=False, team1_lineup=(), team2_lineup=(), picked_by=None) -> MapWinEstimate
    def historical_map_advantage(self, team_id, opponent_id, map_name, as_of, *, is_lan=False) -> float
    def historical_rating(self, team_id: int, map_name: str | None, as_of: datetime) -> RatingSnapshot
    def walk_forward(self) -> WalkForwardRatings
    # persistence (JSON)
    def save(self, path: str | Path) -> Path
    @classmethod
    def load(cls, path: str | Path) -> MapRatingModel
    def to_dict(self) -> dict[str, Any]
    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MapRatingModel
    @staticmethod
    def tune(records, grid, *, holdout_fraction=0.25, base_config=None) -> tuple[RatingConfig, dict[str, Any]]
    # properties: config, last_processed, n_processed, team_ids, records_history

class WalkForwardRatings:               # leakage-free for ANY as_of
    def __init__(self, model: MapRatingModel) -> None       # ValueError unless model.records_history
    @classmethod
    def fit(cls, records: Iterable[MapResultRecord], config: RatingConfig | None = None) -> WalkForwardRatings
    def predict(self, team1_id, team2_id, map_name, as_of, *, is_lan=False, team1_lineup=(), team2_lineup=(), picked_by=None) -> MapWinEstimate
    def map_advantage(self, team_id, opponent_id, map_name, as_of, *, is_lan=False) -> float
    def rating(self, team_id: int, map_name: str | None, as_of: datetime) -> RatingSnapshot
    model: MapRatingModel               # property

def tune(records: Iterable[MapResultRecord], grid: Mapping[str, Sequence[Any]], *, holdout_fraction: float = 0.25,
         base_config: RatingConfig | None = None) -> tuple[RatingConfig, dict[str, Any]]
def evaluate_walk_forward(model: MapRatingModel, records: Iterable[MapResultRecord]) -> WalkForwardResult
def chronological_split(records: Iterable[MapResultRecord], holdout_fraction: float = 0.25) -> tuple[list[MapResultRecord], list[MapResultRecord]]
```

- `MapWinEstimate.components` keys: `overall` (p from overall ratings only), `map` (= `p_team1_win`), `logit_mean`, `logit_sd`, `team1_phi`, `team2_phi`, `team1_map_weight`, `team2_map_weight`, `team1_map_games`, `team2_map_games`, `team1_roster_changes`, `team2_roster_changes`, `lan`, `pick_shift`. `predict_for_sequences` adds `p_picked_by_team1` / `p_picked_by_team2` when the picker of a map differs between sequences; its `p_team1_win` is then the probability-weighted mean over the picker scenarios and `sigma` the standard deviation of that mixture (law of total variance).
- `WalkForwardResult` has `predictions`, `n`, `log_loss`, `brier`, `accuracy`, `log_loss_of(overall=...)`, `brier_of(overall=...)`, `reliability(n_bins) -> list[ReliabilityBin]`, `ece(n_bins)`.
- The `tune` report holds `best_params`, `best_log_loss`, `best_brier`, `baseline_log_loss`, `n_train`, `n_holdout` and `results` (sorted best first).
- `MapRatingModel` and `WalkForwardRatings` both satisfy `cs2bot.features.RatingProvider`. `features.rows.rating_estimate` already uses `historical_predict` when the model has recorded history.

### `cs2bot.models.veto`

```python
class VetoTemplateStep(NamedTuple):
    action: VetoAction
    actor: int | None                   # 0 = first-veto team, 1 = the other team, None = leftover

VETO_TEMPLATES: dict[MatchFormat, tuple[VetoTemplateStep, ...]]     # 7-map pool
BO1_TEMPLATE_2_3_1: tuple[VetoTemplateStep, ...]                    # alternative BO1 order (assumption, see limitations)
def veto_template(fmt: MatchFormat, n_maps: int = 7) -> tuple[VetoTemplateStep, ...]
MapAdvantageFn = Callable[[int, int, str, datetime], float]         # (team, opponent, map, as_of) -> logit

class VetoConfig(BaseModel):            # frozen
    half_life_days: float = 90.0
    team_prior_strength: float = 3.0
    league_prior_strength: float = 20.0
    use_opponent_preference: bool = True
    ridge: float = 1.0
    prune_probability: float = 1e-9
    default_map_pool: tuple[str, ...] = tuple(DEFAULT_ACTIVE_MAP_POOL)

class VetoModel:
    def __init__(self, config: VetoConfig | None = None) -> None
    def fit(self, records: Iterable[VetoRecord], *, map_advantage: MapAdvantageFn | None = None) -> VetoModel
    def fit_until(self, records: Iterable[VetoRecord], as_of: datetime, *, map_advantage: MapAdvantageFn | None = None) -> VetoModel
    def update(self, record: VetoRecord) -> bool            # adds counts, keeps coefficients
    def predict_sequences(self, team1_id: int, team2_id: int, fmt: MatchFormat, map_pool: Sequence[str], *,
                          first_veto: TeamSlot | None = TeamSlot.TEAM1, known_steps: Sequence[VetoStep] = (),
                          top_k: int | None = 50, as_of: datetime | None = None,
                          map_advantage: MapAdvantageFn | None = None,
                          template: Sequence[VetoTemplateStep] | None = None,
                          preserve_marginals: bool = True) -> list[MapSequenceProbability]
    def next_step(self, team1_id, team2_id, fmt, map_pool, *, first_veto=TeamSlot.TEAM1, known_steps=(),
                  as_of=None, map_advantage=None, template=None) -> StepForecast
    def preference(self, team_id: int, map_name: str, action: VetoAction, as_of: datetime | None = None) -> float
    def preferences(self, team_id: int, action: VetoAction, map_pool: Sequence[str], as_of: datetime | None = None) -> dict[str, float]
    def copy(self) -> VetoModel
    def save(self, path: str | Path) -> Path
    @classmethod
    def load(cls, path: str | Path) -> VetoModel
    def to_dict(self) -> dict[str, Any]
    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VetoModel
    # properties: config, coefficients, coefficients_with_advantage, advantage_failures, last_veto_time, n_records, team_ids

def map_slot_distribution(sequences: Sequence[MapSequenceProbability]) -> dict[int, dict[str, float]]   # slot is 1-based
def p_map_played(sequences: Sequence[MapSequenceProbability], map_name: str, *, slot_reach: Mapping[int, float] | None = None) -> float
```

- `VetoCoefficients`: `a_ban, b_ban, c_ban, a_pick, b_pick, c_pick, n_steps, log_likelihood`. `StepForecast`: `step, action, team, probabilities`.
- `MapSequenceProbability.maps` is the playing order: picks in pick order, then the leftover map. Lengths are 1 (BO1), 2 (BO2, no decider), 3 (BO3) and 5 (BO5); `pickers` has `None` for the leftover. This is the shape `build_match_prediction` expects.
- An empty `map_pool` means `config.default_map_pool`.
- `first_veto=None` is a 50/50 mixture of both orders; known steps that name a team resolve the order. A complete known veto returns one sequence with probability 1.
- Without `template=`, known steps that contradict the default order but fit `BO1_TEMPLATE_2_3_1` (BO1, 7 maps: the first two bans by the same team) switch the remaining actors to that order.
- `top_k`: at most `top_k` sequences, probabilities sum to 1. With `preserve_marginals=True` (default) `P(slot k is map m)`, `P(map m picked by team1 / team2 / decider)` and `P(slot k picked by team1)` are exact when `top_k` ≥ 19 (BO3) / 31 (BO5) on seven maps with a known first veto, ≥ 26 / 38 with an unknown one (below that only the slot marginals, then plain rescaling). `top_k=None` returns every sequence (up to 420 for a BO3, 5040 for a BO5).
- `p_map_played` without `slot_reach` is the probability that the map is in the sequence; pass `slot_reach={3: p_decider_reached}` for the probability that it is actually played.

### `cs2bot.models.veto_truncation`, `cs2bot.models.ratings_glicko`

- `truncate_sequences(totals: Mapping[tuple[tuple[str, P], ...], float], top_k: int | None, *, preserve_marginals: bool = True) -> list[tuple[tuple[tuple[str, P], ...], float]]`, `slot_marginal_rank(keys) -> int` (slot cells only) and `marginal_rank(keys) -> int` (slot and picker cells: the tail size used by default).
- `ratings_glicko`: `glicko2_rate`, `volatility_update`, `g`, `expected_score`, `inflate_phi`, `mov_score`, `mr12_map_win_probability`, `logistic_normal_moments`, `to_display`, `from_display`, `logit`, `sigmoid`.

### Wiring example

Training (`services.registry.train_models`):

```python
from cs2bot.models.ratings import MapRatingModel
from cs2bot.models.veto import VetoModel

now = utcnow()
ratings = MapRatingModel().fit_until(map_results, now, record_history=True)
veto = VetoModel().fit_until(veto_records, now, map_advantage=ratings.historical_map_advantage)
ratings.save(artifacts / "ratings.json")
veto.save(artifacts / "veto.json")
# ensemble training rows: pass `ratings` (history recorded) or `ratings.walk_forward()` as rating_provider
```

Prediction (`services.prediction.predict_match`):

```python
ratings = MapRatingModel.load(artifacts / "ratings.json")
veto = VetoModel.load(artifacts / "veto.json")
as_of = utcnow()
sequences = veto.predict_sequences(
    ctx.team1_id, ctx.team2_id, ctx.format, ctx.map_pool,
    first_veto=ctx.first_veto_team,          # None when unknown
    known_steps=ctx.known_veto,
    as_of=as_of, map_advantage=ratings.map_advantage,
)
estimates = ratings.predict_for_sequences(
    ctx.team1_id, ctx.team2_id, sequences, as_of,
    is_lan=ctx.is_lan,
    team1_lineup=[p.player_id for p in ctx.team1_lineup],
    team2_lineup=[p.player_id for p in ctx.team2_lineup],
)   # or one MapWinEstimate per map from the ensemble
prediction = build_match_prediction(..., veto_distribution=sequences, map_estimates=estimates, ...)
```

Incremental use between trainings: `ratings.update(record)` and `veto.update(record)` in chronological order.

## Known limitations

- **Never tested on real data.** HLTV and PandaScore are unreachable from this machine, so every default comes from the synthetic universe, whose vetoes follow true map strength with little noise and always start with team1. Run `tune` on real results before trusting the defaults, especially `initial_sigma`, `map_offset_sd`, `mov_weight` and `pick_advantage`.
- **Timestamps in the future block predictions.** `predict`, `rating`, `map_advantage` and `predict_sequences(as_of=...)` raise `ValueError` when `as_of` is not after the newest fitted record. One future-dated row in the database would therefore stop every prediction; train with `fit_until(records, now)` as in the wiring example.
- **A late result needs a refit.** `update` raises `ValueError` for a record older than the last processed one.
- **A veto model fitted without `map_advantage` ignores `map_advantage` at prediction time**, because it has no weight for that term.
- **Veto order is a template, not learned.** The model does not learn which team vetoes first or which order a tournament uses. Pass `template=` for other orders. `BO1_TEMPLATE_2_3_1` is my reading of the Valve Major rules and is unverified.
- **Truncation approximates the joint distribution.** Slot marginals and per-map / per-slot picker marginals are exact, but which maps occur together in one series (and the picker of a map in a particular slot) is only approximate in a 50-sequence list. Pass `top_k=None` for the exact distribution; the match engine's cost depends on the number of distinct maps, not sequences, but the list is stored in `MatchPrediction.veto_distribution`.
- **Truncation cost on larger pools.** The linear programme takes up to 0.4 s for a BO5 on seven maps and about 2.2 s on nine maps with an unknown first veto (30,240 sequences).
- **Recorded history is in memory only.** A loaded model cannot serve `historical_*` queries or `WalkForwardRatings`; refit with `record_history=True`.
- **Volatility barely moves.** With a handful of games per 7-day period the Glicko-2 volatility stays close to `initial_sigma` (that is how the algorithm behaves), so `initial_sigma` effectively sets how fast uncertainty grows during inactivity.
- **Roster rotation.** A team that rotates a sixth player is charged one roster change at every swap (12% reset by default).
- **Veto fitting time.** Fitting with `historical_map_advantage` calls the rating model 14 times per veto (about 3 s for 600 vetoes).

## Foundation change requests

- `docs/ARCHITECTURE.md`, P5 section: update the `predict_sequences` signature to `top_k: int | None = 50, as_of=None, map_advantage=None, template=None, preserve_marginals=True`, and mention `WalkForwardRatings` / `record_history=True` as the leakage-free provider for historical rows.
- `docs/dev/STATUS.md`: set ratings_veto to done with 183 tests, reviewed (round 1).
- `src/cs2bot/models/__init__.py` (shared, not edited): optionally re-export `MapRatingModel`, `RatingConfig`, `WalkForwardRatings`, `VetoModel`, `VetoConfig`.

## New dependency requests

None. The truncation uses `scipy.optimize.linprog` (HiGHS), which ships with the scipy already installed.

## Review round 1

Independent review of the whole package (brief incl. addendum, report, all source and tests). Checks re-run after the fixes: 180/180 package tests (about 20 s with `-n 2`), `ruff check`, `ruff format --check` and `mypy` clean on the owned paths, 37/37 foundation tests, and the features tests that use the rating provider still pass.

What I verified (re-derived, not trusted):
- Glicko-2 maths against the paper: `g`, `E`, `v`, `Delta`, the Illinois iteration (bracket choice, `f(x)`, halving rule, stop criterion), `phi* / phi' / mu'`, the fractional-period generalisation; the multi-component Kalman split reduces exactly to Glicko-2 with one component (posterior variance `P/(1+P/v)`, mean step `P g (s-E)/(1+P/v)`).
- Time inflation `min(phi0, sqrt(phi^2 + c sigma^2))`, map-offset drift and caps; first game on a map starts from the overall rating with the prior offset variance; shrinkage weight `1 - psi^2/psi0^2`.
- MR12 + MR3 overtime win probability (regulation `C(12+k, k) p^13 q^k`, 12-12 tie, overtime as a race to 4 of 6 with 3-3 repeats); `mov_score` keeps a win strictly above 0.5.
- No look-ahead: `predict`/`rating`/`map_advantage` refuse `as_of <= last`; `fit_until` is strict; history lookups use `bisect_left` (games at `as_of` excluded); veto features are computed before ingesting the veto and the advantage callable is queried at the veto's own time; walk-forward evaluation predicts a match before any of its maps is processed. `features.rows.rating_estimate` and both providers agree on the `predict` / `historical_predict` contract.
- Veto: templates per format and generalised pool sizes, exact DP vs brute force (existing test), Bayesian conditioning on known steps (path likelihood of known steps reweights the first-veto mixture), complete known vetoes, the output shape consumed by `build_match_prediction` (BO2 length 2, leftover picker `None`).

Issues found:
1. **Major, fixed - truncation biased the pickers when the first veto is unknown.** The marginal-preserving cut only constrained `P(slot k is map m)`. For two evenly rated (e.g. unknown) teams with `first_veto=None`, exact probability ties between mirrored sequences were broken toward team1 and the LP put the tail on team1-first sequences: the default 50-sequence list said team1 makes the first pick with probability 0.945 (BO3) / 0.997 (BO5) instead of 0.5, and `predict_for_sequences` / the stored `veto_distribution` / the LLM rationale saw those pickers. Production calls with `first_veto=None` whenever the order is unknown, so this was the common case. The LP now also preserves `P(map m picked by team1 / team2 / decider)` and `P(slot k picked by team1 / team2)` (rank 26 / 38 instead of 19 / 31 with an unknown order, unchanged with a known one); when `top_k` is too small for that it falls back to slot marginals only, then plain rescaling. New `marginal_rank`. Tests: picker marginals exact for unknown and known teams (BO3/BO5), `predict_for_sequences` identical from the full and the cut list, ranks.
2. **Minor, fixed - `predict_for_sequences` sigma for mixed pickers** was the weighted average of the scenario sigmas, which ignores the spread between the scenario means; it is now the mixture standard deviation (law of total variance). Test updated.
3. **Minor, fixed - negative round counts** (malformed external data) were scored as `s = 0.5` updates (`mov_score` returns 0.5 for a non-positive total) instead of being skipped. `update` now returns `False` for them and `evaluate_walk_forward` skips them. Test added.
4. **Minor, fixed - 2-3-1 BO1 vetoes with the default template.** Published steps whose teams contradict the alternating BO1 order kept the template's actors for the remaining steps (e.g. step 5 predicted for team1 after team1, team1, team2, team2). Without `template=`, known steps that do not fit the default order but fit `BO1_TEMPLATE_2_3_1` now switch to it (an explicit `template=` always wins). Test added.

Second pass (same round, after the work above had been saved). I re-ran every check, re-read all
four modules against the brief and the paper, and tried to break them: a 3,000-record fuzz of the
rating model (random teams, maps incl. blank and unknown names, scores incl. 13-13 / negative /
100-2 / 1-0, lineups of 0-6 players, LAN, tiers, pickers, time gaps of 0 s to 400 days) under the
default, an all-features-on and an overall-only config (no exception, every `p` in (0, 1), every
component finite, save/load gives identical predictions); a 2,000-map 13-0 streak (stable, p =
0.9996); a 400-record malformed veto fuzz (duplicate / zero / out-of-range step numbers, blank map
names, empty pools, advantage callables returning NaN / inf / 1e9 or raising) followed by 400 random
`predict_sequences` calls (every result sums to 1 with the format's map count and no repeated map;
the only exceptions are the documented `ValueError`s for pools too small for the format or known
steps that cannot fit the pool); slot marginals of the default 50-sequence list against the full
distribution for BO3 / BO5 with known and unknown first veto (max error 2e-15; 0.02-0.34 s).
Further issues:

5. **Minor, fixed - blank map names created a shared `""` map offset.** A record whose map name
   normalises to an empty string (malformed source data) updated an offset keyed `""`, lumping
   unrelated maps together. Such records now update the overall ratings only. Test added.
6. **Minor, fixed - `evaluate_walk_forward` scored duplicate rows twice.** The same
   `(match_id, map_number)` delivered twice was processed once by `update` but predicted twice,
   biasing `tune` toward matches with duplicated rows. Duplicates are now scored once. Test added.
7. **Minor, fixed - `predict_for_sequences` dropped maps when `pickers` was shorter than `maps`**
   (`zip(strict=False)`); missing pickers now count as unknown (`None`), as in the match engine.
   Test added.

Checks after the second pass: 183/183 package tests, `ruff check`, `ruff format --check` and `mypy`
clean on the owned paths, 37/37 foundation tests, `tests/learning/test_features_rows.py` (the
rating-provider consumer) passes. Public API unchanged.

Not a defect of this package, noted for the integration work: `build_match_prediction` merges
sequences with the same map order and keeps the first sequence's pickers, so with an unknown first
veto the engine's per-sequence pickers are not meaningful (it does not use them today); per-map
picker information should come from `predict_for_sequences`, which aggregates them correctly.

Remaining concerns (not changed):
- `_check_as_of` makes one future-dated result block every prediction (already listed under known limitations; the wiring's `fit_until(records, now)` avoids it).
- Malformed `known_steps` (duplicate step numbers or maps) raise `ValueError` from `predict_sequences`; the prediction service should catch it and fall back to no known steps.
- Forfeits recorded as 1-0 / 16-0 maps are rated as full wins; the data layer should drop them if they appear.
- All defaults still come from synthetic data (see known limitations).
