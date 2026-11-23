"""Turn context flags into one bounded adjustment (pure, deterministic, no I/O).

This is the only place where LLM-derived numbers touch the probability model, so the bounds are
enforced here regardless of what any upstream code did:

* each flag's ``logit_shift`` is clipped to ``+-settings.max_flag_logit_shift``,
* then weighted by the flag's ``confidence`` (clamped to [0, 1]),
* expired flags (``expires_at < now``) are ignored,
* the sum is clipped to ``+-settings.max_total_logit_shift``,
* the match is vetoed when any non-expired flag has ``veto=True`` and ``confidence >= 0.6``.

Non-finite numbers (NaN, +-inf) can only come from a corrupted payload; they are treated as "no
information": a non-finite shift contributes 0 and a non-finite confidence counts as 0.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime

from cs2bot.config import LLMSettings
from cs2bot.domain.context import ContextFlag
from cs2bot.llm.tuning import VETO_MIN_CONFIDENCE
from cs2bot.utils.time import ensure_utc


def clip(value: float, bound: float) -> float:
    """Clip ``value`` to ``[-|bound|, +|bound|]``; non-finite values become 0."""
    if not math.isfinite(value):
        return 0.0
    limit = abs(bound) if math.isfinite(bound) else 0.0
    return max(-limit, min(limit, value))


def _confidence(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def is_expired(flag: ContextFlag, now: datetime) -> bool:
    """A flag is expired when its ``expires_at`` lies strictly before ``now``."""
    return flag.expires_at is not None and ensure_utc(flag.expires_at) < now


def active_flags(flags: Iterable[ContextFlag], *, now: datetime) -> list[ContextFlag]:
    """The flags that are not expired at ``now`` (input order preserved)."""
    current = _require_aware(now)
    return [flag for flag in flags if not is_expired(flag, current)]


def flag_contribution(flag: ContextFlag, settings: LLMSettings) -> float:
    """Confidence-weighted, per-flag-clipped logit contribution of one flag (ignores expiry)."""
    return clip(flag.logit_shift, settings.max_flag_logit_shift) * _confidence(flag.confidence)


def combine_flags(
    flags: Iterable[ContextFlag],
    settings: LLMSettings,
    *,
    now: datetime,
    veto_min_confidence: float = VETO_MIN_CONFIDENCE,
) -> tuple[float, bool]:
    """Combine flags into ``(total_logit_shift, veto)``.

    ``total_logit_shift`` is from team1's perspective (positive favours team1) and is guaranteed to
    lie within ``+-settings.max_total_logit_shift``. The result does not depend on the order of the
    flags (the sum is computed with :func:`math.fsum`).

    ``now`` must be timezone-aware (UTC); a naive value is a programming error and raises
    ``ValueError`` rather than silently mis-judging expiry.
    """
    current = _require_aware(now)
    contributions: list[float] = []
    veto = False
    for flag in flags:
        if is_expired(flag, current):
            continue
        contributions.append(flag_contribution(flag, settings))
        if flag.veto and _confidence(flag.confidence) >= veto_min_confidence:
            veto = True
    total = clip(math.fsum(contributions), settings.max_total_logit_shift)
    return (total if total != 0.0 else 0.0), veto  # normalise -0.0


def _require_aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("combine_flags needs a timezone-aware 'now' (use cs2bot.utils.time.utcnow())")
    return ensure_utc(now)
