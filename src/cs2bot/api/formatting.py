"""Formatting helpers for the API contract.

* money -> string with exactly two decimals (``"12.50"``)
* odds -> string with 2-3 decimals (``"1.85"``, ``"1.875"``, ``"2.00"``)
* spec keys (``"total_rounds|map=1|line=26.5"``) -> fields, and human readable outcome labels
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from cs2bot.domain.markets import (
    SELECTION_DRAW,
    SELECTION_TEAM1,
    SELECTION_TEAM2,
    MarketType,
    parse_player_selection,
    parse_score_selection,
)
from cs2bot.utils.money import D, quantize_money
from cs2bot.utils.time import ensure_utc

_ODDS_QUANTUM = Decimal("0.001")


def to_decimal(value: object) -> Decimal | None:
    """Best-effort Decimal conversion (``None`` for missing/invalid values)."""
    if value is None:
        return None
    try:
        dec = D(value)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return dec if dec.is_finite() else None


def money_str(value: object) -> str | None:
    """Money as a string with exactly two decimals (half-up), ``None`` stays ``None``."""
    dec = to_decimal(value)
    if dec is None:
        return None
    q = quantize_money(dec)
    if q == 0:
        q = abs(q)  # never "-0.00"
    return f"{q:.2f}"


def money_str0(value: object) -> str:
    """Like :func:`money_str` but ``None`` becomes ``"0.00"``."""
    return money_str(value) or "0.00"


def odds_str(value: object) -> str | None:
    """Decimal odds as a string: three decimals, a trailing zero dropped (``1.850`` -> ``"1.85"``)."""
    dec = to_decimal(value)
    if dec is None:
        return None
    text = f"{dec.quantize(_ODDS_QUANTUM, rounding=ROUND_HALF_UP):.3f}"
    return text[:-1] if text.endswith("0") else text


def utc(dt: datetime | None) -> datetime | None:
    return ensure_utc(dt) if dt is not None else None


def age_seconds(dt: datetime | None, now: datetime) -> float | None:
    if dt is None:
        return None
    return round((now - ensure_utc(dt)).total_seconds(), 1)


def round_or_none(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf are not valid JSON
        return None
    return round(value, digits)


# --------------------------------------------------------------------------------------------
# spec keys
# --------------------------------------------------------------------------------------------

_SPEC_KEY_FIELDS: dict[str, str] = {
    "map": "map_number",
    "maps": "maps_scope",
    "team": "team",
    "player": "player_id",
    "opp": "opponent_player_id",
    "half": "half",
    "round": "round_number",
    "line": "line",
}

_MARKET_ORDER: dict[str, int] = {mt.value: i for i, mt in enumerate(MarketType)}

_SELECTION_ORDER: dict[str, int] = {
    "team1": 0,
    "draw": 1,
    "team2": 2,
    "over": 0,
    "under": 1,
    "yes": 0,
    "no": 1,
    "odd": 0,
    "even": 1,
}


def market_type_of(spec_key: str) -> str:
    """``"map_winner|map=2"`` -> ``"map_winner"``."""
    return spec_key.split("|", 1)[0]


def parse_spec_key(spec_key: str) -> dict[str, Any]:
    """Inverse of :meth:`cs2bot.domain.markets.MarketSpec.key` (unknown parts are ignored)."""
    parts = spec_key.split("|")
    out: dict[str, Any] = {"market_type": parts[0]}
    for part in parts[1:]:
        key, sep, raw = part.partition("=")
        name = _SPEC_KEY_FIELDS.get(key)
        if not sep or name is None:
            continue
        try:
            if name == "maps_scope":
                out[name] = [int(x) for x in raw.split(",") if x]
            elif name == "line":
                out[name] = float(raw)
            elif name == "team":
                out[name] = raw
            else:
                out[name] = int(raw)
        except ValueError:
            continue
    return out


def market_sort_key(spec: Mapping[str, Any], selection_key: str) -> tuple[Any, ...]:
    """Stable display order: market type (taxonomy order), map, line, then selection."""
    mt = str(spec.get("market_type") or "")
    map_number = spec.get("map_number")
    line = spec.get("line")
    return (
        _MARKET_ORDER.get(mt, len(_MARKET_ORDER)),
        map_number if isinstance(map_number, int) else 0,
        str(spec.get("player_id") or ""),
        float(line) if isinstance(line, int | float) else 0.0,
        _SELECTION_ORDER.get(selection_key, 5),
        selection_key,
    )


# --------------------------------------------------------------------------------------------
# labels
# --------------------------------------------------------------------------------------------


def _fmt_line(line: Any) -> str:
    return f"{float(line):g}" if isinstance(line, int | float) else "?"


def describe_outcome(
    spec: Mapping[str, Any],
    selection: str,
    *,
    team1: str | None,
    team2: str | None,
    players: Mapping[int, str] | None = None,
) -> str:
    """Human readable label of one outcome, e.g. ``"Map 2 total rounds over 26.5"``.

    Handicap lines are stored from team1's perspective; the label shows the line from the point
    of view of the selected team (team2 gets the opposite sign).
    """
    names = players or {}
    t1, t2 = team1 or "Team 1", team2 or "Team 2"
    mt = str(spec.get("market_type") or "")
    map_number = spec.get("map_number")
    line = spec.get("line")
    on_map = f"Map {map_number} " if isinstance(map_number, int) else ""

    def lead(phrase: str) -> str:
        """``"Map 2 total rounds"`` or, without a map, ``"Total rounds"`` (names are never re-cased)."""
        return on_map + phrase if on_map else phrase[:1].upper() + phrase[1:]

    def team_name(slot: Any) -> str:
        if slot == SELECTION_TEAM1:
            return t1
        if slot == SELECTION_TEAM2:
            return t2
        if slot == SELECTION_DRAW:
            return "draw"
        return str(slot)

    def player_name(pid: Any) -> str:
        if isinstance(pid, int):
            return names.get(pid) or f"player #{pid}"
        return "player"

    def selection_label() -> str:
        pid = parse_player_selection(selection)
        if pid is not None:
            return player_name(pid)
        score = parse_score_selection(selection)
        if score is not None:
            return f"{t1} {score[0]}-{score[1]} {t2}"
        if selection in (SELECTION_TEAM1, SELECTION_TEAM2, SELECTION_DRAW):
            return team_name(selection)
        return selection

    def scope_label() -> str:
        scope = spec.get("maps_scope")
        if isinstance(scope, list | tuple) and scope:
            return f" (maps {'-'.join(str(m) for m in scope)})" if len(scope) > 1 else f" (map {scope[0]})"
        if isinstance(map_number, int):
            return f" (map {map_number})"
        return ""

    text: str
    if mt == MarketType.MATCH_WINNER:
        text = f"Match winner: {selection_label()}"
    elif mt == MarketType.MAP_WINNER:
        text = f"{on_map or 'Map '}winner: {selection_label()}"
    elif mt in (MarketType.MAP_HANDICAP, MarketType.ROUND_HANDICAP) and selection in (
        SELECTION_TEAM1,
        SELECTION_TEAM2,
    ):
        prefix = "Map handicap" if mt == MarketType.MAP_HANDICAP else lead("round handicap")
        if isinstance(line, int | float):
            own = float(line) if selection == SELECTION_TEAM1 else -float(line)
            text = f"{prefix}: {team_name(selection)} {own:+g}"
        else:
            text = f"{prefix}: {team_name(selection)}"
    elif mt == MarketType.TOTAL_MAPS:
        text = f"Total maps {selection} {_fmt_line(line)}"
    elif mt == MarketType.BOTH_TEAMS_WIN_A_MAP:
        text = f"Both teams win a map: {selection}"
    elif mt == MarketType.TOTAL_ROUNDS:
        text = f"{lead('total rounds')} {selection} {_fmt_line(line)}"
    elif mt == MarketType.TEAM_TOTAL_ROUNDS:
        text = f"{on_map}{team_name(spec.get('team'))} rounds {selection} {_fmt_line(line)}"
    elif mt == MarketType.TOTAL_ROUNDS_ODD_EVEN:
        text = f"{lead('total rounds')}: {selection}"
    elif mt == MarketType.CORRECT_MAP_SCORE:
        text = f"Correct map score: {selection_label()}"
    elif mt == MarketType.MAP_CORRECT_SCORE:
        text = f"{lead('correct score')}: {selection_label()}"
    elif mt == MarketType.MAP_OVERTIME:
        text = f"{lead('overtime')}: {selection}"
    elif mt == MarketType.PISTOL_ROUND_WINNER:
        rnd = spec.get("round_number")
        text = f"{lead('pistol round')}{f' {rnd}' if rnd else ''}: {selection_label()}"
    elif mt == MarketType.RACE_TO_ROUNDS:
        text = f"{lead('race to')} {_fmt_line(line)} rounds: {selection_label()}"
    elif mt == MarketType.HALF_WINNER:
        text = f"{lead('half')} {spec.get('half') or '?'}: {selection_label()}"
    elif mt == MarketType.PLAYER_KILLS:
        text = f"{player_name(spec.get('player_id'))} kills{scope_label()} {selection} {_fmt_line(line)}"
    elif mt == MarketType.PLAYER_KILLS_H2H:
        text = (
            f"Kills duel{scope_label()} {player_name(spec.get('player_id'))} vs "
            f"{player_name(spec.get('opponent_player_id'))}: {selection_label()}"
        )
    elif mt == MarketType.TOP_FRAGGER:
        team = spec.get("team")
        of_team = f" of {team_name(team)}" if team in (SELECTION_TEAM1, SELECTION_TEAM2) else ""
        text = f"Top fragger{of_team}{scope_label()}: {selection_label()}"
    else:
        name = mt or "market"
        text = f"{name[:1].upper()}{name[1:]}: {selection_label()}"
    return text
