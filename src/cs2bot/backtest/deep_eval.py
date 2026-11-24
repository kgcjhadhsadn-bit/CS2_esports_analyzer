"""Does the deep feature block help? Retrain the ensemble with and without it and compare.

:func:`compare_feature_sets` fits :class:`~cs2bot.models.ensemble.MapWinEnsemble` three times on the
same map results, the same rating provider and the same time-ordered holdout:

* ``baseline`` - the feature set before deep_data (``FeatureConfig(deep_features=False)``);
* ``deep`` - plus the deep block without market data (schedule, format, economy, duels, ...);
* ``deep_market`` - plus the Betclic line-movement features (only when the data has market history);
* ``deep_unscreened`` (``include_unscreened=True``) - the deep block forced in, without the ensemble's
  feature-group screen (shows what the screen protects against and the deep features' importance).

The ``deep`` / ``deep_market`` variants use the default ensemble, which keeps an optional feature group
only when it improves the out-of-fold log-loss of the pre-holdout data
(``EnsembleConfig.screen_feature_groups``).

and reports the holdout log-loss / Brier / ECE of the calibrated probabilities (cross-fitted, honest
out-of-sample), the high-confidence reliability (``[0.75, 1]`` in 2.5-pp bins) and the feature
importance of each variant.

:func:`synthetic_deep_data` builds a :class:`~cs2bot.datasets.deep.DeepData` for a synthetic universe:
match metadata (one tournament per tier), duel records from the simulated kill events (the first kill
of a round is the opening duel; an AWPer's kill is an AWP kill with probability ``awp_rate``) and,
optionally, a match-winner price history around the *true* series probability (opening price with
``opening_noise_sd``, a few drifting moves, a closing price with ``closing_noise_sd``) - the same
construction as :func:`cs2bot.backtest.synthetic.synthetic_odds_history`. Synthetic matches have no
venue, stage text, clutch rows or coach changes, so those features stay NaN there.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import numpy as np

from cs2bot.backtest.synthetic import synthetic_series_truth
from cs2bot.datasets.deep import DeepData, DuelRecord, MarketPoint, MatchMetaRecord
from cs2bot.domain.enums import MatchStatus, PlayerRole, Side
from cs2bot.domain.records import MapResultRecord
from cs2bot.features.rows import RatingProvider
from cs2bot.features.store import FeatureConfig, FeatureStore
from cs2bot.models.ensemble import EnsembleConfig, FitReport, MapWinEnsemble
from cs2bot.testing.synthetic import SynthUniverse, synthetic_two_way_odds

__all__ = [
    "DeepEvalReport",
    "VariantResult",
    "compare_feature_sets",
    "markdown_summary",
    "synthetic_deep_data",
]


def synthetic_deep_data(
    universe: SynthUniverse,
    *,
    with_market: bool = True,
    with_duels: bool = True,
    awp_rate: float = 0.7,
    margin: float = 0.06,
    opening_noise_sd: float = 0.06,
    closing_noise_sd: float = 0.02,
    opening_lead: timedelta = timedelta(hours=30),
    n_moves: int = 3,
    truth_sims: int = 1500,
    seed: int = 0,
) -> DeepData:
    """Deep inputs for a synthetic universe (see the module docstring)."""
    rng = np.random.default_rng(seed)
    meta = [
        MatchMetaRecord(
            match_id=m.id,
            scheduled_at=m.played_at,
            team1_id=m.team1_id,
            team2_id=m.team2_id,
            status=MatchStatus.FINISHED,
            format=m.format,
            tier=m.tier,
            is_lan=m.is_lan,
            tournament_id={"s": 1, "a": 2, "b": 3}.get(m.tier.value),
            tournament_name=f"Synthetic {m.tier.value.upper()}-tier",
        )
        for m in universe.matches
    ]
    duels: list[DuelRecord] = []
    if with_duels:
        for m in universe.matches:
            for mp in m.maps:
                played_at = m.played_at + timedelta(hours=mp.map_number - 1)
                for r in mp.rounds:
                    for i, (a, v) in enumerate(r.kills):
                        att, vic = universe.players[a], universe.players[v]
                        a_team1 = att.team_id == m.team1_id
                        side = r.team1_side if a_team1 else r.team1_side.other
                        awp = att.role is PlayerRole.AWPER and rng.random() < awp_rate
                        v_awp = vic.role is PlayerRole.AWPER and rng.random() < awp_rate
                        duels.append(
                            DuelRecord(
                                match_id=m.id,
                                map_number=mp.map_number,
                                map_name=mp.map_name,
                                played_at=played_at,
                                attacker_id=a,
                                victim_id=v,
                                attacker_team_id=att.team_id,
                                victim_team_id=vic.team_id,
                                attacker_side=side,
                                weapon="awp" if awp else "ak47" if side is Side.T else "m4a1",
                                victim_weapon="awp" if v_awp else None,
                                is_opening=i == 0,
                            )
                        )
    market: dict[int, list[MarketPoint]] = {}
    if with_market:
        truth = synthetic_series_truth(universe, n_sims=truth_sims, seed=seed)
        for m in universe.matches:
            t = truth.get(m.id)
            if t is None or t.p_draw > 0:
                continue
            pts: list[MarketPoint] = []
            steps = max(1, n_moves)
            for k in range(steps + 1):
                frac = k / steps
                noise = opening_noise_sd + (closing_noise_sd - opening_noise_sd) * frac
                o1, o2 = synthetic_two_way_odds(t.p_team1_win, margin=margin, noise_sd=noise, rng=rng)
                at = m.played_at - opening_lead * (1.0 - frac) - timedelta(minutes=10)
                pts.append(MarketPoint(at, o1, o2))
            market[m.id] = pts
    return DeepData(match_meta=meta, duels=duels, market=market)


@dataclass
class VariantResult:
    name: str
    n_features: int
    fallback: bool
    log_loss: float | None
    brier: float | None
    ece: float | None
    rating_log_loss: float | None
    high_confidence: dict[str, Any]
    top_features: list[tuple[str, float]]
    report: FitReport | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "variant": self.name,
            "n_features": self.n_features,
            "log_loss": self.log_loss,
            "brier": self.brier,
            "ece": self.ece,
            "rating_log_loss": self.rating_log_loss,
            "n_high": self.high_confidence.get("n"),
            "high_hit_rate": self.high_confidence.get("hit_rate"),
            "high_mean_p": self.high_confidence.get("mean_predicted"),
        }


@dataclass
class DeepEvalReport:
    variants: list[VariantResult] = field(default_factory=list)

    def by_name(self, name: str) -> VariantResult:
        for v in self.variants:
            if v.name == name:
                return v
        raise KeyError(name)

    def table(self) -> list[dict[str, Any]]:
        return [v.as_row() for v in self.variants]


def _fit_variant(
    name: str,
    map_results: Sequence[MapResultRecord],
    store: FeatureStore,
    ratings: RatingProvider,
    config: EnsembleConfig,
) -> VariantResult:
    ens = MapWinEnsemble(config)
    rep = ens.fit(map_results, store=store, rating_provider=ratings)
    cal = rep.holdout.get("calibrated")
    rating = rep.holdout.get("rating")
    imp = ens.feature_importance()
    return VariantResult(
        name=name,
        n_features=rep.n_features,
        fallback=rep.fallback,
        log_loss=cal.log_loss if cal else None,
        brier=cal.brier if cal else None,
        ece=cal.ece if cal else None,
        rating_log_loss=rating.log_loss if rating else None,
        high_confidence=dict(rep.high_confidence),
        top_features=list(imp.items())[:30],
        report=rep,
    )


def compare_feature_sets(
    universe: SynthUniverse,
    *,
    deep: DeepData | None = None,
    ensemble_config: EnsembleConfig | None = None,
    with_rounds: bool = True,
    include_unscreened: bool = False,
) -> DeepEvalReport:
    """Fit the ensemble with the baseline, deep and deep+market feature sets (module docstring)."""
    from cs2bot.models.ratings import MapRatingModel

    deep = deep if deep is not None else synthetic_deep_data(universe)
    records = universe.map_results()
    rounds = universe.round_records() if with_rounds else []
    players = universe.player_map_records()
    vetoes = universe.veto_records()
    cfg = ensemble_config or EnsembleConfig(holdout_days=90, n_bootstrap=2)
    ratings = MapRatingModel().fit(records, record_history=True)
    report = DeepEvalReport()

    def store(config: FeatureConfig, data: DeepData | None) -> FeatureStore:
        return FeatureStore.build(
            records, player_maps=players, rounds=rounds, vetoes=vetoes, config=config, deep=data
        )

    report.variants.append(
        _fit_variant("baseline", records, store(FeatureConfig(deep_features=False), None), ratings, cfg)
    )
    no_market = DeepData(
        match_meta=deep.match_meta,
        team_meta=deep.team_meta,
        clutches=deep.clutches,
        duels=deep.duels,
        coach_changes=deep.coach_changes,
    )
    deep_store = store(FeatureConfig(), no_market)
    report.variants.append(_fit_variant("deep", records, deep_store, ratings, cfg))
    if include_unscreened:
        unscreened = cfg.model_copy(update={"screen_feature_groups": False})
        report.variants.append(_fit_variant("deep_unscreened", records, deep_store, ratings, unscreened))
    if deep.market or deep.sharp:
        report.variants.append(
            _fit_variant("deep_market", records, store(FeatureConfig(), deep), ratings, cfg)
        )
    return report


def _fmt(x: float | None, digits: int = 4) -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{digits}f}"


def markdown_summary(report: DeepEvalReport, *, top: int = 15) -> str:
    """The comparison and the importance table as Markdown (used for docs/dev/reports/deep_data.md)."""
    lines = [
        "| variant | features | log-loss | Brier | ECE | rating log-loss | n >= 0.75 | hit rate | mean p |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for v in report.variants:
        r = v.as_row()
        lines.append(
            f"| {v.name} | {v.n_features} | {_fmt(v.log_loss)} | {_fmt(v.brier)} | {_fmt(v.ece)} | "
            f"{_fmt(v.rating_log_loss)} | {r['n_high'] or 0} | {_fmt(r['high_hit_rate'], 3)} | "
            f"{_fmt(r['high_mean_p'], 3)} |"
        )
    for v in report.variants:
        lines += [
            "",
            f"Top {top} features ({v.name}, share of gain):",
            "",
            "| feature | gain share |",
            "|---|---|",
        ]
        lines += [f"| {name} | {share:.3f} |" for name, share in v.top_features[:top]]
    return "\n".join(lines)
