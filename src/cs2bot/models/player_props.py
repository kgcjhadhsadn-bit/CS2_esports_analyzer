"""Player kill projections: empirical-Bayes kills-per-round x simulated rounds -> NB kill distribution.

Model
-----
For player ``i`` on map ``M`` against opponent team ``O`` with ``R`` rounds played::

    kills | R ~ NegBin(mean = R * kpr_i * map_factor[M] * opp_factor[O] * team_scale, size = s)

* **Exposure-adjusted rates.** Map factors (kills per round on a map vs average) and opponent
  defensive factors (kills *conceded* per player-round by a team vs average, i.e. the opponent's
  deaths allowed) are estimated jointly with player multipliers by a few rounds of iterative
  proportional fitting of the multiplicative Poisson model ``E[k] = g * a_i * m_M * o_O * R``, each
  factor shrunk toward 1 with pseudo-rounds.
* **Empirical Bayes per player.** With exposure ``E_i = sum w R m o`` and kills ``K_i = sum w k`` the
  player's neutral KPR has a Gamma(alpha, beta) prior with the mean/variance of his role group
  (method-of-moments between-player variance, Morris 1983), giving the posterior mean
  ``(alpha + c_i K_i) / (beta + c_i E_i)``. ``c_i`` is an effective-sample factor that accounts for
  the recency weights ``w`` (half-life) and the over-dispersion of per-map kill counts
  (quasi-Poisson: ``Var = phi * mean``). Roles with few players fall back to the global prior.
* **Dispersion.** Per role, the NB size ``s`` is estimated by the method of moments from residuals:
  ``1/s = sum w [(k - mu)^2 - mu] / sum w mu^2``.
* **Uncertainty of the rate** (posterior variance for known players; role-prior variance times
  ``unknown_player_cv_mult**2`` for unknown players -> wider dispersion) is folded into the NB size:
  ``1/s_eff = (1 + 1/s)(1 + cv^2) - 1`` (product of two independent mean-one Gamma multipliers).
* **Score skew (optional, ``score_skew`` = gamma).** Kills are not independent of the scoreline: a
  team that wins more rounds kills more. Given per-simulation rounds won/lost, the mean becomes
  ``kpr * (R + gamma * (D - E[D]))`` with ``D`` = own minus opponent rounds; centring keeps the
  marginal mean at ``kpr * E[R]`` (no double counting with the opponent factor), while
  correlating player kills with the simulated map winner (top-fragger realism). The part of the
  historical over-dispersion explained by score variation is removed from ``1/s``
  (``gamma**2 * hist_margin_var``). ``gamma = 0`` gives the plain model.
* **Team normalisation (optional).** Every kill is an opponent death. A team's expected kills
  should be consistent with the expected deaths of the *actual* opposing lineup: team A's rates are
  scaled by ``sqrt(mean DPR of B's lineup / B's historical team DPR)`` (geometric mean of the
  team-level and the lineup-level defensive evidence; corrects for stand-ins / roster changes).

No look-ahead: all estimates for a projection use only records with ``played_at`` strictly before
``as_of`` (cut-off rounded *down* to the minute), with decay weights relative to ``as_of``.
"""

from __future__ import annotations

import json
import math
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict

from cs2bot.domain.enums import PlayerRole, TeamSlot
from cs2bot.domain.predictions import PlayerProjection
from cs2bot.domain.records import PlayerMapRecord
from cs2bot.models.sim_stats import nb_pmf_matrix, nb_support, pmf_moments, pmf_to_dict
from cs2bot.utils.names import normalize_map_name
from cs2bot.utils.time import ensure_utc

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

ROLE_ORDER: tuple[PlayerRole, ...] = tuple(r for r in PlayerRole if r is not PlayerRole.UNKNOWN)
ROLE_CODE: dict[PlayerRole, int] = {r: i for i, r in enumerate(ROLE_ORDER)}
NO_ROLE = -1

#: Fallback KPR by role when there is no data at all (CS2 pro averages: ~0.68 overall, AWPers
#: ~0.75-0.85, in-game leaders ~0.55-0.62).
DEFAULT_ROLE_KPR: dict[PlayerRole, float] = {
    PlayerRole.AWPER: 0.76,
    PlayerRole.ENTRY: 0.70,
    PlayerRole.LURKER: 0.69,
    PlayerRole.RIFLER: 0.68,
    PlayerRole.ROTATOR: 0.67,
    PlayerRole.ANCHOR: 0.66,
    PlayerRole.SUPPORT: 0.62,
    PlayerRole.IGL: 0.58,
}


class PlayerInput(BaseModel):
    """One expected player of the match (input to :func:`build_match_prediction`)."""

    model_config = ConfigDict(frozen=True)

    player_id: int
    team: TeamSlot
    nickname: str | None = None
    role: PlayerRole | None = None


@dataclass(frozen=True)
class KillModelConfig:
    """Hyper-parameters of :class:`PlayerKillModel` (documented defaults)."""

    map_prior_rounds: float = 4000.0  # pseudo player-rounds shrinking map factors to 1
    opp_prior_rounds: float = 1500.0  # pseudo player-rounds shrinking opponent factors to 1
    ipf_player_prior_rounds: float = 60.0  # light shrinkage of player multipliers inside the IPF
    ipf_iterations: int = 4
    min_effective_rounds_for_tau: float = 40.0
    min_players_for_tau: int = 6
    role_prior_pseudo_players: float = 5.0  # shrink role means toward the global mean
    min_tau2: float = 1e-4
    default_kpr: float = 0.68
    default_tau: float = 0.08  # between-player sd of true KPR (no data)
    default_size: float = 12.0  # per-map NB size (no data)
    min_size: float = 2.0
    max_size: float = 400.0
    min_records_for_size: int = 30
    unknown_player_cv_mult: float = 1.5
    deaths_prior_rounds: float = 150.0
    score_skew: float = 0.3
    hist_margin_var: float = 0.12  # Var(D / R) of historical maps (margin per round)
    normalise_teams: bool = True
    min_lineup_for_normalisation: int = 4
    normalisation_bounds: tuple[float, float] = (0.85, 1.18)
    cache_size: int = 64


@dataclass(frozen=True)
class PlayerRate:
    """Kill-rate parameters of one player for one map/opponent, as used for projections."""

    player_id: int
    team: TeamSlot | None
    map_name: str
    kpr: float  # expected kills per round incl. map / opponent / team adjustments
    base_kpr: float  # neutral (average map, average opponent) posterior mean KPR
    cv2: float  # squared coefficient of variation of the rate (estimation uncertainty)
    size: float  # historical per-map NB size of the player's role
    dpr: float  # deaths per round (shrunk)
    known: bool
    role: PlayerRole | None
    map_factor: float = 1.0
    opp_factor: float = 1.0
    team_scale: float = 1.0

    def scaled(self, factor: float) -> PlayerRate:
        return replace(self, kpr=self.kpr * factor, team_scale=self.team_scale * factor)


def effective_size(size: float, cv2: float, score_skew: float = 0.0, hist_margin_var: float = 0.0) -> float:
    """NB size after removing score-explained dispersion and adding rate uncertainty (see module doc)."""
    inv_hist = 1.0 / max(size, 1e-9)
    inv_cond = max(inv_hist - score_skew * score_skew * hist_margin_var, 0.25 * inv_hist)
    var_x = (1.0 + inv_cond) * (1.0 + max(cv2, 0.0)) - 1.0
    return 1.0 / max(var_x, 1e-9)


@dataclass(frozen=True)
class KillsDistribution:
    pmf: FloatArray
    mean: float
    var: float


@dataclass(frozen=True)
class KillsMixture:
    """NB mixture components of a kill distribution: one component per distinct (rounds, margin).

    ``matrix[u]`` is the NB pmf of component ``u`` (weight ``probs[u]``); ``margins`` are own-minus-
    opponent rounds (> 0 = the player's team won the map). :meth:`pmf` mixes all components or a
    subset (e.g. maps won by the player's team), so conditional pmfs reuse the same matrix.
    """

    probs: FloatArray
    rounds: FloatArray
    margins: FloatArray
    matrix: FloatArray

    def pmf(self, mask: npt.NDArray[np.bool_] | None = None) -> FloatArray:
        w = self.probs if mask is None else self.probs * mask
        total = float(w.sum())
        if total <= 0.0:
            w, total = self.probs, float(self.probs.sum())
        out = (w / total) @ self.matrix
        return np.asarray(out / out.sum(), dtype=np.float64)

    def distribution(self, mask: npt.NDArray[np.bool_] | None = None) -> KillsDistribution:
        pmf = self.pmf(mask)
        mean, var = pmf_moments(pmf)
        return KillsDistribution(pmf=pmf, mean=mean, var=var)

    @property
    def expected_rounds(self) -> float:
        return float(np.dot(self.probs, self.rounds) / self.probs.sum())


def kills_mixture(
    rate: PlayerRate,
    total_rounds: npt.ArrayLike | Mapping[int, float],
    *,
    margin: npt.ArrayLike | None = None,
    weights: npt.ArrayLike | None = None,
    margin_center: float | None = None,
    score_skew: float = 0.0,
    hist_margin_var: float = 0.12,
) -> KillsMixture:
    """Build the NB mixture over the rounds distribution (and optionally the score margin).

    ``total_rounds``: a ``{rounds: p}`` dict or an array of per-simulation total rounds;
    ``margin``: per-simulation own-minus-opponent rounds (enables the score skew);
    ``weights``: per-simulation weights; ``margin_center``: E[margin] over *all* simulations of the
    map (defaults to the weighted mean of ``margin``).
    """
    skew = score_skew if margin is not None else 0.0
    if isinstance(total_rounds, Mapping):
        items = sorted((int(k), float(v)) for k, v in total_rounds.items() if float(v) > 0)
        if not items:
            raise ValueError("empty rounds distribution")
        r_u = np.asarray([k for k, _ in items], dtype=np.float64)
        probs = np.asarray([v for _, v in items], dtype=np.float64)
        d_u = np.zeros_like(r_u)
        skew = 0.0
    else:
        r = np.asarray(total_rounds, dtype=np.int64).reshape(-1)
        if r.size == 0:
            raise ValueError("empty rounds array")
        w = None if weights is None else np.asarray(weights, dtype=np.float64).reshape(-1)
        dm = np.zeros_like(r) if margin is None else np.asarray(margin, dtype=np.int64).reshape(-1)
        uniq, inv = np.unique(r * 4096 + (dm + 2048), return_inverse=True)
        probs = np.bincount(inv, weights=w, minlength=uniq.size).astype(np.float64)
        r_u = (uniq // 4096).astype(np.float64)
        d_u = ((uniq % 4096) - 2048).astype(np.float64)
    probs = probs / probs.sum()
    center = float(np.dot(probs, d_u)) if margin_center is None else float(margin_center)
    mult = np.maximum(r_u + skew * (d_u - center), 0.1 * r_u) if skew > 0.0 else r_u
    mu = np.maximum(rate.kpr * mult, 1e-6)
    s_eff = effective_size(rate.size, rate.cv2, skew, hist_margin_var if skew > 0 else 0.0)
    k_max = nb_support(float(mu.max()), s_eff)
    return KillsMixture(probs=probs, rounds=r_u, margins=d_u, matrix=nb_pmf_matrix(mu, s_eff, k_max))


def kills_distribution(
    rate: PlayerRate,
    total_rounds: npt.ArrayLike | Mapping[int, float],
    *,
    margin: npt.ArrayLike | None = None,
    weights: npt.ArrayLike | None = None,
    margin_center: float | None = None,
    score_skew: float = 0.0,
    hist_margin_var: float = 0.12,
) -> KillsDistribution:
    """Kill pmf as a NB mixture over the rounds distribution (see :func:`kills_mixture`)."""
    return kills_mixture(
        rate,
        total_rounds,
        margin=margin,
        weights=weights,
        margin_center=margin_center,
        score_skew=score_skew,
        hist_margin_var=hist_margin_var,
    ).distribution()


def sample_kills(
    rate: PlayerRate,
    own_rounds: npt.ArrayLike,
    opp_rounds: npt.ArrayLike,
    rng: np.random.Generator,
    *,
    margin_center: float = 0.0,
    score_skew: float = 0.0,
    hist_margin_var: float = 0.12,
) -> IntArray:
    """One kill draw per simulated map (Gamma-Poisson = NB), consistent with :func:`kills_distribution`."""
    own = np.asarray(own_rounds, dtype=np.float64)
    opp = np.asarray(opp_rounds, dtype=np.float64)
    r = own + opp
    mult = np.maximum(r + score_skew * ((own - opp) - margin_center), 0.1 * r) if score_skew > 0.0 else r
    mu = np.maximum(rate.kpr * mult, 1e-9)
    s_eff = effective_size(rate.size, rate.cv2, score_skew, hist_margin_var if score_skew > 0 else 0.0)
    lam = rng.gamma(shape=s_eff, scale=mu / s_eff)
    return rng.poisson(lam).astype(np.int64)


def top_fragger_probs(
    kills: npt.ArrayLike,
    player_ids: Sequence[int],
    *,
    row_mask: npt.ArrayLike | None = None,
    row_weights: npt.ArrayLike | None = None,
) -> dict[str, float]:
    """P(each player has the most kills) from joint samples ``kills[sample, player]``.

    Ties are split evenly (dead-heat rule), so the probabilities sum to 1. ``row_mask`` restricts
    to samples where the scope was played (e.g. map 3 of a BO3).
    """
    k = np.asarray(kills)
    if k.ndim != 2 or k.shape[1] != len(player_ids):
        raise ValueError("kills must have shape (n_samples, n_players)")
    w = None if row_weights is None else np.asarray(row_weights, dtype=np.float64)
    if row_mask is not None:
        mask = np.asarray(row_mask, dtype=bool)
        k = k[mask]
        w = None if w is None else w[mask]
    if k.shape[0] == 0 or (w is not None and w.sum() <= 0):
        u = 1.0 / max(len(player_ids), 1)
        return {str(p): u for p in player_ids}
    mx = k.max(axis=1, keepdims=True)
    ties = (k == mx).astype(np.float64)
    share = ties / ties.sum(axis=1, keepdims=True)
    probs = share.mean(axis=0) if w is None else (w[:, None] * share).sum(axis=0) / w.sum()
    return {str(p): float(x) for p, x in zip(player_ids, probs, strict=True)}


@dataclass(frozen=True)
class _Estimates:
    g: float
    gd: float
    map_factor: FloatArray
    opp_factor: FloatArray
    team_dpr: FloatArray
    lam: FloatArray
    lam_var: FloatArray
    known: npt.NDArray[np.bool_]
    role: npt.NDArray[np.int16]
    dpr: FloatArray
    role_mu: dict[int, float]
    role_tau2: dict[int, float]
    role_size: dict[int, float]
    global_mu: float
    global_tau2: float
    global_size: float
    n_records: int


class PlayerKillModel:
    """Empirical-Bayes player kill model (see module docstring). Fit once, project as of any date."""

    VERSION = 1

    def __init__(self, config: KillModelConfig | None = None) -> None:
        self.config = config or KillModelConfig()
        self.half_life_days = 120.0
        self._player_ids: list[int] = []
        self._team_ids: list[int] = []
        self._maps: list[str] = []
        self._pid: dict[int, int] = {}
        self._tid: dict[int, int] = {}
        self._mid: dict[str, int] = {}
        self._cache: OrderedDict[float, _Estimates] = OrderedDict()
        self._t: FloatArray = np.zeros(0)
        self._p: IntArray = np.zeros(0, dtype=np.int64)
        self._team: IntArray = np.zeros(0, dtype=np.int64)
        self._opp: IntArray = np.zeros(0, dtype=np.int64)
        self._map: IntArray = np.zeros(0, dtype=np.int64)
        self._kills: FloatArray = np.zeros(0)
        self._deaths: FloatArray = np.zeros(0)
        self._rounds: FloatArray = np.zeros(0)
        self._role: npt.NDArray[np.int16] = np.zeros(0, dtype=np.int16)

    def _set_arrays(self, arr: FloatArray) -> None:
        """Columns: t, player idx, team idx, opponent idx, map idx, kills, deaths, rounds, role code."""
        if arr.size == 0:
            arr = np.zeros((0, 9))
        self._t = arr[:, 0].copy()
        self._p = arr[:, 1].astype(np.int64)
        self._team = arr[:, 2].astype(np.int64)
        self._opp = arr[:, 3].astype(np.int64)
        self._map = arr[:, 4].astype(np.int64)
        self._kills = arr[:, 5].copy()
        self._deaths = arr[:, 6].copy()
        self._rounds = arr[:, 7].copy()
        self._role = arr[:, 8].astype(np.int16)

    # ------------------------------------------------------------------ fitting
    def fit(self, records: Iterable[PlayerMapRecord], *, half_life_days: float = 120.0) -> PlayerKillModel:
        """Store the records (compact arrays). Estimates are computed lazily per ``as_of``."""
        if half_life_days <= 0:
            raise ValueError("half_life_days must be positive")
        self.half_life_days = float(half_life_days)
        pid: dict[int, int] = {}
        tid: dict[int, int] = {}
        mid: dict[str, int] = {}
        cols: list[tuple[float, int, int, int, int, float, float, float, int]] = []
        for rec in records:
            if rec.rounds_played <= 0:
                continue
            t = ensure_utc(rec.played_at).timestamp() / 86400.0
            role = rec.role
            code = ROLE_CODE[role] if role is not None and role is not PlayerRole.UNKNOWN else NO_ROLE
            cols.append(
                (
                    t,
                    pid.setdefault(rec.player_id, len(pid)),
                    tid.setdefault(rec.team_id, len(tid)),
                    tid.setdefault(rec.opponent_team_id, len(tid)),
                    mid.setdefault(normalize_map_name(rec.map_name), len(mid)),
                    float(rec.kills),
                    float(rec.deaths),
                    float(rec.rounds_played),
                    code,
                )
            )
        self._pid, self._tid, self._mid = pid, tid, mid
        self._player_ids = sorted(pid, key=lambda k: pid[k])
        self._team_ids = sorted(tid, key=lambda k: tid[k])
        self._maps = sorted(mid, key=lambda k: mid[k])
        self._set_arrays(np.asarray(cols, dtype=np.float64).reshape(-1, 9))
        self._cache.clear()
        return self

    @property
    def n_records(self) -> int:
        return int(self._t.size)

    def knows_player(self, player_id: int) -> bool:
        return player_id in self._pid

    # ------------------------------------------------------------------ estimation (as of)
    def _cutoff(self, as_of: datetime) -> float:
        t = ensure_utc(as_of).timestamp() / 86400.0
        return math.floor(t * 1440.0) / 1440.0  # minute resolution, rounded DOWN (never later)

    def _estimates(self, as_of: datetime) -> _Estimates:
        key = self._cutoff(as_of)
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            return hit
        est = self._compute(key)
        self._cache[key] = est
        while len(self._cache) > self.config.cache_size:
            self._cache.popitem(last=False)
        return est

    def _default_estimates(self) -> _Estimates:
        cfg = self.config
        n_p, n_t, n_m = len(self._player_ids), len(self._team_ids), len(self._maps)
        role_mu = {ROLE_CODE[r]: v for r, v in DEFAULT_ROLE_KPR.items()}
        tau2 = cfg.default_tau**2
        return _Estimates(
            g=cfg.default_kpr,
            gd=cfg.default_kpr,
            map_factor=np.ones(n_m),
            opp_factor=np.ones(n_t),
            team_dpr=np.full(n_t, cfg.default_kpr),
            lam=np.full(n_p, cfg.default_kpr),
            lam_var=np.full(n_p, tau2),
            known=np.zeros(n_p, dtype=bool),
            role=np.full(n_p, NO_ROLE, dtype=np.int16),
            dpr=np.full(n_p, cfg.default_kpr),
            role_mu=role_mu,
            role_tau2=dict.fromkeys(role_mu, tau2),
            role_size=dict.fromkeys(role_mu, cfg.default_size),
            global_mu=cfg.default_kpr,
            global_tau2=tau2,
            global_size=cfg.default_size,
            n_records=0,
        )

    def _compute(self, cutoff: float) -> _Estimates:
        cfg = self.config
        sel = np.flatnonzero(self._t < cutoff)
        if sel.size == 0:
            return self._default_estimates()
        n_p, n_t, n_m = len(self._player_ids), len(self._team_ids), len(self._maps)
        t = self._t[sel]
        p = self._p[sel]
        team = self._team[sel]
        opp = self._opp[sel]
        mp = self._map[sel]
        k = self._kills[sel]
        dth = self._deaths[sel]
        r = self._rounds[sel]
        w = np.power(0.5, (cutoff - t) / self.half_life_days)
        wr = w * r
        wk = w * k
        g = float(wk.sum() / wr.sum())
        gd = float((w * dth).sum() / wr.sum())

        # --- multiplicative Poisson model by iterative proportional fitting (map / opponent / player)
        a = np.ones(n_p)
        mf = np.ones(n_m)
        of = np.ones(n_t)
        obs_m = np.bincount(mp, weights=wk, minlength=n_m)
        obs_o = np.bincount(opp, weights=wk, minlength=n_t)
        obs_p = np.bincount(p, weights=wk, minlength=n_p)
        expo_m = np.bincount(mp, weights=wr, minlength=n_m)
        expo_o = np.bincount(opp, weights=wr, minlength=n_t)
        for _ in range(cfg.ipf_iterations):
            e_m = np.bincount(mp, weights=wr * g * a[p] * of[opp], minlength=n_m)
            mf = (obs_m + cfg.map_prior_rounds * g) / (e_m + cfg.map_prior_rounds * g)
            mf /= float(np.dot(expo_m, mf) / expo_m.sum())
            e_o = np.bincount(opp, weights=wr * g * a[p] * mf[mp], minlength=n_t)
            of = (obs_o + cfg.opp_prior_rounds * g) / (e_o + cfg.opp_prior_rounds * g)
            of /= float(np.dot(expo_o, of) / expo_o.sum())
            e_p = np.bincount(p, weights=wr * g * mf[mp] * of[opp], minlength=n_p)
            a = (obs_p + cfg.ipf_player_prior_rounds * g) / (e_p + cfg.ipf_player_prior_rounds * g)

        adj = mf[mp] * of[opp]
        expo_p = np.bincount(p, weights=wr * adj, minlength=n_p)  # neutral player-rounds
        known = expo_p > 0

        # --- latest known role per player (as of the cut-off)
        role_p = np.full(n_p, NO_ROLE, dtype=np.int16)
        has_role = self._role[sel] >= 0
        if has_role.any():
            order = np.argsort(t[has_role], kind="stable")
            role_p[p[has_role][order]] = self._role[sel][has_role][order]

        role_size: dict[int, float] = {}
        global_size = cfg.default_size
        lam = np.full(n_p, g)
        lam_var = np.full(n_p, cfg.default_tau**2)
        role_mu: dict[int, float] = {}
        role_tau2: dict[int, float] = {}
        global_mu, global_tau2 = g, cfg.default_tau**2
        for _pass in range(2):
            size_p = np.array([role_size.get(int(c), global_size) for c in role_p]) if n_p else np.zeros(0)
            prior_mu_p = np.array([role_mu.get(int(c), global_mu) for c in role_p]) if n_p else np.zeros(0)
            phi = 1.0 + prior_mu_p[p] * r * adj / size_p[p]
            num = np.bincount(p, weights=w * r * adj, minlength=n_p)
            den = np.bincount(p, weights=w * w * r * adj * phi, minlength=n_p)
            c = np.where(den > 0, num / np.where(den > 0, den, 1.0), 0.0)
            # global prior
            global_mu, g_tau2 = self._group_prior(obs_p, expo_p, c, known)
            global_tau2 = g_tau2 if g_tau2 is not None else cfg.default_tau**2
            role_mu, role_tau2 = {}, {}
            for code in range(len(ROLE_ORDER)):
                members = known & (role_p == code)
                n_mem = int(members.sum())
                if n_mem == 0:
                    continue
                mu_r, tau2_r = self._group_prior(obs_p, expo_p, c, members)
                n0 = cfg.role_prior_pseudo_players
                role_mu[code] = (n_mem * mu_r + n0 * global_mu) / (n_mem + n0)
                role_tau2[code] = tau2_r if tau2_r is not None else global_tau2
            mu_p = np.array([role_mu.get(int(cd), global_mu) for cd in role_p]) if n_p else np.zeros(0)
            tau2_p = np.array([role_tau2.get(int(cd), global_tau2) for cd in role_p]) if n_p else np.zeros(0)
            alpha = mu_p * mu_p / tau2_p
            beta = mu_p / tau2_p
            lam = (alpha + c * obs_p) / (beta + c * expo_p)
            lam_var = lam / (beta + c * expo_p)
            # NB size per role (method of moments on per-map residuals)
            mu_hat = lam[p] * r * adj
            resid = (k - mu_hat) ** 2 - mu_hat
            global_size = self._size_mom(w, resid, mu_hat, np.ones(sel.size, dtype=bool))
            role_size = {}
            rec_role = role_p[p]
            for code in range(len(ROLE_ORDER)):
                mask = rec_role == code
                if int(mask.sum()) >= cfg.min_records_for_size:
                    role_size[code] = self._size_mom(w, resid, mu_hat, mask)

        dpr_num = np.bincount(p, weights=w * dth, minlength=n_p)
        dpr_den = np.bincount(p, weights=wr, minlength=n_p)
        dpr = (dpr_num + cfg.deaths_prior_rounds * gd) / (dpr_den + cfg.deaths_prior_rounds)
        tdpr_num = np.bincount(team, weights=w * dth, minlength=n_t)
        tdpr_den = np.bincount(team, weights=wr, minlength=n_t)
        team_dpr = (tdpr_num + cfg.deaths_prior_rounds * gd) / (tdpr_den + cfg.deaths_prior_rounds)
        return _Estimates(
            g=g,
            gd=gd,
            map_factor=mf,
            opp_factor=of,
            team_dpr=team_dpr,
            lam=lam,
            lam_var=lam_var,
            known=known,
            role=role_p,
            dpr=dpr,
            role_mu=role_mu,
            role_tau2=role_tau2,
            role_size=role_size,
            global_mu=float(global_mu),
            global_tau2=float(global_tau2),
            global_size=float(global_size),
            n_records=int(sel.size),
        )

    def _group_prior(
        self, kills: FloatArray, expo: FloatArray, c: FloatArray, members: npt.NDArray[np.bool_]
    ) -> tuple[float, float | None]:
        """Pooled mean and method-of-moments between-player variance of KPR for a player group."""
        cfg = self.config
        e_sum = float(expo[members].sum())
        if e_sum <= 0:
            return cfg.default_kpr, None
        mu = float(kills[members].sum() / e_sum)
        eff = c[members] * expo[members]
        good = eff >= cfg.min_effective_rounds_for_tau
        if int(good.sum()) < cfg.min_players_for_tau:
            return mu, None
        x = kills[members][good] / expo[members][good]
        v = mu / eff[good]
        tau2 = float(np.mean((x - mu) ** 2) - np.mean(v))
        return mu, max(tau2, cfg.min_tau2)

    def _size_mom(
        self, w: FloatArray, resid: FloatArray, mu_hat: FloatArray, mask: npt.NDArray[np.bool_]
    ) -> float:
        cfg = self.config
        den = float((w[mask] * mu_hat[mask] ** 2).sum())
        if den <= 0 or int(mask.sum()) < cfg.min_records_for_size:
            return cfg.default_size
        inv = float((w[mask] * resid[mask]).sum() / den)
        inv = min(max(inv, 1.0 / cfg.max_size), 1.0 / cfg.min_size)
        return 1.0 / inv

    # ------------------------------------------------------------------ rates & projections
    def rate(
        self,
        player_id: int,
        map_name: str,
        opponent_team_id: int | None,
        *,
        as_of: datetime,
        role: PlayerRole | None = None,
        team: TeamSlot | None = None,
    ) -> PlayerRate:
        """Kill-rate parameters of a player for one map / opponent as of ``as_of``.

        Known players use their posterior; unknown players (or players with no records before
        ``as_of``) get the prior of ``role`` (or the global prior) with inflated uncertainty.
        """
        cfg = self.config
        est = self._estimates(as_of)
        m = normalize_map_name(map_name)
        mi = self._mid.get(m)
        mfac = float(est.map_factor[mi]) if mi is not None else 1.0
        oi = self._tid.get(opponent_team_id) if opponent_team_id is not None else None
        ofac = float(est.opp_factor[oi]) if oi is not None else 1.0
        pi = self._pid.get(player_id)
        given = ROLE_CODE[role] if role is not None and role is not PlayerRole.UNKNOWN else NO_ROLE
        if pi is not None and bool(est.known[pi]):
            code = int(est.role[pi])
            if code == NO_ROLE:
                code = given
            base = float(est.lam[pi])
            cv2 = float(est.lam_var[pi]) / max(base * base, 1e-12)
            dpr = float(est.dpr[pi])
            known = True
        else:
            code = given
            base = est.role_mu.get(code, est.global_mu)
            tau2 = est.role_tau2.get(code, est.global_tau2)
            cv2 = tau2 / max(base * base, 1e-12) * cfg.unknown_player_cv_mult**2
            dpr = est.gd
            known = False
        size = est.role_size.get(code, est.global_size)
        return PlayerRate(
            player_id=player_id,
            team=team,
            map_name=m,
            kpr=base * mfac * ofac,
            base_kpr=base,
            cv2=cv2,
            size=size,
            dpr=dpr,
            known=known,
            role=ROLE_ORDER[code] if code >= 0 else None,
            map_factor=mfac,
            opp_factor=ofac,
        )

    def team_scales(
        self,
        players: Sequence[PlayerInput],
        *,
        team1_id: int,
        team2_id: int,
        as_of: datetime,
    ) -> tuple[float, float]:
        """Team normalisation factors (team1, team2); 1.0 when a lineup is too incomplete.

        Team A's kill rates are scaled by ``sqrt(mean DPR of B's actual lineup / B's historical team
        DPR)``: kills by A must equal deaths of B, and B's lineup-level death rate is better evidence
        than B's team history when the lineup changed (stand-ins). Bounded by
        ``normalisation_bounds``.
        """
        cfg = self.config
        est = self._estimates(as_of)
        lo, hi = cfg.normalisation_bounds

        def lineup_ratio(slot: TeamSlot, team_id: int) -> float:
            members = [pl for pl in players if pl.team is slot]
            if len(members) < cfg.min_lineup_for_normalisation:
                return 1.0
            dprs = []
            for pl in members:
                pi = self._pid.get(pl.player_id)
                dprs.append(float(est.dpr[pi]) if pi is not None and bool(est.known[pi]) else est.gd)
            ti = self._tid.get(team_id)
            hist = float(est.team_dpr[ti]) if ti is not None else est.gd
            return float(np.mean(dprs)) / max(hist, 1e-9)

        s1 = math.sqrt(max(lineup_ratio(TeamSlot.TEAM2, team2_id), 1e-9))
        s2 = math.sqrt(max(lineup_ratio(TeamSlot.TEAM1, team1_id), 1e-9))
        return min(max(s1, lo), hi), min(max(s2, lo), hi)

    def lineup_rates(
        self,
        players: Sequence[PlayerInput],
        *,
        map_name: str,
        team1_id: int,
        team2_id: int,
        as_of: datetime,
        normalise: bool | None = None,
    ) -> dict[int, PlayerRate]:
        """Rates of every listed player for one map, with optional team normalisation."""
        rates: dict[int, PlayerRate] = {}
        for pl in players:
            opp_id = team2_id if pl.team is TeamSlot.TEAM1 else team1_id
            rates[pl.player_id] = self.rate(
                pl.player_id, map_name, opp_id, as_of=as_of, role=pl.role, team=pl.team
            )
        do_norm = self.config.normalise_teams if normalise is None else normalise
        if do_norm and players:
            s1, s2 = self.team_scales(players, team1_id=team1_id, team2_id=team2_id, as_of=as_of)
            for pl in players:
                rates[pl.player_id] = rates[pl.player_id].scaled(s1 if pl.team is TeamSlot.TEAM1 else s2)
        return rates

    def project(
        self,
        player_id: int,
        team: TeamSlot,
        map_name: str,
        opponent_team_id: int | None,
        rounds_pmf: Mapping[int, float] | npt.ArrayLike,
        *,
        as_of: datetime,
        nickname: str | None = None,
        role: PlayerRole | None = None,
        team_rounds: tuple[npt.ArrayLike, npt.ArrayLike] | None = None,
        weights: npt.ArrayLike | None = None,
        kpr_scale: float = 1.0,
    ) -> PlayerProjection:
        """Kill projection for one player on one map.

        ``rounds_pmf``: ``{total rounds: p}`` or an array of simulated total rounds. If
        ``team_rounds = (own rounds per sim, opponent rounds per sim)`` is given (same length as the
        rounds array), the score skew is applied. ``kpr_scale`` applies an external normalisation.
        """
        rate = self.rate(player_id, map_name, opponent_team_id, as_of=as_of, role=role, team=team)
        if kpr_scale != 1.0:
            rate = rate.scaled(kpr_scale)
        margin = None
        total: Mapping[int, float] | npt.ArrayLike = rounds_pmf
        if team_rounds is not None and not isinstance(rounds_pmf, Mapping):
            own = np.asarray(team_rounds[0], dtype=np.int64)
            opp = np.asarray(team_rounds[1], dtype=np.int64)
            margin = own - opp
            total = own + opp
        dist = kills_distribution(
            rate,
            total,
            margin=margin,
            weights=None if isinstance(rounds_pmf, Mapping) else weights,
            score_skew=self.config.score_skew,
            hist_margin_var=self.config.hist_margin_var,
        )
        exp_rounds = _expected_rounds(rounds_pmf, weights)
        return PlayerProjection(
            player_id=player_id,
            nickname=nickname,
            team=team,
            kills_pmf=pmf_to_dict(dist.pmf),
            kills_mean=dist.mean,
            kills_var=dist.var,
            deaths_mean=rate.dpr * exp_rounds,
        )

    def top_fragger_probs(
        self,
        players: Sequence[PlayerInput],
        map_name: str,
        team1_id: int,
        team2_id: int,
        team1_rounds: npt.ArrayLike,
        team2_rounds: npt.ArrayLike,
        *,
        as_of: datetime,
        rng: np.random.Generator | int | None = None,
        weights: npt.ArrayLike | None = None,
        n_samples: int | None = None,
        normalise: bool | None = None,
    ) -> dict[str, float]:
        """P(top fragger) on one map by joint simulation over the simulated scorelines (ties split)."""
        gen = rng if isinstance(rng, np.random.Generator) else np.random.default_rng(rng)
        rates = self.lineup_rates(
            players, map_name=map_name, team1_id=team1_id, team2_id=team2_id, as_of=as_of, normalise=normalise
        )
        t1 = np.asarray(team1_rounds, dtype=np.int64)
        t2 = np.asarray(team2_rounds, dtype=np.int64)
        w = None if weights is None else np.asarray(weights, dtype=np.float64)
        n = int(n_samples or t1.size)
        if w is not None or n != t1.size:
            prob = None if w is None else w / w.sum()
            idx = gen.choice(t1.size, size=n, p=prob)
        else:
            idx = np.arange(t1.size)
        center1 = float(np.average(t1 - t2, weights=w))
        cols = []
        for pl in players:
            own, opp = (t1[idx], t2[idx]) if pl.team is TeamSlot.TEAM1 else (t2[idx], t1[idx])
            center = center1 if pl.team is TeamSlot.TEAM1 else -center1
            cols.append(
                sample_kills(
                    rates[pl.player_id],
                    own,
                    opp,
                    gen,
                    margin_center=center,
                    score_skew=self.config.score_skew,
                    hist_margin_var=self.config.hist_margin_var,
                )
            )
        return top_fragger_probs(np.stack(cols, axis=1), [pl.player_id for pl in players])

    # ------------------------------------------------------------------ inspection
    def summary(self, as_of: datetime) -> dict[str, Any]:
        """Population-level estimates as of a date (for diagnostics / the dashboard)."""
        est = self._estimates(as_of)
        return {
            "n_records": est.n_records,
            "global_kpr": est.g,
            "global_dpr": est.gd,
            "global_tau": math.sqrt(est.global_tau2),
            "global_size": est.global_size,
            "role_kpr": {ROLE_ORDER[c].value: v for c, v in est.role_mu.items()},
            "role_size": {ROLE_ORDER[c].value: v for c, v in est.role_size.items()},
            "map_factor": {m: float(est.map_factor[i]) for m, i in self._mid.items()},
        }

    # ------------------------------------------------------------------ persistence
    def save(self, path: str | Path) -> Path:
        """Save to a compressed ``.npz`` (no pickle). Returns the path written."""
        p = Path(path)
        if p.suffix != ".npz":
            p = p.with_name(p.name + ".npz")
        p.parent.mkdir(parents=True, exist_ok=True)
        cfg = asdict(self.config)
        meta = {
            "kind": "cs2bot.PlayerKillModel",
            "version": self.VERSION,
            "half_life_days": self.half_life_days,
            "player_ids": self._player_ids,
            "team_ids": self._team_ids,
            "maps": self._maps,
            "config": cfg,
        }
        np.savez_compressed(
            p,
            meta=np.asarray(json.dumps(meta)),
            t=self._t,
            p=self._p,
            team=self._team,
            opp=self._opp,
            map=self._map,
            kills=self._kills,
            deaths=self._deaths,
            rounds=self._rounds,
            role=self._role,
        )
        return p

    @classmethod
    def load(cls, path: str | Path) -> PlayerKillModel:
        p = Path(path)
        if not p.exists() and p.suffix != ".npz":
            p = p.with_name(p.name + ".npz")
        with np.load(p, allow_pickle=False) as z:
            meta = json.loads(str(z["meta"]))
            if meta.get("kind") != "cs2bot.PlayerKillModel":
                raise ValueError("not a PlayerKillModel file")
            cfg_raw = dict(meta.get("config", {}))
            if "normalisation_bounds" in cfg_raw:
                cfg_raw["normalisation_bounds"] = tuple(cfg_raw["normalisation_bounds"])
            model = cls(KillModelConfig(**cfg_raw))
            model.half_life_days = float(meta["half_life_days"])
            model._player_ids = [int(x) for x in meta["player_ids"]]
            model._team_ids = [int(x) for x in meta["team_ids"]]
            model._maps = [str(x) for x in meta["maps"]]
            model._pid = {v: i for i, v in enumerate(model._player_ids)}
            model._tid = {v: i for i, v in enumerate(model._team_ids)}
            model._mid = {v: i for i, v in enumerate(model._maps)}
            model._t = np.asarray(z["t"], dtype=np.float64)
            model._p = np.asarray(z["p"], dtype=np.int64)
            model._team = np.asarray(z["team"], dtype=np.int64)
            model._opp = np.asarray(z["opp"], dtype=np.int64)
            model._map = np.asarray(z["map"], dtype=np.int64)
            model._kills = np.asarray(z["kills"], dtype=np.float64)
            model._deaths = np.asarray(z["deaths"], dtype=np.float64)
            model._rounds = np.asarray(z["rounds"], dtype=np.float64)
            model._role = np.asarray(z["role"], dtype=np.int16)
        return model


def _expected_rounds(rounds_pmf: Mapping[int, float] | npt.ArrayLike, weights: npt.ArrayLike | None) -> float:
    if isinstance(rounds_pmf, Mapping):
        total = sum(float(v) for v in rounds_pmf.values())
        return sum(int(k) * float(v) for k, v in rounds_pmf.items()) / total if total > 0 else 0.0
    arr = np.asarray(rounds_pmf, dtype=np.float64)
    if weights is None:
        return float(arr.mean())
    return float(np.average(arr, weights=np.asarray(weights, dtype=np.float64)))


__all__ = [
    "DEFAULT_ROLE_KPR",
    "ROLE_ORDER",
    "KillModelConfig",
    "KillsDistribution",
    "KillsMixture",
    "PlayerInput",
    "PlayerKillModel",
    "PlayerRate",
    "effective_size",
    "kills_distribution",
    "kills_mixture",
    "sample_kills",
    "top_fragger_probs",
]
