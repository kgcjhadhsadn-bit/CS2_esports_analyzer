"""What actually happened in a match: :class:`~cs2bot.domain.results.MatchActuals` from the database.

Used to settle bets, to score predictions and by the backtest. Nothing is guessed: a field that the
database cannot support (no round history, no stat lines) is left empty so the settlement layer
answers ``UNKNOWN`` instead of inventing a result.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.datasets._common import source_rank
from cs2bot.datasets.loaders import _clean_map_name, _map_is_finished, _valid_score
from cs2bot.db.models import MapPlayed, Match, PlayerMapStats, Round
from cs2bot.domain.enums import MatchFormat, MatchStatus, TeamSlot
from cs2bot.domain.results import MapActual, MatchActuals

_FORMAT_BY_BEST_OF = {1: MatchFormat.BO1, 2: MatchFormat.BO2, 3: MatchFormat.BO3, 5: MatchFormat.BO5}


def _source_extra(match: Match, source: str) -> Mapping[str, Any]:
    extra = match.extra if isinstance(match.extra, dict) else {}
    section = extra.get(source)
    return section if isinstance(section, dict) else {}


def _is_forfeit(match: Match) -> bool:
    """Full walkover / default win.

    HLTV sync stores ``Match.extra["hltv"]["forfeit"]`` (docs/dev/reports/hltv.md). PandaScore's
    match payload has a ``forfeit`` boolean; if its sync keeps it under ``extra["pandascore"]`` (or
    at the top level) it is honoured too - an assumption, that package is not built yet.
    """
    extra = match.extra if isinstance(match.extra, dict) else {}
    return bool(
        _source_extra(match, "hltv").get("forfeit")
        or _source_extra(match, "pandascore").get("forfeit")
        or extra.get("forfeit") is True
    )


def _has_forfeited_maps(match: Match) -> bool:
    """Some (not all) maps were awarded by default: the series result is not a sporting result."""
    return bool(_source_extra(match, "hltv").get("forfeited_maps"))


def _validated_winners(
    winners: Sequence[TeamSlot], numbers: Sequence[int], team1_rounds: int, team2_rounds: int
) -> tuple[TeamSlot, ...] | None:
    """The round winners in order, only if they are complete and agree with the final score."""
    n = team1_rounds + team2_rounds
    if len(winners) != n or list(numbers) != list(range(1, n + 1)):
        return None
    if sum(1 for w in winners if w is TeamSlot.TEAM1) != team1_rounds:
        return None
    return tuple(winners)


def _round_winners(session: Session, map_row: MapPlayed) -> tuple[TeamSlot, ...] | None:
    """Winner of every round: from the ``rounds`` table, else from the stored HLTV round history."""
    t1, t2 = map_row.team1_rounds or 0, map_row.team2_rounds or 0
    rows = session.execute(
        select(Round.round_number, Round.team1_side, Round.winner_side)
        .where(Round.map_played_id == map_row.id)
        .order_by(Round.round_number)
    ).all()
    if rows:
        winners = [TeamSlot.TEAM1 if w is s else TeamSlot.TEAM2 for _, s, w in rows]
        valid = _validated_winners(winners, [n for n, _, _ in rows], t1, t2)
        if valid is not None:
            return valid
    extra = map_row.extra if isinstance(map_row.extra, dict) else {}
    hltv = extra.get("hltv")
    if isinstance(hltv, dict) and hltv.get("rounds_complete") and isinstance(hltv.get("rounds"), list):
        try:
            history = sorted(hltv["rounds"], key=lambda r: int(r["round"]))
            winners = [TeamSlot(r["winner"]) for r in history]
            numbers = [int(r["round"]) for r in history]
        except (KeyError, TypeError, ValueError):
            return None
        return _validated_winners(winners, numbers, t1, t2)
    return None


def _player_kills(session: Session, map_ids: Sequence[int]) -> dict[int, dict[int, int]]:
    """map_played id -> player id -> kills, taking each player's most trusted source row."""
    best: dict[tuple[int, int], tuple[int, int]] = {}
    if not map_ids:
        return {}
    rows = session.execute(
        select(
            PlayerMapStats.map_played_id, PlayerMapStats.player_id, PlayerMapStats.source, PlayerMapStats.kills
        ).where(PlayerMapStats.map_played_id.in_(list(map_ids)))
    )
    for map_id, player_id, source, kills in rows:
        if kills is None:
            continue
        rank = source_rank(source)
        key = (map_id, player_id)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, int(kills))
    out: dict[int, dict[int, int]] = defaultdict(dict)
    for (map_id, player_id), (_, kills) in best.items():
        out[map_id][player_id] = kills
    return out


def _series_decided(fmt: MatchFormat, wins1: int, wins2: int) -> bool:
    need = fmt.maps_to_win
    if need is None:  # BO2: both maps are always played
        return wins1 + wins2 == fmt.n_maps
    return max(wins1, wins2) == need and min(wins1, wins2) < need


def load_match_actuals(session: Session, match_id: int) -> MatchActuals | None:
    """Final (or current) state of a match, or ``None`` if the match / its teams are unknown.

    * ``maps``: maps with a decided score (winner has >= 13 rounds), ordered by map number.
    * ``round_winners``: only when every round of the map is known and the winners add up to the
      final score - from the ``rounds`` table (any source), else from the HLTV round history kept in
      ``maps_played.extra["hltv"]["rounds"]``. Otherwise ``None``.
    * ``player_kills``: kills per player with the same source preference as the player records
      (DEMO > HLTV > PANDASCORE > SYNTHETIC).
    * ``forfeited``: the match was a full walkover (see :func:`_is_forfeit`).
      ``cancelled``: status CANCELED without a forfeit flag.
    * ``complete``: ``True`` once nothing more is expected - a cancelled / forfeited match, or a
      FINISHED match whose stored maps are numbered 1..n without gaps, decide the series for its
      format and agree with ``matches.team1_score`` / ``team2_score`` when those hold map counts.
      A series in which single maps were awarded by default (HLTV ``forfeited_maps``) is never
      ``complete``: its played maps are listed, but the series result needs the bookmaker's ruling.
    """
    match = session.get(Match, match_id)
    if match is None or match.team1_id is None or match.team2_id is None:
        return None

    map_rows = list(
        session.scalars(
            select(MapPlayed).where(MapPlayed.match_id == match_id).order_by(MapPlayed.map_number)
        )
    )
    played = [
        m
        for m in map_rows
        if _map_is_finished(m.status, match.status)
        and _valid_score(m.team1_rounds, m.team2_rounds)
        and _clean_map_name(m.map_name) is not None
    ]
    kills = _player_kills(session, [m.id for m in played])
    maps: list[MapActual] = []
    for m in played:
        name = _clean_map_name(m.map_name)
        assert name is not None and m.team1_rounds is not None and m.team2_rounds is not None
        maps.append(
            MapActual(
                map_number=m.map_number,
                map_name=name,
                team1_rounds=m.team1_rounds,
                team2_rounds=m.team2_rounds,
                round_winners=_round_winners(session, m),
                player_kills=dict(kills.get(m.id, {})),
            )
        )

    wins1 = sum(1 for m in maps if m.winner is TeamSlot.TEAM1)
    wins2 = len(maps) - wins1
    best_of = _source_extra(match, "hltv").get("best_of")
    fmt = match.format or _FORMAT_BY_BEST_OF.get(best_of if isinstance(best_of, int) else -1)
    if fmt is None:  # last resort: the number of map rows (BO3 is by far the most common format)
        n_rows = len(map_rows)
        fmt = MatchFormat.BO1 if n_rows == 1 else MatchFormat.BO5 if n_rows >= 4 else MatchFormat.BO3

    forfeited = _is_forfeit(match)
    cancelled = match.status is MatchStatus.CANCELED and not forfeited
    if forfeited or cancelled:
        complete = True
    elif match.status is not MatchStatus.FINISHED or _has_forfeited_maps(match):
        complete = False
    else:
        contiguous = [m.map_number for m in maps] == list(range(1, len(maps) + 1))
        complete = contiguous and _series_decided(fmt, wins1, wins2)
        s1, s2 = match.team1_score, match.team2_score
        # Sources store either map counts ("2-1") or, for a BO1, sometimes the round score ("13-9").
        if complete and s1 is not None and s2 is not None and max(s1, s2) <= fmt.n_maps:
            complete = (s1, s2) == (wins1, wins2)

    return MatchActuals(
        match_id=match.id,
        format=fmt,
        team1_id=match.team1_id,
        team2_id=match.team2_id,
        maps=tuple(maps),
        forfeited=forfeited,
        cancelled=cancelled,
        complete=complete,
    )
