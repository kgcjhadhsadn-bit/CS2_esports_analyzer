# datasets_features: implementation report

Tests: `.venv/bin/python -m pytest tests/learning -q -n 2` - 71 passed, 2 skipped (PostgreSQL, need `CS2BOT_TEST_DATABASE_URL`), 0 failed (19 s; 63 before review round 1). With a PostgreSQL 16 URL the 2 PostgreSQL tests pass too. `tests/foundation`: 37 passed. `ruff check`, `ruff format --check` and `mypy` are clean on `src/cs2bot/datasets`, `src/cs2bot/features`, `tests/learning`.

## Summary

The package was picked up from an interrupted build: loaders, the feature store and the meta module existed but had never been run (35 mypy errors, 16 lint errors, no tests, no public feature API, no role inference). Everything was verified, repaired and completed.

- **`cs2bot.datasets`** turns DB rows into the plain records of `cs2bot.domain.records`, chronological and in-scope by default, in team1/team2 orientation, on portable SQL (SQLite and PostgreSQL). It also builds the `MatchContext` of one match, scores its data completeness (the betting gate) and builds `MatchActuals` for settlement.
- **`cs2bot.features`** is a `FeatureStore` with per-team / per-map / per-player / per-pairing time indexes and prefix sums, so a window aggregate is two binary searches. On a synthetic history of 3,000 maps the store is built in about 0.5 s and 3,000 training rows with 234 features in about 1 s. Every query takes `as_of` and reads strictly earlier data only.
- **Feature rows** (`build_training_rows`, `features_for_match_map`) share one code path, so training and live rows cannot drift apart. A row holds 72 single-team features for each team plus their differences, head-to-head, a strength proxy, match context, map and patch meta, and rating features when a `RatingProvider` is given.
- **Roles** (`features/roles.py`) are inferred from demo kills, per-round rows and position samples: AWPER, ENTRY, LURKER, ANCHOR, ROTATOR, SUPPORT, RIFLER. IGL is never inferred and a stored IGL is never overwritten.
- **Meta** (`features/meta.py`, `config/patches.yaml`): active map pool detection, a patch timeline of 16 CS2 updates since 2023-09-27, and Bayesian online change-point detection (Adams & MacKay 2007) on each team's opponent-adjusted round-share residuals.

Two modelling defects were found and fixed while verifying the inherited code:

- **Regime shift was an inverse proxy of team strength** (correlation -0.54 with true strength on synthetic data). While the strength proxy is still learning a new team, its residuals are one-sided; the change-point filter read the end of that warm-up as a regime change. The first 15 maps of every team are now left out of the series (`RegimeConfig.warmup_maps`); the correlation is -0.03 and the feature is predictive in a universe with real form drift.
- **`regime_shift` on one- or two-map regimes** is now NaN (`RegimeConfig.min_regime_maps = 4`).

One behaviour goes beyond the brief: when the veto is unpublished and too few recent results exist to detect the pool, `load_match_context` uses the configured pool only if the owner changed it; otherwise it uses the Active Duty pool of the match day from `config/patches.yaml`. The built-in default in `config.py` is stale (see foundation change requests) and is wrong for every historical match in a backtest.

## Public API

### `cs2bot.datasets`

```python
load_map_results(session, *, since=None, until=None, in_scope_only=True) -> list[MapResultRecord]
load_veto_records(session, *, since=None, until=None, in_scope_only=True, finished_only=True) -> list[VetoRecord]
load_round_records(session, *, since=None, until=None, in_scope_only=True, sources: Collection[DataSource] | None = None) -> list[RoundRecord]
load_player_map_records(session, *, since=None, until=None, in_scope_only=True) -> list[PlayerMapRecord]
to_frame(records: Iterable[BaseModel], *, columns: Sequence[str] | None = None) -> pandas.DataFrame

load_match_context(session, match_id, *, settings: Settings, now=None, include_context_flags=True) -> MatchContext   # raises MatchNotFoundError
data_completeness(session, ctx: MatchContext, *, now=None, config: CompletenessConfig = DEFAULT) -> float            # [0, 1]
completeness_breakdown(session, ctx, *, now=None, config=DEFAULT) -> dict[str, float]   # lineups, recent_maps, veto_history, player_stats, odds_link
load_match_actuals(session, match_id) -> MatchActuals | None

load_team_demo_stats(session, *, since=None, until=None, in_scope_only=True, sources=(DataSource.DEMO,)) -> list[TeamDemoMapRecord]
load_role_frames(session, *, window_days=120, as_of=None, position_seconds=None, in_scope_only=False) -> RoleFrames
available_position_seconds(session, *, sample_rows=20_000) -> list[int]
SOURCE_PRIORITY = (DEMO, HLTV, PANDASCORE, SYNTHETIC)
```

Conventions shared by the loaders:

- **Time window:** `since <= t < until`, where `t` is `maps_played.played_at`, falling back to `matches.scheduled_at` (vetoes use `scheduled_at`). `until=as_of` returns exactly what was known strictly before `as_of`. Naive datetimes are read as UTC.
- **Skipped, never fatal:** cancelled matches, matches without both teams, maps whose winner has fewer than 13 rounds or with equal scores, placeholder map names (`TBA`), rows that fail validation.
- **Map results:** `picked_by` is `None` for the decider (`is_decider` wins over a contradicting `picked_by_team_id`) and for an unknown pick. Lineups come from `match_lineups`, else from the players with a stat line on the map. `is_lan` / `tier` fall back to the tournament.
- **Rounds:** economy fields are assigned to team1/team2 by `team1_side`. HLTV rows keep them `None`. `team1_score_before` / `team2_score_before` are cumulative within the map and become `None` after a gap in the stored rounds.
- **Player records:** the preferred source row supplies kills / deaths / team; optional fields it lacks (for example the HLTV rating next to a DEMO row) are filled from the next source. `awp_kills` falls back to counting the `kills` table when that map's kills carry weapon names.
- **`data_completeness` weights:** lineups 0.30 (5 players in `match_lineups` for this match; half credit for a lineup carried over from the previous match), recent maps 0.25 (8 maps per team in 90 days), veto history 0.15 (3 vetoes per team in 180 days), player stats 0.20 (5 stat lines per lineup player in 180 days), odds link 0.10 (a Betclic event linked AUTO or CONFIRMED). With the default gate of 0.95 every component must be essentially complete; a missing Betclic link alone gives 0.90.
- **`load_match_actuals`:** `round_winners` only when every round is present and the winners add up to the score (rounds table first, then `maps_played.extra["hltv"]["rounds"]`). `forfeited` = a forfeit flag (`Match.extra["hltv"]["forfeit"]`, `extra["pandascore"]["forfeit"]` or top-level `extra["forfeit"]`) **and no map played**. `cancelled` = status CANCELED, no forfeit flag **and no map played**. Both void every bet, so neither is set while a decided map exists. `complete` is `False` for an undecided series, a series score that disagrees with the maps, a forfeit flag or HLTV `forfeited_maps` next to played maps (HLTV sets `forfeit=True` for partial forfeits too), and a CANCELED match with played maps - settlement then answers UNKNOWN and asks the bookmaker.

### `cs2bot.features`

```python
class RatingProvider(Protocol):            # runtime_checkable
    def predict(self, team1_id: int, team2_id: int, map_name: str, as_of: datetime, *, is_lan: bool = False) -> MapWinEstimate

FeatureStore.build(map_results, *, player_maps=(), rounds=(), vetoes=(), team_demo_stats=(),
                   config: FeatureConfig | None = None, patch_timeline: PatchTimeline | "default" | None = "default") -> FeatureStore
FeatureStore.from_session(session, *, since=None, until=None, in_scope_only=True, with_rounds=True, with_demo=True,
                          config=None, patch_timeline="default") -> FeatureStore
store.cutoff(as_of, exclude_match_id=None) -> datetime     # as_of, or the start of exclude_match_id when earlier
store.team_features(team_id, map_name, as_of, *, lineup=(), standins=None, exclude_match_id=None) -> dict[str, float]
store.matchup_features(team1_id, team2_id, map_name, as_of, *, exclude_match_id=None) -> dict[str, float]
store.map_features(map_name, as_of, *, exclude_match_id=None) -> dict[str, float]
store.meta_features(as_of) -> dict[str, float]
store.recent_regime_break(team_id, as_of, *, exclude_match_id=None) -> RegimeBreak     # detected, probability, shift, maps_in_regime
store.form_trend(team_id, as_of, *, exclude_match_id=None) -> float
store.team_strength(team_id, map_name | None, as_of, *, exclude_match_id=None) -> tuple[float, float]
store.last_lineup(team_id, as_of, *, exclude_match_id=None) -> tuple[int, ...]
store.match_start(match_id) -> datetime | None;  store.standin_count(match_id, team_id) -> int | None
store.map_results -> tuple[MapResultRecord, ...];  len(store);  has_round_data / has_demo_data / has_veto_data / has_player_data

build_training_rows(map_results, *, store, rating_provider=None, mirror=False) -> tuple[pandas.DataFrame, numpy.ndarray, pandas.DataFrame]
features_for_match_map(ctx: MatchContext, map_name, *, store, as_of, rating_provider=None,
                       picked_by: TeamSlot | "auto" | None = "auto") -> dict[str, float]
match_map_features(store, *, team1_id, team2_id, map_name, as_of, team1_lineup=(), team2_lineup=(), team1_standins=None,
                   team2_standins=None, is_lan=False, tier=Tier.UNRANKED, fmt=None, picked_by=None, team1_start_side=None,
                   exclude_match_id=None, rating_provider=None) -> dict[str, float]
rating_estimate(provider, team1_id, team2_id, map_name, as_of, *, is_lan) -> MapWinEstimate
rating_features(estimate: MapWinEstimate) -> dict[str, float]
RATING_LOGIT_FEATURE = "rating_logit";  META_COLUMNS;  TIER_RANK

# roles
infer_roles(source: Session | RoleFrames, *, window_days=120, as_of=None, config: RoleConfig | None = None) -> dict[int, tuple[PlayerRole, float]]
update_player_roles(session, *, window_days=120, as_of=None, config=None, roles=None,
                    unknown_stored_confidence=0.5, confidence_half_life_days=180.0) -> int      # flushes, does not commit; idempotent (re-confirmed labels only get a daily timestamp refresh, not counted)
role_metrics(frames: RoleFrames, config=None) -> pandas.DataFrame;  decide_role(metrics, config=None, *, flash_median=nan)

# meta
detect_active_map_pool(map_results, as_of, *, window_days=60, min_share=0.04, recent_days=14, min_maps=30, pool_size=7) -> list[str]
load_patch_timeline(path=None) -> PatchTimeline;  PatchTimeline.from_yaml(path)
days_since_last_patch(as_of, *, min_impact="moderate", timeline=None) -> float;  patch_id(as_of, *, timeline=None) -> str | None
timeline.last_patch / days_since_last_patch / days_since_pool_change / patch_id / patch_index / map_pool_at
bocpd(values, config: BOCPDConfig | None = None) -> BOCPDResult
RegimeDetector.from_map_results(map_results, *, strength_config=None, config=None)
recent_regime_break(team_id, as_of, *, detector) -> RegimeBreak;  form_trend(team_id, as_of, *, detector) -> float
```

**Row layout** (`build_training_rows` returns `X`, `y`, `meta`):

- `X`: float DataFrame, NaN = unavailable. Columns are `t1_<f>`, `t2_<f>`, `diff_<f>` for the 72 team features, then `h2h_maps`, `h2h_win_rate`, `h2h_round_diff`, `h2h_map_maps`, `h2h_map_win_rate`, `strength_logit`, `strength_p_win`, `is_lan`, `tier_rank`, `best_of`, `pick_team1`, `pick_team2`, `pick_diff`, `map_ct_round_share_180d`, `map_global_maps_180d`, `days_since_patch`, `days_since_pool_change`, `patch_index`, and with a provider `rating_p`, `rating_logit`, `rating_sigma`, `rating_c_<component>`.
- Team features: form (`win_rate_30d/90d`, `round_diff_*`, `map_win_rate_30d/90d/365d`), opponent-adjusted form (`adj_round_share_90d`, `adj_win_90d`, `opp_strength_90d`, `map_adj_round_share_365d`, `elo`, `map_elo_offset`), schedule (`days_since_last_match`, `maps_14d`, `matches_14d`), roster (`roster_continuity`, `maps_with_lineup`, `days_since_roster_change`, `roster_new_players`, `standin_count`, `lineup_known`), LAN and pick records, pool (`pool_depth`, `pool_depth_winning`, `map_play_share_180d`, `map_pick_share_180d`), sides (`ct_win_pct_90d`, `t_win_pct_90d`, `map_ct_win_pct_365d`, `map_t_win_pct_365d`), rounds (`pistol_win_pct`, `eco_force_conv`, `anti_eco_win_pct`, `full_buy_win_pct`, `post_plant_win_pct`, `retake_win_pct`, all `_180d`), demo (`trade_kill_share`, `traded_death_share`, `util_dmg_per_round`, `flashed_per_round`, all `_180d`), veto (`map_ban_rate_180d`, `map_pick_rate_180d`), players (`pl_rating_mean/max/min`, `pl_kpr_mean/max`, `pl_kd_diff_pr_mean`, `pl_adr_mean`, `pl_kast_mean`, `pl_opening_win_pct`, `pl_opening_kpr`, `pl_awp_share_max`, `pl_awp_kpr_max`, `pl_known`, `pl_maps_mean`), regime (`regime_break_prob`, `regime_shift`, `form_trend`).
- `y`: 1.0 when team1 won the map.
- `meta`: `match_id`, `map_number`, `map_name`, `as_of`, `played_at`, `team1_id`, `team2_id`, `mirrored`.

Rules the consumer must know:

- **`as_of` of a training row is the start of its match** and the match is excluded, so map 2 never sees map 1 of the same series.
- **Rating provider at past timestamps:** `MapRatingModel.predict` refuses an `as_of` before its last game. Fit it with `record_history=True` (its `historical_predict` is then used automatically) or pass `WalkForwardRatings`. The refusal is passed on, never swallowed.
- **`pick_*` are all 0 for a decider and for an unknown pick.** Before the veto is published, pass each candidate picker from `MapSequenceProbability.pickers` as `picked_by`.
- **`mirror=True`** adds each map again with the teams swapped (label flipped). Group cross-validation folds by `match_id`.
- **`exclude_match_id` cuts the query time:** every store query and `match_map_features` use `store.cutoff(as_of, exclude_match_id)`, i.e. `as_of` is clamped to the start of the excluded match when the store holds it. `features_for_match_map` passes `ctx.match_id`, so backtests over finished matches and live calls during a running series stay pre-match.

Wiring example:

```python
from cs2bot import datasets
from cs2bot.features import FeatureStore, build_training_rows, features_for_match_map, update_player_roles
from cs2bot.models.ratings import MapRatingModel

# training (nightly)
store = FeatureStore.from_session(session, until=as_of)            # or FeatureStore.build(records, ...)
ratings = MapRatingModel().fit(store.map_results, record_history=True)
X, y, meta = build_training_rows(store.map_results, store=store, rating_provider=ratings)
update_player_roles(session); session.commit()                      # after demo parsing

# live prediction
ctx = datasets.load_match_context(session, match_id, settings=settings)
completeness = datasets.data_completeness(session, ctx)
for seq in veto_sequences:                                          # hypothetical pickers before the veto
    for map_name, picker in zip(seq.maps, seq.pickers):
        row = features_for_match_map(ctx, map_name, store=store, as_of=now, rating_provider=ratings, picked_by=picker)

# settlement
actuals = datasets.load_match_actuals(session, match_id)            # None if the match is unknown
```

## Known limitations

- **PostgreSQL:** verified in review round 1 against a local PostgreSQL 16 (`tests/learning/test_datasets_postgres.py`, marker `postgres`): every loader, the context / completeness / actuals, role inference and the training matrix are identical to SQLite. Position-sample queries only ran on the handcrafted role fixture.
- **Role thresholds are not fitted.** No labelled role data exists; the thresholds in `RoleConfig` follow public statistics of professional players and were checked on handcrafted fixtures only, never on a real parsed demo. Site attribution relies on `BombsiteA` / `BombsiteB` place names plus coordinates; on Nuke (stacked sites) it depends on `z`.
- **Patch dates:** 10 of the 16 entries in `config/patches.yaml` are marked `date_verified: false`. The day of the July 2026 Cache/Overpass swap is disputed between sources (07-06, 07-08, 07-09) and was downgraded to unverified. A day or two does not matter to the features.
- **Change-point detection is slow by design:** a shift of 0.10 in round share is flagged within 20 maps in 68 % of 300 simulated series, with a false alarm about 1 % of the time. `regime_break_prob` is a weak feature in a universe without drift.
- **`adj_round_share_90d` regresses to the mean** when team strength does not drift (the proxy has already absorbed the surprise). Its sign is for the ensemble to learn; it is not a "hot team wins" indicator.
- **The store is immutable.** New results need a rebuild: about 0.5 s per 3,000 maps plus loading. Loading two years of round rows takes on the order of 10 s; use `with_rounds=False` for frequent rebuilds.
- **`team1_starts_ct` is off by default** (`FeatureConfig.use_start_side`): the side is unknown before the veto, so the feature would exist in training and be missing live.
- **Player features use the lineup that played** in training rows. Lineups announced late are a live-data risk, not a leak.
- **No role-based features in the row yet.** Roles are written to `players` and exposed through `LineupPlayer.role`; role matchups belong to the round model.

## Foundation change requests

- `src/cs2bot/config.py`: `DEFAULT_ACTIVE_MAP_POOL` is `[ancient, anubis, dust2, inferno, mirage, nuke, train]`. Train left Active Duty in January 2026 and Cache replaced Overpass in July 2026; the current pool is `[ancient, anubis, cache, dust2, inferno, mirage, nuke]`. `datasets` works around it through `config/patches.yaml`, but the veto model and anything else reading `settings.scope.active_map_pool` get the stale pool.
- `docs/ARCHITECTURE.md`, P7 section: list `load_match_context`, `data_completeness`, `load_match_actuals`, `FeatureStore`, `build_training_rows`, `features_for_match_map`, `RatingProvider`, `infer_roles` / `update_player_roles`.
- `src/cs2bot/db/models.py` (optional): a `players.role_inferred_at` column. The timestamp is kept in `players.extra["role_inferred_at"]` for now.
- `src/cs2bot/domain/records.py` (optional): `MapResultRecord.is_decider: bool | None`, so "decider" and "pick unknown" can be told apart in training rows.

## New dependency requests

None.

## Review round 1 (first pass)

Independent review of the whole package (brief incl. Addendum, every module, tests). Checks after the fixes: `pytest tests/learning -n 2` 68 passed (17 s), `tests/foundation` 37 passed, `ruff check`, `ruff format --check` and `mypy` clean.

**Verified (re-derived, not trusted):** loader orientation and the per-side economy mapping by `team1_side`; cumulative score-before with gap handling; source preference and field fill-in; AWP counting without team kills; the `since <= t < until` window; side-split arithmetic (`CT rounds played = own CT wins + opponent T wins`); prefix-sum windows and the trailing-exclusion logic; matches-in-window counting; roster streaks; h2h orientation flip; the strength proxy (pre-game values only); BOCPD (Student-t predictive of the NIG model, hazard split, parameter updates, tail merge, strictly online); patch-file loading; role rules; `data_completeness` weights; `load_match_actuals` against the settlement engine's contract (`engine/settlement.py`) and the HLTV / PandaScore sync conventions for forfeits (`ingest/hltv/sync/matches.py`, `ingest/pandascore/sync.py`); `RatingProvider` against `MapRatingModel.historical_predict` / `records_history`. The Addendum is fully implemented (loaders, `team_rounds_won` / `opponent_rounds_won`, `rounds.source`, `LineupPlayer.role`, `MatchContext.start_sides`).

**Issues found**

1. **Major, fixed - partial forfeits voided bets on maps that were played.** The HLTV parser sets `extra["hltv"]["forfeit"] = True` whenever any map is a "Default" map or a note mentions a forfeit, not only for walkovers. `load_match_actuals` turned that into `forfeited=True, complete=True`, and settlement voids every bet on a forfeited match - a lost bet on a played map 1 would have been booked as a refund (ledger and bankroll overstated). Now `forfeited` requires that no map was played; a forfeit flag or `forfeited_maps` next to played maps gives `complete=False` (settlement answers UNKNOWN and asks the bookmaker). The existing test only set `forfeited_maps` without `forfeit`, which is not what the HLTV sync writes.
2. **Major, fixed - a CANCELED match with played maps voided those maps too** (`cancelled=True` regardless of played maps). Now `cancelled` requires no played map; otherwise the match is incomplete and left to the bookmaker.
3. **Moderate, fixed - `exclude_match_id` only removed trailing records** (documented limitation). A query with `as_of` after the start of the excluded match saw other teams' later results, and window counts (`maps_30d`, `map_maps_*`, `maps_14d`) went negative (-1 reproduced). New `FeatureStore.cutoff(as_of, exclude_match_id)`: every store query and `match_map_features` (so meta and rating features too) clamp `as_of` to the start of the excluded match; window lower bounds are clamped to the upper bound.
4. **Minor, fixed - `maps_played.is_decider` was ignored**: a decider row that also carried `picked_by_team_id` became a pick. `is_decider` now wins.
5. **Minor, fixed - `standin_count` reported 0 for a team without any stat line in that match** (unknown, not "no stand-in"); it is now `None` -> NaN, matching the live path when no lineup is known.
6. **Minor, fixed - context flags with a non-finite severity / confidence / logit shift** were either dropped by a validation error or (NaN / inf shift) passed through and would poison every probability; they are now skipped with a debug log. Betting vetoes are not affected: `engine.decision` reads veto flags from the DB itself.

New tests: `test_actuals_partial_forfeit_as_stored_by_hltv_never_voids_played_maps`, `test_actuals_cancelled_match_with_played_maps_is_left_to_the_bookmaker`, `test_context_skips_flags_with_non_finite_values`, `test_decider_flag_wins_over_a_contradicting_picker`, `test_excluded_match_cuts_every_query_at_its_start`, plus an assertion for unknown stand-in counts.

**Remaining concerns (not changed)**

- PostgreSQL is still untested (no test database). The SQL is portable by construction (the GROUP BY of `load_team_demo_stats` repeats the `COALESCE` expression, as PostgreSQL requires).
- Roles are only checked on handcrafted fixtures; thresholds are literature-based, not fitted.
- Patch-file dates marked `date_verified: false` were not re-researched in this round.
- A decider and an unknown pick still look identical in training rows (`MapResultRecord` has no `is_decider`; see the foundation change request above).
- `data_completeness` with the default gate of 0.95 effectively requires confirmed lineups in `match_lineups` (a carried-over lineup alone gives at most 0.85). That is intended, but it means no bets until HLTV/PandaScore publish lineups.

## Review round 1

Second, independent reviewer of round 1 (the first pass above had been saved before a container restart and was re-verified rather than trusted). Checks after the fixes: `pytest tests/learning -n 2` 71 passed + 2 PostgreSQL tests skipped without a URL (both pass against PostgreSQL 16), `tests/foundation` 37 passed, `ruff check`, `ruff format --check`, `mypy` clean.

**Verified:** read every module against the brief and Addendum (all points implemented, nothing stubbed); re-derived the BOCPD recursions (NIG Student-t predictive, conjugate updates, hazard split), side-split arithmetic, prefix-sum windows, the `matches_14d` carry-over, h2h orientation, trade attribution in `load_team_demo_stats`, HLTV round-history orientation in `load_match_actuals` (matches `ingest/hltv/sync/matches.py`), the `RatingProvider` dispatch against the current `MapRatingModel.historical_predict` / `records_history` / `WalkForwardRatings`. Leakage probe: for 40 random maps of a 317-map universe (rounds, vetoes, stat lines), the training row from the full store equals the row from a store loaded with `until=<match start>` - every feature identical except `pl_known` (issue 2). PostgreSQL: started a throw-away PostgreSQL 16 cluster and compared every loader, `load_team_demo_stats` (the GROUP BY), role frames, `load_match_context`, `completeness_breakdown`, `load_match_actuals`, `infer_roles` / `update_player_roles` and `build_training_rows` with SQLite: identical.

**Issues found**

1. **Minor, fixed - `update_player_roles` was not idempotent in production.** With `as_of=None` the stored confidence had aged by a few milliseconds on the next run, so every player was rewritten with a new timestamp and counted as changed (the existing test only used a fixed `as_of`). A label re-confirmed with the same confidence is now left alone and returns 0; its `role_inferred_at` is refreshed at most once a day (so a label that keeps being confirmed does not age), not counted.
2. **Minor, fixed - `pl_known` was NaN for a store without any stat line but 0 for a store whose stat lines all lie after `as_of`**, so a walk-forward store cut early in the history produced a different value than the full store for the same row. It is now 0 in both cases.
3. **Minor, fixed - `days_since_pool_change` counted "Train added to the game" (2024-11-13, kind `map_pool` but no new pool) as an Active Duty change.** New `PatchTimeline.days_since_pool_change` only counts `map_pool` entries that list a pool; the store feature uses it.
4. **Minor, fixed - context-flag `evidence` stored as a bare JSON string was split into single characters** (`[str(e) for e in "url"]`); non-list values are now handled (string -> one item, other junk -> empty, `None` items dropped).
5. **Data, fixed - `config/patches.yaml`:** the 2025-07-16 Overpass-for-Anubis entry is confirmed by several outlets (hotspawn.com, dust2.us, blast.tv, esports.net) and is now `date_verified: true`; the Cache entry notes the 2026-06-22 announcement and the source split (07-08 majority, 07-09) and stays unverified.

New tests: `test_update_player_roles_is_idempotent_with_the_clock_running`, `test_player_count_is_the_same_for_a_store_without_stat_lines_and_one_cut_before_them`, `test_context_flag_evidence_is_always_a_list_of_strings`, pool-change assertions in `test_patch_lookups`, and `tests/learning/test_datasets_postgres.py` (2 tests, marker `postgres`).

**Remaining concerns (not changed)**

- Roles are only checked on handcrafted fixtures; thresholds are literature-based. Position-sample SQL ran on PostgreSQL with the handcrafted fixture only.
- A decider and an unknown pick still look identical in training rows (foundation change request above).
- `lineup_known = 1` also for a lineup carried over from the previous match (live), while training rows always have the real lineup; `data_completeness` discounts carried-over lineups, so betting is gated, but the feature itself does not tell the two apart.
- `features_for_match_map` during a running series with a `MapRatingModel` fitted without history and already updated with map 1: the clamped `as_of` is before its last game and `predict` raises (by design, documented above).
