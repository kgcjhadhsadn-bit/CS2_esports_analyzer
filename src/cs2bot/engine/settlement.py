"""Settlement: decide what happened to a bet selection from the final state of a match.

:func:`resolve_outcome` is the mirror image of :func:`cs2bot.engine.pricing.price_outcome`: the
same ``(MarketSpec, selection)`` pair, in *our* orientation (``team1`` / ``team2`` =
``matches.team1_id`` / ``team2_id``), is resolved against a :class:`~cs2bot.domain.results.MatchActuals`
with exactly the conventions the pricing engine used to compute its probability:

* a forfeited or cancelled match voids every bet;
* a map-scoped market (and a player market on given maps) is **VOID** when that map was not played
  (map 3 of a 2-0 series; any listed map of a ``maps_scope`` market);
* handicaps are from team1's perspective (``margin_team1 + line``), totals compare ``value`` with
  ``line``; a result exactly on an integer line is a **PUSH**;
* round totals, team totals, odd/even and round handicaps count overtime rounds unless
  ``include_overtime_in_totals`` is off (then an overtime map counts as its 12-12 regulation score);
* race to N rounds is VOID when neither team reaches N; half 1 = rounds 1-12, half 2 = rounds 13-24
  actually played (overtime excluded), a level half is the ``draw`` selection; pistol rounds are
  rounds 1 and 13;
* player kills: ``map_number`` -> that map, ``maps_scope`` -> the sum over the listed maps (all must
  be played), neither -> the sum over every map played; a level head-to-head is a PUSH or a loss per
  ``h2h_tie``.

The bookmaker rules (``include_overtime_in_totals``, ``h2h_tie``) default to
:class:`cs2bot.config.BetclicSettings` through :func:`cs2bot.engine.pricing.market_rules`, the same
resolver pricing uses.

**Never a guess.** Whenever the data cannot decide the bet the result is
:attr:`SettlementOutcome.UNKNOWN` and the settlement service must fall back to the bookmaker's own
bet history: an unfinished or inconsistent series, a map score that is not a possible CS2 final
score (MR12 + MR3 overtimes), missing or inconsistent round history for pistol / half / race
markets, missing kill statistics for a player, a selection or spec the pricing engine would not
price. Two cases are deliberately UNKNOWN although something is known:

* a top-fragger **tie** for first place: bookmakers apply dead-heat rules (a partial win), which
  ``SettlementOutcome`` cannot express (pricing uses the dead-heat adjusted probability);
* a team-restricted top-fragger market without ``player_teams``: ``MapActual.player_kills`` does not
  say which team a player belongs to.

A race-to-N market is decided without round history when the final score alone settles it (only one
team reached N, or neither did).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from cs2bot.config import BetclicSettings
from cs2bot.db.models import Bet
from cs2bot.domain.enums import BetStatus, MatchFormat, TeamSlot
from cs2bot.domain.markets import (
    SELECTION_DRAW,
    SELECTION_EVEN,
    SELECTION_NO,
    SELECTION_ODD,
    SELECTION_OVER,
    SELECTION_TEAM1,
    SELECTION_TEAM2,
    SELECTION_UNDER,
    SELECTION_YES,
    MarketSpec,
    MarketType,
    parse_player_selection,
    parse_score_selection,
)
from cs2bot.domain.results import MapActual, MatchActuals, SettlementOutcome
from cs2bot.engine.pricing import (
    REGULATION_ROUNDS,
    ROUNDS_TO_WIN_REGULATION,
    H2HTie,
    compare_to_line,
    final_map_scores,
    is_final_round_score,
    market_rules,
    rounds_value,
)

#: Rounds per regulation half (MR12).
HALF_ROUNDS = REGULATION_ROUNDS // 2
#: A top-fragger WIN needs kill statistics for at least this many players per team on every map of
#: the scope (a full CS2 team); with fewer, an unlisted player could have out-fragged the selection.
TEAM_SIZE = 5

WON = SettlementOutcome.WON
LOST = SettlementOutcome.LOST
VOID = SettlementOutcome.VOID
PUSH = SettlementOutcome.PUSH
UNKNOWN = SettlementOutcome.UNKNOWN


@dataclass(frozen=True)
class Resolution:
    """A settlement outcome with the reason behind a VOID / PUSH / UNKNOWN (empty for WON / LOST)."""

    outcome: SettlementOutcome
    reason: str = ""


class _Undecidable(Exception):
    """Internal: the data cannot decide this bet -> ``SettlementOutcome.UNKNOWN`` with the reason."""


@dataclass(frozen=True)
class _Rules:
    include_ot: bool
    h2h_tie: H2HTie
    player_teams: Mapping[int, TeamSlot] | None


@dataclass(frozen=True)
class _Series:
    """A validated, finished series."""

    fmt: MatchFormat
    maps: dict[int, MapActual]  # map_number -> map, exactly 1..len(maps)
    team1_maps: int
    team2_maps: int

    @property
    def maps_played(self) -> int:
        return len(self.maps)


_Handler = Callable[[MarketSpec, str, _Series, _Rules], Resolution]


# ----------------------------------------------------------------------------------------------
# public API
# ----------------------------------------------------------------------------------------------


def explain_outcome(
    spec: MarketSpec,
    selection: str,
    actuals: MatchActuals,
    *,
    include_overtime_in_totals: bool | None = None,
    h2h_tie: str | None = None,
    betclic: BetclicSettings | None = None,
    player_teams: Mapping[int, TeamSlot] | None = None,
) -> Resolution:
    """:func:`resolve_outcome` plus the reason for a VOID / PUSH / UNKNOWN result (for logs and the
    dashboard). Raises ``ValueError`` only for an invalid ``h2h_tie`` argument."""
    include_ot, tie = market_rules(include_overtime_in_totals, h2h_tie, betclic)
    if actuals.cancelled:
        return Resolution(VOID, "match cancelled")
    if actuals.forfeited:
        return Resolution(VOID, "match forfeited (walkover)")
    handler = _HANDLERS.get(spec.market_type)
    if handler is None:
        return Resolution(UNKNOWN, f"market type {spec.market_type.value!r} is never settled locally")
    if player_teams is None:
        # forward compatible: used as soon as MatchActuals carries the lineups (see the report's
        # foundation change requests); today the caller has to pass player_teams
        carried = getattr(actuals, "player_teams", None)
        player_teams = carried if isinstance(carried, Mapping) and carried else None
    try:
        series = _series(actuals)
        return handler(spec, selection, series, _Rules(include_ot, tie, player_teams))
    except _Undecidable as exc:
        return Resolution(UNKNOWN, str(exc))


def resolve_outcome(
    spec: MarketSpec,
    selection: str,
    actuals: MatchActuals,
    *,
    include_overtime_in_totals: bool | None = None,
    h2h_tie: str | None = None,
    betclic: BetclicSettings | None = None,
    player_teams: Mapping[int, TeamSlot] | None = None,
) -> SettlementOutcome:
    """Settle one selection of one market against what actually happened (module docstring).

    ``include_overtime_in_totals`` / ``h2h_tie`` default to the Betclic settlement rules in the
    settings, exactly like :func:`cs2bot.engine.pricing.price_outcome`. ``player_teams`` (internal
    player id -> team slot) is only needed for team-restricted top-fragger markets. Returns
    ``UNKNOWN`` - never a guess - when the data cannot decide the bet.
    """
    return explain_outcome(
        spec,
        selection,
        actuals,
        include_overtime_in_totals=include_overtime_in_totals,
        h2h_tie=h2h_tie,
        betclic=betclic,
        player_teams=player_teams,
    ).outcome


def spec_from_key(key: str) -> MarketSpec:
    """Rebuild a :class:`MarketSpec` from its canonical ``MarketSpec.key()`` (stored in
    ``bets.spec_key``), e.g. ``"total_rounds|map=2|line=26.5"``. ``player_name`` is not part of the
    key and stays ``None``. Raises ``ValueError`` for a malformed key."""
    parts = key.split("|")
    try:
        market_type = MarketType(parts[0])
    except ValueError:
        raise ValueError(f"unknown market type in spec key {key!r}") from None
    fields: dict[str, object] = {"market_type": market_type}
    names = {
        "map": ("map_number", int),
        "team": ("team", str),
        "player": ("player_id", int),
        "opp": ("opponent_player_id", int),
        "half": ("half", int),
        "round": ("round_number", int),
        "line": ("line", float),
    }
    for part in parts[1:]:
        name, sep, raw = part.partition("=")
        try:
            if not sep:
                raise ValueError("missing '='")
            if name == "maps":
                fields["maps_scope"] = tuple(int(m) for m in raw.split(","))
            else:
                field, convert = names[name]
                fields[field] = convert(raw)
        except (KeyError, ValueError):
            raise ValueError(f"malformed part {part!r} in spec key {key!r}") from None
    return MarketSpec.model_validate(fields)


def resolve_bet(
    bet: Bet,
    actuals: MatchActuals,
    *,
    include_overtime_in_totals: bool | None = None,
    h2h_tie: str | None = None,
    betclic: BetclicSettings | None = None,
    player_teams: Mapping[int, TeamSlot] | None = None,
) -> Resolution:
    """:func:`explain_outcome` for a ``bets`` row (the spec is rebuilt from ``bets.spec_key``).

    A bet on another match or with an unreadable spec key is UNKNOWN.
    """
    if bet.match_id is not None and bet.match_id != actuals.match_id:
        return Resolution(UNKNOWN, f"bet is on match {bet.match_id}, the result is for {actuals.match_id}")
    try:
        spec = spec_from_key(bet.spec_key)
    except ValueError as exc:
        return Resolution(UNKNOWN, str(exc))
    return explain_outcome(
        spec,
        bet.selection_key,
        actuals,
        include_overtime_in_totals=include_overtime_in_totals,
        h2h_tie=h2h_tie,
        betclic=betclic,
        player_teams=player_teams,
    )


def bet_status_for(outcome: SettlementOutcome) -> BetStatus | None:
    """``bets.status`` of a settled bet: WON / LOST / VOID (a push is a refund, i.e. VOID);
    ``None`` for UNKNOWN (the bet stays open)."""
    if outcome is WON:
        return BetStatus.WON
    if outcome is LOST:
        return BetStatus.LOST
    if outcome.is_refund:
        return BetStatus.VOID
    return None


# ----------------------------------------------------------------------------------------------
# validation
# ----------------------------------------------------------------------------------------------


def _map_problem(m: MapActual) -> str | None:
    if m.team1_rounds < 0 or m.team2_rounds < 0:
        return f"map {m.map_number} has a negative score"
    if not is_final_round_score(m.team1_rounds, m.team2_rounds):
        return (
            f"map {m.map_number}: {m.team1_rounds}-{m.team2_rounds} is not a possible final CS2 score "
            "(MR12 with MR3 overtimes)"
        )
    return None


def _series(actuals: MatchActuals) -> _Series:
    """Validate that ``actuals`` describes a finished, internally consistent series."""
    if not actuals.complete:
        raise _Undecidable("the series is not finished or its map data is incomplete")
    fmt = actuals.format
    maps: dict[int, MapActual] = {}
    for m in actuals.maps:
        if m.map_number in maps:
            raise _Undecidable(f"map {m.map_number} is listed twice")
        maps[m.map_number] = m
    count = len(maps)
    if sorted(maps) != list(range(1, count + 1)):
        raise _Undecidable(f"map numbers {sorted(maps)} are not 1..{count}")
    for k in sorted(maps):
        problem = _map_problem(maps[k])
        if problem is not None:
            raise _Undecidable(problem)
    wins1 = sum(1 for m in maps.values() if m.winner is TeamSlot.TEAM1)
    wins2 = count - wins1
    need = fmt.maps_to_win
    if need is None:  # BO2: both maps are always played
        if count != fmt.n_maps:
            raise _Undecidable(f"a {fmt.value} has {fmt.n_maps} maps, got {count}")
    else:
        if count > fmt.n_maps or max(wins1, wins2) != need or min(wins1, wins2) >= need:
            raise _Undecidable(f"{wins1}-{wins2} is not a finished {fmt.value} series")
        series_winner = TeamSlot.TEAM1 if wins1 > wins2 else TeamSlot.TEAM2
        if maps[count].winner is not series_winner:
            raise _Undecidable(f"{wins1}-{wins2}: maps were played after the {fmt.value} was decided")
    return _Series(fmt=fmt, maps=maps, team1_maps=wins1, team2_maps=wins2)


def _history(m: MapActual) -> tuple[TeamSlot, ...]:
    """The map's round history, validated against its final score."""
    rounds = m.round_winners
    if rounds is None:
        raise _Undecidable(f"map {m.map_number} has no round history")
    wins1 = sum(1 for w in rounds if w is TeamSlot.TEAM1)
    if len(rounds) != m.total_rounds or wins1 != m.team1_rounds:
        raise _Undecidable(f"map {m.map_number}: the round history does not match the final score")
    a = b = 0
    for winner in rounds[:-1]:
        if winner is TeamSlot.TEAM1:
            a += 1
        else:
            b += 1
        if is_final_round_score(a, b):
            raise _Undecidable(f"map {m.map_number}: the round history continues after the map was decided")
    return rounds


# ----------------------------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------------------------


def _decided(won: bool) -> Resolution:
    return Resolution(WON if won else LOST)


def _line(spec: MarketSpec) -> float:
    if spec.line is None or not math.isfinite(spec.line):
        raise _Undecidable(f"{spec.market_type.value} needs a line")
    return float(spec.line)


def _team_sign(selection: str) -> int:
    if selection == SELECTION_TEAM1:
        return 1
    if selection == SELECTION_TEAM2:
        return -1
    raise _Undecidable(f"selection {selection!r} is not 'team1' / 'team2'")


def _over_under(value: float, line: float, selection: str) -> Resolution:
    if selection == SELECTION_OVER:
        sign = 1
    elif selection == SELECTION_UNDER:
        sign = -1
    else:
        raise _Undecidable(f"selection {selection!r} is not 'over' / 'under'")
    result = compare_to_line(value, line) * sign
    if result == 0:
        return Resolution(PUSH, f"result {value:g} is exactly the line {line:g}")
    return _decided(result > 0)


def _handicap(margin_team1: float, line: float, selection: str) -> Resolution:
    sign = _team_sign(selection)
    result = compare_to_line(margin_team1 + line, 0.0) * sign
    if result == 0:
        return Resolution(PUSH, f"team1 margin {margin_team1:g} with handicap {line:+g} is level")
    return _decided(result > 0)


def _yes_no(happened: bool, selection: str) -> Resolution:
    if selection == SELECTION_YES:
        return _decided(happened)
    if selection == SELECTION_NO:
        return _decided(not happened)
    raise _Undecidable(f"selection {selection!r} is not 'yes' / 'no'")


def _slot_map(spec: MarketSpec, series: _Series) -> MapActual | None:
    """The map a slot market is about; ``None`` when that map was not played (the bet is void)."""
    k = spec.map_number
    if k is None:
        raise _Undecidable(f"{spec.market_type.value} needs map_number")
    if k < 1 or k > series.fmt.n_maps:
        raise _Undecidable(f"map {k} does not exist in a {series.fmt.value} series")
    return series.maps.get(k)


def _not_played(k: int | None) -> Resolution:
    return Resolution(VOID, f"map {k} was not played")


# ----------------------------------------------------------------------------------------------
# match-level markets
# ----------------------------------------------------------------------------------------------


def _match_winner(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    a, b = series.team1_maps, series.team2_maps
    if selection == SELECTION_TEAM1:
        return _decided(a > b)
    if selection == SELECTION_TEAM2:
        return _decided(b > a)
    if selection == SELECTION_DRAW:
        if series.fmt is not MatchFormat.BO2:
            raise _Undecidable(f"a series draw only exists in BO2, not {series.fmt.value}")
        return _decided(a == b)
    raise _Undecidable(f"selection {selection!r} is not team1 / team2 / draw")


def _correct_map_score(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    score = parse_score_selection(selection)
    if score is None:
        raise _Undecidable(f"selection {selection!r} is not a map score like '2-1'")
    if score not in final_map_scores(series.fmt):
        raise _Undecidable(f"{selection} is not a possible final score of a {series.fmt.value}")
    return _decided(score == (series.team1_maps, series.team2_maps))


def _map_handicap(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    return _handicap(series.team1_maps - series.team2_maps, _line(spec), selection)


def _total_maps(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    return _over_under(series.maps_played, _line(spec), selection)


def _both_teams_win_a_map(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    return _yes_no(series.team1_maps >= 1 and series.team2_maps >= 1, selection)


# ----------------------------------------------------------------------------------------------
# map-slot markets (void when the map is not played)
# ----------------------------------------------------------------------------------------------


def _map_winner(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    m = _slot_map(spec, series)
    sign = _team_sign(selection)
    if m is None:
        return _not_played(spec.map_number)
    return _decided((m.winner is TeamSlot.TEAM1) == (sign > 0))


def _round_handicap(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    m = _slot_map(spec, series)
    line = _line(spec)
    _team_sign(selection)
    if m is None:
        return _not_played(spec.map_number)
    a = rounds_value(m.team1_rounds, m.team2_rounds, include_overtime=rules.include_ot, team=SELECTION_TEAM1)
    b = rounds_value(m.team1_rounds, m.team2_rounds, include_overtime=rules.include_ot, team=SELECTION_TEAM2)
    return _handicap(a - b, line, selection)


def _total_rounds(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    m = _slot_map(spec, series)
    line = _line(spec)
    if selection not in (SELECTION_OVER, SELECTION_UNDER):
        raise _Undecidable(f"selection {selection!r} is not 'over' / 'under'")
    if m is None:
        return _not_played(spec.map_number)
    total = rounds_value(m.team1_rounds, m.team2_rounds, include_overtime=rules.include_ot)
    return _over_under(total, line, selection)


def _team_total_rounds(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    m = _slot_map(spec, series)
    if spec.team is None:
        raise _Undecidable("team_total_rounds needs spec.team")
    line = _line(spec)
    if selection not in (SELECTION_OVER, SELECTION_UNDER):
        raise _Undecidable(f"selection {selection!r} is not 'over' / 'under'")
    if m is None:
        return _not_played(spec.map_number)
    rounds = rounds_value(m.team1_rounds, m.team2_rounds, include_overtime=rules.include_ot, team=spec.team)
    return _over_under(rounds, line, selection)


def _odd_even(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    m = _slot_map(spec, series)
    if selection not in (SELECTION_ODD, SELECTION_EVEN):
        raise _Undecidable(f"selection {selection!r} is not 'odd' / 'even'")
    if m is None:
        return _not_played(spec.map_number)
    total = rounds_value(m.team1_rounds, m.team2_rounds, include_overtime=rules.include_ot)
    return _decided((total % 2 == 1) == (selection == SELECTION_ODD))


def _map_correct_score(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    m = _slot_map(spec, series)
    score = parse_score_selection(selection)
    if score is None:
        raise _Undecidable(f"selection {selection!r} is not a round score like '13-9'")
    if not is_final_round_score(*score):
        raise _Undecidable(f"{selection} is not a possible final CS2 map score")
    if m is None:
        return _not_played(spec.map_number)
    return _decided(score == (m.team1_rounds, m.team2_rounds))


def _map_overtime(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    m = _slot_map(spec, series)
    if selection not in (SELECTION_YES, SELECTION_NO):
        raise _Undecidable(f"selection {selection!r} is not 'yes' / 'no'")
    if m is None:
        return _not_played(spec.map_number)
    return _yes_no(m.went_to_overtime, selection)


def _pistol_round(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    m = _slot_map(spec, series)
    rnd = spec.round_number
    if rnd not in (1, ROUNDS_TO_WIN_REGULATION):
        raise _Undecidable(f"pistol rounds are rounds 1 and 13, got round_number={rnd!r}")
    sign = _team_sign(selection)
    if m is None:
        return _not_played(spec.map_number)
    rounds = _history(m)
    if len(rounds) < rnd:  # cannot happen for a valid final score (>= 13 rounds); defensive
        raise _Undecidable(f"map {m.map_number} has no round {rnd}")
    return _decided((rounds[rnd - 1] is TeamSlot.TEAM1) == (sign > 0))


def _race_to_rounds(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    m = _slot_map(spec, series)
    line = _line(spec)
    if not line.is_integer() or line < 1:
        raise _Undecidable(f"race-to line must be a positive whole number of rounds, got {line:g}")
    target = int(line)
    sign = _team_sign(selection)
    if m is None:
        return _not_played(spec.map_number)
    a, b = m.team1_rounds, m.team2_rounds
    if max(a, b) < target:
        return Resolution(VOID, f"neither team reached {target} rounds on map {m.map_number}")
    if min(a, b) < target:  # only the map winner got there: no round history needed
        first = TeamSlot.TEAM1 if a > b else TeamSlot.TEAM2
    else:
        first = None
        x = y = 0
        for winner in _history(m):
            if winner is TeamSlot.TEAM1:
                x += 1
            else:
                y += 1
            if x == target or y == target:
                first = TeamSlot.TEAM1 if x == target else TeamSlot.TEAM2
                break
        if first is None:  # pragma: no cover - both final scores are >= target
            raise _Undecidable(f"map {m.map_number}: nobody reached {target} in the round history")
    return _decided((first is TeamSlot.TEAM1) == (sign > 0))


def _half_winner(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    m = _slot_map(spec, series)
    half = spec.half
    if half not in (1, 2):
        raise _Undecidable(f"half must be 1 or 2, got {half!r}")
    if selection not in (SELECTION_TEAM1, SELECTION_TEAM2, SELECTION_DRAW):
        raise _Undecidable(f"selection {selection!r} is not team1 / team2 / draw")
    if m is None:
        return _not_played(spec.map_number)
    rounds = _history(m)
    part = rounds[:HALF_ROUNDS] if half == 1 else rounds[HALF_ROUNDS:REGULATION_ROUNDS]
    wins1 = sum(1 for w in part if w is TeamSlot.TEAM1)
    wins2 = len(part) - wins1
    if selection == SELECTION_DRAW:
        return _decided(wins1 == wins2)
    if selection == SELECTION_TEAM1:
        return _decided(wins1 > wins2)
    return _decided(wins2 > wins1)


# ----------------------------------------------------------------------------------------------
# player markets
# ----------------------------------------------------------------------------------------------


def _scope_maps(spec: MarketSpec, series: _Series) -> list[MapActual] | None:
    """Maps a player market covers; ``None`` when a listed map was not played (the bet is void)."""
    if spec.map_number is not None:
        numbers = [spec.map_number]
    elif spec.maps_scope is not None:
        numbers = sorted(set(spec.maps_scope))
        if not numbers:
            raise _Undecidable("maps_scope is empty")
    else:
        return [series.maps[k] for k in sorted(series.maps)]
    if numbers[0] < 1 or numbers[-1] > series.fmt.n_maps:
        raise _Undecidable(f"maps {numbers} are not all part of a {series.fmt.value} series")
    if any(k not in series.maps for k in numbers):
        return None
    return [series.maps[k] for k in numbers]


def _scope_void(spec: MarketSpec) -> Resolution:
    which = spec.map_number if spec.map_number is not None else spec.maps_scope
    return Resolution(VOID, f"not every map of the market ({which}) was played")


def _kills(player_id: int, maps: list[MapActual]) -> int:
    total = 0
    for m in maps:
        kills = m.player_kills.get(player_id)
        if kills is None:
            raise _Undecidable(f"no kill statistics for player {player_id} on map {m.map_number}")
        if kills < 0:
            raise _Undecidable(f"negative kills for player {player_id} on map {m.map_number}")
        total += kills
    return total


def _player_kills(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    if spec.player_id is None:
        raise _Undecidable("player_kills needs player_id")
    line = _line(spec)
    if selection not in (SELECTION_OVER, SELECTION_UNDER):
        raise _Undecidable(f"selection {selection!r} is not 'over' / 'under'")
    maps = _scope_maps(spec, series)
    if maps is None:
        return _scope_void(spec)
    return _over_under(_kills(spec.player_id, maps), line, selection)


def _player_kills_h2h(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    a, b = spec.player_id, spec.opponent_player_id
    if a is None or b is None or a == b:
        raise _Undecidable("player_kills_h2h needs two different players")
    chosen = parse_player_selection(selection)
    if chosen not in (a, b):
        raise _Undecidable(f"selection {selection!r} is neither player {a} nor player {b}")
    handicap = 0.0 if spec.line is None else _line(spec)
    maps = _scope_maps(spec, series)
    if maps is None:
        return _scope_void(spec)
    result = compare_to_line(_kills(a, maps) + handicap, _kills(b, maps))
    if result == 0:
        if rules.h2h_tie == "push":
            return Resolution(PUSH, "the two players finished level")
        return Resolution(LOST, "the two players finished level (ties lose)")
    return _decided((result > 0) == (chosen == a))


def _top_fragger(spec: MarketSpec, selection: str, series: _Series, rules: _Rules) -> Resolution:
    pid = parse_player_selection(selection)
    if pid is None:
        raise _Undecidable(f"selection {selection!r} is not 'player:<id>'")
    if spec.player_id is not None and spec.player_id != pid:
        raise _Undecidable(f"spec is for player {spec.player_id} but the selection is player {pid}")
    maps = _scope_maps(spec, series)
    if maps is None:
        return _scope_void(spec)
    team = None if spec.team is None else TeamSlot(spec.team)
    teams = rules.player_teams
    if team is not None:
        if teams is None:
            raise _Undecidable("a team-restricted top-fragger market needs player_teams")
        if teams.get(pid) is not team:
            raise _Undecidable(f"player {pid} is not known to play for {team.value}")
    totals: dict[int, int] = {}
    complete = True  # statistics cover full teams on every map (required to declare a winner)
    for m in maps:
        if not m.player_kills:
            raise _Undecidable(f"no kill statistics for map {m.map_number}")
        if pid not in m.player_kills:
            raise _Undecidable(f"no kill statistics for player {pid} on map {m.map_number}")
        listed = 0
        for player, kills in m.player_kills.items():
            if kills < 0:
                raise _Undecidable(f"negative kills for player {player} on map {m.map_number}")
            if team is not None and (teams is None or teams.get(player) is not team):
                continue
            listed += 1
            totals[player] = totals.get(player, 0) + kills
        if listed < (TEAM_SIZE if team is not None else 2 * TEAM_SIZE):
            complete = False
    mine = totals[pid]
    best_other = max((k for player, k in totals.items() if player != pid), default=None)
    if best_other is not None and mine < best_other:
        return Resolution(LOST)
    if not complete:
        raise _Undecidable("kill statistics do not cover full teams - cannot confirm the top fragger")
    if best_other is not None and mine == best_other:
        raise _Undecidable("tie for top fragger: dead-heat rules apply - settle from the bookmaker")
    return Resolution(WON)


_HANDLERS: dict[MarketType, _Handler] = {
    MarketType.MATCH_WINNER: _match_winner,
    MarketType.MAP_WINNER: _map_winner,
    MarketType.CORRECT_MAP_SCORE: _correct_map_score,
    MarketType.MAP_HANDICAP: _map_handicap,
    MarketType.TOTAL_MAPS: _total_maps,
    MarketType.BOTH_TEAMS_WIN_A_MAP: _both_teams_win_a_map,
    MarketType.ROUND_HANDICAP: _round_handicap,
    MarketType.TOTAL_ROUNDS: _total_rounds,
    MarketType.TEAM_TOTAL_ROUNDS: _team_total_rounds,
    MarketType.TOTAL_ROUNDS_ODD_EVEN: _odd_even,
    MarketType.MAP_CORRECT_SCORE: _map_correct_score,
    MarketType.MAP_OVERTIME: _map_overtime,
    MarketType.PISTOL_ROUND_WINNER: _pistol_round,
    MarketType.RACE_TO_ROUNDS: _race_to_rounds,
    MarketType.HALF_WINNER: _half_winner,
    MarketType.PLAYER_KILLS: _player_kills,
    MarketType.PLAYER_KILLS_H2H: _player_kills_h2h,
    MarketType.TOP_FRAGGER: _top_fragger,
}
