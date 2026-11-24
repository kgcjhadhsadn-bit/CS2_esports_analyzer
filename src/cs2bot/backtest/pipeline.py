"""The full prediction pipeline as a backtest trainer: ratings / ensemble -> veto -> match prediction.

:class:`PipelineTrainer` fits, on a :class:`~cs2bot.backtest.types.TrainingSet` only:

* the map rating model (:class:`~cs2bot.models.ratings.WalkForwardRatings`, history-aware so the
  ensemble's training rows can ask it about past dates),
* the veto model (with the rating model's map advantage),
* the round model (only when simulating), the player kill model (only with ``player_props``),
* optionally the :class:`~cs2bot.models.ensemble.MapWinEnsemble` on a ``FeatureStore`` of the set.

The returned :class:`PipelinePredictor` turns a :class:`~cs2bot.backtest.types.MatchQuery` into a
:class:`~cs2bot.domain.predictions.MatchPrediction`, exactly like the live bot:

1. veto distribution - the published veto steps from the query's context condition
   ``VetoModel.predict_sequences`` (a complete veto gives one sequence, ``veto_known=True``);
2. per-map :class:`MapWinEstimate` s - the ensemble's ``predict_for_sequences`` or the rating model's;
3. ``build_match_prediction`` (round-by-round simulation: every market type), or - with
   ``simulate=False`` - :func:`series_match_prediction`, an exact series calculation without round
   simulation that prices the series-level markets (match winner, map winner, correct map score,
   map handicap, total maps, both teams win a map) in milliseconds.

Between retrains ``observe`` keeps the rating model current (incremental updates, or a refit when
results arrive out of order); the other models wait for the next retrain.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from cs2bot.backtest.types import MatchQuery, TrainingSet
from cs2bot.config import DEFAULT_ACTIVE_MAP_POOL, Settings, get_settings
from cs2bot.domain.enums import MatchFormat, VetoAction
from cs2bot.domain.match import MatchContext
from cs2bot.domain.predictions import (
    MapSequenceProbability,
    MapSlotPrediction,
    MapWinEstimate,
    MatchPrediction,
    PredictionCore,
)
from cs2bot.domain.records import MapResultRecord
from cs2bot.models.ensemble import EnsembleConfig, MapWinEnsemble
from cs2bot.models.match_engine import build_match_prediction, players_from_context, scenario_probabilities
from cs2bot.models.player_props import PlayerKillModel
from cs2bot.models.ratings import MapRatingModel, RatingConfig, WalkForwardRatings
from cs2bot.models.round_model import RoundModel
from cs2bot.models.sim_series import series_outcome
from cs2bot.models.sim_stats import stable_seed
from cs2bot.models.veto import VetoConfig, VetoModel
from cs2bot.utils.names import normalize_map_name
from cs2bot.utils.time import ensure_utc

logger = logging.getLogger(__name__)

__all__ = ["PipelineConfig", "PipelinePredictor", "PipelineTrainer", "series_match_prediction"]


class PipelineConfig(BaseModel):
    """What the pipeline trainer fits and how the predictor predicts."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    simulate: bool = True  # build_match_prediction (all markets) vs exact series maths (series markets)
    n_sims: int = 2_000  # map simulations per scenario (the live default is 20 000; a backtest is long)
    scenario_n_sims: int | None = None
    use_ensemble: bool = False
    ensemble_config: EnsembleConfig | None = None
    rating_config: RatingConfig | None = None
    veto_config: VetoConfig | None = None
    veto_top_k: int = 50
    player_props: bool = False  # fit the kill model and project lineups (slow)
    online_ratings: bool = True  # keep the rating model current between retrains
    seed: int = 20231
    model_version_prefix: str = "bt"


def _played_sequences(
    seqs: Sequence[MapSequenceProbability], fmt: MatchFormat
) -> list[tuple[tuple[str, ...], float]]:
    """Normalised, truncated, merged and renormalised (maps, probability) pairs."""
    n = fmt.n_maps
    merged: dict[tuple[str, ...], float] = defaultdict(float)
    for s in seqs:
        q = float(s.probability)
        if not q > 0:
            continue
        maps = tuple(normalize_map_name(m) for m in s.maps)[:n]
        if len(maps) < n:
            raise ValueError(f"veto sequence {maps} has fewer than the {n} maps a {fmt.value} needs")
        merged[maps] += q
    total = sum(merged.values())
    if total <= 0:
        raise ValueError("the veto distribution has no positive probability")
    return [(maps, q / total) for maps, q in merged.items()]


def series_match_prediction(
    *,
    match_id: int,
    fmt: MatchFormat,
    team1_id: int,
    team2_id: int,
    veto_distribution: Sequence[MapSequenceProbability],
    map_estimates: dict[str, MapWinEstimate],
    model_version: str,
    created_at: datetime,
    veto_known: bool = False,
    data_completeness: float = 0.0,
    logit_shift: float = 0.0,
) -> MatchPrediction:
    """A :class:`MatchPrediction` from exact series maths (no round simulation).

    Maps are independent given their probabilities, so for each veto sequence
    :func:`~cs2bot.models.sim_series.series_outcome` gives the exact map-score distribution and
    P(slot k played); the sequences are mixed by their probabilities. ``low`` / ``high`` shift every
    map by -/+ one sigma in logit space (perfectly correlated, as in ``build_match_prediction``).
    Slots carry no round-level distributions (empty ``score_pmf``), so the pricing engine refuses
    round markets (``UnpriceableMarket``) instead of guessing.
    """
    if created_at.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    seqs = _played_sequences(veto_distribution, fmt)
    estimates = {normalize_map_name(k): v for k, v in map_estimates.items()}
    probs: dict[str, tuple[float, float, float]] = {}
    for maps, _ in seqs:
        for m in maps:
            if m not in probs:
                if m not in estimates:
                    raise ValueError(f"no map estimate for {m!r}")
                c, lo, hi, _ = scenario_probabilities(estimates[m], logit_shift)
                probs[m] = (c, lo, hi)

    def core(idx: int) -> PredictionCore:
        n = fmt.n_maps
        score: dict[str, float] = defaultdict(float)
        played = [0.0] * n
        win_joint = [0.0] * n
        dist: list[dict[str, float]] = [defaultdict(float) for _ in range(n)]
        for maps, q in seqs:
            ps = [probs[m][idx] for m in maps]
            res = series_outcome(fmt, ps)
            for (a, b), pr in res.final.items():
                score[f"{a}-{b}"] += q * pr
            for k in range(n):
                pk = res.p_played[k]
                played[k] += q * pk
                win_joint[k] += q * pk * ps[k]
                dist[k][maps[k]] += q * pk
        slots = []
        for k in range(n):
            pk = played[k]
            slots.append(
                MapSlotPrediction(
                    map_number=k + 1,
                    p_played=min(max(pk, 0.0), 1.0),
                    map_distribution={m: v / pk for m, v in dist[k].items()} if pk > 0 else {},
                    p_team1_win=win_joint[k] / pk if pk > 0 else 0.5,
                    score_pmf={},
                    p_overtime=0.0,
                )
            )
        p1 = 0.0
        p_draw = 0.0
        for key, pr in score.items():
            a, b = (int(x) for x in key.split("-"))
            if a > b:
                p1 += pr
            elif a == b:
                p_draw += pr
        return PredictionCore(p_team1_win=p1, p_draw=p_draw, map_score_pmf=dict(score), slots=slots)

    central, low, high = core(0), core(1), core(2)
    return MatchPrediction(
        match_id=match_id,
        created_at=ensure_utc(created_at),
        model_version=model_version,
        format=fmt,
        team1_id=team1_id,
        team2_id=team2_id,
        central=central,
        low=low,
        high=high,
        sigma_team1_win=abs(high.p_team1_win - low.p_team1_win) / 2.0,
        map_estimates=estimates,
        veto_distribution=list(veto_distribution),
        veto_known=veto_known,
        data_completeness=data_completeness,
        n_simulations=0,
        components={"engine": "series", "scenario_map_p": {m: list(v) for m, v in probs.items()}},
    )


def _veto_complete(ctx: MatchContext, fmt: MatchFormat) -> bool:
    """True when the published steps fix the played maps (all picks and the decider are known)."""
    if not ctx.known_veto:
        return False
    if ctx.veto_complete:
        return True
    played = [s for s in ctx.known_veto if s.action in (VetoAction.PICK, VetoAction.LEFTOVER)]
    return len(played) >= fmt.n_maps


class PipelinePredictor:
    """Predicts a :class:`MatchQuery` with the models fitted by :class:`PipelineTrainer`."""

    def __init__(
        self,
        *,
        config: PipelineConfig,
        settings: Settings,
        ratings: WalkForwardRatings,
        veto: VetoModel,
        round_model: RoundModel | None,
        kill_model: PlayerKillModel | None,
        ensemble: MapWinEnsemble | None,
        store: object | None,
        map_results: Sequence[MapResultRecord],
        trained_through: datetime | None,
        model_version: str,
    ) -> None:
        self.config = config
        self.settings = settings
        self.ratings = ratings
        self.veto = veto
        self.round_model = round_model
        self.kill_model = kill_model
        self.ensemble = ensemble
        self.store = store
        self._map_results = list(map_results)
        self.trained_through = trained_through
        self.model_version = model_version

    # ------------------------------------------------------------------ online updates
    def observe(self, training: TrainingSet) -> None:
        """Bring the rating model up to date with the results visible at ``training.cutoff``."""
        if not self.config.online_ratings or len(training.map_results) == len(self._map_results):
            return
        known = {(r.match_id, r.map_number) for r in self._map_results}
        new = [r for r in training.map_results if (r.match_id, r.map_number) not in known]
        if not new:
            return
        model: MapRatingModel = self.ratings.model
        last = model.last_processed
        ordered = sorted(new, key=lambda r: (ensure_utc(r.played_at), r.match_id, r.map_number))
        if last is None or ensure_utc(ordered[0].played_at) > last:
            for rec in ordered:
                model.update(rec)
        else:  # out of order (overlapping matches): refit from scratch
            self.ratings = WalkForwardRatings.fit(training.map_results, self.config.rating_config)
        self._map_results = list(training.map_results)
        latest = training.latest_time
        if latest is not None and (self.trained_through is None or latest > self.trained_through):
            self.trained_through = latest

    # ------------------------------------------------------------------ prediction
    def _map_pool(self, ctx: MatchContext, as_of: datetime) -> list[str]:
        if ctx.map_pool:
            return [normalize_map_name(m) for m in ctx.map_pool]
        try:
            from cs2bot.features.meta import detect_active_map_pool

            pool = detect_active_map_pool(self._map_results, as_of)
            if pool:
                return list(pool)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("map pool detection failed: %s", exc)
        return list(self.settings.scope.active_map_pool or DEFAULT_ACTIVE_MAP_POOL)

    def veto_distribution(self, query: MatchQuery) -> tuple[list[MapSequenceProbability], bool]:
        ctx = query.to_context()
        pool = self._map_pool(ctx, query.as_of)
        seqs = self.veto.predict_sequences(
            query.team1_id,
            query.team2_id,
            query.format,
            pool,
            first_veto=ctx.first_veto_team,
            known_steps=ctx.known_veto,
            top_k=self.config.veto_top_k,
            as_of=query.as_of,
            map_advantage=self.ratings.map_advantage,
        )
        return seqs, _veto_complete(ctx, query.format) or len(seqs) == 1

    def map_estimates(
        self, query: MatchQuery, sequences: Sequence[MapSequenceProbability]
    ) -> dict[str, MapWinEstimate]:
        ctx = query.to_context()
        if self.ensemble is not None and not self.ensemble.is_fallback and self.store is not None:
            return self.ensemble.predict_for_sequences(
                ctx,
                sequences,
                as_of=query.as_of,
                store=self.store,  # type: ignore[arg-type]
                rating_provider=self.ratings,
            )
        return self.ratings.model.predict_for_sequences(
            query.team1_id,
            query.team2_id,
            sequences,
            query.as_of,
            is_lan=query.is_lan,
            team1_lineup=[p.player_id for p in ctx.team1_lineup],
            team2_lineup=[p.player_id for p in ctx.team2_lineup],
        )

    def predict(self, query: MatchQuery) -> MatchPrediction:
        as_of = ensure_utc(query.as_of)
        sequences, veto_known = self.veto_distribution(query)
        estimates = self.map_estimates(query, sequences)
        if not self.config.simulate:
            return series_match_prediction(
                match_id=query.match_id,
                fmt=query.format,
                team1_id=query.team1_id,
                team2_id=query.team2_id,
                veto_distribution=sequences,
                map_estimates=estimates,
                model_version=self.model_version,
                created_at=as_of,
                veto_known=veto_known,
                data_completeness=1.0,
            )
        ctx = query.to_context()
        players = None
        if (
            self.config.player_props
            and self.kill_model is not None
            and (ctx.team1_lineup or ctx.team2_lineup)
        ):
            players = players_from_context(ctx)
        return build_match_prediction(
            match_id=query.match_id,
            fmt=query.format,
            team1_id=query.team1_id,
            team2_id=query.team2_id,
            veto_distribution=sequences,
            map_estimates=estimates,
            round_model=self.round_model or RoundModel.default(),
            start_sides=dict(ctx.start_sides) or None,
            players=players,
            kill_model=self.kill_model if players else None,
            context_flags=(),
            n_sims=self.config.n_sims,
            scenario_n_sims=self.config.scenario_n_sims,
            seed=stable_seed(self.config.seed, query.match_id),
            model_version=self.model_version,
            data_completeness=1.0,
            veto_known=veto_known,
            created_at=as_of,
            as_of=as_of,
        )


class PipelineTrainer:
    """Backtest trainer for the whole pipeline (module docstring)."""

    def __init__(self, config: PipelineConfig | None = None, *, settings: Settings | None = None) -> None:
        self.config = config or PipelineConfig()
        self.settings = settings or get_settings()

    def __call__(self, training: TrainingSet) -> PipelinePredictor:
        cfg = self.config
        ms = self.settings.models
        ratings = WalkForwardRatings.fit(training.map_results, cfg.rating_config)
        veto = VetoModel(cfg.veto_config)
        if training.vetoes:
            veto.fit(training.vetoes, map_advantage=ratings.map_advantage)
        round_model: RoundModel | None = None
        if cfg.simulate:
            round_model = (
                RoundModel().fit(
                    training.rounds, as_of=training.cutoff, half_life_days=ms.round_model_half_life_days
                )
                if training.rounds
                else RoundModel.default()
            )
        kill_model: PlayerKillModel | None = None
        if cfg.player_props and training.player_maps:
            kill_model = PlayerKillModel().fit(
                training.player_maps, half_life_days=ms.kill_model_half_life_days
            )
        ensemble: MapWinEnsemble | None = None
        store = None
        if cfg.use_ensemble and training.map_results:
            from cs2bot.features.store import FeatureStore

            store = FeatureStore.build(
                training.map_results,
                player_maps=training.player_maps,
                rounds=training.rounds,
                vetoes=training.vetoes,
            )
            ensemble = MapWinEnsemble(cfg.ensemble_config, settings=self.settings)
            ensemble.fit(training.map_results, store=store, rating_provider=ratings)
        latest = training.latest_time
        version = f"{cfg.model_version_prefix}-{training.cutoff:%Y%m%d}"
        if ensemble is not None:
            version += f"-{ensemble.model_version}"
        return PipelinePredictor(
            config=cfg,
            settings=self.settings,
            ratings=ratings,
            veto=veto,
            round_model=round_model,
            kill_model=kill_model,
            ensemble=ensemble,
            store=store,
            map_results=training.map_results,
            trained_through=latest,
            model_version=version,
        )
