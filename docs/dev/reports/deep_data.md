# deep_data: implementation report

Review round 1 (see the last section): `tests/learning tests/models tests/ingest tests/odds tests/foundation` - 1584 passed, 18 skipped; whole suite (`tests -n 2`) - 3171 passed, 21 skipped, 0 failed; ruff, ruff format, mypy clean on every owned path. New review tests: `tests/learning/test_deep_review.py` (16).

Tests: `.venv/bin/python -m pytest tests/learning tests/models tests/ingest tests/odds -q -n 2` - 1531 passed, 18 skipped (PostgreSQL / browser), 0 failed (2.5 min) (deep_data's own new modules: `test_deep_*`, 58 tests in 7 files: `tests/learning/test_deep_{features,confidence,ensemble}.py`, `tests/odds/test_deep_odds_movement.py`, `tests/ingest/hltv/test_deep_hltv_proxy.py`, `tests/ingest/pandascore/test_deep_pandascore_optional.py`). Whole suite (`tests -n 2`): 3132 passed, 14 skipped, 0 failed (6 min). `tests/foundation`: passed. `ruff check`, `ruff format --check` and `mypy` are clean on every owned path.

## Summary

Requirement by requirement:

1. **New leakage-safe features** - `cs2bot.features.deep.DeepFeatureIndex`, built by `FeatureStore.build(..., deep=DeepData)` / `FeatureStore.from_session(..., with_deep=True)` and added to every row by `match_map_features` (the single code path of `build_training_rows` and `features_for_match_map`, so training and live rows cannot drift apart). 48 team factors (as `diff_dp_*`; `t1_`/`t2_` too with `DeepConfig(team_columns="all")`) and 22 match factors plus 11 market columns (12 since review round 1: `mkt_lead_hours`):
   * schedule / fatigue (maps and series in 3 / 7 days, rest days, consecutive play days, second series the same day, maps today);
   * travel and time zones (venue country/region/UTC offset vs the team's home from `teams.country`, the HLTV extra or the players' countries; curated table in `features/geo.py` with ~100 countries and ~100 event cities; online events = no travel), first match after travel, days at the event;
   * stakes (`features/stakes.py`: grand final / final / semi / quarter / playoff / lower / upper bracket / elimination / decider / qualification / Swiss / group / opening / low-stakes from the stage text, an ordinal `stakes_level`, the team's record in the event before the match, Swiss elimination / qualification match from that record);
   * format specialists (BO1 vs BO3 series win rate and gap, BO3 map win rate, decider-map win rate and count, own-pick and opponent-pick edge over the overall map win rate);
   * economy and clutch (pistol win rate per side, conversion of rounds 2-3 after a pistol win and recovery after a loss, force-buy and eco win rate against a full buy, eco / force upsets conceded, post-plant and retake win rate on this map, 1v1 / 1vX clutch win rates and clutches per map from `player_round_stats`);
   * player match-ups (demo kills): lineup opening-duel win rate per side (all maps and this map), the main T-side opener's win rate (entry), share of deaths traded, the AWPer's AWP-vs-AWP duel win rate and AWP kill share, AWPer-vs-AWPer head-to-head kills, lineup-vs-lineup kills on this map, and an opening-duel edge **by map area** (team1's T side vs team2's CT side in the same `victim_place`, minus the reverse);
   * roster and meta: days since the last coach change (`roster_periods.is_coach`), coach change in 90 days, days since the map (re-)entered Active Duty (new maps such as Cache are less predictable); roster change, stand-ins, patch recency, form trend and change points already existed;
   * **market signal** (`cs2bot.odds.movement`): Betclic match-winner opening and current de-vigged probability, logit move, number of moves, net steam moves, hours since the last move, hours quoted, overround, and the optional sharp benchmark (`sharp_p_last`, `sharp_minus_mkt`). `SharpOddsSource` protocol with `DisabledSharpOddsSource` (default) and `JsonFileSharpOddsSource`; `closing_line_reference` gives the CLV reference (sharp close when available, else Betclic's close). `DEFAULT_ACTIVE_MAP_POOL` verified and updated to `[ancient, anubis, cache, dust2, inferno, mirage, nuke]` (Cache entry in `config/patches.yaml` now verified: 2026-07-08).
   * **No look-ahead, tested**: for random maps of a synthetic universe with kills, clutches, venues, stages and market history, every column of the training row built from the full history equals the row from a store that only saw data before the match started (`test_rows_from_full_history_equal_rows_from_history_cut_at_kickoff`); live rows equal training rows (`test_live_rows_equal_training_rows`). Writing that test found and fixed a real leak in the first draft: a series was dated by its first map, so a previous series still being played at `as_of` contributed its later maps; series are now dated by their last map.
2. **Calibration for the confidence rule** (`cs2bot.models.confidence`): reliability over `[0.75, 1.0]` in 2.5-pp bins with Wilson intervals (`high_confidence_reliability`, `high_confidence_report`); a conservative monotone `HighConfidenceShrinker` (Beta posterior per bin around a skeptical prior: without history a probability keeps half of its excess over 0.75 - 0.95 becomes 0.85; a well-populated calibrated bin converges back to `p`; an over-confident bin is pulled to its observed rate; never raises a probability); a per-market `MarketTrust` verdict (enough history overall and above 0.75, ECE within tolerance, not over-confident above 0.75 by more than the tolerance at 95%); `ConfidenceGuard` bundles both per market type with a pooled fallback and persists as JSON. The ensemble's `FitReport.high_confidence` and the backtest's `BacktestReport.high_confidence` / `market_trust` now report the range; `BacktestReport.predictions` (not serialised) feeds `ConfidenceGuard.from_predictions`.
3. **HLTV fallback on a VPS**: `HLTVFetcher` retries a blocked fetch (challenge / 403 / 429, or the direct route's cool-down) through `settings.hltv.proxy_url` with its own exponential cool-down (`proxy_cooldown_base_s` / `proxy_cooldown_max_s`, persisted in `_cooldown_proxy.json`) and its own browser profile (`<profile>-proxy`). `proxy_mode="after_block"` (default) or `"always"`. Challenges are never solved on either route; when both routes are cooling down no request is made.
4. **PandaScore optional**: every PandaScore sync (`sync_upcoming`, `sync_past`, `sync_modified`, `sync_match`) returns an empty report with `details["disabled"] = True` and a single warning without touching the network or the DB when no token is configured; one INFO "PandaScore disabled" note per process. HLTV-only matches get a **derived tier** from HLTV stars and the teams' ranks (`ingest.scope.derive_tier`, written to `matches.tier` with `extra.hltv.tier_source = "derived"`), so scope, datasets (tier features) and completeness work with HLTV alone. The scheduler already skips the PandaScore jobs without a token (integration).
5. **Retrain on the synthetic universe** - see the tables below. The deep block is guarded by a **feature-group screen** in the ensemble (`EnsembleConfig.screen_feature_groups`, default on): the deep group and the market group are each added only when they improve the stacked out-of-fold log-loss of the pre-holdout data by at least `screen_min_gain` (0.0005) **and** by at least one standard error of the paired per-match loss difference. Without the screen, adding ~80 columns that carry no signal in the synthetic universe made the holdout log-loss worse in one seed (see `deep_unscreened`); with it, calibration and log-loss do not get worse.
6. Report, tests, lint, types: this file.

### Synthetic retrain (requirement 5)

`cs2bot.backtest.compare_feature_sets` on `generate_universe(n_teams=20, n_matches=1200, days=540, drift_sd_per_30d=0.08)` with `synthetic_deep_data` (duels from the simulated kills, an AWP weapon for AWPers, match-winner price history around the true series probability: opening noise 0.06, closing noise 0.02, margin 6%), `EnsembleConfig(holdout_days=90, n_bootstrap=2)`. Holdout metrics are the cross-fitted calibrated probabilities on the latest 90 days (honest out-of-sample). The synthetic universe has no venues, stage texts, clutch rows or coach changes, and its outcomes are generated from team strength only - so the deep factors carry no real signal there and the correct behaviour is to *not hurt*.

Seed 7 (holdout: latest 90 days; "n >= 0.75" = calibrated holdout predictions folded to the favourite's side):

| variant | features | log-loss | Brier | ECE | rating-only log-loss | n >= 0.75 | hit rate | mean p |
|---|---|---|---|---|---|---|---|---|
| baseline (pre-deep_data features) | 251 | 0.4609 | 0.1489 | 0.0255 | 0.4551 | 248 | 0.843 | 0.883 |
| deep (default, screened) | 251 | 0.4609 | 0.1489 | 0.0255 | 0.4551 | 248 | 0.843 | 0.883 |
| deep_unscreened (forced in) | 331 | 0.4705 | 0.1517 | 0.0341 | 0.4551 | 224 | 0.848 | 0.894 |
| deep_market (default, screened) | 251 | 0.4609 | 0.1489 | 0.0255 | 0.4551 | 248 | 0.843 | 0.883 |

Seed 11:

| variant | features | log-loss | Brier | ECE | rating-only log-loss | n >= 0.75 | hit rate | mean p |
|---|---|---|---|---|---|---|---|---|
| baseline | 251 | 0.4161 | 0.1300 | 0.0375 | 0.4021 | 267 | 0.906 | 0.909 |
| deep (default, screened) | 251 | 0.4161 | 0.1300 | 0.0375 | 0.4021 | 267 | 0.906 | 0.909 |
| deep_unscreened (forced in) | 331 | 0.4151 | 0.1299 | 0.0495 | 0.4021 | 247 | 0.907 | 0.922 |
| deep_market (default, screened) | 251 | 0.4161 | 0.1300 | 0.0375 | 0.4021 | 267 | 0.906 | 0.909 |

Screen decisions (pre-holdout out-of-fold, paired per-match gain +- standard error): seed 7 deep -0.0012 +- 0.0013 (dropped), market +0.0012 +- 0.0018 (dropped: inside the noise); seed 11 deep -0.0014 +- 0.0015, market +0.0005 +- 0.0010 (both dropped). An earlier version of the screen (fixed minimum gain only) kept the market group in seed 7 on a +0.0012 out-of-fold gain and the holdout log-loss then got worse (0.4702) - hence the standard-error rule (`screen_z = 1`).

Feature importance (seed 7, share of LightGBM gain, final + bootstrap models), baseline vs the deep block forced in:

| baseline feature | gain share | deep_unscreened feature | gain share |
|---|---|---|---|
| rating_p | 0.161 | rating_p | 0.186 |
| rating_c_logit_mean | 0.138 | rating_logit | 0.111 |
| rating_logit | 0.122 | rating_c_logit_mean | 0.103 |
| strength_logit | 0.072 | strength_logit | 0.060 |
| strength_p_win | 0.038 | strength_p_win | 0.044 |
| diff_map_ban_rate_180d | 0.031 | rating_c_map | 0.042 |
| diff_elo | 0.026 | diff_map_ban_rate_180d | 0.032 |
| diff_retake_win_pct_180d | 0.025 | diff_elo | 0.025 |
| rating_c_overall | 0.022 | diff_retake_win_pct_180d | 0.023 |
| rating_c_map | 0.021 | **diff_dp_lost_to_force** | 0.021 |
| diff_pistol_win_pct_180d | 0.017 | rating_c_overall | 0.017 |
| diff_round_diff_90d | 0.012 | diff_pistol_win_pct_180d | 0.014 |
| diff_map_round_diff_90d | 0.010 | **diff_dp_pistol_recovery** | 0.011 |
| diff_opp_pick_win_rate_365d | 0.010 | diff_round_diff_90d | 0.009 |
| t1_elo | 0.006 | **diff_dp_entry_open_t_win** | 0.008 |

Reproduce: `compare_feature_sets(generate_universe(n_teams=20, n_matches=1200, seed=7, days=540, drift_sd_per_30d=0.08), deep=synthetic_deep_data(uni, seed=7), include_unscreened=True)` then `markdown_summary(report)` (about 2 minutes).

Reading: on data where the deep factors carry no signal, the screen drops them, so the default model reproduces the baseline exactly - calibration and log-loss do not get worse. Forced in, the ~80 extra columns cost up to 0.01 log-loss and 0.01 ECE (seed 7) or are neutral (seed 11); the economy / entry features that LightGBM then picks up are partly redundant with existing ones. On real data, where fatigue, travel, stakes, economy and duel records are not noise, the same screen keeps a group as soon as its gain beats its noise. The high-confidence columns show why requirement 2 matters: in seed 7 the calibrated holdout predictions above 0.75 average 0.883 but win 84.3 % - four points over-confident, exactly what `HighConfidenceShrinker` and the market trust check are for.

## Public API

### `cs2bot.datasets` (new: `datasets/deep.py`)

```python
@dataclass(frozen=True) MatchMetaRecord(match_id, scheduled_at, team1_id, team2_id, status=SCHEDULED, format=None,
                                        tier=UNRANKED, is_lan=False, stage=None, tournament_id=None,
                                        tournament_name=None, location=None)
@dataclass(frozen=True) TeamMetaRecord(team_id, country=None, player_countries=())
@dataclass(frozen=True) ClutchRecord(match_id, map_number, map_name, played_at, team_id, player_id, opponents, won)
@dataclass(frozen=True) DuelRecord(match_id, map_number, map_name, played_at, attacker_id, victim_id,
                                   attacker_team_id=None, victim_team_id=None, attacker_side=None, weapon=None,
                                   victim_weapon=None, is_opening=False, is_trade=False, was_traded=False,
                                   victim_place=None)
@dataclass(frozen=True) CoachChangeRecord(team_id, started_on: date, player_id=None)
@dataclass(frozen=True) MarketPoint(captured_at, team1_odds: float | None, team2_odds: float | None)

@dataclass DeepData(match_meta=(), team_meta={}, clutches=(), duels=(), coach_changes=(), market={}, sharp={})
DeepData.from_session(session, *, since=None, until=None, in_scope_only=True, with_demo=True, with_market=True,
                      sharp: SharpOddsSource | None = None) -> DeepData      # each part loaded defensively
DeepData.cut(until) -> DeepData                                                  # drop everything observed >= until

load_match_meta(session) -> list[MatchMetaRecord]            # every non-cancelled match with two teams (static facts)
load_team_meta(session) -> dict[int, TeamMetaRecord]
load_clutch_records(session, *, since=None, until=None, in_scope_only=True) -> list[ClutchRecord]
load_duel_records(session, *, since=None, until=None, in_scope_only=True) -> list[DuelRecord]
load_coach_changes(session, *, until=None) -> list[CoachChangeRecord]
```

### `cs2bot.features`

```python
FeatureConfig(..., deep_features: bool = True, deep: DeepConfig = DeepConfig())     # new fields
FeatureStore.build(map_results, *, player_maps=(), rounds=(), vetoes=(), team_demo_stats=(), config=None,
                   patch_timeline="default", deep: DeepData | None = None) -> FeatureStore
FeatureStore.from_session(session, *, since=None, until=None, in_scope_only=True, with_rounds=True, with_demo=True,
                          config=None, patch_timeline="default", with_deep=True) -> FeatureStore
store.deep -> DeepFeatureIndex | None                          # None only with FeatureConfig(deep_features=False)
match_map_features(store, ..., stage: str | None = None)       # new keyword; exclude_match_id = the match predicted
features_for_match_map(ctx, ...)                               # passes ctx.stage automatically

DeepConfig(format_window_days=365, economy_window_days=180, map_round_window_days=365, clutch_window_days=180,
           duel_window_days=180, map_duel_window_days=365, h2h_window_days=365, min_open_attempts=10,
           min_awp_kills=30, min_awper_share=0.25, swiss_wins_needed=3, swiss_losses_needed=3,
           with_market=True, team_columns: Literal["diff", "all"] = "diff",
           market_lead_hours: tuple[float, float] | None = (0.25, 48.0))     # review round 1
DeepFeatureIndex(records, *, rounds=(), deep=None, patch_timeline=None, config=None)
index.features(team1_id, team2_id, map_name, as_of, *, match_id=None, exclude_match_id=None, team1_lineup=(),
               team2_lineup=(), is_lan=None, stage=None) -> (team1: dict, team2: dict, match: dict)
index.market_reference(match_id, ts, kick) -> float   # epoch s the market block reads (review round 1)
DEEP_TEAM_FEATURES, DEEP_MATCH_FEATURES (now incl. "mkt_lead_hours"), deep_feature_names(team_columns="diff")

classify_stage(text) -> StageInfo  (.features() -> stage_* + stakes_level); swiss_situation(wins, losses, ...)
parse_location(text) -> Place | None; team_home(country, player_countries=()) -> Place | None; tz_shift_hours(a, b)
```

### `cs2bot.odds` (new modules, nothing in mapping / linking changed; `odds/__init__.py` not edited)

```python
from cs2bot.odds.movement import (MovementConfig, PricePoint, SelectionMovement, selection_movement, devig_two_way,
                                  market_features, MARKET_FEATURE_NAMES, load_selection_history,
                                  load_match_winner_history)
selection_movement(history: Sequence[PricePoint], as_of, *, config=MovementConfig()) -> SelectionMovement
    # opening/current odds, n_moves, steam_up/steam_down, prob_change, hours_since_last_move, hours_quoted, min/max
market_features(points: Sequence[MarketPoint], as_of, *, flip=False, sharp=(), config=...) -> dict[str, float]
load_selection_history(session, selection_ids, *, until=None) -> dict[int, list[PricePoint]]
load_match_winner_history(session, *, until=None) -> dict[int, list[MarketPoint]]   # AUTO/CONFIRMED links only

from cs2bot.odds.sharp import (SharpOddsSource, SharpQuote, DisabledSharpOddsSource, JsonFileSharpOddsSource,
                               closing_line_reference, ClosingReference, sharp_source_from_settings)
class SharpOddsSource(Protocol): enabled: bool; quotes(match_id, *, until=None) -> list[SharpQuote]
closing_line_reference(session, selection_id, kickoff, *, sharp=None, match_id=None, selection_key=None,
                       spec_key=None) -> ClosingReference | None     # .probability, .odds, .source ("sharp"|"betclic")
sharp_source_from_settings(settings) -> SharpOddsSource              # settings.models.sharp_odds_file
```

### `cs2bot.models.confidence` (new) and the ensemble

```python
ConfidenceConfig(threshold=0.75, bin_width=0.025, skeptic_lambda=0.5, prior_strength=50.0, min_predictions=300,
                 min_high_predictions=60, max_ece=0.04, high_tolerance=0.03,
                 min_bin_predictions=20)                                    # review round 1
high_confidence_reliability(p, y, *, lo=0.75, width=0.025, fold=False, include_empty=True) -> list[HighConfidenceBin]
high_confidence_report(p, y, *, config=None, fold=False) -> dict      # JSON: n, hit_rate, mean_predicted, gap, bins
HighConfidenceShrinker(config=None).fit(p, y); .adjust(p) -> float; .adjust_many(p); .to_dict()/.from_dict()
    # adjust: never above p, non-decreasing; p >= threshold adjusted (p == threshold included), p below
    # the threshold kept but capped at the adjusted threshold value (review round 1)
assess_market_trust(market_type, p, y, *, config=None) -> MarketTrust   # .trusted, .reasons, n, n_high, ece, ...
ConfidenceGuard.fit(records: Iterable[(market_type, p_win, won)], *, config=None)
ConfidenceGuard.from_predictions(backtest_report.predictions)
guard.adjust(market_type, p) -> float   # own shrinker; min(own, pooled) while own n_high < min_high_predictions
guard.is_trusted(market_type) -> bool; guard.reasons(market_type)
guard.save(path) / ConfidenceGuard.load(path)

EnsembleConfig(..., screen_feature_groups=True, feature_groups=(("deep", (...)), ("market", ("mkt_", "sharp_"))),
               screen_min_gain=0.0005, screen_z=1.0,   # new fields
               calibration_end_prior=1.0)              # review round 1: isotonic end plateaus tempered
temper_isotonic_ends(cal: IsotonicCalibrator, p, y, *, prior=1.0) -> IsotonicCalibrator   # review round 1
FitReport.high_confidence: dict; FitReport.feature_groups: dict    # new fields (persisted with the bundle)
BacktestReport.high_confidence: dict; .market_trust: dict; .predictions: list[PredictionRecord] (exclude=True)
```

### `cs2bot.backtest.deep_eval` (re-exported from `cs2bot.backtest`)

```python
synthetic_deep_data(universe, *, with_market=True, with_duels=True, awp_rate=0.7, margin=0.06,
                    opening_noise_sd=0.06, closing_noise_sd=0.02, opening_lead=timedelta(hours=30), n_moves=3,
                    truth_sims=1500, seed=0) -> DeepData
compare_feature_sets(universe, *, deep=None, ensemble_config=None, with_rounds=True,
                     include_unscreened=False) -> DeepEvalReport      # .variants, .by_name(), .table()
markdown_summary(report, *, top=15) -> str
```

### `cs2bot.ingest`

```python
HLTVFetcher(settings, *, ..., proxy_page_loader=None)      # new keyword (tests / custom transports)
fetcher.proxy_fallback_enabled -> bool; fetcher.proxy_blocked_until -> datetime | None
cs2bot.ingest.hltv.fetcher: direct_settings(settings), proxy_profile_dir(path), PROXY_COOLDOWN_FILE = "_cooldown_proxy.json"
cs2bot.ingest.scope.derive_tier(stars, team1_rank, team2_rank, rules=DEFAULT_TIER_RULES) -> Tier; HLTVTierRules
cs2bot.ingest.pandascore.pandascore_enabled(client) -> bool; DISABLED_NOTE
```

### Settings (added fields only)

* `hltv.proxy_mode: "after_block" | "always" = "after_block"`, `hltv.proxy_cooldown_base_s = 1800`, `hltv.proxy_cooldown_max_s = 43200`.
* `models.sharp_odds_file: str | None = None` (disabled), `models.confidence_guard_file = data/models/confidence_guard.json`.
* `DEFAULT_ACTIVE_MAP_POOL = [ancient, anubis, cache, dust2, inferno, mirage, nuke]`.

### Wiring for the integration owner (services / flows / decision - not edited here)

```python
# 1. training (services.registry.train_models): build the store with the deep inputs, like the live path does
from cs2bot.datasets import DeepData
from cs2bot.odds.sharp import sharp_source_from_settings
deep = DeepData.from_session(session, until=as_of, sharp=sharp_source_from_settings(settings))
store = FeatureStore.build(map_results, player_maps=player_maps, rounds=rounds, vetoes=vetoes, deep=deep)
#    (or simply FeatureStore.from_session(session, until=as_of)). Live: ModelBundle.feature_store already uses
#    FeatureStore.from_session(session, until=now), which now loads DeepData (add sharp=... if a feed exists).
#    Until (1) is wired, training has only the map-derived deep columns; live-only columns are ignored by the
#    ensemble (it reindexes to its training features), so nothing breaks.

# 2. confidence guard: after a backtest (cs2bot backtest / nightly), persist the guard
guard = ConfidenceGuard.from_predictions(report.predictions); guard.save(settings.models.confidence_guard_file)

# 3. decision (engine.decision.evaluate), before the min_probability check. Status after review round 1:
#    services.betting.trusted_markets / cli backtest already read / write the guard's trusted flags
#    (cloud_autonomy); guard.adjust is NOT wired yet.
p_adj = guard.adjust(spec.market_type.value, outcome.p_win)          # use p_adj for the probability and EV checks
if require_trusted and not guard.is_trusted(spec.market_type.value): reject("market_not_trusted", guard.reasons(...))
#    suggested settings: DecisionSettings.use_confidence_guard: bool = True, require_trusted_market: bool = False
#    (True once enough backtest history exists).

# 4. CLV in services.settlement: closing_line_reference(session, selection_id, kickoff,
#    sharp=sharp_source_from_settings(settings), match_id=..., selection_key=..., spec_key=...)
```

## Known limitations

* **Real deep data is unverified.** Venues (`tournaments.location`), stage texts, team / player countries, clutch rows and coach periods are read where the ingest packages store them; HLTV pages were never fetched live (blocked here), so how often these fields are filled on the owner's data is unknown. Missing parts give NaN features, which the screen then ignores.
* **Synthetic evaluation cannot show a benefit** of fatigue, travel, stakes or duel features: the generator has none of these effects. The claim proven here is "does not get worse" (thanks to the group screen), not "gets better". The market group was close to neutral too (the synthetic rating model is already near the truth the prices are built from).
* **Dead rubbers** are only recognised from the stage text (showmatch / 3rd place / all-star); round-robin standings are not reconstructed. Swiss situations assume a 3-win / 3-loss Swiss.
* **Time zones ignore daylight-saving time**; multi-zone countries use their main esports hub. Team "home" from `teams.country` may be the organisation's country, not where the players live.
* **Map areas** use the kill's `victim_place` (demo place names); areas with different names per map version are not merged.
* **Overlapping series of one team** (never in real data, frequent in the random synthetic schedule) make a store cut at kick-off see part of the other series; documented in the leakage test.
* **The group screen** costs about two extra cross-validation passes per ensemble fit (tens of seconds on a few thousand maps) and uses one out-of-fold comparison, so a weakly useful group can be dropped by noise.
* **The confidence guard is not wired** into the decision layer (owned by another package); see "Wiring". Probabilities near 0.75 are shrunk at most to the threshold region when no history exists, so the guard alone does not stop betting; `require_trusted_market` does.
* **Sharp feed**: only a local JSON file adapter exists (no paid feed configured); the protocol is ready for an HTTP adapter.
* `config/patches.yaml` still has unverified days for minor patches (not pool changes).
* **Load size**: `FeatureStore.from_session` now also loads every demo kill (with two joins to `player_round_stats`) for the duel features. Fine for a few thousand parsed maps; with a very large demo backlog the live store rebuild gets slower - a `since` window on the duel loader (365 days is all the features use live) would be the fix.
* **Existing tests changed** (owned areas): the veto tests pin their synthetic universes to the legacy pool (they ban "train"), `test_default_pool_comes_from_the_active_duty_list` now checks the new pool, one store test accepts a pool depth of 6-7 on its small universe, and `test_missing_token_is_reported_not_raised` (PandaScore) now expects the "disabled" report.

## Foundation change requests

* `src/cs2bot/services/registry.py` (`train_models`): build the training store with `DeepData.from_session(session, until=as_of, sharp=sharp_source_from_settings(settings))` (or `FeatureStore.from_session(session, until=as_of)`) so training and live rows have the same deep columns (see Wiring 1).
* `src/cs2bot/engine/decision.py` + `src/cs2bot/config.py` `DecisionSettings`: `use_confidence_guard: bool = True`, `require_trusted_market: bool = False`; load `ConfidenceGuard` from `settings.models.confidence_guard_file` and apply `guard.adjust` / `guard.is_trusted` (Wiring 3); a backtest job should write the guard (Wiring 2).
* `src/cs2bot/services/settlement.py`: CLV against `closing_line_reference(...)` (sharp close when configured).
* `src/cs2bot/db/models.py` (optional): `tournaments.country` / `utc_offset` and `teams.region` columns would make travel features exact instead of parsed from free text; `matches.stage_kind` for a parsed stage.
* `src/cs2bot/domain/match.py` (optional): `MatchContext.location` so live context carries the venue without the metadata lookup.
* `docs/ARCHITECTURE.md` P7: mention `DeepData`, `DeepFeatureIndex`, `models.confidence`, `odds.movement` / `odds.sharp`.

## New dependency requests

None.

## Review round 1

Independent review (special focus: look-ahead leakage of every new feature, calibration in the
0.75-1.0 range, nothing breaks without a PandaScore token). The brief has no Addendum / Review focus
section beyond that. Every requirement was re-checked against the code, the math re-derived, and the
fixes below were made in the owned paths with regression tests in
`tests/learning/test_deep_review.py` (16 tests; existing tests adapted where the behaviour changed
on purpose).

### What was verified (no change needed)

* **Leakage of the stored data**: every results-like input (maps, rounds, kills, clutches, coach periods,
  market snapshots, sharp quotes) is read strictly before `as_of`; match metadata (schedule, stage,
  venue, tournament, format) is static pre-match information; the store's `cutoff` clamps finished
  matches to their start; the "full history vs history cut at kick-off" test is meaningful (deep columns
  are filled) and still passes after the fixes. Coach changes become visible the day after the change
  (date-only data). Betclic live prices are not stored (`store_live_odds=False`), so the market block
  can never see an in-play price; snapshots keep their selection (remaps after a team-order flip
  re-key the selection, the price history stays with the team).
* **Economy / clutch / duel math**: pistol rounds 1 and 13 (MR12), conversion / recovery over the two
  following rounds, force / eco vs full buy, post-plant (T) vs retake (CT) after a plant, clutch 1v1 vs
  1vX, opening duels per side, trade share, AWP-vs-AWP duels, pair kills in both directions, area edge.
* **Stakes**: Swiss elimination / qualification from the record before the match.
* **HLTV proxy fallback**: direct first, retry through the proxy only on a challenge / 403 / 429 or an
  active direct cool-down, separate cool-down file and browser profile, never a challenge solver, the
  proxy URL (credentials) is never logged, `proxy_mode="always"` = classic single route.
* **PandaScore optional**: every sync returns a "disabled" report without network or DB access, one INFO
  note per process; services / scheduler / CLI / doctor check the token before building a client;
  `data_completeness` has no PandaScore component (lineups, recent maps, vetoes, player stats and the
  odds link all come from HLTV / Betclic); HLTV-only matches get a derived tier, so scope and the tier
  features work.
* **Confidence math**: Wilson intervals, bins of 2.5 pp over [0.75, 1] (last bin closed), Beta posterior
  per bin around the skeptical prior, running minimum from the top (monotone, conservative), JSON
  round trip. The cloud_autonomy wiring (`services.betting.trusted_markets`, `cli backtest` writing the
  guard) uses `ConfidenceGuard.load / is_trusted / trust / from_predictions / save`, all unchanged.

### Issues found and fixed

| # | Severity | Issue | Fix |
|---|---|---|---|
| 1 | major | **A series still being played counted as won / lost.** A live match already has its first finished maps in the database (`load_map_results` keeps a FINISHED map of a LIVE match). The deep index placed the series at its latest stored map and called it decided as soon as the map score differed: a BO3 at 1-0 was a series *win* for `dp_bo3_series_win`, `dp_bo1_series_win`, `dp_event_wins` / `dp_event_losses` and the Swiss elimination / qualification flags of the team's *next* match - live only (in training every series is complete), i.e. a train / live skew right in the stakes features. | A series is decided only when the stored maps finish it (`_series_complete`: a BO3 needs a team with 2 map wins, BO5 3, BO1 1, BO2 two maps; unknown format = old behaviour). It still counts for the schedule load. |
| 2 | major | **Market block could be read in the wrong orientation.** `flip` was only set when the match metadata matched the reversed teams; with no metadata (a failed metadata load, a match outside it) or with teams that are not the match's teams, a mirrored training row / live context got team1's price as its own (shown: P = 0.729 for both orientations). | The block needs the metadata and the row's teams must be the match's teams in one order or the other; otherwise every market column is NaN. |
| 3 | major (calibration) | **Training read the closing line, live reads earlier prices.** Training rows are computed at kick-off, so the market block saw the price minutes before the start (the most accurate one); live, the bot predicts and bets 10 min .. 48 h before the start and sees an earlier, noisier price. A model fitted on closing prices over-trusts early prices - over-confidence exactly in the >= 75 % range. `mkt_hours_since_move` / `mkt_hours_quoted` were also measured to two different reference points. | A kick-off row (`as_of` within 5 min of / after the scheduled start) now reads the market a deterministic per-match lead before the scheduled start, uniform in `DeepConfig.market_lead_hours = (0.25, 48.0)` hours (the betting window of `DecisionSettings`); live rows read the price on offer at `as_of`. New column `mkt_lead_hours` (age of the price relative to the start; NaN without a quote) lets the model learn how much a price is worth by its age. `market_lead_hours=None` restores the old behaviour. Test: a late steam move after the lead never changes a training row. |
| 4 | major (train / live skew) | **Schedule features anchored to `as_of`.** Live rows are computed hours before the start, training rows at the start, so `dp_rest_days`, `dp_days_at_event`, `dp_days_since_coach_change` (levels), the 3 / 7-day windows, "the same UTC day" of `dp_same_day_series` / `dp_maps_today` / `dp_play_streak_days` described different things live and in training (a prediction made the evening before used the wrong day; a series scheduled earlier on the match day but after `as_of` was not counted). The existing parity test only covered the backtest path (the match in the store, `as_of` clamped to its start). | The schedule anchor is the match's scheduled start (`MatchMetaRecord.scheduled_at`, public before the match; ignored when more than 7 days ahead): windows are `[start - d, as_of)`, rest / event / coach days are measured to the start, the match day is the start's UTC day, scheduled series earlier on that day are counted from the public schedule. Results are still only read before `as_of`. New test: a live row at 08:00 equals the kick-off row when nothing was played in between. |
| 5 | major (calibration) | **Isotonic calibration gave small extreme blocks 0.999.** Isotonic regression gives the top block of holdout maps that were all won the value `1 - eps` whatever its size (e.g. 30 lopsided maps -> 0.999), which the series simulation turns into near-certain match / handicap probabilities - the costliest kind of error under the confidence rule. (Pre-existing in the ensemble, found while checking the 0.75-1.0 range.) | `temper_isotonic_ends`: the two end plateaus get `EnsembleConfig.calibration_end_prior = 1.0` pseudo-win and pseudo-loss (rule of succession, `(k+1)/(n+2)`), only the ends move, monotonicity kept; 0 = plain isotonic. Holdout log-loss and ECE improved in both seeds (table below). |
| 6 | minor (calibration) | `HighConfidenceShrinker.adjust` returned `p == threshold` unchanged, so a probability of exactly 0.75 escaped an over-confident first bin and still passed `p >= 0.75`. | `p >= t` is adjusted; below `t` the value is capped at the adjusted threshold value (monotone across the threshold, never above `p`). |
| 7 | minor (calibration) | Trust verdict was lenient: "over-confident" only when the Wilson *upper* bound of the whole high range was below mean - tolerance (400 predictions at 0.90 winning 80 % passed). | Trusted now also needs the observed hit rate of the high range >= mean prediction - `high_tolerance` (a point estimate) and no 2.5-pp bin with >= `min_bin_predictions` (20) significantly over-confident. Conservative on purpose: `trusted` is what `require_trusted_calibration` relies on. |
| 8 | minor (calibration) | `ConfidenceGuard.adjust` used a market's own, nearly empty shrinker even when the pooled history showed strong over-confidence. | While a market has fewer than `min_high_predictions` high-confidence predictions, the more conservative of its own and the pooled adjustment is used. |
| 9 | minor | Stage text: a Major's "Elimination stage" (a Swiss stage) was flagged as an elimination match (stakes 3) on every match; GSL "winners' / losers' match" counted as playoff; "lower bracket round 1" as an opening match; lower-bracket matches were elimination only with a round / final word. | `_ELIM` ignores "elimination stage"; every lower-bracket / losers' match is an elimination; group matches are not playoff; bracket rounds are not "opening". |
| 10 | minor | The ensemble's group screen ran a full CV pass for a group whose columns are all constant (e.g. `mkt_n_moves` / `mkt_steam` = 0 without any market data). | Only columns with at least two distinct values make a group worth screening. |

### Synthetic retrain after the fixes (requirement 5)

Same setup as above (`generate_universe(n_teams=20, n_matches=1200, days=540, drift_sd_per_30d=0.08)`,
`synthetic_deep_data`, `EnsembleConfig(holdout_days=90, n_bootstrap=2)`), plus the market block forced in
(`deep_market_unscreened`). Before -> after the review for the default model:

| seed | variant | log-loss | Brier | ECE | n >= 0.75 | hit rate | mean p |
|---|---|---|---|---|---|---|---|
| 7 | baseline / deep / deep_market (screened, identical) | 0.4609 -> **0.4569** | 0.1489 -> 0.1488 | 0.0255 -> **0.0226** | 248 | 0.843 | 0.883 -> **0.878** |
| 7 | deep_unscreened | 0.4705 -> 0.4668 | 0.1517 | 0.0341 -> 0.0309 | 224 | 0.848 | 0.894 -> 0.888 |
| 7 | deep_market_unscreened | - -> 0.4690 | 0.1527 | 0.0454 | 234 | 0.855 | 0.882 |
| 11 | baseline / deep / deep_market (screened, identical) | 0.4161 -> **0.4066** | 0.1300 -> 0.1300 | 0.0375 -> **0.0345** | 267 | 0.906 | 0.909 -> **0.904** |
| 11 | deep_unscreened | 0.4151 -> 0.4081 | 0.1299 -> 0.1308 | 0.0495 -> 0.0296 | 260 | 0.900 | 0.922 -> 0.908 |
| 11 | deep_market_unscreened | - -> 0.4022 | 0.1285 | 0.0268 | 254 | 0.898 | 0.910 |

Screen decisions (paired out-of-fold gain +- SE): seed 7 deep -0.0012 +- 0.0013, market (read at the
lead) +0.0002 +- 0.0013; seed 11 deep -0.0014 +- 0.0015, market +0.0003 +- 0.0011 - all dropped, so the
default model is the baseline in both seeds. The tempered isotonic ends lower the holdout log-loss by
0.004 / 0.010 and the ECE by 0.003, and the high-confidence predictions are less over-confident (seed 7:
mean p 0.878 for a hit rate of 0.843, was 0.883). The market block read at a realistic lead carries
less signal than the near-closing prices it was trained on before (as expected: the synthetic prices
are noisier further from the start); forced in it is mixed (worse in seed 7, better in seed 11), which
is exactly what the screen is for. Feature importance is unchanged (calibration does not touch
LightGBM; the synthetic metadata has `scheduled_at = played_at`, so the schedule anchor is the same).

### Remaining concerns

* **Probability adjustment not wired.** `services.betting` now reads the guard's `trusted` flags
  (cloud_autonomy), but nothing calls `ConfidenceGuard.adjust` before the `min_probability` check / the
  stake tier. Until it is wired (engine.decision / services.betting, not deep_data's paths), the shrinker
  only shows up in reports. Recommended: `p_adj = guard.adjust(spec.market_type.value, p_win_given_settled)`
  for the confidence check and the stake tier.
* **Rest of the row is still computed at kick-off in training** (form, ratings, player aggregates):
  the review aligned the deep schedule and market blocks; the other blocks keep the existing
  convention (the difference matters little for 90-365-day aggregates).
* **Lead distribution is an assumption**: uniform over the betting window. If the bot in practice bets
  mostly early (as soon as odds appear and p >= 0.75), a lead distribution skewed to 24-48 h would match
  better (`DeepConfig.market_lead_hours`).
* **Team home from the current roster**: `load_team_meta` uses today's players' countries when
  `teams.country` is empty - a small static look-ahead (a team that moved region). `teams.country` from
  HLTV is preferred and is the usual source.
* **Trust counts both sides of a two-way market** (and every line of a handicap / total market), so `n`
  overstates the number of independent events; the per-bin and point-estimate checks partly compensate.
* **Swiss record per tournament id**: a multi-stage event stored under one tournament id carries the
  earlier stage's record into the next Swiss stage (no stage boundary in the data).
* Whole-suite / live checks as before: HLTV, Betclic and PandaScore payloads are unverified against the
  live services.
