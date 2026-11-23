"""Cost guard: do not re-run the (expensive) Opus match analysis for unchanged inputs.

The analysis of a match depends on the news shown to the model and on the lineups. Those are
condensed into a fingerprint (:func:`context_fingerprint`); :class:`AnalysisMemo` remembers the
last result per ``(match id, fingerprint)`` in a small thread-safe LRU. A different fingerprint
(new article, lineup change, published veto) is a cache miss, so fresh information is always
analysed immediately; identical inputs are re-analysed only after ``max_age`` has passed.

The in-memory memo is lost on restart. :mod:`cs2bot.llm.pipeline` adds a database check on top
(fingerprint stored in ``matches.extra["llm"]`` next to the persisted flags).

The rationale writer uses a second :class:`AnalysisMemo` keyed by :func:`rationale_fingerprint`
(``MemoEntry.note`` then holds the Markdown text).
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from cs2bot.domain.betting import BetDecision
from cs2bot.domain.context import ContextFlag, NewsItemIn
from cs2bot.domain.match import LineupPlayer, MatchContext
from cs2bot.domain.predictions import MatchPrediction


def _lineup_key(lineup: Sequence[LineupPlayer]) -> list[list[int]]:
    return sorted([player.player_id, int(player.is_standin)] for player in lineup)


def context_fingerprint(ctx: MatchContext, news: Sequence[NewsItemIn]) -> str:
    """Stable hash of everything that should trigger a new analysis when it changes.

    Included: match id, both lineups (player ids + stand-in marks), the known veto, the scheduled
    start, the format, and the set of news URLs (``news_items.url`` is unique, so it serves as the
    news id). The order of the news items and of the players does not matter.
    """
    payload = {
        "match_id": ctx.match_id,
        "team1_id": ctx.team1_id,
        "team2_id": ctx.team2_id,
        "format": ctx.format.value,
        "scheduled_at": ctx.scheduled_at.isoformat(),
        "team1_lineup": _lineup_key(ctx.team1_lineup),
        "team2_lineup": _lineup_key(ctx.team2_lineup),
        "veto": [
            [s.step, s.action.value, s.team.value if s.team else None, s.map_name] for s in ctx.known_veto
        ],
        "news": sorted({item.url for item in news}),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def rationale_fingerprint(
    prediction: MatchPrediction,
    decisions: Sequence[BetDecision],
    ctx: MatchContext,
    *,
    probability_step: float = 0.025,
) -> str:
    """Stable hash of what a reader of the rationale would call "the same picture".

    Included: match, model version, format, both lineups, whether the veto is published (and the
    final map order once it is), the context flags (type, team, veto, shift to two decimals), the
    accepted bets (market + selection) and the band of width ``probability_step`` that team1's match
    win probability falls into. Deliberately *not* included: odds, stakes, expected values and the
    reasons of rejected outcomes - they drift every few minutes without changing the story, and a
    text rewritten on each drift would cost an Opus request per prediction cycle.

    A probability sitting on a band edge can alternate between two fingerprints; both texts are then
    remembered (the memo is keyed by fingerprint), so that costs two requests, not one per cycle.
    """
    step = probability_step if probability_step > 0 else 0.025
    final_order: list[str] | None = None
    if prediction.veto_known and prediction.veto_distribution:
        final_order = list(max(prediction.veto_distribution, key=lambda seq: seq.probability).maps)
    payload = {
        "match_id": prediction.match_id,
        "model_version": prediction.model_version,
        "format": prediction.format.value,
        "team1_lineup": _lineup_key(ctx.team1_lineup),
        "team2_lineup": _lineup_key(ctx.team2_lineup),
        "veto_known": prediction.veto_known,
        "final_order": final_order,
        "p_band": math.floor(prediction.central.p_team1_win / step + 1e-9),
        "flags": sorted(
            [
                flag.flag_type.value,
                flag.team.value if flag.team else "",
                flag.veto,
                round(flag.logit_shift, 2),
            ]
            for flag in prediction.context_flags
        ),
        "accepted": sorted(
            [d.priced.outcome.spec_key, d.priced.outcome.selection] for d in decisions if d.accepted
        ),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class MemoEntry:
    """One remembered analysis. ``ok=False`` records a failed / refused attempt (for back-off)."""

    match_id: int
    fingerprint: str
    analyzed_at: datetime
    ok: bool
    flags: tuple[ContextFlag, ...] = ()
    note: str = ""
    model: str | None = None
    status: str = "ok"
    # True when the failure is tied to the inputs themselves (refusal, truncated or invalid output):
    # sending the same inputs again would most likely fail - and be billed - again.
    content_related: bool = False


class AnalysisMemo:
    """Thread-safe LRU keyed by ``(match_id, fingerprint)``."""

    def __init__(self, max_entries: int = 512) -> None:
        self._max_entries = max(1, int(max_entries))
        self._entries: OrderedDict[tuple[int, str], MemoEntry] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def lookup(self, match_id: int, fingerprint: str) -> MemoEntry | None:
        """Return the stored entry regardless of its age (and refresh its LRU slot)."""
        key = (match_id, fingerprint)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
            return entry

    def get(self, match_id: int, fingerprint: str, *, now: datetime, max_age: timedelta) -> MemoEntry | None:
        """Return the entry if it is younger than ``max_age`` at ``now``.

        An entry "from the future" (``analyzed_at > now``, e.g. after a clock change or when a
        backtest replays an earlier instant) is never returned.
        """
        entry = self.lookup(match_id, fingerprint)
        if entry is None:
            return None
        age = now - entry.analyzed_at
        if age < timedelta(0) or age >= max_age:
            return None
        return entry

    def put(self, entry: MemoEntry) -> None:
        key = (entry.match_id, entry.fingerprint)
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def invalidate(self, match_id: int | None = None) -> None:
        """Forget one match (all fingerprints) or everything."""
        with self._lock:
            if match_id is None:
                self._entries.clear()
                return
            for key in [k for k in self._entries if k[0] == match_id]:
                del self._entries[key]
