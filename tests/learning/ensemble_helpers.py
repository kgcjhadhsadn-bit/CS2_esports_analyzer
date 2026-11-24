"""Shared, cached builders for the ensemble / backtest tests (not a test module)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from cs2bot.domain.records import MapResultRecord
from cs2bot.features import FeatureStore
from cs2bot.models.ensemble import EnsembleConfig, FitReport, MapWinEnsemble
from cs2bot.models.ratings import WalkForwardRatings
from cs2bot.testing.synthetic import SynthUniverse, generate_universe

#: The team that plays only a handful of maps in the ensemble fixture ("little data").
RARE_TEAM = 16

#: Small-data, fast settings for tests (the defaults are tuned for the owner's data, not speed).
FAST_ENSEMBLE = EnsembleConfig(
    n_bootstrap=4,
    learning_rate=0.08,
    max_boost_rounds=300,
    early_stopping_rounds=25,
    n_cv_folds=3,
    holdout_days=90,
    sigma_floor=0.01,
)


@dataclass(frozen=True)
class EnsembleFixture:
    universe: SynthUniverse
    records: tuple[MapResultRecord, ...]
    store: FeatureStore
    ratings: WalkForwardRatings
    ensemble: MapWinEnsemble
    report: FitReport


@lru_cache(maxsize=4)
def ensemble_universe(seed: int = 3) -> SynthUniverse:
    return generate_universe(n_teams=16, n_matches=500, seed=seed, days=500, with_kills=False)


def thin_out(records: list[MapResultRecord], team_id: int, keep_matches: int) -> list[MapResultRecord]:
    """Drop every map of ``team_id`` except those of its first ``keep_matches`` matches."""
    kept: list[int] = []
    out: list[MapResultRecord] = []
    for r in sorted(records, key=lambda r: (r.played_at, r.match_id, r.map_number)):
        if team_id in (r.team1_id, r.team2_id) and r.match_id not in kept:
            if len(kept) >= keep_matches:
                continue
            kept.append(r.match_id)
        out.append(r)
    return out


@lru_cache(maxsize=2)
def fitted_ensemble(seed: int = 3) -> EnsembleFixture:
    """An ensemble fitted on a 500-match universe in which ``RARE_TEAM`` played only 2 matches."""
    uni = ensemble_universe(seed)
    records = thin_out(uni.map_results(), RARE_TEAM, keep_matches=2)
    keep_ids = {r.match_id for r in records}
    store = FeatureStore.build(
        records,
        rounds=[r for r in uni.round_records() if r.match_id in keep_ids],
        vetoes=[v for v in uni.veto_records() if v.match_id in keep_ids],
    )
    ratings = WalkForwardRatings.fit(records)
    ens = MapWinEnsemble(FAST_ENSEMBLE)
    report = ens.fit(records, store=store, rating_provider=ratings)
    return EnsembleFixture(uni, tuple(records), store, ratings, ens, report)
