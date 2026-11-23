"""Series (BO1 / BO2 / BO3 / BO5) combinatorics given independent per-map win probabilities.

Maps are independent given the strength scenario. Map ``k`` of a series is played iff the series is
still undecided after maps ``1..k-1`` (BO1: map 1 always; BO2: both maps always, 1-1 is a draw;
BO3: map 3 only at 1-1; BO5: map 4 unless 3-0/0-3, map 5 only at 2-2). Whether map ``k`` is played
depends only on earlier maps, so it is independent of map ``k``'s own outcome.

The dynamic programme runs over series states ``(team1 maps, team2 maps)``; terminal states are the
final map scores. :func:`series_sum_pmf` carries a pmf of an additive per-map quantity (player kills)
through the same programme, conditioning each map's contribution on that map's winner, which keeps
the correlation between kills and series length exact.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from cs2bot.domain.enums import MatchFormat
from cs2bot.models.sim_stats import FloatArray, pad_add


@dataclass(frozen=True)
class SeriesResult:
    """Outcome distribution of one series given per-map probabilities."""

    p_played: tuple[float, ...]  # P(map k is played), k = 0..n_maps-1
    final: dict[tuple[int, int], float]  # final map score (team1, team2) -> probability

    @property
    def p_team1_win(self) -> float:
        return sum(p for (a, b), p in self.final.items() if a > b)

    @property
    def p_draw(self) -> float:
        return sum(p for (a, b), p in self.final.items() if a == b)

    def score_pmf(self) -> dict[str, float]:
        return {
            f"{a}-{b}": p for (a, b), p in sorted(self.final.items(), key=lambda kv: (-kv[0][0], kv[0][1]))
        }


def _decided(state: tuple[int, int], need: int | None) -> bool:
    return need is not None and max(state) >= need


def series_outcome(fmt: MatchFormat, p_maps: Sequence[float]) -> SeriesResult:
    """P(each map is played) and the final map-score distribution for one ordered map list."""
    n = fmt.n_maps
    if len(p_maps) < n:
        raise ValueError(f"{fmt.value} needs {n} map probabilities, got {len(p_maps)}")
    need = fmt.maps_to_win
    states: dict[tuple[int, int], float] = {(0, 0): 1.0}
    final: dict[tuple[int, int], float] = {}
    played: list[float] = []
    for k in range(n):
        p = float(p_maps[k])
        played.append(sum(states.values()))
        nxt: dict[tuple[int, int], float] = {}
        for (a, b), pr in states.items():
            for state, q in (((a + 1, b), pr * p), ((a, b + 1), pr * (1.0 - p))):
                if q > 0.0:
                    nxt[state] = nxt.get(state, 0.0) + q
        states = {}
        for state, pr in nxt.items():
            if _decided(state, need):
                final[state] = final.get(state, 0.0) + pr
            else:
                states[state] = pr
    for state, pr in states.items():  # BO2 (no early decision) ends here
        final[state] = final.get(state, 0.0) + pr
    return SeriesResult(p_played=tuple(played), final=final)


def series_sum_pmf(
    fmt: MatchFormat,
    p_maps: Sequence[float],
    pmf_if_team1_wins: Sequence[FloatArray],
    pmf_if_team2_wins: Sequence[FloatArray],
) -> FloatArray:
    """Distribution of ``sum over played maps of X_k``.

    ``X_k`` given that team1 (team2) wins map ``k`` has pmf ``pmf_if_team1_wins[k]``
    (``pmf_if_team2_wins[k]``); maps are independent given the scenario.
    """
    n = fmt.n_maps
    need = fmt.maps_to_win
    states: dict[tuple[int, int], FloatArray] = {(0, 0): np.array([1.0])}
    total = np.zeros(1, dtype=np.float64)
    for k in range(n):
        p = float(p_maps[k])
        nxt: dict[tuple[int, int], FloatArray] = {}
        for (a, b), acc in states.items():
            for state, q, pmf in (
                ((a + 1, b), p, pmf_if_team1_wins[k]),
                ((a, b + 1), 1.0 - p, pmf_if_team2_wins[k]),
            ):
                if q <= 0.0:
                    continue
                contrib = q * np.convolve(acc, pmf)
                nxt[state] = pad_add(nxt[state], contrib) if state in nxt else contrib
        states = {}
        for state, acc in nxt.items():
            if _decided(state, need):
                total = pad_add(total, acc)
            else:
                states[state] = acc
    for acc in states.values():
        total = pad_add(total, acc)
    s = float(total.sum())
    return total / s if s > 0 else np.array([1.0])


def series_prefix_sum_pmf(
    fmt: MatchFormat,
    p_maps: Sequence[float],
    pmf_if_team1_wins: Sequence[FloatArray],
    pmf_if_team2_wins: Sequence[FloatArray],
    upto: int,
) -> tuple[float, FloatArray]:
    """``(P(map upto is played), pmf of X_1 + ... + X_upto given that map upto is played)``.

    ``upto`` is 1-based. Map ``upto`` is played iff the series is undecided after maps ``1..upto-1``,
    so the condition only involves earlier maps: their contributions are carried through the series
    programme with winner-conditional pmfs (e.g. in a BO3, "maps 1-3" given that map 3 is played means
    each team won one of maps 1-2), and map ``upto`` itself enters with its unconditional pmf. When
    the map can never be played the probability is 0 and the returned pmf is ``[1.0]``.
    """
    n = fmt.n_maps
    if not 1 <= upto <= n:
        raise ValueError(f"upto must be in 1..{n} for a {fmt.value}, got {upto}")
    need = fmt.maps_to_win
    states: dict[tuple[int, int], FloatArray] = {(0, 0): np.array([1.0])}
    for k in range(upto - 1):
        p = float(p_maps[k])
        nxt: dict[tuple[int, int], FloatArray] = {}
        for (a, b), acc in states.items():
            for state, q, pmf in (
                ((a + 1, b), p, pmf_if_team1_wins[k]),
                ((a, b + 1), 1.0 - p, pmf_if_team2_wins[k]),
            ):
                if q <= 0.0:
                    continue
                contrib = q * np.convolve(acc, pmf)
                nxt[state] = pad_add(nxt[state], contrib) if state in nxt else contrib
        states = {state: acc for state, acc in nxt.items() if not _decided(state, need)}
    acc_total = np.zeros(1, dtype=np.float64)
    for acc in states.values():
        acc_total = pad_add(acc_total, acc)
    mass = float(acc_total.sum())
    if mass <= 0.0:
        return 0.0, np.array([1.0])
    p_last = float(p_maps[upto - 1])
    last = pad_add(
        p_last * np.asarray(pmf_if_team1_wins[upto - 1]),
        np.asarray(pmf_if_team2_wins[upto - 1]),
        1.0 - p_last,
    )
    out = np.convolve(acc_total / mass, last)
    return mass, out / out.sum()


def played_mask(fmt: MatchFormat, team1_won: np.ndarray) -> np.ndarray:
    """Vectorised "slot played" mask for simulated series.

    ``team1_won`` has shape ``(n_maps, n_series)`` (outcome of each slot *if* played); returns a bool
    array of the same shape: slot k is played iff the series was undecided after slots 0..k-1.
    """
    n = fmt.n_maps
    need = fmt.maps_to_win
    out = np.ones(team1_won.shape, dtype=bool)
    if need is None:
        return out
    w1 = np.zeros(team1_won.shape[1], dtype=np.int16)
    w2 = np.zeros(team1_won.shape[1], dtype=np.int16)
    for k in range(n):
        out[k] = (w1 < need) & (w2 < need)
        w1 += out[k] & team1_won[k]
        w2 += out[k] & ~team1_won[k]
    return out


__all__ = ["SeriesResult", "played_mask", "series_outcome", "series_prefix_sum_pmf", "series_sum_pmf"]
