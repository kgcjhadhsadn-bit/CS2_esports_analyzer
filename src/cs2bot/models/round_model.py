"""Round-level win probability model.

For team A against team B in one round::

    logit P(A wins) = d + s_A * (ct_bias[map] + pistol * pistol_ct_adj) + E[buy_A, buy_B]

* ``d`` - strength difference A minus B in *round-logit units* (the map simulator calibrates it so
  the simulated map-win probability matches the rating/ensemble estimate);
* ``s_A`` = +1 if A plays CT, -1 if T; ``ct_bias[map]`` is the logit of the CT round-win rate for
  equal teams and equal buys (e.g. 0.20 = 55 % CT);
* ``pistol`` = 1 on pistol rounds (both teams on pistol-round buys), ``pistol_ct_adj`` lets pistol
  rounds have a different side bias than gun rounds;
* ``E`` is an antisymmetric buy-matchup table over (PISTOL, ECO, FORCE, FULL): ``E[a, b] = -E[b, a]``,
  ``E[a, a] = 0``. Six free parameters ``beta_ij = E[j, i]`` (i < j in code order).

Fitting (:meth:`RoundModel.fit`) is penalised maximum likelihood (= MAP with Gaussian priors) with a
nuisance strength parameter ``delta`` per (match, map) instance, so that the side/buy effects are
estimated *within* map instances. This removes the confounding that stronger teams both win more
rounds and full-buy more often. Priors encode public knowledge and dominate when data is thin:

* CT round-win rates by map in CS2 pro play (2024-25 HLTV / bo3.gg / dust2.us snapshots): Nuke
  ~56-58 % CT, Ancient ~54 %, Train ~54-56 %, Overpass ~54 %, Mirage ~52 %, Inferno ~51 %,
  Dust2 ~50 %, Anubis the most T-sided map (~45 % CT, HLTV: 55.5 % T over 12 months).
* Buy matchups: full buy vs eco ~88 % (logit 2.0), full vs force ~70 % (0.85), force vs eco ~75 %
  (1.1); pistol rounds ~50/50 apart from the side bias.

Recent rounds can be up-weighted with an exponential half-life (side balance shifts with patches).
Only rounds that happened strictly before ``as_of`` are used when ``as_of`` is given.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from scipy import optimize, sparse, special

from cs2bot.domain.enums import BuyType, Side, TeamSlot
from cs2bot.domain.records import RoundRecord
from cs2bot.models.economy import BUY_CODE, BUY_ORDER, PISTOL_CODE
from cs2bot.models.sim_stats import logit
from cs2bot.utils.names import normalize_map_name
from cs2bot.utils.time import ensure_utc

FloatArray = npt.NDArray[np.float64]

#: Prior CT-side logits for equal teams and buys (see module docstring for sources).
DEFAULT_CT_BIAS_PRIOR: dict[str, float] = {
    "nuke": round(logit(0.565), 3),
    "ancient": round(logit(0.540), 3),
    "train": round(logit(0.545), 3),
    "overpass": round(logit(0.540), 3),
    "cache": round(logit(0.535), 3),
    "mirage": round(logit(0.520), 3),
    "inferno": round(logit(0.510), 3),
    "vertigo": round(logit(0.510), 3),
    "dust2": round(logit(0.505), 3),
    "anubis": round(logit(0.450), 3),
}

#: Buy pairs (i, j) with i < j in code order; parameter beta_ij = E[j, i] (advantage of buy j over i).
BUY_PAIRS: tuple[tuple[int, int], ...] = tuple(
    (i, j) for i in range(len(BUY_ORDER)) for j in range(i + 1, len(BUY_ORDER))
)
#: Prior means of beta_ij in BUY_PAIRS order: (P,E) (P,Fo) (P,Fu) (E,Fo) (E,Fu) (Fo,Fu).
DEFAULT_BUY_PAIR_PRIOR: tuple[float, ...] = (0.0, 1.1, 2.0, 1.1, 2.0, 0.85)


@dataclass(frozen=True)
class RoundModelPriors:
    """Gaussian priors (mean, sd) of the round model parameters."""

    ct_bias_mean: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_CT_BIAS_PRIOR))
    ct_bias_default: float = 0.05  # unknown maps: slight CT edge (most CS2 maps are CT-sided)
    ct_bias_sd: float = 0.12
    pistol_ct_adj_mean: float = 0.0
    pistol_ct_adj_sd: float = 0.20
    buy_pair_mean: tuple[float, ...] = DEFAULT_BUY_PAIR_PRIOR
    buy_pair_sd: float = 0.35
    strength_sd: float = 0.60  # per (match, map) nuisance strength difference

    def ct_prior(self, map_name: str) -> float:
        return float(self.ct_bias_mean.get(normalize_map_name(map_name), self.ct_bias_default))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ct_bias_mean"] = dict(self.ct_bias_mean)
        d["buy_pair_mean"] = list(self.buy_pair_mean)
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> RoundModelPriors:
        return cls(
            ct_bias_mean={str(k): float(v) for k, v in dict(d.get("ct_bias_mean", {})).items()},
            ct_bias_default=float(d.get("ct_bias_default", 0.05)),
            ct_bias_sd=float(d.get("ct_bias_sd", 0.12)),
            pistol_ct_adj_mean=float(d.get("pistol_ct_adj_mean", 0.0)),
            pistol_ct_adj_sd=float(d.get("pistol_ct_adj_sd", 0.20)),
            buy_pair_mean=tuple(float(x) for x in d.get("buy_pair_mean", DEFAULT_BUY_PAIR_PRIOR)),
            buy_pair_sd=float(d.get("buy_pair_sd", 0.35)),
            strength_sd=float(d.get("strength_sd", 0.60)),
        )


def classify_buy(equipment_value: int | None, round_number: int, rounds_per_half: int = 12) -> BuyType | None:
    """Fallback buy classification from a *team* equipment value (sum over 5 players).

    Pistol rounds are identified by round number (1 and ``rounds_per_half + 1``). Per-player
    thresholds: < $1,500 eco, < $3,800 force, otherwise full (common community definitions:
    team value < $7.5k eco, $7.5k-$19k force/half-buy, >= $19k full buy).
    """
    if round_number in (1, rounds_per_half + 1):
        return BuyType.PISTOL
    if equipment_value is None:
        return None
    per_player = equipment_value / 5.0
    if per_player < 1500:
        return BuyType.ECO
    if per_player < 3800:
        return BuyType.FORCE
    return BuyType.FULL


def _pairs_to_table(pairs: Sequence[float] | FloatArray) -> FloatArray:
    n = len(BUY_ORDER)
    table = np.zeros((n, n), dtype=np.float64)
    for (i, j), beta in zip(BUY_PAIRS, pairs, strict=True):
        table[j, i] = beta
        table[i, j] = -beta
    return table


class RoundModel:
    """P(team A wins a round | side, buy matchup, map, strength difference). See module docstring.

    Treat a fitted model as immutable: its :meth:`fingerprint` (part of every simulation cache key)
    is cached and only reset by :meth:`fit`; build a new instance instead of editing attributes.
    """

    VERSION = 1

    def __init__(
        self,
        *,
        ct_bias: Mapping[str, float] | None = None,
        pistol_ct_adj: float | None = None,
        buy_pairs: Sequence[float] | None = None,
        priors: RoundModelPriors | None = None,
    ) -> None:
        self.priors = priors or RoundModelPriors()
        source = self.priors.ct_bias_mean if ct_bias is None else ct_bias
        self.ct_bias: dict[str, float] = {normalize_map_name(k): float(v) for k, v in source.items()}
        self.pistol_ct_adj = float(self.priors.pistol_ct_adj_mean if pistol_ct_adj is None else pistol_ct_adj)
        pairs = self.priors.buy_pair_mean if buy_pairs is None else tuple(buy_pairs)
        if len(pairs) != len(BUY_PAIRS):
            raise ValueError(f"buy_pairs needs {len(BUY_PAIRS)} values")
        self.buy_pairs: FloatArray = np.asarray(pairs, dtype=np.float64)
        self.fit_stats: dict[str, Any] = {}
        self._fingerprint: str | None = None

    # ------------------------------------------------------------------ construction
    @classmethod
    def default(cls) -> RoundModel:
        """A model with prior (public-knowledge) parameters only."""
        return cls()

    # ------------------------------------------------------------------ parameters
    @property
    def buy_table(self) -> FloatArray:
        """4x4 antisymmetric matrix ``E[a, b]`` indexed by buy codes (PISTOL, ECO, FORCE, FULL)."""
        return _pairs_to_table(self.buy_pairs)

    def ct_bias_for(self, map_name: str) -> float:
        m = normalize_map_name(map_name)
        if m in self.ct_bias:
            return self.ct_bias[m]
        return self.priors.ct_prior(m)

    def buy_effect(self, buy_a: BuyType, buy_b: BuyType) -> float:
        return float(self.buy_table[BUY_CODE[buy_a], BUY_CODE[buy_b]])

    def logit_tables(self, map_name: str) -> tuple[float, float, FloatArray]:
        """``(ct_bias, pistol_ct_adj, buy_table)`` for fast vectorised evaluation in the simulator."""
        return self.ct_bias_for(map_name), self.pistol_ct_adj, self.buy_table

    # ------------------------------------------------------------------ prediction
    def round_win_prob_codes(
        self,
        a_is_ct: npt.ArrayLike,
        buy_a: npt.ArrayLike,
        buy_b: npt.ArrayLike,
        map_name: str,
        d: float | npt.ArrayLike = 0.0,
    ) -> FloatArray:
        """Vectorised P(A wins) from numpy arrays (``a_is_ct`` bool, buys as int codes 0..3)."""
        ct_bias, padj, table = self.logit_tables(map_name)
        is_ct = np.asarray(a_is_ct, dtype=bool)
        ba = np.asarray(buy_a, dtype=np.intp)
        bb = np.asarray(buy_b, dtype=np.intp)
        sign = np.where(is_ct, 1.0, -1.0)
        pistol = (ba == PISTOL_CODE) & (bb == PISTOL_CODE)
        eta = np.asarray(d, dtype=np.float64) + sign * (ct_bias + np.where(pistol, padj, 0.0)) + table[ba, bb]
        return np.asarray(special.expit(eta), dtype=np.float64)

    def round_win_prob(
        self,
        a_side: Side | Sequence[Side] | npt.NDArray[np.bool_],
        buy_a: BuyType | Sequence[BuyType] | npt.NDArray[np.integer[Any]],
        buy_b: BuyType | Sequence[BuyType] | npt.NDArray[np.integer[Any]],
        map_name: str,
        d: float | npt.ArrayLike = 0.0,
    ) -> FloatArray:
        """P(team A wins the round); accepts enums, sequences of enums or numpy arrays (vectorised).

        ``a_side`` may be a :class:`Side`, a sequence of sides, or a bool array (True = A is CT);
        buys may be :class:`BuyType` values or int codes (``economy.BUY_CODE``).
        """
        return self.round_win_prob_codes(
            _side_array(a_side), _buy_array(buy_a), _buy_array(buy_b), map_name, d
        )

    # ------------------------------------------------------------------ fitting
    def fit(
        self,
        rounds: Iterable[RoundRecord],
        *,
        as_of: datetime | None = None,
        half_life_days: float | None = 365.0,
        rounds_per_half: int = 12,
        max_iter: int = 2000,
    ) -> RoundModel:
        """Estimate per-map CT bias, the pistol side adjustment and the buy-matchup table.

        Penalised logistic regression (Gaussian priors from :class:`RoundModelPriors`) with one
        nuisance strength term per (match_id, map_number). Rounds without buy information are
        classified from equipment values (:func:`classify_buy`); pistol rounds are recognised by
        round number; rounds whose buys stay unknown are skipped (counted in ``fit_stats``).
        Returns ``self``.
        """
        cutoff = ensure_utc(as_of) if as_of is not None else None
        rows: list[tuple[int, int, int, int, int, int]] = []
        times: list[float] = []
        map_index: dict[str, int] = {}
        inst_index: dict[tuple[int, int], int] = {}
        skipped = 0
        for rec in rounds:
            played = ensure_utc(rec.played_at)
            if cutoff is not None and played >= cutoff:
                continue
            b1 = rec.team1_buy or classify_buy(rec.team1_equipment_value, rec.round_number, rounds_per_half)
            b2 = rec.team2_buy or classify_buy(rec.team2_equipment_value, rec.round_number, rounds_per_half)
            if b1 is None or b2 is None:
                skipped += 1
                continue
            m_i = map_index.setdefault(normalize_map_name(rec.map_name), len(map_index))
            inst_i = inst_index.setdefault((rec.match_id, rec.map_number), len(inst_index))
            side_i = 1 if rec.team1_side is Side.CT else -1
            y_i = 1 if rec.winner is TeamSlot.TEAM1 else 0
            rows.append((m_i, inst_i, side_i, BUY_CODE[b1], BUY_CODE[b2], y_i))
            times.append(played.timestamp() / 86400.0)
        self._fingerprint = None
        maps = sorted(map_index, key=lambda k: map_index[k])
        if not rows:
            self.fit_stats = {"n_rounds": 0, "n_skipped": skipped, "converged": True, "note": "priors only"}
            return self

        arr = np.asarray(rows, dtype=np.float64)
        n = arr.shape[0]
        map_col = arr[:, 0].astype(np.intp)
        inst_col = arr[:, 1].astype(np.intp)
        sign = arr[:, 2]
        b1c = arr[:, 3].astype(np.intp)
        b2c = arr[:, 4].astype(np.intp)
        y = arr[:, 5]
        t = np.asarray(times, dtype=np.float64)
        t_ref = cutoff.timestamp() / 86400.0 if cutoff is not None else float(t.max())
        if half_life_days is not None and half_life_days > 0:
            w = np.power(0.5, np.maximum(t_ref - t, 0.0) / half_life_days)
        else:
            w = np.ones(n, dtype=np.float64)

        n_maps, n_inst, n_pairs = len(maps), len(inst_index), len(BUY_PAIRS)
        off_pistol = n_maps
        off_buy = n_maps + 1
        off_inst = off_buy + n_pairs
        n_par = off_inst + n_inst

        pistol = (b1c == PISTOL_CODE) & (b2c == PISTOL_CODE)
        r_idx: list[FloatArray] = []
        c_idx: list[FloatArray] = []
        vals: list[FloatArray] = []
        all_rows = np.arange(n, dtype=np.float64)
        # CT bias per map
        r_idx.append(all_rows)
        c_idx.append(map_col.astype(np.float64))
        vals.append(sign)
        # pistol side adjustment
        pr = np.flatnonzero(pistol)
        r_idx.append(pr.astype(np.float64))
        c_idx.append(np.full(pr.size, off_pistol, dtype=np.float64))
        vals.append(sign[pr])
        # buy pairs: +1 when team1 holds the stronger buy j of pair (i, j), -1 when team2 does
        for p_i, (i, j) in enumerate(BUY_PAIRS):
            pos = np.flatnonzero((b1c == j) & (b2c == i))
            neg = np.flatnonzero((b1c == i) & (b2c == j))
            for idx, v in ((pos, 1.0), (neg, -1.0)):
                if idx.size:
                    r_idx.append(idx.astype(np.float64))
                    c_idx.append(np.full(idx.size, off_buy + p_i, dtype=np.float64))
                    vals.append(np.full(idx.size, v))
        # nuisance strength per map instance
        r_idx.append(all_rows)
        c_idx.append((off_inst + inst_col).astype(np.float64))
        vals.append(np.ones(n))
        design = sparse.csr_matrix(
            (
                np.concatenate(vals),
                (np.concatenate(r_idx).astype(np.intp), np.concatenate(c_idx).astype(np.intp)),
            ),
            shape=(n, n_par),
        )
        design_t = design.T.tocsr()

        pr_ = self.priors
        mu = np.zeros(n_par, dtype=np.float64)
        prec = np.zeros(n_par, dtype=np.float64)
        mu[:n_maps] = [pr_.ct_prior(name) for name in maps]
        prec[:n_maps] = 1.0 / pr_.ct_bias_sd**2
        mu[off_pistol] = pr_.pistol_ct_adj_mean
        prec[off_pistol] = 1.0 / pr_.pistol_ct_adj_sd**2
        mu[off_buy:off_inst] = pr_.buy_pair_mean
        prec[off_buy:off_inst] = 1.0 / pr_.buy_pair_sd**2
        prec[off_inst:] = 1.0 / pr_.strength_sd**2

        def objective(theta: FloatArray) -> tuple[float, FloatArray]:
            eta = design @ theta
            nll = float(np.sum(w * (np.logaddexp(0.0, eta) - y * eta)))
            resid = w * (special.expit(eta) - y)
            grad = np.asarray(design_t @ resid, dtype=np.float64)
            diff = theta - mu
            nll += 0.5 * float(np.sum(prec * diff * diff))
            grad += prec * diff
            return nll, grad

        res = optimize.minimize(
            objective, mu.copy(), jac=True, method="L-BFGS-B", options={"maxiter": max_iter, "gtol": 1e-7}
        )
        theta = np.asarray(res.x, dtype=np.float64)
        for col, name in enumerate(maps):
            self.ct_bias[name] = float(theta[col])
        self.pistol_ct_adj = float(theta[off_pistol])
        self.buy_pairs = theta[off_buy:off_inst].copy()

        ct_won_arr = (y == 1) == (sign > 0)
        per_map: dict[str, dict[str, float]] = {}
        for col, name in enumerate(maps):
            sel = map_col == col
            per_map[name] = {
                "n_rounds": int(sel.sum()),
                "raw_ct_win_rate": float(np.average(ct_won_arr[sel], weights=w[sel])),
                "ct_bias": float(theta[col]),
            }
        self.fit_stats = {
            "n_rounds": int(n),
            "n_skipped": int(skipped),
            "n_map_instances": int(n_inst),
            "effective_rounds": float(w.sum()),
            "converged": bool(res.success),
            "iterations": int(res.nit),
            "objective": float(res.fun),
            "per_map": per_map,
            "pistol_rounds": int(pistol.sum()),
            "half_life_days": half_life_days,
            "as_of": cutoff.isoformat() if cutoff is not None else None,
        }
        return self

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "cs2bot.RoundModel",
            "version": self.VERSION,
            "ct_bias": dict(sorted(self.ct_bias.items())),
            "pistol_ct_adj": self.pistol_ct_adj,
            "buy_pairs": [float(x) for x in self.buy_pairs],
            "buy_pair_order": [[BUY_ORDER[i].value, BUY_ORDER[j].value] for i, j in BUY_PAIRS],
            "priors": self.priors.to_dict(),
            "fit_stats": self.fit_stats,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> RoundModel:
        if d.get("kind") not in (None, "cs2bot.RoundModel"):
            raise ValueError("not a RoundModel payload")
        model = cls(
            ct_bias={str(k): float(v) for k, v in dict(d["ct_bias"]).items()},
            pistol_ct_adj=float(d["pistol_ct_adj"]),
            buy_pairs=[float(x) for x in d["buy_pairs"]],
            priors=RoundModelPriors.from_dict(d.get("priors", {})),
        )
        model.fit_stats = dict(d.get("fit_stats", {}))
        return model

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> RoundModel:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def fingerprint(self) -> str:
        """Short stable hash of the parameters (used in simulation cache keys)."""
        if self._fingerprint is None:
            payload = {
                "ct_bias": {k: round(v, 10) for k, v in sorted(self.ct_bias.items())},
                "pistol": round(self.pistol_ct_adj, 10),
                "pairs": [round(float(x), 10) for x in self.buy_pairs],
                "prior_default": round(self.priors.ct_bias_default, 10),
                "prior_ct": {k: round(float(v), 10) for k, v in sorted(self.priors.ct_bias_mean.items())},
            }
            raw = json.dumps(payload, sort_keys=True).encode("utf-8")
            self._fingerprint = hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:16]
        return self._fingerprint

    def __repr__(self) -> str:
        return f"RoundModel(maps={len(self.ct_bias)}, fingerprint={self.fingerprint()})"


def _side_array(a_side: Side | Sequence[Side] | npt.NDArray[np.bool_]) -> npt.NDArray[np.bool_]:
    if isinstance(a_side, Side):
        return np.asarray(a_side is Side.CT)
    if isinstance(a_side, np.ndarray):
        return a_side.astype(bool)
    return np.asarray([s is Side.CT for s in a_side], dtype=bool)


def _buy_array(
    buy: BuyType | Sequence[BuyType] | npt.NDArray[np.integer[Any]],
) -> npt.NDArray[np.intp]:
    if isinstance(buy, BuyType):
        return np.asarray(BUY_CODE[buy], dtype=np.intp)
    if isinstance(buy, np.ndarray):
        return buy.astype(np.intp)
    return np.asarray([BUY_CODE[b] for b in buy], dtype=np.intp)


__all__ = [
    "BUY_PAIRS",
    "DEFAULT_BUY_PAIR_PRIOR",
    "DEFAULT_CT_BIAS_PRIOR",
    "RoundModel",
    "RoundModelPriors",
    "classify_buy",
]
