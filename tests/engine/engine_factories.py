"""Hand-built predictions, quotes and DB rows for the engine tests.

Map slots use a fixed round-score template so expected probabilities can be computed by hand:

=========  =============  =========  ===============
team1 win  share of q      team2 win  share of 1 - q
=========  =============  =========  ===============
13-5       0.2             7-13       0.25
13-8       0.3             10-13      0.35
13-11      0.3             11-13      0.20
16-14      0.2             14-16      0.20
=========  =============  =========  ===============

(``q`` = P(team1 wins the map | played); P(overtime) = 0.2 for every map.)
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count

from sqlalchemy.orm import Session

from cs2bot.db.models import Bet, BookmakerEvent, Match
from cs2bot.domain.betting import BetDecision, PricedOutcome
from cs2bot.domain.enums import BetStatus, Bookmaker, LinkStatus, MatchFormat, TeamSlot
from cs2bot.domain.markets import (
    SELECTION_DRAW,
    SELECTION_TEAM1,
    SELECTION_TEAM2,
    MarketSpec,
    MarketType,
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

T0 = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)

TEAM1_WIN_SCORES: dict[str, float] = {"13-5": 0.2, "13-8": 0.3, "13-11": 0.3, "16-14": 0.2}
TEAM2_WIN_SCORES: dict[str, float] = {"7-13": 0.25, "10-13": 0.35, "11-13": 0.2, "14-16": 0.2}

TEAM1_PLAYERS = (101, 102, 103, 104, 105)
TEAM2_PLAYERS = (201, 202, 203, 204, 205)

_ids = count(1)


def slot_score_pmf(q: float) -> dict[str, float]:
    pmf = {k: v * q for k, v in TEAM1_WIN_SCORES.items()}
    pmf.update({k: v * (1.0 - q) for k, v in TEAM2_WIN_SCORES.items()})
    return pmf


def series_distribution(fmt: MatchFormat, q: list[float]) -> tuple[dict[str, float], list[float]]:
    """Exact final-series pmf ("a-b") and P(map k played) for independent maps."""
    need = fmt.maps_to_win
    running: dict[tuple[int, int], float] = {(0, 0): 1.0}
    final: dict[tuple[int, int], float] = defaultdict(float)
    p_played: list[float] = []
    for k in range(fmt.n_maps):
        p_played.append(sum(running.values()))
        nxt: dict[tuple[int, int], float] = defaultdict(float)
        for (a, b), p in running.items():
            for da, db, pp in ((1, 0, q[k]), (0, 1, 1.0 - q[k])):
                s = (a + da, b + db)
                if need is not None and max(s) >= need:
                    final[s] += p * pp
                else:
                    nxt[s] += p * pp
        running = nxt
    for s, p in running.items():
        final[s] += p
    return {f"{a}-{b}": p for (a, b), p in final.items() if p > 0}, p_played


def kills_pmf(center: int, spread: int = 2) -> dict[str, float]:
    """Uniform pmf over {center - spread, center, center + spread} (+ center + 2*spread)."""
    values = [center - spread, center, center + spread, center + 2 * spread]
    return {str(v): 0.25 for v in values}


def projection(player_id: int, team: TeamSlot, pmf: dict[str, float]) -> PlayerProjection:
    mean = sum(int(k) * p for k, p in pmf.items())
    var = sum((int(k) - mean) ** 2 * p for k, p in pmf.items())
    return PlayerProjection(player_id=player_id, team=team, kills_pmf=pmf, kills_mean=mean, kills_var=var)


def slot_players(map_number: int) -> dict[str, PlayerProjection]:
    out: dict[str, PlayerProjection] = {}
    for i, pid in enumerate(TEAM1_PLAYERS):
        out[str(pid)] = projection(pid, TeamSlot.TEAM1, kills_pmf(14 + i + map_number))
    for i, pid in enumerate(TEAM2_PLAYERS):
        out[str(pid)] = projection(pid, TeamSlot.TEAM2, kills_pmf(13 + i))
    return out


def make_slot(map_number: int, q: float, p_played: float, *, with_players: bool = True) -> MapSlotPrediction:
    return MapSlotPrediction(
        map_number=map_number,
        p_played=p_played,
        map_distribution={"mirage": 0.6, "inferno": 0.4},
        p_team1_win=q,
        score_pmf=slot_score_pmf(q),
        p_overtime=0.2,
        p_team1_pistol={"1": 0.55, "13": 0.48},
        p_team1_half={"1": 0.5, "2": 0.45},
        p_half_draw={"1": 0.2, "2": 0.18},
        # race to 16 happens only in OT maps: team1 first = P(16-14), team2 first = P(14-16)
        p_team1_race_to={"5": 0.58, "13": q, "16": 0.2 * q},
        players=slot_players(map_number) if with_players else {},
    )


def make_core(
    fmt: MatchFormat = MatchFormat.BO3,
    q: list[float] | None = None,
    *,
    with_players: bool = True,
) -> PredictionCore:
    q = q or [0.6, 0.55, 0.5, 0.45, 0.4][: fmt.n_maps]
    pmf, p_played = series_distribution(fmt, q)
    if fmt is MatchFormat.BO2:
        p1, p_draw = pmf.get("2-0", 0.0), pmf.get("1-1", 0.0)
    else:
        need = fmt.maps_to_win
        assert need is not None
        p1 = sum(p for k, p in pmf.items() if int(k.split("-")[0]) == need)
        p_draw = 0.0
    slots = [make_slot(k + 1, q[k], p_played[k], with_players=with_players) for k in range(fmt.n_maps)]
    by_scope: dict[str, dict[str, PlayerProjection]] = {}
    top: dict[str, dict[str, float]] = {}
    if with_players:
        by_scope["maps:1,2"] = {
            "101": projection(101, TeamSlot.TEAM1, {"30": 0.2, "33": 0.3, "35": 0.3, "38": 0.2}),
            "201": projection(201, TeamSlot.TEAM2, {"28": 0.25, "33": 0.25, "34": 0.25, "40": 0.25}),
        }
        by_scope["match"] = {
            "101": projection(101, TeamSlot.TEAM1, {"30": 0.1, "36": 0.4, "45": 0.3, "52": 0.2}),
            "201": projection(201, TeamSlot.TEAM2, {"29": 0.3, "36": 0.3, "44": 0.4}),
        }
        if fmt.n_maps >= 3:
            by_scope["maps:1,2,3"] = {
                "101": projection(101, TeamSlot.TEAM1, {"45": 0.5, "55": 0.5}),
            }
        top["match"] = {"101": 0.3, "102": 0.15, "103": 0.1, "201": 0.25, "202": 0.2}
        top["map:1"] = {"101": 0.28, "102": 0.12, "103": 0.1, "201": 0.3, "202": 0.2}
        top["match|team1"] = {"101": 0.45, "102": 0.25, "103": 0.15, "104": 0.1, "105": 0.05}
        top["match|team2"] = {"201": 0.4, "202": 0.3, "203": 0.1, "204": 0.1, "205": 0.1}
    return PredictionCore(
        p_team1_win=p1,
        p_draw=p_draw,
        map_score_pmf=pmf,
        slots=slots,
        player_kills_by_scope=by_scope,
        top_fragger=top,
    )


def make_prediction(
    fmt: MatchFormat = MatchFormat.BO3,
    q: list[float] | None = None,
    *,
    shift: float | None = 0.05,
    sigma_team1_win: float = 0.04,
    with_players: bool = True,
    match_id: int = 1,
) -> MatchPrediction:
    """Prediction with central maps ``q`` and low/high scenarios at ``q -/+ shift`` (None = no scenarios)."""
    q = q or [0.6, 0.55, 0.5, 0.45, 0.4][: fmt.n_maps]
    low = high = None
    if shift is not None:
        low = make_core(fmt, [x - shift for x in q], with_players=with_players)
        high = make_core(fmt, [x + shift for x in q], with_players=with_players)
    return MatchPrediction(
        match_id=match_id,
        created_at=T0,
        model_version="test",
        format=fmt,
        team1_id=1,
        team2_id=2,
        central=make_core(fmt, q, with_players=with_players),
        low=low,
        high=high,
        sigma_team1_win=sigma_team1_win,
        veto_known=True,
        data_completeness=1.0,
    )


# ------------------------------------------------------------------ orientation mirror


def _flip_score_key(key: str) -> str:
    a, b = key.split("-")
    return f"{b}-{a}"


def _flip_projection(p: PlayerProjection) -> PlayerProjection:
    return p.model_copy(update={"team": p.team.other})


def mirror_slot(slot: MapSlotPrediction) -> MapSlotPrediction:
    neither = {
        k: sum(p for s, p in slot.score_pmf.items() if max(map(int, s.split("-"))) < int(k))
        for k in slot.p_team1_race_to
    }
    return slot.model_copy(
        update={
            "p_team1_win": 1.0 - slot.p_team1_win,
            "score_pmf": {_flip_score_key(k): v for k, v in slot.score_pmf.items()},
            "p_team1_pistol": {k: 1.0 - v for k, v in slot.p_team1_pistol.items()},
            "p_team1_half": {k: 1.0 - v - slot.p_half_draw[k] for k, v in slot.p_team1_half.items()},
            "p_team1_race_to": {k: 1.0 - v - neither[k] for k, v in slot.p_team1_race_to.items()},
            "players": {k: _flip_projection(v) for k, v in slot.players.items()},
        }
    )


def mirror_core(core: PredictionCore) -> PredictionCore:
    top: dict[str, dict[str, float]] = {}
    for key, table in core.top_fragger.items():
        new_key = key.replace("|team1", "|TMP").replace("|team2", "|team1").replace("|TMP", "|team2")
        top[new_key] = dict(table)
    return PredictionCore(
        p_team1_win=1.0 - core.p_team1_win - core.p_draw,
        p_draw=core.p_draw,
        map_score_pmf={_flip_score_key(k): v for k, v in core.map_score_pmf.items()},
        slots=[mirror_slot(s) for s in core.slots],
        player_kills_by_scope={
            scope: {pid: _flip_projection(p) for pid, p in table.items()}
            for scope, table in core.player_kills_by_scope.items()
        },
        top_fragger=top,
    )


def mirror_prediction(pred: MatchPrediction) -> MatchPrediction:
    return pred.model_copy(
        update={
            "team1_id": pred.team2_id,
            "team2_id": pred.team1_id,
            "central": mirror_core(pred.central),
            "low": mirror_core(pred.high) if pred.high is not None else None,
            "high": mirror_core(pred.low) if pred.low is not None else None,
        }
    )


_HANDICAP_MARKETS = {MarketType.MAP_HANDICAP, MarketType.ROUND_HANDICAP}


def mirror_spec(spec: MarketSpec) -> MarketSpec:
    update: dict[str, object] = {}
    if spec.market_type in _HANDICAP_MARKETS and spec.line is not None:
        update["line"] = -spec.line
    if spec.team is not None:
        update["team"] = SELECTION_TEAM2 if spec.team == SELECTION_TEAM1 else SELECTION_TEAM1
    return spec.model_copy(update=update)


def mirror_selection(selection: str) -> str:
    if selection == SELECTION_TEAM1:
        return SELECTION_TEAM2
    if selection == SELECTION_TEAM2:
        return SELECTION_TEAM1
    if selection == SELECTION_DRAW:
        return selection
    score = parse_score_selection(selection)
    if score is not None:
        return f"{score[1]}-{score[0]}"
    return selection


# ------------------------------------------------------------------ quotes / decisions


def make_quote(
    spec: MarketSpec | None = None,
    selection: str = SELECTION_TEAM1,
    *,
    odds: str | Decimal = "1.60",
    captured_at: datetime | None = None,
    is_available: bool = True,
) -> OddsQuote:
    n = next(_ids)
    return OddsQuote(
        selection_id=n,
        bookmaker_event_id=1,
        external_event_id="ev-1",
        market_raw_name="Zwycięzca meczu",
        selection_raw_name=selection,
        spec=spec or MarketSpec(market_type=MarketType.MATCH_WINNER),
        selection_key=selection,
        odds=Decimal(str(odds)),
        captured_at=captured_at or T0,
        is_available=is_available,
    )


def make_outcome(
    spec: MarketSpec | None = None,
    selection: str = SELECTION_TEAM1,
    *,
    p_win: float = 0.8,
    p_push: float = 0.0,
    p_void: float = 0.0,
    sigma: float = 0.02,
) -> OutcomeProbability:
    spec = spec or MarketSpec(market_type=MarketType.MATCH_WINNER)
    return OutcomeProbability(
        spec_key=spec.key(), selection=selection, p_win=p_win, p_push=p_push, p_void=p_void, sigma=sigma
    )


def make_priced(
    *,
    spec: MarketSpec | None = None,
    selection: str = SELECTION_TEAM1,
    p_win: float = 0.8,
    p_void: float = 0.0,
    sigma: float = 0.02,
    odds: str = "1.60",
    ev: float | None = None,
    kelly_full: float | None = None,
    captured_at: datetime | None = None,
    is_available: bool = True,
) -> PricedOutcome:
    from cs2bot.engine.ev import TaxModel, price

    spec = spec or MarketSpec(market_type=MarketType.MATCH_WINNER)
    outcome = make_outcome(spec, selection, p_win=p_win, p_void=p_void, sigma=sigma)
    quote = make_quote(spec, selection, odds=odds, captured_at=captured_at, is_available=is_available)
    priced = price(outcome, quote, TaxModel(Decimal("0.12"), Decimal("2280"), Decimal("0.10")))
    update: dict[str, object] = {}
    if ev is not None:
        update["ev"] = ev
    if kelly_full is not None:
        update["kelly_full"] = kelly_full
    return priced.model_copy(update=update) if update else priced


def make_decision(
    *,
    match_id: int = 1,
    kelly_full: float = 0.3,
    odds: str = "1.60",
    p_win: float = 0.8,
    accepted: bool = True,
    decided_at: datetime | None = None,
) -> BetDecision:
    priced = make_priced(odds=odds, p_win=p_win, kelly_full=kelly_full)
    return BetDecision(
        priced=priced,
        match_id=match_id,
        accepted=accepted,
        reasons=[] if accepted else ["low_ev: test"],
        decided_at=decided_at or T0,
    )


# ------------------------------------------------------------------ DB rows


def add_match(
    session: Session, *, match_id: int | None = None, scheduled_at: datetime | None = None
) -> Match:
    match = Match(id=match_id, scheduled_at=scheduled_at or T0 + timedelta(hours=3), format=MatchFormat.BO3)
    session.add(match)
    session.flush()
    return match


def add_event(
    session: Session,
    match_id: int | None,
    *,
    confidence: float = 0.97,
    status: LinkStatus = LinkStatus.AUTO,
) -> BookmakerEvent:
    event = BookmakerEvent(
        bookmaker=Bookmaker.BETCLIC,
        external_id=f"ev-{next(_ids)}",
        name="A - B",
        team1_raw="A",
        team2_raw="B",
        match_id=match_id,
        link_confidence=confidence,
        link_status=status,
        first_seen_at=T0,
        last_seen_at=T0,
    )
    session.add(event)
    session.flush()
    return event


def add_bet(
    session: Session,
    *,
    stake: str | Decimal = "10.00",
    status: BetStatus = BetStatus.PLACED,
    match_id: int | None = None,
    dry_run: bool = False,
    placed_at: datetime | None = None,
    created_at: datetime | None = None,
    settled_at: datetime | None = None,
    pnl: str | Decimal | None = None,
    payout: str | Decimal | None = None,
    odds: str | Decimal = "2.00",
    spec_key: str = "match_winner",
    selection_key: str = "team1",
    tax: str | Decimal | None = None,
    extra: dict[str, object] | None = None,
) -> Bet:
    stake_d = Decimal(str(stake))
    bet = Bet(
        match_id=match_id,
        external_event_id="ev-1",
        market_raw="Zwycięzca meczu",
        selection_raw="A",
        spec_key=spec_key,
        selection_key=selection_key,
        requested_odds=Decimal(str(odds)),
        min_acceptable_odds=Decimal(str(odds)),
        placed_odds=Decimal(str(odds)),
        stake=stake_d,
        tax=Decimal(str(tax)) if tax is not None else (stake_d * Decimal("0.12")).quantize(Decimal("0.01")),
        status=status,
        dry_run=dry_run,
        idempotency_key=f"idem-{next(_ids)}",
        placed_at=placed_at,
        settled_at=settled_at,
        pnl=Decimal(str(pnl)) if pnl is not None else None,
        payout=Decimal(str(payout)) if payout is not None else None,
        extra=extra or {},
    )
    if created_at is not None:
        bet.created_at = created_at
    session.add(bet)
    session.flush()
    return bet
