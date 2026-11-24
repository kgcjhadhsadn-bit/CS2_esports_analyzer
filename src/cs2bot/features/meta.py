"""Meta layer: which maps are being played, which patch we are on, and whether a team's form broke.

* :func:`detect_active_map_pool` - the Active Duty pool as it is actually played in recent results.
* :class:`PatchTimeline` (``config/patches.yaml``) - :func:`days_since_last_patch`, :func:`patch_id`.
* :func:`bocpd` - Bayesian online change point detection (Adams & MacKay 2007, "Bayesian Online
  Changepoint Detection", arXiv:0710.3742) with a Normal-Inverse-Gamma model, and
  :class:`RegimeDetector` which runs it on every team's opponent-adjusted round-share residuals to
  answer :meth:`~RegimeDetector.recent_regime_break` and :meth:`~RegimeDetector.form_trend`.

Everything is leakage-safe: results are indexed by time and a query at ``as_of`` only reads values
computed from maps played strictly before ``as_of`` (BOCPD is an online filter, so the value stored
at map ``i`` never depends on later maps).
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, field_validator
from scipy.special import gammaln

from cs2bot.domain.records import MapResultRecord
from cs2bot.features.strength import StrengthConfig, walk_strength
from cs2bot.utils.names import normalize_map_name
from cs2bot.utils.paths import resolve_path
from cs2bot.utils.time import ensure_utc

DEFAULT_PATCHES_FILE: Final[Path] = Path("config/patches.yaml")
_SECONDS_PER_DAY: Final[float] = 86_400.0

# --------------------------------------------------------------------------------------- map pool


class _PlayedMap(Protocol):
    @property
    def map_name(self) -> str: ...

    @property
    def played_at(self) -> dt.datetime: ...


def detect_active_map_pool(
    map_results: Iterable[_PlayedMap],
    as_of: dt.datetime,
    *,
    window_days: int = 60,
    min_share: float = 0.04,
    recent_days: int = 14,
    min_maps: int = 30,
    pool_size: int | None = 7,
) -> list[str]:
    """Maps that make up the pool currently played, from results in ``[as_of - window, as_of)``.

    A map is in the pool when it accounts for at least ``min_share`` of the maps played in the
    window (with seven maps a fair share is ~14 %, so 4 % tolerates unpopular maps but rejects
    showmatch / lower-tier noise). Pool rotations are handled with a short recent window: once the
    last ``recent_days`` hold at least ``min_maps`` maps, a candidate that has (almost) vanished
    there (< ``min_share / 2``) is dropped - it was rotated out - and a map that is clearly played
    there (>= ``2 * min_share``) is added even if its share of the long window is still small - it
    was just rotated in. If more than ``pool_size`` maps remain, the most played recent ones win.

    Returns a sorted list; ``[]`` when the window holds fewer than ``min_maps`` maps (no evidence -
    the caller falls back to configuration). Only records strictly before ``as_of`` are used.
    """
    end = ensure_utc(as_of)
    start = end - dt.timedelta(days=window_days)
    recent_start = end - dt.timedelta(days=recent_days)
    counts: Counter[str] = Counter()
    recent: Counter[str] = Counter()
    for rec in map_results:
        t = ensure_utc(rec.played_at)
        if not start <= t < end:
            continue
        name = normalize_map_name(rec.map_name)
        counts[name] += 1
        if t >= recent_start:
            recent[name] += 1
    total = sum(counts.values())
    if total < min_maps:
        return []
    share = {m: c / total for m, c in counts.items()}
    pool = {m for m, s in share.items() if s >= min_share}
    recent_total = sum(recent.values())
    use_recent = recent_total >= min_maps
    recent_share = {m: c / recent_total for m, c in recent.items()} if use_recent else {}
    if use_recent:
        pool = {m for m in pool if recent_share.get(m, 0.0) >= min_share / 2}
        pool |= {m for m, s in recent_share.items() if s >= 2 * min_share}
    if pool_size is not None and len(pool) > pool_size:
        ranked = sorted(
            pool, key=lambda m: (recent_share.get(m, 0.0) if use_recent else share[m], share.get(m, 0.0), m)
        )
        pool = set(ranked[-pool_size:])
    return sorted(pool)


# --------------------------------------------------------------------------------- patch timeline

Impact = Literal["minor", "moderate", "major"]
_IMPACT_LEVEL: Final[dict[str, int]] = {"minor": 0, "moderate": 1, "major": 2}


class Patch(BaseModel):
    """One entry of ``config/patches.yaml`` (field meanings are documented in that file)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    date: dt.date
    kind: str = "mechanics"
    impact: Impact = "moderate"
    date_verified: bool = False
    title: str = ""
    notes: str = ""
    map_pool: tuple[str, ...] | None = None

    @field_validator("map_pool", mode="before")
    @classmethod
    def _normalise_pool(cls, value: Any) -> Any:
        if value is None:
            return None
        return tuple(sorted(normalize_map_name(str(m)) for m in value))

    @property
    def effective_at(self) -> dt.datetime:
        """00:00 UTC of the release day (see the note on dates in the YAML header)."""
        return dt.datetime(self.date.year, self.date.month, self.date.day, tzinfo=dt.UTC)


class PatchTimeline:
    """Chronological list of notable CS2 updates with as-of lookups."""

    def __init__(self, patches: Iterable[Patch]) -> None:
        self.patches: tuple[Patch, ...] = tuple(sorted(patches, key=lambda p: (p.date, p.id)))
        ids = [p.id for p in self.patches]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate patch ids in the patch timeline")

    @classmethod
    def from_yaml(cls, path: Path | str | None = None) -> PatchTimeline:
        """Load ``path`` (default ``config/patches.yaml``, resolved against CWD then the repo root).

        A malformed entry raises ``ValueError`` naming the entry: this is a hand-edited config file
        and a silent skip would shift every ``patch_index`` after it.
        """
        resolved = resolve_path(path or DEFAULT_PATCHES_FILE)
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        entries = raw.get("patches") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            raise ValueError(f"{resolved}: expected a top-level 'patches' list")
        patches: list[Patch] = []
        for i, entry in enumerate(entries):
            try:
                patches.append(Patch.model_validate(entry))
            except ValueError as exc:
                raise ValueError(f"{resolved}: invalid patch entry #{i + 1}: {exc}") from exc
        return cls(patches)

    def __len__(self) -> int:
        return len(self.patches)

    def last_patch(
        self,
        as_of: dt.datetime,
        *,
        min_impact: Impact = "minor",
        kinds: Sequence[str] | None = None,
    ) -> Patch | None:
        """The latest patch released at or before ``as_of`` that matches the filters."""
        t = ensure_utc(as_of)
        level = _IMPACT_LEVEL[min_impact]
        found: Patch | None = None
        for p in self.patches:
            if p.effective_at > t:
                break
            if _IMPACT_LEVEL[p.impact] >= level and (kinds is None or p.kind in kinds):
                found = p
        return found

    def days_since_last_patch(
        self,
        as_of: dt.datetime,
        *,
        min_impact: Impact = "moderate",
        kinds: Sequence[str] | None = None,
    ) -> float:
        """Days between the last matching patch and ``as_of`` (NaN before the first one)."""
        p = self.last_patch(as_of, min_impact=min_impact, kinds=kinds)
        if p is None:
            return math.nan
        return (ensure_utc(as_of) - p.effective_at).total_seconds() / _SECONDS_PER_DAY

    def days_since_pool_change(self, as_of: dt.datetime) -> float:
        """Days since the last Active Duty change: an entry of kind ``map_pool`` that lists the new
        pool. Entries of that kind without a ``map_pool`` (a map added to the game, not to the
        competitive pool) are not pool changes. NaN before the first change."""
        t = ensure_utc(as_of)
        found: Patch | None = None
        for p in self.patches:
            if p.effective_at > t:
                break
            if p.kind == "map_pool" and p.map_pool:
                found = p
        if found is None:
            return math.nan
        return (t - found.effective_at).total_seconds() / _SECONDS_PER_DAY

    def patch_id(self, as_of: dt.datetime, *, min_impact: Impact = "minor") -> str | None:
        p = self.last_patch(as_of, min_impact=min_impact)
        return p.id if p is not None else None

    def patch_index(self, as_of: dt.datetime, *, min_impact: Impact = "minor") -> int:
        """Number of matching patches released at or before ``as_of`` minus one (-1 = none yet)."""
        t = ensure_utc(as_of)
        level = _IMPACT_LEVEL[min_impact]
        return sum(1 for p in self.patches if p.effective_at <= t and _IMPACT_LEVEL[p.impact] >= level) - 1

    def map_pool_at(self, as_of: dt.datetime) -> list[str] | None:
        """The Active Duty pool according to the timeline (calendar truth, not tournament usage)."""
        t = ensure_utc(as_of)
        pool: tuple[str, ...] | None = None
        for p in self.patches:
            if p.effective_at > t:
                break
            if p.map_pool:
                pool = p.map_pool
        return list(pool) if pool is not None else None


@lru_cache(maxsize=8)
def _cached_timeline(resolved: str, mtime_ns: int) -> PatchTimeline:
    return PatchTimeline.from_yaml(resolved)


def load_patch_timeline(path: Path | str | None = None) -> PatchTimeline:
    """Cached :meth:`PatchTimeline.from_yaml` (re-read automatically when the file changes)."""
    resolved = resolve_path(path or DEFAULT_PATCHES_FILE)
    return _cached_timeline(str(resolved), resolved.stat().st_mtime_ns)


def days_since_last_patch(
    as_of: dt.datetime, *, min_impact: Impact = "moderate", timeline: PatchTimeline | None = None
) -> float:
    """Days since the last patch of at least ``min_impact`` (default timeline: config/patches.yaml)."""
    return (timeline or load_patch_timeline()).days_since_last_patch(as_of, min_impact=min_impact)


def patch_id(as_of: dt.datetime, *, timeline: PatchTimeline | None = None) -> str | None:
    """Id of the latest listed patch at ``as_of`` (``None`` before the CS2 release)."""
    return (timeline or load_patch_timeline()).patch_id(as_of)


# ------------------------------------------------------------------------ change-point detection


@dataclass(frozen=True)
class BOCPDConfig:
    """Bayesian online change point detection settings.

    Observations in a regime are i.i.d. Normal with unknown mean and variance; the conjugate prior
    is Normal-Inverse-Gamma(``mu0, kappa0, alpha0, beta0``), which gives a Student-t predictive. The
    defaults suit round-share residuals: mean 0, per-map standard deviation about 0.11
    (``beta0 / (alpha0 - 1) = 0.012``). The hazard is constant: a regime lasts ``hazard_lambda``
    observations on average. Run lengths above ``max_run_length`` are merged into the last bin.

    ``hazard_lambda = 80`` and ``kappa0 = 0.25`` were picked on simulated residual series (sd 0.11):
    a shift of 0.10 in round share is flagged (P >= 0.5) within 20 maps in ~70 % of the runs, while
    a series without any change spends about 1 % of the time above 0.5. A shorter expected regime
    reacts faster but raises false alarms sharply (lambda 40: 3-4 % of the time).
    """

    hazard_lambda: float = 80.0
    mu0: float = 0.0
    kappa0: float = 0.25
    alpha0: float = 3.0
    beta0: float = 0.024
    max_run_length: int = 250
    #: "Recent" = the current regime is at most this many observations old.
    recent_window: int = 12


@dataclass(frozen=True)
class BOCPDResult:
    """Filtered quantities after each observation (index ``i`` uses observations ``0..i`` only).

    * ``map_run_length[i]``: most probable number of observations in the current regime.
    * ``p_recent_change[i]``: P(the current regime started within the last ``recent_window``
      observations).
    * ``regime_mean[i]``: posterior mean of the current regime's level (run-length averaged).
    * ``change_points``: indices where a new regime was detected to start (retrospective reading of
      the MAP run length; for diagnostics and tests, not for features).
    """

    map_run_length: np.ndarray
    p_recent_change: np.ndarray
    regime_mean: np.ndarray
    change_points: tuple[int, ...]


def bocpd(values: Sequence[float] | np.ndarray, config: BOCPDConfig | None = None) -> BOCPDResult:
    """Run-length filtering of Adams & MacKay (2007) for a univariate series.

    For each new observation ``x``: evaluate the Student-t predictive of ``x`` under every current
    run length, split the mass into "regime continues" (run length + 1, weight ``1 - H``) and
    "regime ended" (run length 0, weight ``H = 1 / hazard_lambda``), normalise, then update the
    sufficient statistics of every run length. A run length of ``k`` after observation ``i`` means
    the current regime consists of observations ``i - k + 1 .. i``.
    """
    cfg = config or BOCPDConfig()
    x = np.asarray(values, dtype=float)
    n = len(x)
    hazard = 1.0 / cfg.hazard_lambda
    run_probs = np.array([1.0])
    mu = np.array([cfg.mu0])
    kappa = np.array([cfg.kappa0])
    alpha = np.array([cfg.alpha0])
    beta = np.array([cfg.beta0])
    map_rl = np.zeros(n, dtype=int)
    p_recent = np.zeros(n)
    regime_mean = np.zeros(n)
    for i in range(n):
        xi = x[i]
        if not math.isfinite(xi):  # missing observation: carry the previous state forward
            map_rl[i] = map_rl[i - 1] if i else 0
            p_recent[i] = p_recent[i - 1] if i else 1.0
            regime_mean[i] = regime_mean[i - 1] if i else cfg.mu0
            continue
        df = 2.0 * alpha
        scale2 = beta * (kappa + 1.0) / (alpha * kappa)
        z2 = (xi - mu) ** 2 / scale2
        log_pred = (
            gammaln((df + 1.0) / 2.0)
            - gammaln(df / 2.0)
            - 0.5 * np.log(df * math.pi * scale2)
            - (df + 1.0) / 2.0 * np.log1p(z2 / df)
        )
        joint = run_probs * np.exp(log_pred - log_pred.max())
        new_probs = np.empty(len(joint) + 1)
        new_probs[0] = joint.sum() * hazard
        new_probs[1:] = joint * (1.0 - hazard)
        new_mu = np.concatenate(([cfg.mu0], (kappa * mu + xi) / (kappa + 1.0)))
        new_kappa = np.concatenate(([cfg.kappa0], kappa + 1.0))
        new_alpha = np.concatenate(([cfg.alpha0], alpha + 0.5))
        new_beta = np.concatenate(([cfg.beta0], beta + kappa * (xi - mu) ** 2 / (2.0 * (kappa + 1.0))))
        if len(new_probs) > cfg.max_run_length + 1:  # merge the tail into the last bin
            new_probs[cfg.max_run_length] += new_probs[cfg.max_run_length + 1 :].sum()
            new_probs = new_probs[: cfg.max_run_length + 1]
            new_mu, new_kappa = new_mu[: cfg.max_run_length + 1], new_kappa[: cfg.max_run_length + 1]
            new_alpha, new_beta = new_alpha[: cfg.max_run_length + 1], new_beta[: cfg.max_run_length + 1]
        total = new_probs.sum()
        run_probs = new_probs / total if total > 0 else np.full(len(new_probs), 1.0 / len(new_probs))
        mu, kappa, alpha, beta = new_mu, new_kappa, new_alpha, new_beta
        map_rl[i] = int(np.argmax(run_probs))
        p_recent[i] = float(run_probs[: cfg.recent_window + 1].sum())
        regime_mean[i] = float(np.dot(run_probs, mu))
    return BOCPDResult(map_rl, p_recent, regime_mean, _change_points(map_rl))


def _change_points(map_run_length: np.ndarray, *, min_drop: int = 5, min_regime: int = 3) -> tuple[int, ...]:
    """Regime start indices read off the MAP run length.

    When the MAP run length falls by at least ``min_drop`` between consecutive observations, the
    filter has decided that a new regime began ``run length`` observations ago. The start index is
    recorded once the new regime is at least ``min_regime`` observations old (single outliers make
    the MAP run length dip for one step and are ignored this way).
    """
    points: list[int] = []
    n = len(map_run_length)
    i = 1
    while i < n:
        prev, cur = int(map_run_length[i - 1]), int(map_run_length[i])
        if prev - cur >= min_drop:
            start = i - cur + 1
            j = i
            while j + 1 < n and int(map_run_length[j + 1]) == int(map_run_length[j]) + 1:
                j += 1
            if int(map_run_length[j]) >= min_regime and (not points or start > points[-1]):
                points.append(max(start, 0))
            i = j + 1
        else:
            i += 1
    return tuple(points)


@dataclass(frozen=True)
class RegimeConfig:
    """How :class:`RegimeDetector` turns BOCPD output into a "form broke recently" signal."""

    bocpd: BOCPDConfig = BOCPDConfig()
    #: P(recent change) at or above this counts as a detected break.
    threshold: float = 0.5
    #: A team's first maps are dropped from its series: while the strength proxy is still learning
    #: how good a new team is, its residuals are one-sided (a strong newcomer beats expectations
    #: every time). Left in, that transient is detected as a "regime" whose end looks like a slump
    #: for every strong team and a surge for every weak one.
    warmup_maps: int = 15
    #: Teams with fewer maps than this (after the warm-up) never report a break.
    min_maps: int = 20
    #: Maps of the previous regime used as the baseline for the size of the shift.
    baseline_maps: int = 40
    #: The current regime must be at least this many maps old before its level is compared with
    #: the baseline. A regime of one or two maps is just the last result: the filter resets on a
    #: surprising map, and what is surprising differs by team strength (a favourite's surprises
    #: are losses), so very short regimes would make ``shift`` a biased, noisy inverse of strength.
    min_regime_maps: int = 4
    #: Half-lives (in maps) of the fast and slow moving averages behind ``form_trend``.
    trend_fast_halflife: float = 6.0
    trend_slow_halflife: float = 30.0
    trend_min_maps: int = 8


@dataclass(frozen=True)
class RegimeBreak:
    """Answer of :meth:`RegimeDetector.recent_regime_break`.

    ``probability`` = P(the team's current performance regime started within the last
    ``recent_window`` maps); ``shift`` = level of the current regime minus the level of the maps
    before it, in round-share units (+0.05 = wins 5 percentage points more rounds than expected
    against the same opponents; NaN while the current regime is younger than
    ``RegimeConfig.min_regime_maps``); ``maps_in_regime`` = most probable age of the regime in maps.
    All NaN / 0 with ``detected=False`` when the team has too little history.
    """

    detected: bool
    probability: float
    shift: float
    maps_in_regime: int


_NO_BREAK: Final[RegimeBreak] = RegimeBreak(False, math.nan, math.nan, 0)


@dataclass
class _TeamRegime:
    result: BOCPDResult
    cum: np.ndarray  # prefix sums of the residuals (length n + 1)
    trend: np.ndarray


def _ewma(values: np.ndarray, halflife: float) -> np.ndarray:
    """Bias-corrected exponentially weighted mean (equals the plain mean for the first samples)."""
    decay = 0.5 ** (1.0 / halflife)
    out = np.zeros(len(values))
    num = den = 0.0
    for i, v in enumerate(values):
        num = decay * num + v
        den = decay * den + 1.0
        out[i] = num / den
    return out


class RegimeDetector:
    """Change-point detection on each team's performance series, queried by ``(team, as_of)``.

    The series is the opponent-adjusted round-share residual of every map the team played (observed
    round share minus the share expected from the strength proxy, see ``features.strength``).
    Detection runs lazily per team and is cached.
    """

    def __init__(
        self,
        series: Mapping[int, tuple[np.ndarray, np.ndarray, np.ndarray]],
        config: RegimeConfig | None = None,
    ) -> None:
        """``series``: team id -> (unix times ascending, residuals, match ids), equal lengths.

        Pass each team's full history: the first ``config.warmup_maps`` entries are dropped here.
        """
        self.config = config or RegimeConfig()
        skip = max(0, self.config.warmup_maps)
        self._series = {
            team: (np.asarray(t)[skip:], np.asarray(v, dtype=float)[skip:], np.asarray(m)[skip:])
            for team, (t, v, m) in series.items()
        }
        self._cache: dict[int, _TeamRegime] = {}

    @classmethod
    def from_map_results(
        cls,
        map_results: Iterable[MapResultRecord],
        *,
        strength_config: StrengthConfig | None = None,
        config: RegimeConfig | None = None,
    ) -> RegimeDetector:
        records = sorted(map_results, key=lambda r: (ensure_utc(r.played_at), r.match_id, r.map_number))
        trace = walk_strength(records, strength_config)
        per_team: dict[int, list[tuple[float, float, int]]] = defaultdict(list)
        for i, rec in enumerate(records):
            total = rec.team1_rounds + rec.team2_rounds
            if total <= 0:
                continue
            t = ensure_utc(rec.played_at).timestamp()
            resid = rec.team1_rounds / total - float(trace.exp_share1[i])
            per_team[rec.team1_id].append((t, resid, rec.match_id))
            per_team[rec.team2_id].append((t, -resid, rec.match_id))
        return cls(
            {
                team: (
                    np.array([r[0] for r in rows]),
                    np.array([r[1] for r in rows]),
                    np.array([r[2] for r in rows], dtype=np.int64),
                )
                for team, rows in per_team.items()
            },
            config,
        )

    def _team(self, team_id: int) -> _TeamRegime | None:
        if team_id not in self._series:
            return None
        cached = self._cache.get(team_id)
        if cached is None:
            _, values, _ = self._series[team_id]
            fast = _ewma(values, self.config.trend_fast_halflife)
            slow = _ewma(values, self.config.trend_slow_halflife)
            cached = _TeamRegime(
                result=bocpd(values, self.config.bocpd),
                cum=np.concatenate(([0.0], np.cumsum(values))),
                trend=fast - slow,
            )
            self._cache[team_id] = cached
        return cached

    def _last_index(self, team_id: int, as_of: dt.datetime, exclude_match_id: int | None) -> int:
        """Index of the team's last map strictly before ``as_of`` (-1 if none)."""
        times, _, match_ids = self._series[team_id]
        idx = int(np.searchsorted(times, ensure_utc(as_of).timestamp(), side="left"))
        while exclude_match_id is not None and idx > 0 and int(match_ids[idx - 1]) == exclude_match_id:
            idx -= 1
        return idx - 1

    def recent_regime_break(
        self, team_id: int, as_of: dt.datetime, *, exclude_match_id: int | None = None
    ) -> RegimeBreak:
        """Did the team's level change recently, judged only from maps strictly before ``as_of``?"""
        team = self._team(team_id)
        if team is None:
            return _NO_BREAK
        i = self._last_index(team_id, as_of, exclude_match_id)
        if i < 0:
            return _NO_BREAK
        run = int(team.result.map_run_length[i])
        start = max(i - run + 1, 0)
        base_lo = max(start - self.config.baseline_maps, 0)
        if start > base_lo and i + 1 - start >= self.config.min_regime_maps:
            baseline = float(team.cum[start] - team.cum[base_lo]) / (start - base_lo)
            current = float(team.cum[i + 1] - team.cum[start]) / max(i + 1 - start, 1)
            shift = current - baseline
        else:
            shift = math.nan
        probability = float(team.result.p_recent_change[i])
        enough = i + 1 >= self.config.min_maps
        return RegimeBreak(
            detected=enough and probability >= self.config.threshold,
            probability=probability if enough else math.nan,
            shift=shift if enough else math.nan,
            maps_in_regime=run,
        )

    def form_trend(self, team_id: int, as_of: dt.datetime, *, exclude_match_id: int | None = None) -> float:
        """Fast minus slow moving average of the residuals (round-share units; > 0 = improving).

        NaN until the team has ``trend_min_maps`` maps before ``as_of``.
        """
        team = self._team(team_id)
        if team is None:
            return math.nan
        i = self._last_index(team_id, as_of, exclude_match_id)
        if i + 1 < self.config.trend_min_maps:
            return math.nan
        return float(team.trend[i])


def recent_regime_break(team_id: int, as_of: dt.datetime, *, detector: RegimeDetector) -> RegimeBreak:
    """Module-level form of :meth:`RegimeDetector.recent_regime_break`."""
    return detector.recent_regime_break(team_id, as_of)


def form_trend(team_id: int, as_of: dt.datetime, *, detector: RegimeDetector) -> float:
    """Module-level form of :meth:`RegimeDetector.form_trend`."""
    return detector.form_trend(team_id, as_of)
