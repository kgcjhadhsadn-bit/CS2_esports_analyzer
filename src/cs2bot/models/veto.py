"""Map veto model: per-team pick/ban propensities -> distribution over the maps a series is played on.

Templates
---------
:data:`VETO_TEMPLATES` holds the standard 7-map-pool veto orders (``actor`` 0 = the team with the
first veto, 1 = the other team, ``None`` = leftover / decider):

* BO1: ban, ban, ban, ban, ban, ban (alternating), leftover played;
* BO2: ban, ban, pick, pick - the remaining maps are not played;
* BO3: ban, ban, pick, pick, ban, ban, leftover decider;
* BO5: ban, ban, pick, pick, pick, pick, leftover.

:func:`veto_template` generalises them to other pool sizes: the opening ban pair is kept when the
pool allows it, the picks follow, then bans until exactly one map is left for the leftover (BO2:
two picks after up to two bans, nothing else); actors alternate throughout. Tournaments with other
orders can pass ``template=`` explicitly; published (known) steps always override the template.

Preferences
-----------
For every team and action (ban / pick) the model keeps exponentially recency-weighted counts
(half-life ``half_life_days``, ~90 days) of

* ``chosen[m]`` - how often the team chose map ``m``, and
* ``exposure[m]`` - the expected count under a uniform chooser (``1 / |remaining|`` per decision in
  which ``m`` was available), which corrects for maps already removed by earlier steps.

The league-wide ratio is smoothed toward 1 (``q_m = (S_m + k_L) / (E_m + k_L)``) and the team's ratio
is smoothed toward the league (Bayesian shrinkage with ``team_prior_strength`` pseudo-decisions):

    preference(team, m, action) = log( (S_team,m + k q_m) / (E_team,m + k) ).

It is the log of an observed / expected ratio (a one-step estimate of the Luce choice strength) and
falls back to the league rate for teams without history.

Choice model
------------
Each veto step is a softmax (conditional logit) over the remaining maps with utility

    u(m) = a_action * preference(team, m, action)
           + b_action * sign_action * advantage(team, opponent, m)
           + c_action * preference(opponent, m, pick)

with ``sign = +1`` for picks and ``-1`` for bans, so ``b > 0`` means "pick maps where we are stronger
than this opponent, ban maps where we are weaker". ``advantage`` is an optional callable (e.g.
:meth:`cs2bot.models.ratings.MapRatingModel.map_advantage`, logit units); ``c`` captures reacting to
the opponent's comfort maps (ban them / avoid picking into them). ``(a, b, c)`` per action are fitted
by penalised maximum likelihood (L-BFGS, ridge toward ``a = 1, b = c = 0``) on historical steps
whose features are computed **walk-forward** (each veto's features use only earlier vetoes, and the
advantage callable is queried at the veto's own time). Two coefficient sets are kept: one with the
advantage term (if an advantage callable was given to :meth:`VetoModel.fit`) and one without, used
when :meth:`VetoModel.predict_sequences` is called without ``map_advantage``. A model fitted
*without* an advantage callable has no weight for the term and therefore ignores ``map_advantage``
at prediction time. Advantage values are sanitised (NaN / inf -> 0, clipped to +-10 logits); a
lookup that raises during :meth:`VetoModel.fit` counts as "no information" for that step
(:attr:`VetoModel.advantage_failures`), at prediction time the exception propagates.

Assumptions (documented, not verifiable from this machine): maps are played in pick order followed
by the leftover map; the first-veto team ("team A") is not known before the veto is published, so
callers pass ``first_veto=None`` unless they know it; organisers with a different order (for
example BO1 with 2-3-1 bans, or a BO5 on a 7-map pool starting with picks) pass ``template=``.

Sequence distribution
---------------------
:meth:`VetoModel.predict_sequences` enumerates veto paths exactly with a forward dynamic programme
over (remaining maps, maps picked so far) - paths that only differ in the order of bans merge -
pruning branches below ``prune_probability`` of the step's mass, and aggregates the probability of
every played-map sequence with its pickers. Known steps condition the distribution (forced choices,
and maps known to be chosen later stay available); ``first_veto=None`` is a 50/50 prior over which
team starts, updated by the likelihood of any known steps. A complete published veto yields a
single sequence with probability 1 without needing the model or the pool.

The list is cut to ``top_k`` sequences (default 50) and renormalised. Because 50 sequences hold only
about half of a BO3's probability (and a fifth of a BO5's), the cut preserves the per-slot map
marginals exactly instead of just rescaling the survivors - see :mod:`cs2bot.models.veto_truncation`.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, NamedTuple

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field
from scipy import optimize, special

from cs2bot.config import DEFAULT_ACTIVE_MAP_POOL
from cs2bot.domain.enums import MatchFormat, TeamSlot, VetoAction
from cs2bot.domain.predictions import MapSequenceProbability
from cs2bot.domain.records import VetoRecord, VetoStep
from cs2bot.models.veto_truncation import truncate_sequences
from cs2bot.utils.logging import get_logger
from cs2bot.utils.names import normalize_map_name
from cs2bot.utils.time import ensure_utc, utcnow

log = get_logger(__name__)

__all__ = [
    "BO1_TEMPLATE_2_3_1",
    "VETO_TEMPLATES",
    "MapAdvantageFn",
    "StepForecast",
    "VetoCoefficients",
    "VetoConfig",
    "VetoModel",
    "VetoTemplateStep",
    "map_slot_distribution",
    "p_map_played",
    "veto_template",
]

FloatArray = npt.NDArray[np.float64]

#: ``(team_id, opponent_id, map_name, as_of) -> logit advantage of team over opponent on the map``.
MapAdvantageFn = Callable[[int, int, str, datetime], float]

_SECONDS_PER_DAY: Final[float] = 86_400.0
_FORMAT_KIND: Final[str] = "cs2bot.VetoModel"
_FORMAT_VERSION: Final[int] = 1
_ACTIONS: Final[tuple[VetoAction, VetoAction]] = (VetoAction.BAN, VetoAction.PICK)
#: Advantage values (logit units) are clipped to this magnitude before entering the utilities.
_MAX_ADVANTAGE: Final[float] = 10.0
_PICKS_BY_FORMAT: Final[dict[MatchFormat, int]] = {
    MatchFormat.BO1: 0,
    MatchFormat.BO2: 2,
    MatchFormat.BO3: 2,
    MatchFormat.BO5: 4,
}


# ---------------------------------------------------------------------------------------- templates


class VetoTemplateStep(NamedTuple):
    """One template step: ``actor`` 0 = first-veto team, 1 = the other team, ``None`` = leftover."""

    action: VetoAction
    actor: int | None


def _has_leftover(fmt: MatchFormat) -> bool:
    return fmt is not MatchFormat.BO2


def veto_template(fmt: MatchFormat, n_maps: int = 7) -> tuple[VetoTemplateStep, ...]:
    """Veto order for ``fmt`` and a pool of ``n_maps`` maps (see the module docstring).

    Raises ``ValueError`` when the pool is too small to play the format.
    """
    n_picks = _PICKS_BY_FORMAT[fmt]
    if n_maps < fmt.n_maps:
        raise ValueError(f"a {fmt.value} needs at least {fmt.n_maps} maps in the pool, got {n_maps}")
    actions: list[VetoAction]
    if fmt is MatchFormat.BO2:
        n_bans = min(2, n_maps - n_picks)
        actions = [VetoAction.BAN] * n_bans + [VetoAction.PICK] * n_picks
    else:
        n_bans = n_maps - n_picks - 1
        if fmt is MatchFormat.BO1:
            actions = [VetoAction.BAN] * n_bans
        else:
            pre = min(2, n_bans)
            actions = [VetoAction.BAN] * pre + [VetoAction.PICK] * n_picks + [VetoAction.BAN] * (n_bans - pre)
    steps = [VetoTemplateStep(action, i % 2) for i, action in enumerate(actions)]
    if _has_leftover(fmt):
        steps.append(VetoTemplateStep(VetoAction.LEFTOVER, None))
    return tuple(steps)


#: Standard veto orders for the 7-map Active Duty pool.
VETO_TEMPLATES: Final[dict[MatchFormat, tuple[VetoTemplateStep, ...]]] = {
    fmt: veto_template(fmt, 7) for fmt in MatchFormat
}

#: Alternative BO1 order used at Valve-sponsored events (assumption from the published Major rules:
#: team A removes 2 maps, team B removes 3, team A removes 1, the remaining map is played). Pass it
#: as ``template=`` for those tournaments; the default BO1 template alternates single bans.
BO1_TEMPLATE_2_3_1: Final[tuple[VetoTemplateStep, ...]] = (
    VetoTemplateStep(VetoAction.BAN, 0),
    VetoTemplateStep(VetoAction.BAN, 0),
    VetoTemplateStep(VetoAction.BAN, 1),
    VetoTemplateStep(VetoAction.BAN, 1),
    VetoTemplateStep(VetoAction.BAN, 1),
    VetoTemplateStep(VetoAction.BAN, 0),
    VetoTemplateStep(VetoAction.LEFTOVER, None),
)

#: Other orders that are used in practice, tried (when no ``template=`` is given) if the published
#: known steps contradict the default template but fit one of these - e.g. a BO1 whose first two
#: bans were both made by the same team is a 2-3-1 veto, so its remaining actors follow that order.
_ALTERNATIVE_TEMPLATES: Final[dict[tuple[MatchFormat, int], tuple[tuple[VetoTemplateStep, ...], ...]]] = {
    (MatchFormat.BO1, 7): (BO1_TEMPLATE_2_3_1,),
}


# ------------------------------------------------------------------------------------------- config


class VetoConfig(BaseModel):
    """Knobs of :class:`VetoModel` (defaults chosen on synthetic universes, see the tests)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    half_life_days: float = Field(default=90.0, gt=0.0)
    team_prior_strength: float = Field(default=3.0, gt=0.0)
    league_prior_strength: float = Field(default=20.0, gt=0.0)
    use_opponent_preference: bool = True
    ridge: float = Field(default=1.0, ge=0.0)
    prune_probability: float = Field(default=1e-9, ge=0.0, lt=1.0)
    default_map_pool: tuple[str, ...] = tuple(DEFAULT_ACTIVE_MAP_POOL)


class VetoCoefficients(BaseModel):
    """Fitted softmax weights per action (see the module docstring)."""

    model_config = ConfigDict(frozen=True)

    a_ban: float = 1.0
    b_ban: float = 0.0
    c_ban: float = 0.0
    a_pick: float = 1.0
    b_pick: float = 0.0
    c_pick: float = 0.0
    n_steps: int = 0
    log_likelihood: float | None = None

    def for_action(self, action: VetoAction) -> tuple[float, float, float]:
        if action is VetoAction.BAN:
            return self.a_ban, self.b_ban, self.c_ban
        return self.a_pick, self.b_pick, self.c_pick


class StepForecast(BaseModel):
    """Distribution of the next unknown veto step."""

    step: int  # 1-based
    action: VetoAction
    team: TeamSlot | None  # None for the leftover, or when the acting team is uncertain
    probabilities: dict[str, float]


# ------------------------------------------------------------------------------------------- state


@dataclass
class _ChoiceStats:
    """Exponentially decayed choice counts of one actor (team or league) for one action."""

    ref: datetime | None = None
    chosen: dict[str, float] = field(default_factory=dict)
    exposure: dict[str, float] = field(default_factory=dict)
    decisions: float = 0.0

    def factor(self, t: datetime | None, half_life_days: float) -> float:
        if self.ref is None or t is None or t <= self.ref:
            return 1.0
        days = (t - self.ref).total_seconds() / _SECONDS_PER_DAY
        return float(0.5 ** (days / half_life_days))

    def decay_to(self, t: datetime, half_life_days: float) -> None:
        f = self.factor(t, half_life_days)
        if f != 1.0:
            self.chosen = {m: v * f for m, v in self.chosen.items()}
            self.exposure = {m: v * f for m, v in self.exposure.items()}
            self.decisions *= f
        if self.ref is None or t > self.ref:
            self.ref = t

    def add(self, t: datetime, chosen: str, remaining: Sequence[str], half_life_days: float) -> None:
        self.decay_to(t, half_life_days)
        self.chosen[chosen] = self.chosen.get(chosen, 0.0) + 1.0
        share = 1.0 / len(remaining)
        for m in remaining:
            self.exposure[m] = self.exposure.get(m, 0.0) + share
        self.decisions += 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref.isoformat() if self.ref is not None else None,
            "chosen": dict(sorted(self.chosen.items())),
            "exposure": dict(sorted(self.exposure.items())),
            "decisions": self.decisions,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> _ChoiceStats:
        ref = d.get("ref")
        return cls(
            ref=ensure_utc(datetime.fromisoformat(ref)) if ref else None,
            chosen={str(k): float(v) for k, v in d.get("chosen", {}).items()},
            exposure={str(k): float(v) for k, v in d.get("exposure", {}).items()},
            decisions=float(d.get("decisions", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class _Decision:
    team_id: int
    opponent_id: int
    action: VetoAction
    remaining: tuple[str, ...]
    chosen: str


@dataclass(frozen=True, slots=True)
class _StepRow:
    action: VetoAction
    own: tuple[float, ...]
    adv: tuple[float, ...]
    opp: tuple[float, ...]
    chosen: int


_PlayedKey = tuple[tuple[str, TeamSlot | None], ...]
_State = tuple[frozenset[str], _PlayedKey]


def _finite(value: float) -> float:
    """Advantage values come from another model: map NaN / inf to 0 (no information) and bound them."""
    x = float(value)
    if not math.isfinite(x):
        return 0.0
    return min(max(x, -_MAX_ADVANTAGE), _MAX_ADVANTAGE)


def _unique(maps: Iterable[str]) -> list[str]:
    out: list[str] = []
    for m in maps:
        n = normalize_map_name(m)
        if n and n not in out:
            out.append(n)
    return out


# -------------------------------------------------------------------------------------------- model


class VetoModel:
    """Per-team veto propensities and the resulting map-sequence distribution."""

    def __init__(self, config: VetoConfig | None = None) -> None:
        self._config = config or VetoConfig()
        self.reset()

    # ------------------------------------------------------------------ properties / housekeeping

    def reset(self) -> None:
        self._team_stats: dict[tuple[int, VetoAction], _ChoiceStats] = {}
        self._league: dict[VetoAction, _ChoiceStats] = {a: _ChoiceStats() for a in _ACTIONS}
        self._coef = VetoCoefficients()
        self._coef_adv: VetoCoefficients | None = None
        self._seen: set[int] = set()
        self._last: datetime | None = None
        self._advantage_failures = 0

    @property
    def config(self) -> VetoConfig:
        return self._config

    @property
    def coefficients(self) -> VetoCoefficients:
        """Coefficients used without an advantage callable."""
        return self._coef

    @property
    def coefficients_with_advantage(self) -> VetoCoefficients | None:
        """Coefficients used with an advantage callable (``None`` if fitted without one)."""
        return self._coef_adv

    @property
    def advantage_failures(self) -> int:
        """Veto steps of the last :meth:`fit` whose advantage lookup raised (treated as 0 advantage)."""
        return self._advantage_failures

    @property
    def last_veto_time(self) -> datetime | None:
        return self._last

    @property
    def n_records(self) -> int:
        return len(self._seen)

    @property
    def team_ids(self) -> list[int]:
        return sorted({tid for tid, _ in self._team_stats})

    def copy(self) -> VetoModel:
        return VetoModel.from_dict(self.to_dict())

    # ------------------------------------------------------------------------------- fitting

    def _record_pool(self, rec: VetoRecord) -> list[str]:
        step_maps = _unique(st.map_name for st in sorted(rec.steps, key=lambda s: s.step))
        if rec.map_pool:
            pool = _unique(rec.map_pool)
        elif any(st.action is VetoAction.LEFTOVER for st in rec.steps):
            pool = list(step_maps)
        else:
            pool = _unique(self._config.default_map_pool)
        for m in step_maps:
            if m not in pool:
                pool.append(m)
        return pool

    def _decisions(self, rec: VetoRecord) -> list[_Decision]:
        remaining = self._record_pool(rec)
        out: list[_Decision] = []
        for st in sorted(rec.steps, key=lambda s: s.step):
            m = normalize_map_name(st.map_name)
            if m not in remaining:
                continue
            if st.action is not VetoAction.LEFTOVER and st.team is not None and len(remaining) >= 2:
                team_id, opp_id = (
                    (rec.team1_id, rec.team2_id)
                    if st.team is TeamSlot.TEAM1
                    else (rec.team2_id, rec.team1_id)
                )
                out.append(_Decision(team_id, opp_id, st.action, tuple(remaining), m))
            remaining.remove(m)
        return out

    def _ingest(self, decisions: Sequence[_Decision], t: datetime) -> None:
        h = self._config.half_life_days
        for d in decisions:
            self._team_stats.setdefault((d.team_id, d.action), _ChoiceStats()).add(
                t, d.chosen, d.remaining, h
            )
            self._league[d.action].add(t, d.chosen, d.remaining, h)

    def fit(self, records: Iterable[VetoRecord], *, map_advantage: MapAdvantageFn | None = None) -> VetoModel:
        """Reset, learn preferences from ``records`` (chronologically) and fit the softmax weights.

        ``map_advantage`` (optional) is queried as ``map_advantage(team_id, opponent_id, map, t)``
        with ``t`` = the veto's own time, so it must be leakage-free for historical times - e.g.
        :meth:`cs2bot.models.ratings.MapRatingModel.historical_map_advantage` of a rating model
        fitted with ``record_history=True``. Returns ``self``.
        """
        self.reset()
        recs = sorted(records, key=lambda r: (ensure_utc(r.played_at), r.match_id))
        rows: list[_StepRow] = []
        for rec in recs:
            if rec.match_id in self._seen or rec.team1_id == rec.team2_id:
                continue
            t = ensure_utc(rec.played_at)
            decisions = self._decisions(rec)
            adv_cache: dict[tuple[int, int, str], float] = {}
            for d in decisions:
                own = tuple(self._pref(d.team_id, d.action, m, t) for m in d.remaining)
                opp = tuple(self._pref(d.opponent_id, VetoAction.PICK, m, t) for m in d.remaining)
                adv = (0.0,) * len(d.remaining)
                if map_advantage is not None:
                    try:
                        for m in d.remaining:
                            key = (d.team_id, d.opponent_id, m)
                            if key not in adv_cache:
                                adv_cache[key] = _finite(map_advantage(d.team_id, d.opponent_id, m, t))
                        adv = tuple(adv_cache[(d.team_id, d.opponent_id, m)] for m in d.remaining)
                    except Exception as exc:  # one bad lookup must not abort the whole fit
                        self._advantage_failures += 1
                        if self._advantage_failures == 1:
                            log.warning(
                                "veto fit: map advantage lookup failed for match %s (%r); using 0 for "
                                "this and any further failing step",
                                rec.match_id,
                                exc,
                            )
                rows.append(_StepRow(d.action, own, adv, opp, d.remaining.index(d.chosen)))
            self._ingest(decisions, t)
            self._seen.add(rec.match_id)
            self._last = t if self._last is None or t > self._last else self._last
        self._coef = self._fit_coefficients(rows, use_adv=False)
        self._coef_adv = self._fit_coefficients(rows, use_adv=True) if map_advantage is not None else None
        return self

    def fit_until(
        self, records: Iterable[VetoRecord], as_of: datetime, *, map_advantage: MapAdvantageFn | None = None
    ) -> VetoModel:
        """:meth:`fit` on the vetoes played strictly before ``as_of``."""
        cut = ensure_utc(as_of)
        return self.fit((r for r in records if ensure_utc(r.played_at) < cut), map_advantage=map_advantage)

    def update(self, record: VetoRecord) -> bool:
        """Add one veto to the preference counts (coefficients are kept). Chronological order only.

        Returns ``False`` for an already seen match; raises ``ValueError`` for an out-of-order record.
        """
        if record.match_id in self._seen or record.team1_id == record.team2_id:
            return False
        t = ensure_utc(record.played_at)
        if self._last is not None and t < self._last:
            raise ValueError(
                f"vetoes must be added chronologically: {t.isoformat()} is before {self._last.isoformat()}"
            )
        self._ingest(self._decisions(record), t)
        self._seen.add(record.match_id)
        self._last = t if self._last is None or t > self._last else self._last
        return True

    def _fit_coefficients(self, rows: Sequence[_StepRow], *, use_adv: bool) -> VetoCoefficients:
        if not rows:
            return VetoCoefficients()
        cfg = self._config
        n = len(rows)
        k = max(len(r.own) for r in rows)
        own = np.zeros((n, k))
        adv = np.zeros((n, k))
        opp = np.zeros((n, k))
        mask = np.zeros((n, k), dtype=bool)
        chosen = np.zeros(n, dtype=np.int64)
        is_pick = np.zeros(n, dtype=bool)
        for i, r in enumerate(rows):
            w = len(r.own)
            own[i, :w] = r.own
            adv[i, :w] = r.adv
            opp[i, :w] = r.opp
            mask[i, :w] = True
            chosen[i] = r.chosen
            is_pick[i] = r.action is VetoAction.PICK
        sign = np.where(is_pick, 1.0, -1.0)
        adv = adv * sign[:, None]
        features = [own, adv, opp]
        free = [True, use_adv, cfg.use_opponent_preference]
        prior = np.array([1.0, 0.0, 0.0])
        idx = np.arange(n)
        groups = [~is_pick, is_pick]

        def unpack(theta: FloatArray) -> FloatArray:
            full = np.tile(prior, (2, 1))
            j = 0
            for g_ in range(2):
                for f_ in range(3):
                    if free[f_]:
                        full[g_, f_] = theta[j]
                        j += 1
            return full

        n_free = 2 * sum(free)

        def objective(theta: FloatArray) -> tuple[float, FloatArray]:
            full = unpack(theta)
            coef = np.where(is_pick[:, None], full[1][None, :], full[0][None, :])  # (n, 3)
            u = coef[:, 0:1] * own + coef[:, 1:2] * adv + coef[:, 2:3] * opp
            u = np.where(mask, u, -np.inf)
            log_z = special.logsumexp(u, axis=1)
            ll = float(np.sum(u[idx, chosen] - log_z))
            prob = np.where(mask, np.exp(u - log_z[:, None]), 0.0)
            grad_full = np.zeros((2, 3))
            for f_, feat in enumerate(features):
                contrib = feat[idx, chosen] - np.sum(prob * feat, axis=1)
                for g_, sel in enumerate(groups):
                    grad_full[g_, f_] = float(np.sum(contrib[sel]))
            penalty = cfg.ridge * float(np.sum((full - prior[None, :]) ** 2))
            grad_pen = 2.0 * cfg.ridge * (full - prior[None, :])
            grad = -(grad_full) + grad_pen
            flat: list[float] = []
            for g_ in range(2):
                for f_ in range(3):
                    if free[f_]:
                        flat.append(float(grad[g_, f_]))
            return -ll + penalty, np.asarray(flat, dtype=np.float64)

        x0 = np.asarray([v for _ in range(2) for f_, v in enumerate(prior) if free[f_]], dtype=np.float64)
        res = optimize.minimize(objective, x0, jac=True, method="L-BFGS-B")
        full = unpack(np.asarray(res.x, dtype=np.float64)) if n_free else np.tile(prior, (2, 1))
        final_obj, _ = objective(np.asarray(res.x, dtype=np.float64))
        penalty = cfg.ridge * float(np.sum((full - prior[None, :]) ** 2))
        return VetoCoefficients(
            a_ban=float(full[0, 0]),
            b_ban=float(full[0, 1]),
            c_ban=float(full[0, 2]),
            a_pick=float(full[1, 0]),
            b_pick=float(full[1, 1]),
            c_pick=float(full[1, 2]),
            n_steps=n,
            log_likelihood=-(final_obj - penalty),
        )

    # ---------------------------------------------------------------------------- preferences

    def _league_ratio(self, action: VetoAction, m: str, t: datetime | None) -> float:
        cfg = self._config
        st = self._league[action]
        f = st.factor(t, cfg.half_life_days)
        k = cfg.league_prior_strength
        return (f * st.chosen.get(m, 0.0) + k) / (f * st.exposure.get(m, 0.0) + k)

    def _pref(self, team_id: int, action: VetoAction, m: str, t: datetime | None) -> float:
        cfg = self._config
        q = self._league_ratio(action, m, t)
        st = self._team_stats.get((team_id, action))
        if st is None:
            return math.log(q)
        f = st.factor(t, cfg.half_life_days)
        k = cfg.team_prior_strength
        return math.log((f * st.chosen.get(m, 0.0) + k * q) / (f * st.exposure.get(m, 0.0) + k))

    def _check_as_of(self, as_of: datetime | None) -> datetime | None:
        if as_of is None:
            return self._last
        t = ensure_utc(as_of)
        if self._last is not None and t <= self._last:
            raise ValueError(
                f"as_of {t.isoformat()} is not after the last fitted veto {self._last.isoformat()}: "
                "use fit_until(records, as_of) for historical predictions"
            )
        return t

    def preference(
        self, team_id: int, map_name: str, action: VetoAction, as_of: datetime | None = None
    ) -> float:
        """Smoothed log observed/expected choice ratio of ``team_id`` for ``map_name`` (0 = neutral)."""
        if action is VetoAction.LEFTOVER:
            raise ValueError("preferences exist for ban and pick only")
        return self._pref(team_id, action, normalize_map_name(map_name), self._check_as_of(as_of))

    def preferences(
        self, team_id: int, action: VetoAction, map_pool: Sequence[str], as_of: datetime | None = None
    ) -> dict[str, float]:
        """:meth:`preference` for every map of ``map_pool``."""
        if action is VetoAction.LEFTOVER:
            raise ValueError("preferences exist for ban and pick only")
        t = self._check_as_of(as_of)
        return {m: self._pref(team_id, action, m, t) for m in _unique(map_pool)}

    # ----------------------------------------------------------------------------- prediction

    def _exp_utilities(
        self,
        team1_id: int,
        team2_id: int,
        pool: Sequence[str],
        t_pref: datetime | None,
        map_advantage: MapAdvantageFn | None,
        t_adv: datetime,
    ) -> dict[tuple[TeamSlot, VetoAction], dict[str, float]]:
        use_adv = map_advantage is not None and self._coef_adv is not None
        coef = self._coef_adv if use_adv and self._coef_adv is not None else self._coef
        out: dict[tuple[TeamSlot, VetoAction], dict[str, float]] = {}
        for slot, team, opp in ((TeamSlot.TEAM1, team1_id, team2_id), (TeamSlot.TEAM2, team2_id, team1_id)):
            adv = dict.fromkeys(pool, 0.0)
            if use_adv and map_advantage is not None:
                adv = {m: _finite(map_advantage(team, opp, m, t_adv)) for m in pool}
            opp_pick = {m: self._pref(opp, VetoAction.PICK, m, t_pref) for m in pool}
            for action in _ACTIONS:
                a, b, c = coef.for_action(action)
                sign = 1.0 if action is VetoAction.PICK else -1.0
                u = {
                    m: a * self._pref(team, action, m, t_pref) + b * sign * adv[m] + c * opp_pick[m]
                    for m in pool
                }
                top = max(u.values())
                out[(slot, action)] = {m: math.exp(val - top) for m, val in u.items()}
        return out

    def _normalise_known(
        self, known_steps: Sequence[VetoStep]
    ) -> tuple[dict[int, tuple[VetoAction, TeamSlot | None, str]], list[str]]:
        known: dict[int, tuple[VetoAction, TeamSlot | None, str]] = {}
        maps_seen: list[str] = []
        for st in known_steps:
            if st.step < 1:
                raise ValueError("veto steps are 1-based")
            idx = st.step - 1
            m = normalize_map_name(st.map_name)
            if idx in known:
                raise ValueError(f"veto step {st.step} given twice")
            if m in maps_seen:
                raise ValueError(f"map {m!r} appears twice in the known veto steps")
            maps_seen.append(m)
            known[idx] = (st.action, None if st.action is VetoAction.LEFTOVER else st.team, m)
        return known, maps_seen

    @staticmethod
    def _complete_sequence(
        fmt: MatchFormat, pool: Sequence[str], known: Mapping[int, tuple[VetoAction, TeamSlot | None, str]]
    ) -> MapSequenceProbability | None:
        """The played sequence if the known steps already determine it (any veto order)."""
        ordered = [known[i] for i in sorted(known)]
        picks = [(m, team) for action, team, m in ordered if action is VetoAction.PICK]
        n_picks = _PICKS_BY_FORMAT[fmt]
        if len(picks) >= fmt.n_maps:
            # Enough picked maps to fill every slot (always the case for a finished BO2 veto; also
            # covers organisers whose BO1/BO3/BO5 maps are all picked): played in pick order.
            chosen = picks[: fmt.n_maps]
            return MapSequenceProbability(
                maps=[m for m, _ in chosen], pickers=[t for _, t in chosen], probability=1.0
            )
        if fmt is MatchFormat.BO2 or len(picks) != n_picks:
            return None
        leftover = [m for action, _, m in ordered if action is VetoAction.LEFTOVER]
        if not leftover:
            used = {m for _, _, m in ordered}
            rest = [m for m in pool if m not in used]
            if len(rest) != 1:
                return None
            leftover = rest
        return MapSequenceProbability(
            maps=[m for m, _ in picks] + [leftover[0]],
            pickers=[t for _, t in picks] + [None],
            probability=1.0,
        )

    def _plan(
        self,
        fmt: MatchFormat,
        n_pool: int,
        first: TeamSlot,
        known: Mapping[int, tuple[VetoAction, TeamSlot | None, str]],
        template: Sequence[VetoTemplateStep] | None,
    ) -> list[tuple[VetoAction, TeamSlot | None]]:
        base = list(template) if template is not None else list(veto_template(fmt, n_pool))
        plan: list[tuple[VetoAction, TeamSlot | None]] = [
            (st.action, None if st.actor is None else (first if st.actor == 0 else first.other))
            for st in base
        ]
        mismatch = False
        for i, (action, team, _) in known.items():
            if i >= len(plan):
                mismatch = True
                continue
            if action is not plan[i][0]:
                mismatch = True
            plan[i] = (action, None if action is VetoAction.LEFTOVER else (team or plan[i][1]))
        if not mismatch or not known:
            return plan
        return self._repair_plan(fmt, n_pool, first, plan, known)

    @staticmethod
    def _repair_plan(
        fmt: MatchFormat,
        n_pool: int,
        first: TeamSlot,
        plan: list[tuple[VetoAction, TeamSlot | None]],
        known: Mapping[int, tuple[VetoAction, TeamSlot | None, str]],
    ) -> list[tuple[VetoAction, TeamSlot | None]]:
        """Known steps deviate from the template: keep them and re-derive the remaining steps."""
        prefix_len = max(known) + 1
        prefix: list[tuple[VetoAction, TeamSlot | None]] = []
        for i in range(prefix_len):
            if i < len(plan):
                # Template step, already merged with the known step by _plan (a known step without
                # team information keeps the template's actor).
                prefix.append(plan[i])
            elif i in known:
                action, team, _ = known[i]
                prefix.append((action, None if action is VetoAction.LEFTOVER else team))
            else:
                raise ValueError("known veto steps have gaps beyond the veto template")
        if any(a is VetoAction.LEFTOVER for a, _ in prefix):
            if prefix[-1][0] is not VetoAction.LEFTOVER and _has_leftover(fmt):
                raise ValueError("the leftover map must be the last veto step")
            return prefix
        picks_needed = _PICKS_BY_FORMAT[fmt] - sum(1 for a, _ in prefix if a is VetoAction.PICK)
        remaining = n_pool - len(prefix)
        if picks_needed < 0:
            if fmt is MatchFormat.BO2:
                return prefix
            raise ValueError(f"known veto steps contain more picks than a {fmt.value} allows")
        tail = 1 if _has_leftover(fmt) else 0
        if remaining < picks_needed + tail:
            raise ValueError("known veto steps are inconsistent with the map pool size")
        bans_needed = remaining - picks_needed - tail if _has_leftover(fmt) else 0
        last_team = next((t for _, t in reversed(prefix) if t is not None), first.other)
        actor = last_team.other
        rest: list[tuple[VetoAction, TeamSlot | None]] = []
        for action in [VetoAction.PICK] * picks_needed + [VetoAction.BAN] * bans_needed:
            rest.append((action, actor))
            actor = actor.other
        if _has_leftover(fmt):
            rest.append((VetoAction.LEFTOVER, None))
        return prefix + rest

    @staticmethod
    def _fits_template(
        base: Sequence[VetoTemplateStep], known: Mapping[int, tuple[VetoAction, TeamSlot | None, str]]
    ) -> bool:
        """Whether the known steps' actions and named teams agree with ``base`` for one veto order."""
        first: TeamSlot | None = None
        for i in sorted(known):
            action, team, _ = known[i]
            if i >= len(base) or base[i].action is not action:
                return False
            if team is None or action is VetoAction.LEFTOVER or base[i].actor is None:
                continue
            implied = team if base[i].actor == 0 else team.other
            if first is not None and implied is not first:
                return False
            first = implied
        return True

    @classmethod
    def _effective_template(
        cls,
        fmt: MatchFormat,
        n_pool: int,
        known: Mapping[int, tuple[VetoAction, TeamSlot | None, str]],
        template: Sequence[VetoTemplateStep] | None,
    ) -> list[VetoTemplateStep]:
        """``template`` if given; else the default order, or a known alternative the steps fit better."""
        if template is not None:
            return list(template)
        default = list(veto_template(fmt, n_pool))
        if not known or cls._fits_template(default, known):
            return default
        for alternative in _ALTERNATIVE_TEMPLATES.get((fmt, n_pool), ()):
            if cls._fits_template(alternative, known):
                return list(alternative)
        return default

    def _infer_first(
        self,
        fmt: MatchFormat,
        n_pool: int,
        known: Mapping[int, tuple[VetoAction, TeamSlot | None, str]],
        template: Sequence[VetoTemplateStep] | None,
    ) -> TeamSlot | None:
        base = list(template) if template is not None else list(veto_template(fmt, n_pool))
        for i in sorted(known):
            action, team, _ = known[i]
            if team is None or i >= len(base) or base[i].actor is None or action is VetoAction.LEFTOVER:
                continue
            return team if base[i].actor == 0 else team.other
        return None

    def _run(
        self,
        plan: Sequence[tuple[VetoAction, TeamSlot | None]],
        pool: Sequence[str],
        expu: Mapping[tuple[TeamSlot, VetoAction], Mapping[str, float]],
        known: Mapping[int, tuple[VetoAction, TeamSlot | None, str]],
        fmt: MatchFormat,
        stop: int | None = None,
    ) -> dict[_State, float]:
        """Forward DP over (remaining maps, played so far); unnormalised path probabilities."""
        prune = self._config.prune_probability
        has_leftover = _has_leftover(fmt)
        n_steps = len(plan) if stop is None else min(stop, len(plan))
        later: list[set[str]] = []
        for i in range(n_steps):
            later.append({m for j, (_, _, m) in known.items() if j > i})
        states: dict[_State, float] = {(frozenset(pool), ()): 1.0}
        for i in range(n_steps):
            action, actor = plan[i]
            ks = known.get(i)
            excluded = later[i]
            mass = sum(states.values())
            nxt: dict[_State, float] = defaultdict(float)
            for (rem, played), p in states.items():
                if not rem:
                    nxt[(rem, played)] += p
                    continue
                if action is VetoAction.LEFTOVER:
                    if ks is not None:
                        options = [(ks[2], 1.0)] if ks[2] in rem else []
                    else:
                        cands = [m for m in rem if m not in excluded] or sorted(rem)
                        options = [(m, 1.0 / len(cands)) for m in sorted(cands)]
                    for m, q in options:
                        new_played = (*played, (m, None)) if has_leftover else played
                        nxt[(rem - {m}, new_played)] += p * q
                    continue
                weights = expu[(actor, action)] if actor is not None else dict.fromkeys(rem, 1.0)
                denom = sum(weights[m] for m in rem)
                if ks is not None:
                    choices = [ks[2]] if ks[2] in rem else []
                else:
                    choices = [m for m in rem if m not in excluded]
                for m in choices:
                    pq = p * weights[m] / denom
                    if ks is None and pq < prune * mass:
                        continue
                    new_played = (*played, (m, actor)) if action is VetoAction.PICK else played
                    nxt[(rem - {m}, new_played)] += pq
            states = dict(nxt)
        if stop is None and has_leftover and not any(a is VetoAction.LEFTOVER for a, _ in plan):
            # Custom template without an explicit leftover step: the last remaining map is played.
            finished: dict[_State, float] = defaultdict(float)
            for (rem, played), p in states.items():
                if len(rem) == 1:
                    finished[(frozenset(), (*played, (next(iter(rem)), None)))] += p
                else:
                    finished[(rem, played)] += p
            states = dict(finished)
        return states

    def _prepare(
        self,
        team1_id: int,
        team2_id: int,
        fmt: MatchFormat,
        map_pool: Sequence[str],
        first_veto: TeamSlot | None,
        known_steps: Sequence[VetoStep],
        as_of: datetime | None,
        template: Sequence[VetoTemplateStep] | None,
    ) -> tuple[
        list[str],
        dict[int, tuple[VetoAction, TeamSlot | None, str]],
        list[tuple[float, TeamSlot]],
        datetime | None,
        list[VetoTemplateStep],
    ]:
        if team1_id == team2_id:
            raise ValueError("team1 and team2 must differ")
        t_pref = self._check_as_of(as_of)
        known, known_maps = self._normalise_known(known_steps)
        # An empty pool (e.g. a MatchContext whose pool is not filled yet) means the default pool.
        pool = _unique(map_pool) or _unique(self._config.default_map_pool)
        for m in known_maps:
            if m not in pool:
                pool.append(m)
        if len(pool) < fmt.n_maps:
            raise ValueError(f"a {fmt.value} needs at least {fmt.n_maps} maps in the pool, got {len(pool)}")
        base = self._effective_template(fmt, len(pool), known, template)
        inferred = self._infer_first(fmt, len(pool), known, base)
        first = inferred or first_veto
        orders = [(1.0, first)] if first is not None else [(0.5, TeamSlot.TEAM1), (0.5, TeamSlot.TEAM2)]
        return pool, known, orders, t_pref, base

    def predict_sequences(
        self,
        team1_id: int,
        team2_id: int,
        fmt: MatchFormat,
        map_pool: Sequence[str],
        *,
        first_veto: TeamSlot | None = TeamSlot.TEAM1,
        known_steps: Sequence[VetoStep] = (),
        top_k: int | None = 50,
        as_of: datetime | None = None,
        map_advantage: MapAdvantageFn | None = None,
        template: Sequence[VetoTemplateStep] | None = None,
        preserve_marginals: bool = True,
    ) -> list[MapSequenceProbability]:
        """Distribution over the played-map sequences (with pickers), most likely first.

        * ``map_pool``: the maps available to the veto; an empty pool means
          ``config.default_map_pool`` (the Active Duty pool).
        * ``first_veto``: the team with the first veto; ``None`` = 50/50 over both orders (the
          known steps, when they name teams, override it).
        * ``known_steps``: already published steps (any subset); they condition the paths. A
          complete veto returns one sequence with probability 1.
        * ``top_k``: at most ``top_k`` sequences are returned and their probabilities are
          **renormalised to sum to 1** after the truncation (also after pruning of paths below
          ``config.prune_probability``). ``None`` returns every sequence (exact; 210-420 for a BO3
          and 2520-5040 for a BO5 on seven maps).
        * ``preserve_marginals`` (default): the truncation keeps the per-slot map marginals
          ``P(slot k is map m)`` of the full distribution exact - the most likely sequences are
          kept and the probability of the truncated tail is re-assigned to the few sequences that
          reproduce its marginals (:mod:`cs2bot.models.veto_truncation`); the per-map and per-slot
          picker marginals are kept exact as well when ``top_k`` allows it (>= 26 / 38 with an
          unknown first veto). This needs ``top_k`` >= 19 (BO3) / 31 (BO5) on a 7-map pool; below
          that, or with ``preserve_marginals=False``,
          the ``top_k`` most likely sequences are simply rescaled (which can move a map's
          probability of being played by 0.2-0.5 when the list is much shorter than the
          distribution).
        * ``as_of``: preference time (recency weighting); must be after the last fitted veto.
          ``None`` = as of the last fitted veto.
        * ``map_advantage``: optional ``(team, opponent, map, as_of) -> logit`` callable (e.g.
          ``MapRatingModel.map_advantage``); called with ``as_of`` (or now when ``as_of`` is None).
        * ``template``: custom veto order for unusual tournament formats. Without it the default
          order is used unless the known steps contradict it but fit a known alternative (a BO1
          whose first two bans come from the same team follows :data:`BO1_TEMPLATE_2_3_1`).
        """
        if top_k is not None and top_k < 1:
            raise ValueError("top_k must be at least 1")
        pool, known, orders, t_pref, base = self._prepare(
            team1_id, team2_id, fmt, map_pool, first_veto, known_steps, as_of, template
        )
        done = self._complete_sequence(fmt, pool, known)
        if done is not None:
            return [done]
        t_adv = ensure_utc(as_of) if as_of is not None else utcnow()
        expu = self._exp_utilities(team1_id, team2_id, pool, t_pref, map_advantage, t_adv)
        totals: dict[_PlayedKey, float] = defaultdict(float)
        for weight, first in orders:
            plan = self._plan(fmt, len(pool), first, known, base)
            for (_, played), p in self._run(plan, pool, expu, known, fmt).items():
                totals[played] += weight * p
        mass = sum(totals.values())
        if mass <= 0.0:
            raise ValueError("the known veto steps are inconsistent with the map pool / format")
        ranked = truncate_sequences(totals, top_k, preserve_marginals=preserve_marginals)
        return [
            MapSequenceProbability(maps=[m for m, _ in played], pickers=[t for _, t in played], probability=p)
            for played, p in ranked
        ]

    def next_step(
        self,
        team1_id: int,
        team2_id: int,
        fmt: MatchFormat,
        map_pool: Sequence[str],
        *,
        first_veto: TeamSlot | None = TeamSlot.TEAM1,
        known_steps: Sequence[VetoStep] = (),
        as_of: datetime | None = None,
        map_advantage: MapAdvantageFn | None = None,
        template: Sequence[VetoTemplateStep] | None = None,
    ) -> StepForecast:
        """Distribution of the first veto step that is not in ``known_steps``.

        Maps that ``known_steps`` assign to a later step are excluded. Raises ``ValueError`` when
        the veto is already complete.
        """
        pool, known, orders, t_pref, base = self._prepare(
            team1_id, team2_id, fmt, map_pool, first_veto, known_steps, as_of, template
        )
        t_adv = ensure_utc(as_of) if as_of is not None else utcnow()
        expu = self._exp_utilities(team1_id, team2_id, pool, t_pref, map_advantage, t_adv)
        probs: dict[str, float] = defaultdict(float)
        actors: set[TeamSlot | None] = set()
        action_seen: VetoAction | None = None
        step_idx: int | None = None
        for weight, first in orders:
            plan = self._plan(fmt, len(pool), first, known, base)
            idx = next((i for i in range(len(plan)) if i not in known), None)
            if idx is None:
                continue
            step_idx = idx
            action, actor = plan[idx]
            action_seen = action
            actors.add(actor)
            # Maps published for a later step cannot be chosen now (the forecast conditions on
            # them being available later, not on the likelihood of those later choices).
            reserved = {m for j, (_, _, m) in known.items() if j > idx}
            for (rem, _), p in self._run(plan, pool, expu, known, fmt, stop=idx).items():
                cands = [m for m in rem if m not in reserved]
                if not cands:
                    continue
                if action is VetoAction.LEFTOVER or actor is None:
                    for m in cands:
                        probs[m] += weight * p / len(cands)
                    continue
                w = expu[(actor, action)]
                denom = sum(w[m] for m in cands)
                for m in cands:
                    probs[m] += weight * p * w[m] / denom
        if step_idx is None or action_seen is None:
            raise ValueError("the veto is already complete")
        total = sum(probs.values())
        if total <= 0.0:
            raise ValueError("the known veto steps are inconsistent with the map pool / format")
        team = next(iter(actors)) if len(actors) == 1 else None
        return StepForecast(
            step=step_idx + 1,
            action=action_seen,
            team=team,
            probabilities={m: p / total for m, p in sorted(probs.items(), key=lambda kv: -kv[1])},
        )

    # ------------------------------------------------------------------------------ persistence

    def to_dict(self) -> dict[str, Any]:
        teams: dict[str, dict[str, Any]] = defaultdict(dict)
        for (tid, action), st in sorted(self._team_stats.items(), key=lambda kv: (kv[0][0], kv[0][1].value)):
            teams[str(tid)][action.value] = st.to_dict()
        return {
            "kind": _FORMAT_KIND,
            "version": _FORMAT_VERSION,
            "config": self._config.model_dump(mode="json"),
            "coefficients": self._coef.model_dump(mode="json"),
            "coefficients_with_advantage": None
            if self._coef_adv is None
            else self._coef_adv.model_dump(mode="json"),
            "last_veto_time": self._last.isoformat() if self._last is not None else None,
            "seen": sorted(self._seen),
            "teams": dict(teams),
            "league": {a.value: self._league[a].to_dict() for a in _ACTIONS},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VetoModel:
        if data.get("kind") != _FORMAT_KIND:
            raise ValueError("not a VetoModel payload")
        if int(data.get("version", 0)) != _FORMAT_VERSION:
            raise ValueError(f"unsupported VetoModel payload version {data.get('version')}")
        model = cls(VetoConfig.model_validate(data["config"]))
        model._coef = VetoCoefficients.model_validate(data["coefficients"])
        raw_adv = data.get("coefficients_with_advantage")
        model._coef_adv = VetoCoefficients.model_validate(raw_adv) if raw_adv else None
        last = data.get("last_veto_time")
        model._last = ensure_utc(datetime.fromisoformat(last)) if last else None
        model._seen = {int(x) for x in data.get("seen", [])}
        for tid, per_action in data.get("teams", {}).items():
            for action_value, raw in per_action.items():
                model._team_stats[(int(tid), VetoAction(action_value))] = _ChoiceStats.from_dict(raw)
        for action_value, raw in data.get("league", {}).items():
            model._league[VetoAction(action_value)] = _ChoiceStats.from_dict(raw)
        return model

    def save(self, path: str | Path) -> Path:
        """Write the model as JSON (parent directories are created). Returns the path."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), separators=(",", ":")), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> VetoModel:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @staticmethod
    def map_slot_distribution(sequences: Sequence[MapSequenceProbability]) -> dict[int, dict[str, float]]:
        """Alias of :func:`map_slot_distribution`."""
        return map_slot_distribution(sequences)

    @staticmethod
    def p_map_played(
        sequences: Sequence[MapSequenceProbability],
        map_name: str,
        *,
        slot_reach: Mapping[int, float] | None = None,
    ) -> float:
        """Alias of :func:`p_map_played`."""
        return p_map_played(sequences, map_name, slot_reach=slot_reach)


# ---------------------------------------------------------------------------------- derived views


def map_slot_distribution(sequences: Sequence[MapSequenceProbability]) -> dict[int, dict[str, float]]:
    """``{slot (1-based): {map: P(the slot is that map)}}`` from a sequence distribution.

    Probabilities are normalised by the total sequence mass, so each slot sums to 1 when every
    sequence reaches that slot (always true for one format). Maps are sorted by probability.
    """
    total = sum(s.probability for s in sequences)
    if total <= 0.0:
        return {}
    acc: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for seq in sequences:
        for k, m in enumerate(seq.maps, start=1):
            acc[k][m] += seq.probability / total
    return {k: dict(sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))) for k, v in sorted(acc.items())}


def p_map_played(
    sequences: Sequence[MapSequenceProbability],
    map_name: str,
    *,
    slot_reach: Mapping[int, float] | None = None,
) -> float:
    """Probability that ``map_name`` is part of the veto's played-map sequence.

    By default this is P(the map is selected), i.e. it assumes every slot is played. Pass
    ``slot_reach`` = ``{slot: P(the series reaches that slot)}`` (e.g. ``{3: 0.45}`` for a BO3
    decider; missing slots count as 1) to get the probability that the map is actually played.
    """
    total = sum(s.probability for s in sequences)
    if total <= 0.0:
        return 0.0
    target = normalize_map_name(map_name)
    out = 0.0
    for seq in sequences:
        for k, m in enumerate(seq.maps, start=1):
            if m == target:
                reach = 1.0 if slot_reach is None else float(slot_reach.get(k, 1.0))
                out += seq.probability * reach
                break
    return out / total
