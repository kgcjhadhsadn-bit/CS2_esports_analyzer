"""Synthetic end-to-end backtest: known truth, bookmaker-like odds, oracle / random / real models.

:func:`run_synthetic_backtest` lets the owner sanity-check the whole pipeline on a universe from
:mod:`cs2bot.testing.synthetic` whose true probabilities are known:

* **truth** - P(team1 wins a map) for every (team1, team2, map) is estimated by a vectorised replica
  of the generator's own round loop (:func:`synthetic_map_truth`; same buy rules, economy, CT bias,
  overtime and 50/50 starting side as ``SynthUniverse.true_map_win_prob``, many times faster). The
  series truth follows from the realised veto order with exact series maths. The truth is exact
  only for universes generated with ``drift_sd_per_30d = 0`` (the generator does not keep the
  drift history).
* **odds** - for the match winner and every map-winner market, an opening price (``opening_lead``
  before kick-off, noise ``opening_noise_sd``) and a closing price (``closing_lead`` before
  kick-off, smaller noise ``closing_noise_sd``) from
  :func:`~cs2bot.testing.synthetic.synthetic_two_way_odds` with a proportional margin - the
  closing line is the more efficient one, as in real markets.
* **models** - ``"oracle"`` (the true probabilities), ``"random"`` (uniform nonsense),
  ``"ratings"`` (rating + veto model with exact series maths), ``"pipeline"`` (ratings + veto + round
  model + simulation via ``build_match_prediction``), ``"ensemble"`` (the pipeline with the
  LightGBM ensemble), or any trainer callable.

An oracle must show positive ROI and positive CLV against noisy, margin-carrying odds; a random
model must lose roughly the margin plus the 12% stake tax.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

import numpy as np

from cs2bot.backtest.odds_history import InMemoryOddsHistory
from cs2bot.backtest.pipeline import PipelineConfig, PipelineTrainer
from cs2bot.backtest.policy import Decider, Pricer, Resolver, Staker
from cs2bot.backtest.report import BacktestReport
from cs2bot.backtest.runner import BacktestConfig, run_backtest
from cs2bot.backtest.types import (
    BacktestMatch,
    HistoricalData,
    MatchQuery,
    ProbabilityTable,
    Trainer,
    TrainingSet,
    outcome_key,
)
from cs2bot.config import Settings, get_settings
from cs2bot.domain.enums import MatchFormat, TeamSlot, VetoAction
from cs2bot.domain.markets import (
    SELECTION_TEAM1,
    SELECTION_TEAM2,
    MarketSpec,
    MarketType,
)
from cs2bot.domain.match import LineupPlayer, MatchContext
from cs2bot.domain.odds import OddsQuote
from cs2bot.domain.predictions import OutcomeProbability
from cs2bot.domain.results import MapActual, MatchActuals
from cs2bot.models.ensemble import EnsembleConfig
from cs2bot.models.sim_series import series_outcome
from cs2bot.testing.synthetic import (
    BOMB_REWARD,
    KILL_REWARD,
    LOSS_BONUS,
    MAX_MONEY,
    OT_START_MONEY,
    START_MONEY,
    WIN_REWARD,
    SynthMatch,
    SynthUniverse,
    synthetic_two_way_odds,
)
from cs2bot.utils.money import D

__all__ = [
    "OracleTrainer",
    "RandomTrainer",
    "SeriesTruth",
    "SyntheticMarkets",
    "played_order",
    "run_synthetic_backtest",
    "synthetic_backtest_matches",
    "synthetic_map_truth",
    "synthetic_odds_history",
    "synthetic_series_truth",
]

SyntheticModel = Literal["oracle", "random", "ratings", "pipeline", "ensemble"]
SyntheticMarkets = tuple[Literal["match_winner", "map_winner"], ...]

# buy values of the generator (team-level round-logit contributions), in BUY code order
_ECO_VALUE, _FORCE_VALUE = -1.7, -0.75


# ---------------------------------------------------------------------------------------------
# truth
# ---------------------------------------------------------------------------------------------


def _simulate_map_wins(diff: np.ndarray, ct_bias: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Vectorised replica of ``cs2bot.testing.synthetic._play_map`` (no kills): team1 won (bool).

    One element per simulated map; ``diff`` is ``round_scale * (team1 - team2 map strength)``.
    Random draws are made for every element (also where the generator would skip a draw), which
    leaves every distribution unchanged. Finished maps are dropped from the state arrays each round.
    """
    n_total = diff.size
    result = np.zeros(n_total, dtype=bool)
    ids = np.arange(n_total)
    diff = np.asarray(diff, dtype=float)
    ct_bias = np.asarray(ct_bias, dtype=float)
    score = np.zeros((2, n_total), dtype=np.int64)
    money = np.full((2, n_total), 5.0 * START_MONEY)
    losses = np.ones((2, n_total), dtype=np.int64)
    t1_ct = rng.random(n_total) < 0.5  # 50/50 starting side
    loss_bonus = np.asarray(LOSS_BONUS, dtype=float)
    rnd = 0
    while ids.size and rnd < 24 + 6 * 60:  # the cap is a safety net, never reached in practice
        rnd += 1
        n = ids.size
        if rnd == 13:
            t1_ct = ~t1_ct
            money[:] = 5.0 * START_MONEY
            losses[:] = 1
        if rnd > 24:
            ot_round = (rnd - 25) % 6
            if ot_round in (0, 3):
                if ot_round == 3:
                    t1_ct = ~t1_ct
                money[:] = 5.0 * OT_START_MONEY
                losses[:] = 1
        value = np.zeros((2, n))
        if rnd in (1, 13):
            spend = np.minimum(money, 5.0 * START_MONEY)
        else:
            avg = money / 5.0
            full = avg >= 4100
            full_spend = np.minimum(
                np.floor(5.0 * np.minimum(avg, 4300.0 + rng.normal(300.0, 150.0, size=(2, n)))), money
            )
            force = ~full & (avg >= 2000) & (rng.random((2, n)) < 0.55)
            force_spend = np.minimum(money, np.floor(5.0 * (2000.0 + rng.uniform(0.0, 600.0, size=(2, n)))))
            eco_spend = np.minimum(money, np.floor(5.0 * rng.uniform(150.0, 450.0, size=(2, n))))
            spend = np.where(full, full_spend, np.where(force, force_spend, eco_spend))
            value = np.where(full, 0.0, np.where(force, _FORCE_VALUE, _ECO_VALUE))
        money = money - spend
        side_term = np.where(t1_ct, ct_bias, -ct_bias)
        logit = diff + side_term + value[0] - value[1]
        win1 = rng.random(n) < 1.0 / (1.0 + np.exp(-logit))
        winner_ct = np.where(win1, t1_ct, ~t1_ct)
        u = rng.random(n)
        elim = np.where(winner_ct, u < 0.7, u < 0.6)
        bomb_reason = ~elim & np.where(winner_ct, u < 0.9, True)  # defused (CT) / exploded (T)
        planted = bomb_reason | (winner_ct & elim & (rng.random(n) < 0.3))
        loser_deaths = np.where(elim, 5, rng.binomial(5, 0.55, size=n))
        winner_deaths = np.minimum(4, rng.binomial(4, 0.33, size=n))
        w = np.where(win1, 0, 1)
        lo = 1 - w
        col = np.arange(n)
        reward = np.where(bomb_reason, float(BOMB_REWARD), float(WIN_REWARD))
        score[w, col] += 1
        money[w, col] += 5.0 * reward + KILL_REWARD * loser_deaths
        losses[w, col] = np.maximum(0, losses[w, col] - 1)
        losses[lo, col] = np.minimum(5, losses[lo, col] + 1)
        bonus = loss_bonus[losses[lo, col] - 1]
        money[lo, col] += (
            5.0 * bonus + KILL_REWARD * winner_deaths + np.where(winner_ct & planted, 800.0, 0.0)
        )
        money = np.minimum(money, 5.0 * MAX_MONEY)
        if rnd <= 24:
            done = (score[0] == 13) | (score[1] == 13)
        else:
            base = 12 + 3 * ((rnd - 25) // 6)
            done = (score[0] - base == 4) | (score[1] - base == 4)
        if done.any():
            result[ids[done]] = score[0, done] > score[1, done]
            keep = ~done
            ids, diff, ct_bias, t1_ct = ids[keep], diff[keep], ct_bias[keep], t1_ct[keep]
            score, money, losses = score[:, keep], money[:, keep], losses[:, keep]
    return result


def synthetic_map_truth(
    universe: SynthUniverse,
    combos: Iterable[tuple[int, int, str]],
    *,
    n_sims: int = 4000,
    seed: int = 0,
) -> dict[tuple[int, int, str], float]:
    """True P(team1 wins the map) for each ``(team1_id, team2_id, map_name)`` (module docstring).

    Symmetric pairs are simulated once (``P(b beats a) = 1 - P(a beats b)``). Standard error is
    about ``0.5 / sqrt(n_sims)``.
    """
    wanted = {(a, b, m) for a, b, m in combos}
    canon = sorted({(min(a, b), max(a, b), m) for a, b, m in wanted})
    out: dict[tuple[int, int, str], float] = {}
    if not canon:
        return out
    rng = np.random.default_rng(seed)
    diffs = np.empty(len(canon))
    biases = np.empty(len(canon))
    for i, (a, b, m) in enumerate(canon):
        ta, tb = universe.teams[a], universe.teams[b]
        diffs[i] = universe.round_scale * (ta.map_strength(m) - tb.map_strength(m))
        biases[i] = universe.ct_bias.get(m, 0.0)
    chunk = max(1, 400_000 // n_sims)
    probs = np.empty(len(canon))
    for start in range(0, len(canon), chunk):
        sl = slice(start, start + chunk)
        d = np.repeat(diffs[sl], n_sims)
        c = np.repeat(biases[sl], n_sims)
        wins = _simulate_map_wins(d, c, rng).reshape(-1, n_sims)
        probs[sl] = wins.mean(axis=1)
    by_canon = {key: float(p) for key, p in zip(canon, probs, strict=True)}
    for a, b, m in wanted:
        p = by_canon[(min(a, b), max(a, b), m)]
        out[(a, b, m)] = p if a <= b else 1.0 - p
    return out


def played_order(match: SynthMatch) -> list[str]:
    """The maps of the series in playing order (picks, then the decider; BO2 has no decider)."""
    picks = [s.map_name for s in match.veto if s.action is VetoAction.PICK]
    leftover = [s.map_name for s in match.veto if s.action is VetoAction.LEFTOVER]
    order = picks + ([] if match.format is MatchFormat.BO2 else leftover)
    return order[: match.format.n_maps]


@dataclass(frozen=True)
class SeriesTruth:
    """True probabilities of one synthetic match (given its realised veto)."""

    match_id: int
    maps: tuple[str, ...]
    p_maps: tuple[float, ...]  # P(team1 wins map k | played)
    p_played: tuple[float, ...]
    p_team1_win: float
    p_draw: float


def synthetic_series_truth(
    universe: SynthUniverse, *, n_sims: int = 4000, seed: int = 0
) -> dict[int, SeriesTruth]:
    """True series probabilities of every match of ``universe``."""
    combos = [(m.team1_id, m.team2_id, name) for m in universe.matches for name in played_order(m)]
    truth = synthetic_map_truth(universe, combos, n_sims=n_sims, seed=seed)
    out: dict[int, SeriesTruth] = {}
    for m in universe.matches:
        maps = played_order(m)
        if len(maps) < m.format.n_maps:
            continue
        p_maps = [truth[(m.team1_id, m.team2_id, name)] for name in maps]
        res = series_outcome(m.format, p_maps)
        out[m.id] = SeriesTruth(
            match_id=m.id,
            maps=tuple(maps),
            p_maps=tuple(p_maps),
            p_played=tuple(res.p_played),
            p_team1_win=res.p_team1_win,
            p_draw=res.p_draw,
        )
    return out


# ---------------------------------------------------------------------------------------------
# matches and odds
# ---------------------------------------------------------------------------------------------


def synthetic_backtest_matches(universe: SynthUniverse) -> list[BacktestMatch]:
    """Every synthetic match with its actual result and its pre-match context (lineups, veto)."""
    out: list[BacktestMatch] = []
    for m in universe.matches:
        maps = []
        for mp in m.maps:
            kills: dict[int, int] = {}
            for r in mp.rounds:
                for attacker, _ in r.kills:
                    kills[attacker] = kills.get(attacker, 0) + 1
            maps.append(
                MapActual(
                    map_number=mp.map_number,
                    map_name=mp.map_name,
                    team1_rounds=mp.team1_rounds,
                    team2_rounds=mp.team2_rounds,
                    round_winners=tuple(r.winner for r in mp.rounds) or None,
                    player_kills=kills,
                )
            )
        actuals = MatchActuals(
            match_id=m.id, format=m.format, team1_id=m.team1_id, team2_id=m.team2_id, maps=tuple(maps)
        )
        context = MatchContext(
            match_id=m.id,
            scheduled_at=m.played_at,
            format=m.format,
            team1_id=m.team1_id,
            team2_id=m.team2_id,
            tier=m.tier,
            is_lan=m.is_lan,
            team1_lineup=[
                LineupPlayer(player_id=p.id, nickname=p.nickname, role=p.role)
                for p in universe.teams[m.team1_id].players
            ],
            team2_lineup=[
                LineupPlayer(player_id=p.id, nickname=p.nickname, role=p.role)
                for p in universe.teams[m.team2_id].players
            ],
            map_pool=list(universe.map_pool),
            known_veto=list(m.veto),
            first_veto_team=TeamSlot.TEAM1,
        )
        out.append(
            BacktestMatch(
                match_id=m.id,
                start_time=m.played_at,
                format=m.format,
                team1_id=m.team1_id,
                team2_id=m.team2_id,
                actuals=actuals,
                context=context,
                tier=m.tier,
                is_lan=m.is_lan,
            )
        )
    return out


def _outcomes(
    truth: SeriesTruth, fmt: MatchFormat, markets: SyntheticMarkets
) -> list[tuple[MarketSpec, float, float]]:
    """(spec, P(team1 side wins | settled), P(settled)) of every two-way market of the match."""
    out: list[tuple[MarketSpec, float, float]] = []
    if "match_winner" in markets and fmt is not MatchFormat.BO2:
        out.append((MarketSpec(market_type=MarketType.MATCH_WINNER), truth.p_team1_win, 1.0))
    if "map_winner" in markets:
        for k, (p, played) in enumerate(zip(truth.p_maps, truth.p_played, strict=True), start=1):
            out.append((MarketSpec(market_type=MarketType.MAP_WINNER, map_number=k), p, played))
    return out


def synthetic_odds_history(
    universe: SynthUniverse,
    truth: dict[int, SeriesTruth],
    *,
    markets: SyntheticMarkets = ("match_winner", "map_winner"),
    margin: float = 0.06,
    opening_noise_sd: float = 0.06,
    closing_noise_sd: float = 0.02,
    opening_lead: timedelta = timedelta(hours=24),
    closing_lead: timedelta = timedelta(minutes=10),
    seed: int = 0,
) -> InMemoryOddsHistory:
    """Opening and closing two-way prices around the truth for every match (module docstring)."""
    rng = np.random.default_rng(seed)
    history = InMemoryOddsHistory()
    sel_id = 0
    for m in universe.matches:
        t = truth.get(m.id)
        if t is None:
            continue
        history.set_kickoff(m.id, m.played_at)
        for spec, p1, _ in _outcomes(t, m.format, markets):
            for lead, noise in ((opening_lead, opening_noise_sd), (closing_lead, closing_noise_sd)):
                o1, o2 = synthetic_two_way_odds(p1, margin=margin, noise_sd=noise, rng=rng)
                for selection, odds in ((SELECTION_TEAM1, o1), (SELECTION_TEAM2, o2)):
                    sel_id += 1
                    history.add(
                        m.id,
                        OddsQuote(
                            selection_id=sel_id,
                            bookmaker_event_id=m.id,
                            external_event_id=f"synth-{m.id}",
                            market_raw_name=spec.key(),
                            selection_raw_name=selection,
                            spec=spec,
                            selection_key=selection,
                            odds=max(D(str(odds)), Decimal("1.01")),
                            captured_at=m.played_at - lead,
                        ),
                    )
    return history


# ---------------------------------------------------------------------------------------------
# synthetic models
# ---------------------------------------------------------------------------------------------


class _TablePredictor:
    def __init__(
        self, tables: dict[int, dict[str, OutcomeProbability]], version: str, cutoff: datetime
    ) -> None:
        self._tables = tables
        self.model_version = version
        self.trained_through: datetime | None = None
        self._cutoff = cutoff

    def predict(self, query: MatchQuery) -> ProbabilityTable:
        return ProbabilityTable(
            match_id=query.match_id,
            as_of=query.as_of,
            outcomes=self._tables.get(query.match_id, {}),
            model_version=self.model_version,
        )


def _table(
    truth: SeriesTruth, fmt: MatchFormat, markets: SyntheticMarkets, sigma: float
) -> dict[str, OutcomeProbability]:
    out: dict[str, OutcomeProbability] = {}
    for spec, p1, played in _outcomes(truth, fmt, markets):
        for selection, p in ((SELECTION_TEAM1, p1), (SELECTION_TEAM2, 1.0 - p1)):
            out[outcome_key(spec, selection)] = OutcomeProbability(
                spec_key=spec.key(),
                selection=selection,
                p_win=played * p,
                p_void=1.0 - played,
                sigma=sigma,
            )
    return out


class OracleTrainer:
    """Predicts the true probabilities (the synthetic truth; uses no training data)."""

    def __init__(
        self,
        truth: dict[int, SeriesTruth],
        formats: dict[int, MatchFormat],
        *,
        markets: SyntheticMarkets = ("match_winner", "map_winner"),
        sigma: float = 0.0,
    ) -> None:
        self._tables = {mid: _table(t, formats[mid], markets, sigma) for mid, t in truth.items()}

    def __call__(self, training: TrainingSet) -> _TablePredictor:
        return _TablePredictor(self._tables, "oracle", training.cutoff)


class RandomTrainer:
    """Predicts uniform random probabilities in [0.05, 0.95] (a model without any skill)."""

    def __init__(
        self,
        truth: dict[int, SeriesTruth],
        formats: dict[int, MatchFormat],
        *,
        markets: SyntheticMarkets = ("match_winner", "map_winner"),
        seed: int = 0,
    ) -> None:
        rng = np.random.default_rng(seed)
        self._tables: dict[int, dict[str, OutcomeProbability]] = {}
        for mid, t in sorted(truth.items()):
            fake = SeriesTruth(
                match_id=mid,
                maps=t.maps,
                p_maps=tuple(float(x) for x in rng.uniform(0.05, 0.95, size=len(t.p_maps))),
                p_played=t.p_played,
                p_team1_win=float(rng.uniform(0.05, 0.95)),
                p_draw=0.0,
            )
            self._tables[mid] = _table(fake, formats[mid], markets, 0.0)

    def __call__(self, training: TrainingSet) -> _TablePredictor:
        return _TablePredictor(self._tables, "random", training.cutoff)


def _settings_for(settings: Settings | None) -> Settings:
    return settings or get_settings()


def run_synthetic_backtest(
    universe: SynthUniverse,
    *,
    model: SyntheticModel | Trainer = "oracle",
    markets: SyntheticMarkets = ("match_winner", "map_winner"),
    margin: float = 0.06,
    opening_noise_sd: float = 0.06,
    closing_noise_sd: float = 0.02,
    config: BacktestConfig | None = None,
    settings: Settings | None = None,
    pipeline_config: PipelineConfig | None = None,
    truth_sims: int = 2000,
    truth: dict[int, SeriesTruth] | None = None,
    seed: int = 0,
    warmup_fraction: float = 0.3,
    pricer: Pricer | None = None,
    decider: Decider | None = None,
    staker: Staker | None = None,
    resolver: Resolver | None = None,
    matches: Sequence[BacktestMatch] | None = None,
) -> BacktestReport:
    """Backtest a model on a synthetic universe against synthetic odds (module docstring).

    ``truth`` (from :func:`synthetic_series_truth`) can be passed to share one truth estimate
    between several runs. The first ``warmup_fraction`` of the matches only serve as training
    history (betting starts after them) unless ``config.start`` is given. Betting rules, bankroll
    and tax come from ``settings`` (default :func:`get_settings`); lower
    ``decision.min_probability`` to see bets on underdogs.
    """
    st = _settings_for(settings)
    if truth is None:
        truth = synthetic_series_truth(universe, n_sims=truth_sims, seed=seed)
    formats = {m.id: m.format for m in universe.matches}
    odds = synthetic_odds_history(
        universe,
        truth,
        markets=markets,
        margin=margin,
        opening_noise_sd=opening_noise_sd,
        closing_noise_sd=closing_noise_sd,
        seed=seed + 1,
    )
    bt_matches = list(matches) if matches is not None else synthetic_backtest_matches(universe)
    data = HistoricalData(
        map_results=universe.map_results(),
        vetoes=universe.veto_records(),
        rounds=universe.round_records() if model in ("pipeline", "ensemble") else (),
        player_maps=(),
        matches=bt_matches,
    )
    cfg = config or BacktestConfig()
    if cfg.start is None and bt_matches and warmup_fraction > 0:
        ordered = sorted(m.start_time for m in bt_matches)
        cfg = cfg.model_copy(
            update={"start": ordered[min(len(ordered) - 1, int(len(ordered) * warmup_fraction))]}
        )
    trainer: Trainer
    if model == "oracle":
        trainer = OracleTrainer(truth, formats, markets=markets)
    elif model == "random":
        trainer = RandomTrainer(truth, formats, markets=markets, seed=seed + 2)
    elif model == "ratings":
        trainer = PipelineTrainer(pipeline_config or PipelineConfig(simulate=False), settings=st)
    elif model == "pipeline":
        trainer = PipelineTrainer(pipeline_config or PipelineConfig(simulate=True), settings=st)
    elif model == "ensemble":
        trainer = PipelineTrainer(
            pipeline_config
            or PipelineConfig(simulate=False, use_ensemble=True, ensemble_config=EnsembleConfig()),
            settings=st,
        )
    elif callable(model):
        trainer = model
    else:
        raise ValueError(f"unknown synthetic model {model!r}")
    report = run_backtest(
        data,
        trainer=trainer,
        odds=odds,
        config=cfg,
        settings=st,
        pricer=pricer,
        decider=decider,
        staker=staker,
        resolver=resolver,
        matches=bt_matches,
    )
    if not math.isfinite(margin) or margin < 0:
        report.warnings.append(f"unusual bookmaker margin {margin}")
    return report
