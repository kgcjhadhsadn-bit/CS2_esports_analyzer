"""A deliberately simple online strength proxy on the round-share scale.

This is **not** the rating model of the bot (that is ``cs2bot.models.ratings``). The feature layer
needs an opponent-strength yardstick of its own so that "form" can be measured relative to who a
team played - without depending on another package and without any look-ahead:

* every team has a rating ``r`` in round-logit units (0 = average) and a small per-map offset;
* the expected round share of team1 on a map is ``sigmoid((r1 + o1m) - (r2 + o2m))``;
* after each map both ratings move by ``k * (observed share - expected share)`` (an Elo update on
  the fraction of rounds won instead of on win/loss, so a 13-3 counts more than a 13-11).

Records are processed in chronological order and the pre-game values are stored with each record,
so the residual ``share - expected`` of a map only uses maps played before it.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from cs2bot.domain.records import MapResultRecord


@dataclass(frozen=True)
class StrengthConfig:
    """Learning rates of the proxy (round-logit units per unit of round-share error)."""

    k: float = 0.5
    #: New teams move faster: ``k_eff = k * (1 + new_team_boost / (1 + games / 5))``.
    new_team_boost: float = 2.0
    map_k: float = 0.25
    #: Map offsets are pulled toward 0 by this factor at every update on that map (shrinkage).
    map_decay: float = 0.97
    #: Map-win probability ~ ``sigmoid(win_scale * round-logit difference)``. For i.i.d. rounds
    #: over ~22 rounds the exact factor is about 4; economy snowballing correlates rounds and
    #: lowers it. Only used for the "wins above expectation" residual.
    win_scale: float = 3.0


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


@dataclass
class StrengthTrace:
    """Per-record values of the proxy, aligned with the (chronological) input order.

    ``pre*`` values are as of just before the map, ``post*`` just after it. ``exp_share1`` /
    ``exp_win1`` are team1's expected round share / map-win probability from the ``pre`` values.
    """

    pre1: np.ndarray
    pre2: np.ndarray
    pre_map1: np.ndarray
    pre_map2: np.ndarray
    post1: np.ndarray
    post2: np.ndarray
    post_map1: np.ndarray
    post_map2: np.ndarray
    exp_share1: np.ndarray
    exp_win1: np.ndarray


def walk_strength(records: Sequence[MapResultRecord], config: StrengthConfig | None = None) -> StrengthTrace:
    """Run the proxy over ``records`` (must already be sorted chronologically)."""
    cfg = config or StrengthConfig()
    n = len(records)
    arrays = [np.zeros(n) for _ in range(10)]
    pre1, pre2, pre_map1, pre_map2, post1, post2, post_map1, post_map2, exp_share1, exp_win1 = arrays
    rating: dict[int, float] = defaultdict(float)
    games: dict[int, int] = defaultdict(int)
    offset: dict[tuple[int, str], float] = defaultdict(float)
    for i, rec in enumerate(records):
        a, b, m = rec.team1_id, rec.team2_id, rec.map_name
        ra, rb = rating[a], rating[b]
        oa, ob = offset[(a, m)], offset[(b, m)]
        diff = (ra + oa) - (rb + ob)
        expected = sigmoid(diff)
        total = rec.team1_rounds + rec.team2_rounds
        share = rec.team1_rounds / total if total > 0 else 0.5
        err = share - expected
        ka = cfg.k * (1.0 + cfg.new_team_boost / (1.0 + games[a] / 5.0))
        kb = cfg.k * (1.0 + cfg.new_team_boost / (1.0 + games[b] / 5.0))
        pre1[i], pre2[i], pre_map1[i], pre_map2[i] = ra, rb, oa, ob
        exp_share1[i] = expected
        exp_win1[i] = sigmoid(cfg.win_scale * diff)
        rating[a] = ra + ka * err
        rating[b] = rb - kb * err
        offset[(a, m)] = cfg.map_decay * oa + cfg.map_k * err
        offset[(b, m)] = cfg.map_decay * ob - cfg.map_k * err
        games[a] += 1
        games[b] += 1
        post1[i], post2[i] = rating[a], rating[b]
        post_map1[i], post_map2[i] = offset[(a, m)], offset[(b, m)]
    return StrengthTrace(
        pre1, pre2, pre_map1, pre_map2, post1, post2, post_map1, post_map2, exp_share1, exp_win1
    )
