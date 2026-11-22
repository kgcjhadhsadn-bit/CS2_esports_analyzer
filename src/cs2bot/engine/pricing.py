"""Pricing engine: turn a :class:`MatchPrediction` into the probability of any bettable outcome.

Every Betclic outcome reaches us as ``(MarketSpec, selection)`` in *our* orientation (``team1`` /
``team2`` = ``matches.team1_id`` / ``team2_id``; the odds mapper already handled swapped bookmaker
events). :func:`price_outcome` returns *unconditional* probabilities ``p_win``, ``p_push`` (stake
refunded because the result lands exactly on an integer line) and ``p_void`` (stake refunded because
the market never settles, e.g. map 3 of a 2-0 series), so ``p_lose = 1 - p_win - p_push - p_void``.

Conventions used here (see also :mod:`cs2bot.domain.markets` and :mod:`cs2bot.domain.predictions`)
---------------------------------------------------------------------------------------------
* All per-slot distributions (``MapSlotPrediction``) are conditional on that map being played; a
  map-scoped bet is void with probability ``1 - p_played``:
  ``p_win = p_played * p(win | played)`` and ``p_void = (1 - p_played) + p_played * p(void | played)``.
* Handicap lines are from team1's perspective: selection ``team1`` wins when
  ``margin_team1 + line > 0``, selection ``team2`` when ``margin_team1 + line < 0``; equality
  (only possible on integer lines) is a push. Totals: ``over`` wins when ``value > line``, ``under``
  when ``value < line``, equality pushes.
* Round markets include overtime (the final ``score_pmf`` includes OT scores). The totals family
  (``TOTAL_ROUNDS``, ``TEAM_TOTAL_ROUNDS``, ``TOTAL_ROUNDS_ODD_EVEN``) can be priced on regulation
  time only (``include_overtime_in_totals=False``): in MR12 a map that reaches overtime was 12-12
  after regulation, so its regulation total is 24 and each team's regulation rounds are 12.
* Player markets choose their scope from the spec: ``map_number=k`` -> ``"map:k"`` (the slot's
  ``players`` first, then ``player_kills_by_scope["map:k"]``), ``maps_scope=(1, 2)`` ->
  ``"maps:1,2"``, neither -> ``"match"``. Distributions of a ``"map:k"`` scope are conditional on map
  k being played, those of a ``"maps:a,b,..."`` scope on *every* listed map being played (the bet is
  void otherwise, i.e. with probability ``1 - P(last listed map is played)``), ``"match"`` is
  unconditional. Head-to-head kill markets treat the two players' kill counts as independent.
* Top-fragger probabilities are read as given (they are expected to already include the
  bookmaker's dead-heat treatment of ties). A team-specific top-fragger market (``spec.team``) reads
  the table stored under ``"<scope>|<team>"``, e.g. ``top_fragger["match|team1"]``.

Uncertainty
-----------
``MatchPrediction.low`` / ``high`` shift team1's strength by -1 / +1 standard deviation on every
map. The same outcome is priced under both scenarios and ``sigma = max(sigma_floor,
|p_high - p_low| / 2)`` on the unconditional ``p_win``. That central difference is the first-order
(slope) term and vanishes for outcomes that peak near the central strength - e.g. "the map goes to
overtime" in an even match, where both scenarios *lower* the probability. Writing
``p(z) ~ p_c + a z + c z^2`` with ``z ~ N(0, 1)``, ``a = (p_high - p_low) / 2`` and
``c = (p_high + p_low - 2 p_c) / 2``, the quadratic part alone has standard deviation ``sqrt(2) |c|``;
the sigma used is ``max(|a|, sqrt(2) |c|)`` (never below the formula above, identical to it unless the
curvature dominates the slope). With only one scenario available the one-sided distance
``|p_scenario - p_central|`` is used. Without scenarios (or when the outcome cannot be
priced under them) a delta-method approximation is used: the match-winner uncertainty is converted
into a logit-scale strength uncertainty ``s = sigma_team1_win / (p_m (1 - p_m))`` (``p_m`` = central
P(team1 wins), denominator floored at 0.01, ``s`` capped at 2) and mapped through the outcome's own
Bernoulli slope: ``sigma ~ p_win (1 - p_win) s``. This assumes the outcome moves with team strength
like the match winner does, which over-states the uncertainty of strength-insensitive outcomes
(odd/even, totals) - the conservative direction for a betting filter.

Anything the prediction cannot support raises :class:`UnpriceableMarket`; such outcomes are never
bet (``price_quotes`` skips them and logs the reason).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from cs2bot.domain.enums import MatchFormat
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
from cs2bot.domain.odds import OddsQuote
from cs2bot.domain.predictions import (
    MapSlotPrediction,
    MatchPrediction,
    OutcomeProbability,
    PlayerProjection,
    PredictionCore,
)
from cs2bot.utils.logging import get_logger

logger = get_logger(__name__)

H2HTie = Literal["push", "lose"]

#: Rounds in MR12 regulation time (two halves of 12).
REGULATION_ROUNDS = 24
#: Rounds needed to win a map in regulation time.
ROUNDS_TO_WIN_REGULATION = 13
#: First overtime is won at 16 (MR3 overtimes: first to 4 of 6 rounds, then +3 per extra overtime).
FIRST_OVERTIME_WIN = 16
#: A pmf whose probabilities sum further than this from 1 is treated as broken (otherwise it is
#: renormalised; small deficits come from truncated overtime tails).
PMF_TOLERANCE = 0.02
#: Cap on the logit-scale strength sigma used by the fallback uncertainty approximation.
MAX_FALLBACK_LOGIT_SIGMA = 2.0
_MIN_BERNOULLI_VARIANCE = 0.01
_EPS = 1e-9


class UnpriceableMarket(ValueError):
    """The prediction does not contain what is needed to price this market/selection."""


@dataclass(frozen=True)
class OutcomeSplit:
    """Unconditional win / push / void probabilities of one outcome under one strength scenario."""

    p_win: float
    p_push: float = 0.0
    p_void: float = 0.0

    @property
    def p_lose(self) -> float:
        return max(0.0, 1.0 - self.p_win - self.p_push - self.p_void)


@dataclass(frozen=True)
class PricingReport:
    """Result of :func:`price_quotes_report`: priced quotes plus the quotes that were skipped."""

    priced: list[tuple[OutcomeProbability, OddsQuote]] = field(default_factory=list)
    skipped: list[tuple[OddsQuote, str]] = field(default_factory=list)


@dataclass(frozen=True)
class _Ctx:
    fmt: MatchFormat
    include_ot: bool
    h2h_tie: H2HTie


@dataclass(frozen=True)
class _Scope:
    key: str  # "map:1", "maps:1,2", "match"
    p_all_played: float
    slot: MapSlotPrediction | None = None  # set for single-map scopes


_Handler = Callable[[MarketSpec, str, PredictionCore, _Ctx], OutcomeSplit]


# ----------------------------------------------------------------------------------------------
# public API
# ----------------------------------------------------------------------------------------------


def price_outcome(
    spec: MarketSpec,
    selection: str,
    prediction: MatchPrediction,
    *,
    sigma_floor: float,
    include_overtime_in_totals: bool = True,
    h2h_tie: str = "push",
) -> OutcomeProbability:
    """Price one outcome from ``prediction.central`` with an uncertainty from ``low`` / ``high``.

    ``h2h_tie`` decides how a tie in a head-to-head kills market settles: ``"push"`` (stake
    refunded) or ``"lose"``. Raises :class:`UnpriceableMarket` when the prediction cannot support
    the outcome and ``ValueError`` for invalid arguments.
    """
    ctx = _context(prediction.format, include_overtime_in_totals, h2h_tie)
    if not math.isfinite(sigma_floor) or sigma_floor < 0:
        raise ValueError(f"sigma_floor must be a non-negative number, got {sigma_floor!r}")
    central = _split(spec, selection, prediction.central, ctx)
    sigma = _outcome_sigma(spec, selection, prediction, ctx, central, sigma_floor)
    return OutcomeProbability(
        spec_key=spec.key(),
        selection=selection,
        p_win=central.p_win,
        p_push=central.p_push,
        p_void=central.p_void,
        sigma=sigma,
    )


def price_core(
    spec: MarketSpec,
    selection: str,
    core: PredictionCore,
    *,
    fmt: MatchFormat,
    include_overtime_in_totals: bool = True,
    h2h_tie: str = "push",
) -> OutcomeSplit:
    """Price one outcome under a single scenario (no uncertainty). Used by backtests and tests."""
    return _split(spec, selection, core, _context(fmt, include_overtime_in_totals, h2h_tie))


def price_quotes_report(
    prediction: MatchPrediction,
    quotes: Iterable[OddsQuote],
    *,
    sigma_floor: float,
    include_overtime_in_totals: bool = True,
    h2h_tie: str = "push",
) -> PricingReport:
    """Price every quote; unpriceable quotes are logged and returned in ``skipped`` with the reason."""
    report = PricingReport()
    for quote in quotes:
        try:
            outcome = price_outcome(
                quote.spec,
                quote.selection_key,
                prediction,
                sigma_floor=sigma_floor,
                include_overtime_in_totals=include_overtime_in_totals,
                h2h_tie=h2h_tie,
            )
        except UnpriceableMarket as exc:
            logger.info(
                "match %s: not pricing %s#%s (bookmaker selection %s, %r / %r): %s",
                prediction.match_id,
                quote.spec.key(),
                quote.selection_key,
                quote.selection_id,
                quote.market_raw_name,
                quote.selection_raw_name,
                exc,
            )
            report.skipped.append((quote, str(exc)))
            continue
        report.priced.append((outcome, quote))
    return report


def price_quotes(
    prediction: MatchPrediction,
    quotes: Iterable[OddsQuote],
    *,
    sigma_floor: float,
    include_overtime_in_totals: bool = True,
    h2h_tie: str = "push",
) -> list[tuple[OutcomeProbability, OddsQuote]]:
    """Price every quote, skipping (and logging) the unpriceable ones."""
    return price_quotes_report(
        prediction,
        quotes,
        sigma_floor=sigma_floor,
        include_overtime_in_totals=include_overtime_in_totals,
        h2h_tie=h2h_tie,
    ).priced


# ----------------------------------------------------------------------------------------------
# uncertainty
# ----------------------------------------------------------------------------------------------


def fallback_sigma(p_win: float, prediction: MatchPrediction) -> float:
    """Delta-method sigma used when the prediction has no usable low/high scenarios (module doc)."""
    p_match = min(1.0, max(0.0, prediction.central.p_team1_win))
    variance = max(p_match * (1.0 - p_match), _MIN_BERNOULLI_VARIANCE)
    sigma_match = prediction.sigma_team1_win
    if not math.isfinite(sigma_match) or sigma_match < 0:
        raise UnpriceableMarket(f"sigma_team1_win = {sigma_match!r} is not a valid standard deviation")
    logit_sigma = min(sigma_match / variance, MAX_FALLBACK_LOGIT_SIGMA)
    return p_win * (1.0 - p_win) * logit_sigma


def _outcome_sigma(
    spec: MarketSpec,
    selection: str,
    prediction: MatchPrediction,
    ctx: _Ctx,
    central: OutcomeSplit,
    sigma_floor: float,
) -> float:
    scenarios: dict[str, float] = {}
    for name, core in (("low", prediction.low), ("high", prediction.high)):
        if core is None:
            continue
        try:
            scenarios[name] = _split(spec, selection, core, ctx).p_win
        except UnpriceableMarket as exc:
            logger.debug("%s#%s unpriceable under the %s scenario: %s", spec.key(), selection, name, exc)
    if len(scenarios) == 2:
        slope = abs(scenarios["high"] - scenarios["low"]) / 2.0
        curvature = abs(scenarios["high"] + scenarios["low"] - 2.0 * central.p_win) / math.sqrt(2.0)
        spread = max(slope, curvature)
    elif len(scenarios) == 1:
        spread = abs(next(iter(scenarios.values())) - central.p_win)
    else:
        spread = fallback_sigma(central.p_win, prediction)
    return max(sigma_floor, spread)


# ----------------------------------------------------------------------------------------------
# dispatch + validation helpers
# ----------------------------------------------------------------------------------------------


def _context(fmt: MatchFormat, include_ot: bool, h2h_tie: str) -> _Ctx:
    if h2h_tie == "push":
        return _Ctx(fmt, include_ot, "push")
    if h2h_tie == "lose":
        return _Ctx(fmt, include_ot, "lose")
    raise ValueError(f"h2h_tie must be 'push' or 'lose', got {h2h_tie!r}")


def _split(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    handler = _HANDLERS.get(spec.market_type)
    if handler is None:
        raise UnpriceableMarket(f"market type {spec.market_type.value!r} is never priced")
    raw = handler(spec, selection, core, ctx)
    return _finalise(raw, f"{spec.key()}#{selection}")


def _finalise(split: OutcomeSplit, what: str) -> OutcomeSplit:
    """Validate and clip float noise so that the three probabilities are a sub-distribution."""
    vals = [split.p_win, split.p_push, split.p_void]
    for v in vals:
        if not math.isfinite(v) or v < -1e-6 or v > 1 + 1e-6:
            raise UnpriceableMarket(f"{what}: invalid probabilities {split}")
    p_win, p_push, p_void = (min(1.0, max(0.0, v)) for v in vals)
    excess = p_win + p_push + p_void - 1.0
    if excess > 1e-6:
        raise UnpriceableMarket(f"{what}: probabilities sum to {1.0 + excess:.6f} > 1")
    if excess > 0:  # float noise: take it from the largest component
        if p_win >= p_void and p_win >= p_push:
            p_win -= excess
        elif p_void >= p_push:
            p_void -= excess
        else:
            p_push -= excess
    return OutcomeSplit(p_win=p_win, p_push=p_push, p_void=p_void)


def _prob(value: float, what: str) -> float:
    v = float(value)
    if not math.isfinite(v) or v < -_EPS or v > 1.0 + _EPS:
        raise UnpriceableMarket(f"{what} = {value!r} is not a probability")
    return min(1.0, max(0.0, v))


def _normalise(raw: Mapping[str, float], what: str) -> dict[str, float]:
    if not raw:
        raise UnpriceableMarket(f"{what} is empty")
    clean: dict[str, float] = {}
    for key, value in raw.items():
        v = float(value)
        if not math.isfinite(v) or v < -_EPS:
            raise UnpriceableMarket(f"{what}[{key!r}] = {value!r} is not a probability")
        clean[key] = max(v, 0.0)
    total = math.fsum(clean.values())
    if abs(total - 1.0) > PMF_TOLERANCE:
        raise UnpriceableMarket(f"{what} sums to {total:.4f} instead of 1")
    return {k: v / total for k, v in clean.items()}


def _score_items(raw: Mapping[str, float], what: str) -> dict[tuple[int, int], float]:
    out: dict[tuple[int, int], float] = {}
    for key, p in _normalise(raw, what).items():
        score = parse_score_selection(key)
        if score is None or min(score) < 0:
            raise UnpriceableMarket(f"{what} has an invalid score key {key!r}")
        out[score] = out.get(score, 0.0) + p
    return out


def _int_items(raw: Mapping[str, float], what: str) -> dict[int, float]:
    out: dict[int, float] = {}
    for key, p in _normalise(raw, what).items():
        try:
            value = float(key)
        except ValueError:
            raise UnpriceableMarket(f"{what} has a non-numeric key {key!r}") from None
        if not value.is_integer() or value < 0:
            raise UnpriceableMarket(f"{what} has an invalid count key {key!r}")
        out[int(value)] = out.get(int(value), 0.0) + p
    return out


def _require_line(spec: MarketSpec) -> float:
    if spec.line is None or not math.isfinite(spec.line):
        raise UnpriceableMarket(f"{spec.market_type.value} needs a line")
    return float(spec.line)


def _require_map_number(spec: MarketSpec) -> int:
    if spec.map_number is None:
        raise UnpriceableMarket(f"{spec.market_type.value} needs map_number")
    return spec.map_number


def _slot(core: PredictionCore, map_number: int, fmt: MatchFormat) -> MapSlotPrediction:
    if map_number < 1 or map_number > fmt.n_maps:
        raise UnpriceableMarket(f"map {map_number} does not exist in a {fmt.value} series")
    for slot in core.slots:
        if slot.map_number == map_number:
            return slot
    raise UnpriceableMarket(f"the prediction has no slot for map {map_number}")


def _condition(p_played: float, cond: OutcomeSplit) -> OutcomeSplit:
    """Turn probabilities conditional on the market settling into unconditional ones."""
    return OutcomeSplit(
        p_win=p_played * cond.p_win,
        p_push=p_played * cond.p_push,
        p_void=(1.0 - p_played) + p_played * cond.p_void,
    )


def _on_slot(slot: MapSlotPrediction, cond: OutcomeSplit) -> OutcomeSplit:
    return _condition(_prob(slot.p_played, f"map {slot.map_number} p_played"), cond)


def _compare(value: float, line: float) -> int:
    """+1 if ``value > line``, -1 if ``value < line``, 0 if equal (a push on integer lines)."""
    diff = value - line
    if diff > _EPS:
        return 1
    if diff < -_EPS:
        return -1
    return 0


def _over_under(items: Iterable[tuple[float, float]], line: float, selection: str) -> OutcomeSplit:
    if selection == SELECTION_OVER:
        sign = 1
    elif selection == SELECTION_UNDER:
        sign = -1
    else:
        raise UnpriceableMarket(f"selection {selection!r} is not 'over' / 'under'")
    win = push = 0.0
    for value, p in items:
        outcome = _compare(value, line) * sign
        if outcome > 0:
            win += p
        elif outcome == 0:
            push += p
    return OutcomeSplit(p_win=win, p_push=push)


def _handicap(margins: Iterable[tuple[float, float]], line: float, selection: str) -> OutcomeSplit:
    """``margins`` are (team1 margin, probability); ``line`` is team1's handicap."""
    sign = _team_sign(selection)
    win = push = 0.0
    for margin, p in margins:
        outcome = _compare(margin + line, 0.0) * sign
        if outcome > 0:
            win += p
        elif outcome == 0:
            push += p
    return OutcomeSplit(p_win=win, p_push=push)


def _team_sign(selection: str) -> int:
    if selection == SELECTION_TEAM1:
        return 1
    if selection == SELECTION_TEAM2:
        return -1
    raise UnpriceableMarket(f"selection {selection!r} is not 'team1' / 'team2'")


def _yes_no(p_yes: float, selection: str) -> OutcomeSplit:
    if selection == SELECTION_YES:
        return OutcomeSplit(p_win=p_yes)
    if selection == SELECTION_NO:
        return OutcomeSplit(p_win=1.0 - p_yes)
    raise UnpriceableMarket(f"selection {selection!r} is not 'yes' / 'no'")


def _is_overtime(team1_rounds: int, team2_rounds: int) -> bool:
    return team1_rounds + team2_rounds > REGULATION_ROUNDS


def _rounds_value(a: int, b: int, ctx: _Ctx, *, team: str | None = None) -> int:
    """Total rounds (``team=None``) or one team's rounds, regulation-only if configured."""
    if not ctx.include_ot and _is_overtime(a, b):
        return REGULATION_ROUNDS if team is None else REGULATION_ROUNDS // 2
    if team is None:
        return a + b
    return a if team == SELECTION_TEAM1 else b


def is_final_round_score(team1_rounds: int, team2_rounds: int) -> bool:
    """True for a possible final CS2 map score (MR12 + MR3 overtimes): 13-x (x <= 11), 16-12..16-14,
    19-15..19-17, ..."""
    hi, lo = max(team1_rounds, team2_rounds), min(team1_rounds, team2_rounds)
    if lo < 0:
        return False
    if hi == ROUNDS_TO_WIN_REGULATION:
        return lo <= ROUNDS_TO_WIN_REGULATION - 2
    if hi >= FIRST_OVERTIME_WIN and (hi - FIRST_OVERTIME_WIN) % 3 == 0:
        return 2 <= hi - lo <= 4
    return False


def final_map_scores(fmt: MatchFormat) -> set[tuple[int, int]]:
    """Every possible final series score (team1 maps first) of a format."""
    n, need = fmt.n_maps, fmt.maps_to_win
    if need is None:  # BO2: both maps are always played
        return {(a, n - a) for a in range(n + 1)}
    return {(need, lost) for lost in range(need)} | {(lost, need) for lost in range(need)}


def _slot_scores(slot: MapSlotPrediction) -> dict[tuple[int, int], float]:
    return _score_items(slot.score_pmf, f"map {slot.map_number} score_pmf")


def _map_scores(core: PredictionCore) -> dict[tuple[int, int], float]:
    return _score_items(core.map_score_pmf, "map_score_pmf")


# ----------------------------------------------------------------------------------------------
# match-level markets
# ----------------------------------------------------------------------------------------------


def _match_winner(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    p1 = _prob(core.p_team1_win, "p_team1_win")
    p_draw = _prob(core.p_draw, "p_draw")
    if p1 + p_draw > 1.0 + 1e-6:
        raise UnpriceableMarket("p_team1_win + p_draw exceeds 1")
    if selection == SELECTION_TEAM1:
        return OutcomeSplit(p_win=p1)
    if selection == SELECTION_TEAM2:
        return OutcomeSplit(p_win=max(0.0, 1.0 - p1 - p_draw))
    if selection == SELECTION_DRAW:
        if ctx.fmt is not MatchFormat.BO2:
            raise UnpriceableMarket(f"a series draw only exists in BO2, not {ctx.fmt.value}")
        return OutcomeSplit(p_win=p_draw)
    raise UnpriceableMarket(f"selection {selection!r} is not team1 / team2 / draw")


def _correct_map_score(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    score = parse_score_selection(selection)
    if score is None:
        raise UnpriceableMarket(f"selection {selection!r} is not a map score like '2-1'")
    pmf = _map_scores(core)
    if score not in pmf and score not in final_map_scores(ctx.fmt):
        raise UnpriceableMarket(f"{selection} is not a possible final score of a {ctx.fmt.value}")
    return OutcomeSplit(p_win=pmf.get(score, 0.0))


def _map_handicap(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    line = _require_line(spec)
    pmf = _map_scores(core)
    return _handicap(((a - b, p) for (a, b), p in pmf.items()), line, selection)


def _total_maps(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    line = _require_line(spec)
    pmf = _map_scores(core)
    return _over_under(((a + b, p) for (a, b), p in pmf.items()), line, selection)


def _both_teams_win_a_map(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    pmf = _map_scores(core)
    p_yes = math.fsum(p for (a, b), p in pmf.items() if a >= 1 and b >= 1)
    return _yes_no(p_yes, selection)


# ----------------------------------------------------------------------------------------------
# map-slot markets (void when the map is not played)
# ----------------------------------------------------------------------------------------------


def _map_winner(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    slot = _slot(core, _require_map_number(spec), ctx.fmt)
    p1 = _prob(slot.p_team1_win, f"map {slot.map_number} p_team1_win")
    p_cond = p1 if _team_sign(selection) > 0 else 1.0 - p1
    return _on_slot(slot, OutcomeSplit(p_win=p_cond))


def _round_handicap(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    slot = _slot(core, _require_map_number(spec), ctx.fmt)
    line = _require_line(spec)
    pmf = _slot_scores(slot)
    return _on_slot(slot, _handicap(((a - b, p) for (a, b), p in pmf.items()), line, selection))


def _total_rounds(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    slot = _slot(core, _require_map_number(spec), ctx.fmt)
    line = _require_line(spec)
    pmf = _slot_scores(slot)
    items = ((_rounds_value(a, b, ctx), p) for (a, b), p in pmf.items())
    return _on_slot(slot, _over_under(items, line, selection))


def _team_total_rounds(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    slot = _slot(core, _require_map_number(spec), ctx.fmt)
    if spec.team is None:
        raise UnpriceableMarket("team_total_rounds needs spec.team")
    line = _require_line(spec)
    pmf = _slot_scores(slot)
    items = ((_rounds_value(a, b, ctx, team=spec.team), p) for (a, b), p in pmf.items())
    return _on_slot(slot, _over_under(items, line, selection))


def _odd_even(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    slot = _slot(core, _require_map_number(spec), ctx.fmt)
    if selection not in (SELECTION_ODD, SELECTION_EVEN):
        raise UnpriceableMarket(f"selection {selection!r} is not 'odd' / 'even'")
    pmf = _slot_scores(slot)
    p_odd = math.fsum(p for (a, b), p in pmf.items() if _rounds_value(a, b, ctx) % 2 == 1)
    return _on_slot(slot, OutcomeSplit(p_win=p_odd if selection == SELECTION_ODD else 1.0 - p_odd))


def _map_correct_score(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    slot = _slot(core, _require_map_number(spec), ctx.fmt)
    score = parse_score_selection(selection)
    if score is None:
        raise UnpriceableMarket(f"selection {selection!r} is not a round score like '13-9'")
    pmf = _slot_scores(slot)
    if score not in pmf and not is_final_round_score(*score):
        raise UnpriceableMarket(f"{selection} is not a possible final CS2 map score")
    return _on_slot(slot, OutcomeSplit(p_win=pmf.get(score, 0.0)))


def _map_overtime(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    slot = _slot(core, _require_map_number(spec), ctx.fmt)
    p_ot = _prob(slot.p_overtime, f"map {slot.map_number} p_overtime")
    return _on_slot(slot, _yes_no(p_ot, selection))


def _pistol_round(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    slot = _slot(core, _require_map_number(spec), ctx.fmt)
    rnd = spec.round_number
    if rnd not in (1, ROUNDS_TO_WIN_REGULATION):
        raise UnpriceableMarket(f"pistol rounds are rounds 1 and 13, got round_number={rnd!r}")
    raw = slot.p_team1_pistol.get(str(rnd))
    if raw is None:
        raise UnpriceableMarket(f"no pistol-round probability for round {rnd} of map {slot.map_number}")
    p1 = _prob(raw, f"map {slot.map_number} pistol round {rnd}")
    return _on_slot(slot, OutcomeSplit(p_win=p1 if _team_sign(selection) > 0 else 1.0 - p1))


def _race_to_rounds(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    """First team to N rounds. If N > 13 the map can end before anyone reaches N: the bet is then
    void, and P(neither reaches N) = P(max(final score) < N) must be known from the score pmf."""
    slot = _slot(core, _require_map_number(spec), ctx.fmt)
    line = _require_line(spec)
    if not line.is_integer() or line < 1:
        raise UnpriceableMarket(f"race-to line must be a positive whole number of rounds, got {line:g}")
    target = int(line)
    raw = slot.p_team1_race_to.get(str(target))
    if raw is None:
        raise UnpriceableMarket(f"no race-to-{target} probability for map {slot.map_number}")
    p1 = _prob(raw, f"map {slot.map_number} race to {target}")
    if target <= ROUNDS_TO_WIN_REGULATION:
        p_neither = 0.0  # the map winner always reaches 13 >= N
    else:
        if not slot.score_pmf:
            raise UnpriceableMarket(
                f"race to {target}: cannot tell how often neither team reaches {target} "
                f"(map {slot.map_number} has no score pmf)"
            )
        pmf = _slot_scores(slot)
        p_neither = math.fsum(p for (a, b), p in pmf.items() if max(a, b) < target)
    p2 = 1.0 - p1 - p_neither
    if p2 < -1e-6:
        raise UnpriceableMarket(
            f"race to {target}: inconsistent probabilities (team1 {p1:.4f}, neither {p_neither:.4f})"
        )
    p_sel = p1 if _team_sign(selection) > 0 else max(0.0, p2)
    return _on_slot(slot, OutcomeSplit(p_win=p_sel, p_void=p_neither))


def _half_winner(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    slot = _slot(core, _require_map_number(spec), ctx.fmt)
    half = spec.half
    if half not in (1, 2):
        raise UnpriceableMarket(f"half must be 1 or 2, got {half!r}")
    key = str(half)
    raw1 = slot.p_team1_half.get(key)
    raw_draw = slot.p_half_draw.get(key)
    if selection == SELECTION_TEAM1:
        if raw1 is None:
            raise UnpriceableMarket(f"no half-{half} probability for map {slot.map_number}")
        return _on_slot(slot, OutcomeSplit(p_win=_prob(raw1, "p_team1_half")))
    if selection == SELECTION_DRAW:
        if raw_draw is None:
            raise UnpriceableMarket(f"no half-{half} draw probability for map {slot.map_number}")
        return _on_slot(slot, OutcomeSplit(p_win=_prob(raw_draw, "p_half_draw")))
    if selection == SELECTION_TEAM2:
        if raw1 is None or raw_draw is None:
            raise UnpriceableMarket(f"half-{half} team2 needs both the team1 and the draw probability")
        p1, pd = _prob(raw1, "p_team1_half"), _prob(raw_draw, "p_half_draw")
        if p1 + pd > 1.0 + 1e-6:
            raise UnpriceableMarket(f"half {half}: p_team1_half + p_half_draw exceeds 1")
        return _on_slot(slot, OutcomeSplit(p_win=max(0.0, 1.0 - p1 - pd)))
    raise UnpriceableMarket(f"selection {selection!r} is not team1 / team2 / draw")


# ----------------------------------------------------------------------------------------------
# player markets
# ----------------------------------------------------------------------------------------------


def _player_scope(spec: MarketSpec, core: PredictionCore, ctx: _Ctx) -> _Scope:
    if spec.map_number is not None:
        maps = [spec.map_number]
    elif spec.maps_scope is not None:
        maps = sorted(set(spec.maps_scope))
        if not maps:
            raise UnpriceableMarket("maps_scope is empty")
    else:
        return _Scope(key="match", p_all_played=1.0)
    if maps[0] < 1 or maps[-1] > ctx.fmt.n_maps:
        raise UnpriceableMarket(f"maps {maps} are not all part of a {ctx.fmt.value} series")
    if len(maps) == 1:
        slot = _slot(core, maps[0], ctx.fmt)
        return _Scope(
            key=f"map:{maps[0]}", p_all_played=_prob(slot.p_played, f"map {maps[0]} p_played"), slot=slot
        )
    last = maps[-1]
    last_slot = next((s for s in core.slots if s.map_number == last), None)
    if last_slot is not None:
        p_all = _prob(last_slot.p_played, f"map {last} p_played")
    elif last <= ctx.fmt.min_maps:
        p_all = 1.0  # maps up to the format's minimum are always played
    else:
        raise UnpriceableMarket(f"cannot tell how likely map {last} is to be played")
    return _Scope(key="maps:" + ",".join(str(m) for m in maps), p_all_played=p_all)


def _find_projection(core: PredictionCore, scope: _Scope, player_id: int) -> PlayerProjection | None:
    pid = str(player_id)
    if scope.slot is not None and pid in scope.slot.players:
        return scope.slot.players[pid]
    return core.player_kills_by_scope.get(scope.key, {}).get(pid)


def _projection(core: PredictionCore, scope: _Scope, player_id: int) -> PlayerProjection:
    proj = _find_projection(core, scope, player_id)
    if proj is None:
        raise UnpriceableMarket(f"no kill projection for player {player_id} in scope {scope.key!r}")
    return proj


def _kills_pmf(core: PredictionCore, scope: _Scope, player_id: int) -> dict[int, float]:
    proj = _projection(core, scope, player_id)
    return _int_items(proj.kills_pmf, f"kills pmf of player {player_id} ({scope.key})")


def _player_kills(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    if spec.player_id is None:
        raise UnpriceableMarket("player_kills needs player_id")
    line = _require_line(spec)
    scope = _player_scope(spec, core, ctx)
    pmf = _kills_pmf(core, scope, spec.player_id)
    return _condition(scope.p_all_played, _over_under(((k, p) for k, p in pmf.items()), line, selection))


def _player_kills_h2h(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    """Player A (``spec.player_id``) vs player B (``opponent_player_id``), independent kill pmfs.

    An optional ``spec.line`` is a kills handicap added to player A. ``selection`` names the player
    backed. Ties push or lose according to ``h2h_tie``.
    """
    a, b = spec.player_id, spec.opponent_player_id
    if a is None or b is None or a == b:
        raise UnpriceableMarket("player_kills_h2h needs two different players")
    chosen = parse_player_selection(selection)
    if chosen not in (a, b):
        raise UnpriceableMarket(f"selection {selection!r} is neither player {a} nor player {b}")
    handicap = 0.0 if spec.line is None else _require_line(spec)
    scope = _player_scope(spec, core, ctx)
    pmf_a = _kills_pmf(core, scope, a)
    pmf_b = _kills_pmf(core, scope, b)
    a_wins = b_wins = tie = 0.0
    for ka, pa in pmf_a.items():
        for kb, pb in pmf_b.items():
            outcome = _compare(ka + handicap, kb)
            if outcome > 0:
                a_wins += pa * pb
            elif outcome < 0:
                b_wins += pa * pb
            else:
                tie += pa * pb
    win = a_wins if chosen == a else b_wins
    cond = OutcomeSplit(p_win=win, p_push=tie) if ctx.h2h_tie == "push" else OutcomeSplit(p_win=win)
    return _condition(scope.p_all_played, cond)


def _top_fragger(spec: MarketSpec, selection: str, core: PredictionCore, ctx: _Ctx) -> OutcomeSplit:
    pid = parse_player_selection(selection)
    if pid is None:
        raise UnpriceableMarket(f"selection {selection!r} is not 'player:<id>'")
    if spec.player_id is not None and spec.player_id != pid:
        raise UnpriceableMarket(f"spec is for player {spec.player_id} but the selection is player {pid}")
    scope = _player_scope(spec, core, ctx)
    key = scope.key if spec.team is None else f"{scope.key}|{spec.team}"
    table = core.top_fragger.get(key)
    if not table:
        raise UnpriceableMarket(f"no top-fragger probabilities for scope {key!r}")
    raw = table.get(str(pid))
    if raw is None:
        raise UnpriceableMarket(f"player {pid} is not in the top-fragger table for {key!r}")
    total = math.fsum(_prob(v, f"top_fragger[{key!r}]") for v in table.values())
    if total > 1.0 + PMF_TOLERANCE:
        raise UnpriceableMarket(f"top-fragger probabilities for {key!r} sum to {total:.4f} > 1")
    if spec.team is not None:
        proj = _find_projection(core, scope, pid)
        if proj is not None and proj.team.value != spec.team:
            raise UnpriceableMarket(f"player {pid} plays for {proj.team.value}, not {spec.team}")
    return _condition(scope.p_all_played, OutcomeSplit(p_win=_prob(raw, f"top_fragger[{key!r}][{pid}]")))


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
