"""Random complete series and the *empirical* prediction that describes them (not a test module).

The pricing / settlement consistency test needs a ``MatchPrediction`` and a population of finished
series that follow exactly its distributions. Instead of sampling from a prediction (which only allows
a statistical comparison) the prediction is built from the sample: :func:`simulate_series` plays whole
series round by round (MR12, MR3 overtimes, kills for ten players) and :func:`empirical_core` turns a
list of such series into the ``PredictionCore`` a perfect simulator would publish for that population -
every slot distribution conditional on the slot being played, kill pmfs per scope, top-fragger tables
with dead heats split evenly, race-to-N with the "neither team gets there" probability. With equal
weights, ``price_outcome(...).p_win`` must then equal the share of series that ``resolve_outcome``
settles as WON *exactly* (float noise aside), and the same for PUSH and VOID. Only head-to-head kill
markets differ: pricing treats the two players as independent, the sample does not.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

import numpy as np

from cs2bot.domain.enums import MatchFormat, TeamSlot
from cs2bot.domain.predictions import MapSlotPrediction, MatchPrediction, PlayerProjection, PredictionCore
from cs2bot.domain.results import MapActual, MatchActuals

TEAM1_IDS = (101, 102, 103, 104, 105)
TEAM2_IDS = (201, 202, 203, 204, 205)
PLAYER_TEAMS: dict[int, TeamSlot] = {
    **dict.fromkeys(TEAM1_IDS, TeamSlot.TEAM1),
    **dict.fromkeys(TEAM2_IDS, TeamSlot.TEAM2),
}
#: Race-to-N targets published by :func:`empirical_core` (16 and 19 can only be reached in overtime).
RACE_TARGETS = (1, 3, 5, 7, 10, 12, 13, 14, 16, 17, 19)
T1, T2 = TeamSlot.TEAM1, TeamSlot.TEAM2


def simulate_map(rng: np.random.Generator, p_round: float) -> tuple[int, int, tuple[TeamSlot, ...]]:
    """One CS2 map with i.i.d. rounds: first to 13, 12-12 -> MR3 overtimes (first to 4 of 6, a 3-3
    overtime starts another one). Returns the final score and the winner of every round."""
    a = b = 0
    rounds: list[TeamSlot] = []

    def play() -> None:
        nonlocal a, b
        if rng.random() < p_round:
            a += 1
            rounds.append(T1)
        else:
            b += 1
            rounds.append(T2)

    while max(a, b) < 13 and not (a == 12 and b == 12):
        play()
    while a == b:  # 12-12, 15-15, ...: play one overtime
        base = a
        while max(a, b) < base + 4 and not (a == base + 3 and b == base + 3):
            play()
    return a, b, tuple(rounds)


def simulate_series(
    rng: np.random.Generator,
    fmt: MatchFormat,
    p_rounds: Sequence[float],
    *,
    match_id: int = 1,
    kills_low: int = 6,
    kills_high: int = 30,
) -> MatchActuals:
    """A finished series: maps are played until the format is decided (both maps in a BO2)."""
    need = fmt.maps_to_win
    maps: list[MapActual] = []
    wins1 = wins2 = 0
    for k in range(fmt.n_maps):
        if need is not None and max(wins1, wins2) >= need:
            break
        a, b, rounds = simulate_map(rng, p_rounds[k])
        if a > b:
            wins1 += 1
        else:
            wins2 += 1
        kills = {pid: int(rng.integers(kills_low, kills_high)) for pid in (*TEAM1_IDS, *TEAM2_IDS)}
        maps.append(
            MapActual(
                map_number=k + 1,
                map_name="mirage",
                team1_rounds=a,
                team2_rounds=b,
                round_winners=rounds,
                player_kills=kills,
            )
        )
    return MatchActuals(match_id=match_id, format=fmt, team1_id=1, team2_id=2, maps=tuple(maps))


def _pmf(values: Iterable[object]) -> dict[str, float]:
    counts = Counter(str(v) for v in values)
    n = sum(counts.values())
    return {k: c / n for k, c in counts.items()}


def _projection(player_id: int, kills: Sequence[int]) -> PlayerProjection:
    arr = np.asarray(kills, dtype=float)
    return PlayerProjection(
        player_id=player_id,
        team=PLAYER_TEAMS[player_id],
        kills_pmf=_pmf(kills),
        kills_mean=float(arr.mean()),
        kills_var=float(arr.var()),
    )


def _first_to(rounds: Sequence[TeamSlot], target: int) -> TeamSlot | None:
    a = b = 0
    for winner in rounds:
        if winner is T1:
            a += 1
        else:
            b += 1
        if a == target:
            return T1
        if b == target:
            return T2
    return None


def _top_fragger(rows: Sequence[dict[int, int]], players: Sequence[int]) -> dict[str, float]:
    """P(top fragger) per player over ``rows`` (kills per player), dead heats split evenly."""
    share = dict.fromkeys(players, 0.0)
    for row in rows:
        best = max(row[p] for p in players)
        tied = [p for p in players if row[p] == best]
        for p in tied:
            share[p] += 1.0 / len(tied)
    return {str(p): v / len(rows) for p, v in share.items()}


def _sum_kills(maps: Iterable[MapActual]) -> dict[int, int]:
    total: Counter[int] = Counter()
    for m in maps:
        total.update(m.player_kills)
    return dict(total)


def _add_top_fragger(table: dict[str, dict[str, float]], scope: str, rows: Sequence[dict[int, int]]) -> None:
    table[scope] = _top_fragger(rows, (*TEAM1_IDS, *TEAM2_IDS))
    table[f"{scope}|team1"] = _top_fragger(rows, TEAM1_IDS)
    table[f"{scope}|team2"] = _top_fragger(rows, TEAM2_IDS)


def empirical_core(
    series: Sequence[MatchActuals], fmt: MatchFormat, *, publish_neither: bool = True
) -> PredictionCore:
    """The ``PredictionCore`` that describes ``series`` exactly (every series has the same weight).

    Follows the producer conventions of :mod:`cs2bot.domain.predictions`. ``publish_neither=False``
    leaves ``p_neither_race_to`` empty, so pricing has to derive it from the score pmf.
    """
    n = len(series)
    slots: list[MapSlotPrediction] = []
    by_scope: dict[str, dict[str, PlayerProjection]] = {}
    top: dict[str, dict[str, float]] = {}
    players = (*TEAM1_IDS, *TEAM2_IDS)
    for k in range(1, fmt.n_maps + 1):
        played = [m for m in (s.map(k) for s in series) if m is not None]
        if not played:
            continue
        cnt = len(played)
        histories = [m.round_winners or () for m in played]

        def half(history: Sequence[TeamSlot], which: int) -> int:
            part = history[:12] if which == 1 else history[12:24]
            wins1 = sum(1 for w in part if w is T1)
            return (wins1 > len(part) - wins1) - (wins1 < len(part) - wins1)

        firsts = {t: [_first_to(h, t) for h in histories] for t in RACE_TARGETS}
        slot_players = {str(pid): _projection(pid, [m.player_kills[pid] for m in played]) for pid in players}
        slots.append(
            MapSlotPrediction(
                map_number=k,
                p_played=cnt / n,
                map_distribution={"mirage": 1.0},
                p_team1_win=sum(1 for m in played if m.winner is T1) / cnt,
                score_pmf=_pmf(f"{m.team1_rounds}-{m.team2_rounds}" for m in played),
                p_overtime=sum(1 for m in played if m.went_to_overtime) / cnt,
                p_team1_pistol={str(r): sum(1 for h in histories if h[r - 1] is T1) / cnt for r in (1, 13)},
                p_team1_half={str(w): sum(1 for h in histories if half(h, w) > 0) / cnt for w in (1, 2)},
                p_half_draw={str(w): sum(1 for h in histories if half(h, w) == 0) / cnt for w in (1, 2)},
                p_team1_race_to={str(t): sum(1 for f in firsts[t] if f is T1) / cnt for t in RACE_TARGETS},
                p_neither_race_to=(
                    {str(t): sum(1 for f in firsts[t] if f is None) / cnt for t in RACE_TARGETS if t > 13}
                    if publish_neither
                    else {}
                ),
                players=slot_players,
            )
        )
        by_scope[f"map:{k}"] = slot_players
        _add_top_fragger(top, f"map:{k}", [m.player_kills for m in played])

    multi_scopes = [tuple(range(1, last + 1)) for last in range(2, fmt.n_maps + 1)]
    for scope_maps in multi_scopes:  # conditional on ALL listed maps being played
        rows = [_sum_kills(s.maps[: len(scope_maps)]) for s in series if s.maps_played >= len(scope_maps)]
        if not rows:
            continue
        key = "maps:" + ",".join(str(m) for m in scope_maps)
        by_scope[key] = {str(pid): _projection(pid, [r[pid] for r in rows]) for pid in players}
        _add_top_fragger(top, key, rows)
    match_rows = [_sum_kills(s.maps) for s in series]  # unconditional: kills over the maps played
    by_scope["match"] = {str(pid): _projection(pid, [r[pid] for r in match_rows]) for pid in players}
    _add_top_fragger(top, "match", match_rows)

    scores = [(s.team1_maps, s.team2_maps) for s in series]
    return PredictionCore(
        p_team1_win=sum(1 for a, b in scores if a > b) / n,
        p_draw=sum(1 for a, b in scores if a == b) / n,
        map_score_pmf=_pmf(f"{a}-{b}" for a, b in scores),
        slots=slots,
        player_kills_by_scope=by_scope,
        top_fragger=top,
    )


def empirical_prediction(
    series: Sequence[MatchActuals], fmt: MatchFormat, *, publish_neither: bool = True
) -> MatchPrediction:
    return MatchPrediction(
        match_id=series[0].match_id,
        created_at=datetime(2026, 11, 20, 18, 0, tzinfo=UTC),
        model_version="empirical",
        format=fmt,
        team1_id=1,
        team2_id=2,
        central=empirical_core(series, fmt, publish_neither=publish_neither),
        sigma_team1_win=0.03,
        veto_known=True,
        data_completeness=1.0,
    )
