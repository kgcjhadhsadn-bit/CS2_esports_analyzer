"""Helpers shared by the datasets / features tests (not a test module)."""

from __future__ import annotations

import math
from collections.abc import Mapping

from sqlalchemy.orm import Session

from cs2bot.testing.synthetic import SynthUniverse, generate_universe, populate_db

_SMALL: dict[tuple[int, int, int], SynthUniverse] = {}


def small_universe(n_teams: int = 6, n_matches: int = 40, seed: int = 5) -> SynthUniverse:
    """A cached small universe for tests that write into their own database."""
    key = (n_teams, n_matches, seed)
    if key not in _SMALL:
        _SMALL[key] = generate_universe(n_teams=n_teams, n_matches=n_matches, seed=seed, days=120)
    return _SMALL[key]


def populate_small(session: Session, *, with_kills: bool = False, **kwargs: int) -> SynthUniverse:
    uni = small_universe(**kwargs)
    populate_db(session, uni, with_rounds=True, with_kills=with_kills)
    session.flush()
    return uni


def same_features(a: Mapping[str, float], b: Mapping[str, float], *, tol: float = 0.0) -> list[str]:
    """Names whose values differ between two feature dicts (NaN equals NaN); [] when identical."""
    bad = list(set(a) ^ set(b))
    for key in set(a) & set(b):
        x, y = a[key], b[key]
        if math.isnan(x) and math.isnan(y):
            continue
        if math.isnan(x) or math.isnan(y) or abs(x - y) > tol:
            bad.append(key)
    return sorted(bad)
