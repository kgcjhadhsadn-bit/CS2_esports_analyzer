"""Truncating a map-sequence distribution to ``top_k`` sequences without distorting what is played.

Why this exists
---------------
A veto on a 7-map pool has 210 ordered BO3 outcomes (420 when the first-veto team is unknown) and
2520 / 5040 BO5 outcomes, and the probability is spread widely: on the synthetic universes the 50
most likely BO3 sequences hold only 45-75% of the mass and the 50 most likely BO5 sequences 10-35%.
Cutting the list and rescaling the survivors therefore changes the probability that a given map is
played by up to 0.25 (BO3) / 0.5 (BO5) - and every map market is priced from exactly these numbers.

What is preserved
-----------------
:func:`truncate_sequences` returns at most ``top_k`` sequences whose probabilities sum to 1 and whose
**per-slot map marginals** ``P(slot k is map m)`` equal those of the full distribution (hence also
``P(map m is among the maps of the series)``):

1. the ``top_k - r`` most likely sequences (the head) are always kept with at least their own
   probability, where ``r`` is the rank of the slot/map incidence matrix (19 for a BO3 and 31 for a
   BO5 on seven maps);
2. the probability of all other sequences (the tail) is re-assigned to at most ``r`` sequences so
   that the tail's slot/map marginals are reproduced exactly. This is a linear programme
   ``min sum_s w_s (-log p_s)  s.t.  A w = A_tail p_tail, w >= 0`` over all sequences (``A`` =
   incidence of sequences on (slot, map) cells): it is always feasible (the tail itself is a
   solution), a simplex vertex has at most ``rank(A)`` positive weights, and the objective puts the
   tail's mass on the most likely sequences that can carry it (head sequences included).

The joint distribution of the maps of one series is only approximated (no list of 50 sequences can
represent 420), and the pickers are not part of the constraints: who picked a map in the tail
representatives is approximate when the first-veto team is unknown.

Fallback: when ``top_k`` is smaller than ``r``, when marginal preservation is switched off, or if
the solver fails, the ``top_k`` most likely sequences are kept and rescaled to sum to 1 (plain
truncation). ``top_k=None`` never truncates.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from typing import Final

import numpy as np
import numpy.typing as npt
from scipy import optimize, sparse

from cs2bot.utils.logging import get_logger

log = get_logger(__name__)

__all__ = ["slot_marginal_rank", "truncate_sequences"]

FloatArray = npt.NDArray[np.float64]

_WEIGHT_EPS: Final[float] = 1e-13
_MARGINAL_TOL: Final[float] = 1e-7
_MAX_REFINEMENTS: Final[int] = 3
_REFINE_MAX_SEQUENCES: Final[int] = 6000


def _sort_token(key: Sequence[tuple[str, object]]) -> tuple[tuple[str, str], ...]:
    """Total order on sequences for deterministic tie-breaking (pickers may be ``None``)."""
    return tuple((m, "" if picker is None else str(picker)) for m, picker in key)


def _incidence(keys: Sequence[Sequence[tuple[str, object]]]) -> sparse.csr_matrix:
    """0/1 matrix ``A[cell, sequence]`` with one row per (slot index, map) cell that occurs."""
    cells: dict[tuple[int, str], int] = {}
    rows: list[int] = []
    cols: list[int] = []
    for j, key in enumerate(keys):
        for k, (m, _) in enumerate(key):
            rows.append(cells.setdefault((k, m), len(cells)))
            cols.append(j)
    data = np.ones(len(rows), dtype=np.float64)
    return sparse.csr_matrix((data, (rows, cols)), shape=(len(cells), len(keys)))


def slot_marginal_rank(keys: Sequence[Sequence[tuple[str, object]]]) -> int:
    """Rank of the slot/map incidence matrix of ``keys``: the size of the tail representation.

    For sequences of equal length this is ``#cells - #slots + 1`` (every slot's cells sum to the
    total mass); it is computed numerically so unusual templates are handled too.
    """
    if not keys:
        return 0
    return _rank(_incidence(keys))


def _rank(a: sparse.csr_matrix) -> int:
    """Rank of a 0/1 incidence matrix via its (small, cells x cells) Gram matrix."""
    gram = np.asarray((a @ a.T).todense(), dtype=np.float64)
    return int(np.linalg.matrix_rank(gram))


def _plain[P: Hashable](
    keys: Sequence[tuple[tuple[str, P], ...]], probs: FloatArray, top_k: int
) -> list[tuple[tuple[tuple[str, P], ...], float]]:
    kept = float(probs[:top_k].sum())
    return [(keys[i], float(probs[i]) / kept) for i in range(min(top_k, len(keys)))]


def truncate_sequences[P: Hashable](
    totals: Mapping[tuple[tuple[str, P], ...], float],
    top_k: int | None,
    *,
    preserve_marginals: bool = True,
) -> list[tuple[tuple[tuple[str, P], ...], float]]:
    """Normalise ``totals`` and keep at most ``top_k`` sequences, most likely first.

    ``totals`` maps a played sequence ``((map, picker), ...)`` in playing order to its
    (unnormalised) probability; the picker type is opaque here (``TeamSlot | None`` in the veto model).
    The result always sums to 1. With ``preserve_marginals`` (default) and ``top_k`` at least
    :func:`slot_marginal_rank`, the per-slot map marginals of the result equal those of the full
    distribution (see the module docstring); otherwise the ``top_k`` most likely sequences are
    rescaled. Raises ``ValueError`` when ``totals`` has no positive mass or ``top_k < 1``.
    """
    if top_k is not None and top_k < 1:
        raise ValueError("top_k must be at least 1")
    items = [(key, float(p)) for key, p in totals.items() if p > 0.0]
    mass = sum(p for _, p in items)
    if not items or mass <= 0.0:
        raise ValueError("the sequence distribution has no positive probability")
    items.sort(key=lambda kv: (-kv[1], _sort_token(kv[0])))
    keys = [key for key, _ in items]
    probs: FloatArray = np.asarray([p / mass for _, p in items], dtype=np.float64)
    n = len(keys)
    if top_k is None or n <= top_k:
        return [(keys[i], float(probs[i])) for i in range(n)]
    if not preserve_marginals:
        return _plain(keys, probs, top_k)

    a = _incidence(keys)
    head = top_k - _rank(a)
    if head < 0:
        return _plain(keys, probs, top_k)
    best = _reassign_tail(a, probs, head)
    if best is None or int(np.count_nonzero(best)) > top_k:
        log.warning("veto truncation: tail re-assignment failed; using plain truncation")
        return _plain(keys, probs, top_k)
    # The vertex usually needs fewer than ``rank`` sequences (it re-uses head sequences): spend the
    # unused budget on more exact head sequences. Bounded, and skipped for very large problems.
    for _ in range(_MAX_REFINEMENTS if n <= _REFINE_MAX_SEQUENCES else 0):
        slack = top_k - int(np.count_nonzero(best))
        if slack <= 0:
            break
        head += slack
        candidate = _reassign_tail(a, probs, head)
        if candidate is None or int(np.count_nonzero(candidate)) > top_k:
            break
        best = candidate
    kept = [(keys[int(i)], float(best[i])) for i in np.flatnonzero(best)]
    kept.sort(key=lambda kv: (-kv[1], _sort_token(kv[0])))
    return kept


def _reassign_tail(a: sparse.csr_matrix, probs: FloatArray, head: int) -> FloatArray | None:
    """Probabilities with ``probs[:head]`` kept and the tail's slot/map marginals re-assigned.

    Solves the linear programme of the module docstring with the dual simplex (a vertex solution,
    hence sparse). Returns the normalised probability vector over all sequences (mostly zeros), or
    ``None`` if the solver fails or the result does not reproduce the marginals.
    """
    target: FloatArray = np.asarray(a @ probs, dtype=np.float64)
    tail_marginals: FloatArray = np.asarray(a[:, head:] @ probs[head:], dtype=np.float64)
    cost = -np.log(np.maximum(probs, 1e-300))
    try:
        res = optimize.linprog(cost, A_eq=a, b_eq=tail_marginals, bounds=(0.0, None), method="highs-ds")
    except ValueError:  # pragma: no cover - defensive: the problem is feasible by construction
        return None
    if not res.success or res.x is None:  # pragma: no cover - feasible by construction
        return None
    extra: FloatArray = np.asarray(res.x, dtype=np.float64)
    extra[extra < _WEIGHT_EPS] = 0.0
    out: FloatArray = probs.copy()
    out[head:] = 0.0
    out += extra
    total = float(out.sum())
    if total <= 0.0:  # pragma: no cover - the head or the tail always carries mass
        return None
    out /= total
    worst = float(np.max(np.abs(np.asarray(a @ out, dtype=np.float64) - target)))
    return out if worst <= _MARGINAL_TOL else None
