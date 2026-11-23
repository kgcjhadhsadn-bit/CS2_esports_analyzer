"""Fakes and hand-built objects for the llm tests (no network, no real Claude client)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
from pydantic import SecretStr
from sqlalchemy.orm import Session

from cs2bot.config import Settings
from cs2bot.db.models import Match, NewsItem, Player, Team
from cs2bot.domain.betting import BetDecision, PricedOutcome
from cs2bot.domain.context import ContextFlag, NewsItemIn
from cs2bot.domain.enums import ContextFlagType, MatchFormat, MatchStatus, PlayerRole, TeamSlot, Tier
from cs2bot.domain.markets import MarketSpec, MarketType
from cs2bot.domain.match import LineupPlayer, MatchContext
from cs2bot.domain.odds import OddsQuote
from cs2bot.domain.predictions import (
    MapSequenceProbability,
    MapSlotPrediction,
    MapWinEstimate,
    MatchPrediction,
    OutcomeProbability,
    PredictionCore,
)
from cs2bot.llm.schemas import ContextFlagSuggestion, MatchContextResult, TriageBatch, TriageItem, TriageLabel

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
START = NOW + timedelta(hours=30)

TEAM1_ID, TEAM2_ID = 1, 2
TEAM1_PLAYERS = (101, 102, 103, 104, 105)
TEAM2_PLAYERS = (201, 202, 203, 204, 205)


# ---------------------------------------------------------------------------------------------
# Fake Claude client
# ---------------------------------------------------------------------------------------------


def fake_response(
    parsed: Any = None,
    *,
    stop_reason: str = "end_turn",
    category: str | None = None,
    model: str | None = None,
    input_tokens: int = 1000,
    output_tokens: int = 200,
) -> SimpleNamespace:
    """An object shaped like the SDK's ParsedMessage (only the attributes the analyzer reads)."""
    stop_details = SimpleNamespace(type="refusal", category=category) if stop_reason == "refusal" else None
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=stop_details,
        parsed_output=parsed,
        model=model,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def refusal(category: str | None = "cyber") -> SimpleNamespace:
    return fake_response(None, stop_reason="refusal", category=category)


class _Endpoint:
    def __init__(self, owner: FakeClient, name: str) -> None:
        self._owner = owner
        self._name = name

    def parse(self, **kwargs: Any) -> Any:
        return self._owner.handle(self._name, kwargs)


class FakeClient:
    """Stands in for ``anthropic.Anthropic``: records every request, replays scripted responses.

    ``script`` is a list of responses / exceptions consumed in order, or a callable
    ``(endpoint, kwargs) -> response | exception``. Exceptions are raised.
    """

    def __init__(self, script: list[Any] | Callable[[str, dict[str, Any]], Any] | None = None) -> None:
        self.script = script if script is not None else []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.messages = _Endpoint(self, "messages")
        self.beta = SimpleNamespace(messages=_Endpoint(self, "beta.messages"))

    def handle(self, endpoint: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((endpoint, kwargs))
        if callable(self.script):
            result = self.script(endpoint, kwargs)
        else:
            if not self.script:
                raise AssertionError(f"unexpected Claude request #{len(self.calls)} to {endpoint}")
            result = self.script.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    # helpers for assertions -------------------------------------------------------------
    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1][1]

    def user_text(self, index: int = -1) -> str:
        return str(self.calls[index][1]["messages"][0]["content"])

    def system_text(self, index: int = -1) -> str:
        system = self.calls[index][1]["system"]
        if isinstance(system, str):
            return system
        return "".join(block["text"] for block in system)


def api_status_error(status: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    cls: type[anthropic.APIStatusError]
    if status == 429:
        cls = anthropic.RateLimitError
    elif status == 401:
        cls = anthropic.AuthenticationError
    elif status == 400:
        cls = anthropic.BadRequestError
    elif status >= 500:
        cls = anthropic.InternalServerError
    else:
        cls = anthropic.APIStatusError
    return cls(f"HTTP {status}", response=response, body=None)


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


def make_settings(**llm_overrides: Any) -> Settings:
    """Settings with a (fake) API key so the analyzer counts as enabled; override llm fields by name."""
    settings = Settings()
    llm = settings.llm.model_copy(update={"api_key": SecretStr("sk-test-not-a-real-key"), **llm_overrides})
    return settings.model_copy(update={"llm": llm})


# ---------------------------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------------------------


def make_ctx(**overrides: Any) -> MatchContext:
    base: dict[str, Any] = {
        "match_id": 10,
        "scheduled_at": START,
        "format": MatchFormat.BO3,
        "team1_id": TEAM1_ID,
        "team2_id": TEAM2_ID,
        "team1_name": "Alpha Wolves",
        "team2_name": "Bravo Foxes",
        "tier": Tier.S,
        "is_lan": True,
        "tournament_name": "Synthetic Major",
        "stage": "Quarter-final",
        "team1_lineup": [
            LineupPlayer(player_id=pid, nickname=f"a{pid}", role=PlayerRole.RIFLER) for pid in TEAM1_PLAYERS
        ],
        "team2_lineup": [
            LineupPlayer(player_id=pid, nickname=f"b{pid}", is_standin=(pid == 205)) for pid in TEAM2_PLAYERS
        ],
        "map_pool": ["ancient", "anubis", "dust2", "inferno", "mirage", "nuke", "train"],
    }
    base.update(overrides)
    return MatchContext(**base)


def make_news(n: int = 1, **overrides: Any) -> NewsItemIn:
    base: dict[str, Any] = {
        "source": "hltv",
        "url": f"https://www.hltv.org/news/{4000 + n}/story-{n}",
        "title": f"Story {n}",
        "body": f"Body of story {n}.",
        "published_at": NOW - timedelta(hours=10 - n),
        "team_ids": [TEAM2_ID],
        "player_ids": [],
    }
    base.update(overrides)
    return NewsItemIn(**base)


def suggestion(**overrides: Any) -> ContextFlagSuggestion:
    base: dict[str, Any] = {
        "flag_type": ContextFlagType.STAND_IN,
        "team": TeamSlot.TEAM2,
        "severity": 0.5,
        "confidence": 0.9,
        "logit_shift": 0.1,
        "veto": False,
        "rationale": "Bravo Foxes play with an emergency stand-in.",
        "evidence": ["news-1"],
        "expires_at": None,
    }
    base.update(overrides)
    return ContextFlagSuggestion(**base)


def context_result(*flags: ContextFlagSuggestion, note: str = "One stand-in reported.") -> MatchContextResult:
    return MatchContextResult(flags=list(flags), note=note)


def triage_item(news_id: int, **overrides: Any) -> TriageItem:
    base: dict[str, Any] = {
        "news_id": news_id,
        "label": TriageLabel.STAND_IN,
        "relevance": 0.8,
        "team_names": [],
        "team_ids": [],
        "summary": f"Summary of {news_id}.",
    }
    base.update(overrides)
    return TriageItem(**base)


def triage_batch(*items: TriageItem) -> TriageBatch:
    return TriageBatch(items=list(items))


def make_flag(**overrides: Any) -> ContextFlag:
    base: dict[str, Any] = {
        "flag_type": ContextFlagType.STAND_IN,
        "team": TeamSlot.TEAM2,
        "severity": 0.5,
        "confidence": 1.0,
        "logit_shift": 0.1,
        "veto": False,
        "rationale": "stand-in",
        "evidence": ["https://www.hltv.org/news/1/x"],
        "expires_at": None,
    }
    base.update(overrides)
    return ContextFlag(**base)


def _slot(map_number: int, p_played: float, map_name: str, q: float) -> MapSlotPrediction:
    return MapSlotPrediction(
        map_number=map_number,
        p_played=p_played,
        map_distribution={map_name: 0.8, "nuke": 0.2},
        p_team1_win=q,
        score_pmf={"13-7": q, "9-13": 1.0 - q},
        p_overtime=0.1,
    )


def make_prediction(**overrides: Any) -> MatchPrediction:
    central = PredictionCore(
        p_team1_win=0.64,
        map_score_pmf={"2-0": 0.34, "2-1": 0.30, "1-2": 0.20, "0-2": 0.16},
        slots=[_slot(1, 1.0, "mirage", 0.62), _slot(2, 1.0, "inferno", 0.55), _slot(3, 0.5, "ancient", 0.6)],
    )
    base: dict[str, Any] = {
        "match_id": 10,
        "created_at": NOW,
        "model_version": "test-1",
        "format": MatchFormat.BO3,
        "team1_id": TEAM1_ID,
        "team2_id": TEAM2_ID,
        "central": central,
        "sigma_team1_win": 0.04,
        "map_estimates": {
            "mirage": MapWinEstimate(
                map_name="mirage", p_team1_win=0.62, sigma=0.04, picked_by=TeamSlot.TEAM1, components={"glicko": 0.6}
            )
        },
        "veto_distribution": [
            MapSequenceProbability(
                maps=["mirage", "inferno", "ancient"], pickers=[TeamSlot.TEAM1, TeamSlot.TEAM2, None], probability=0.4
            )
        ],
        "veto_known": False,
        "context_flags": [make_flag(rationale="Bravo Foxes field a stand-in.")],
        "logit_shift_applied": 0.1,
        "data_completeness": 0.97,
        "n_simulations": 20000,
    }
    base.update(overrides)
    return MatchPrediction(**base)


def make_decision(
    *,
    accepted: bool,
    selection: str = "team1",
    odds: str = "1.80",
    p_win: float = 0.78,
    ev: float = 0.05,
    reasons: list[str] | None = None,
    market_name: str = "Zwycięzca meczu",
    selection_name: str = "Alpha Wolves",
) -> BetDecision:
    spec = MarketSpec(market_type=MarketType.MATCH_WINNER)
    quote = OddsQuote(
        selection_id=1,
        bookmaker_event_id=1,
        external_event_id="ev1",
        market_raw_name=market_name,
        selection_raw_name=selection_name,
        spec=spec,
        selection_key=selection,
        odds=Decimal(odds),
        captured_at=NOW,
    )
    priced = PricedOutcome(
        outcome=OutcomeProbability(spec_key=spec.key(), selection=selection, p_win=p_win, sigma=0.03),
        quote=quote,
        effective_odds=(Decimal(odds) * Decimal("0.88")).quantize(Decimal("0.0001")),
        ev=ev,
        kelly_full=0.1,
    )
    return BetDecision(
        priced=priced,
        match_id=10,
        accepted=accepted,
        reasons=[] if accepted else (reasons or ["low_ev: EV +0.0100 < 0.0300"]),
        stake=Decimal("20.00") if accepted else Decimal("0"),
        decided_at=NOW,
    )


# ---------------------------------------------------------------------------------------------
# Database seeding
# ---------------------------------------------------------------------------------------------


def seed_match(session: Session, **ctx_overrides: Any) -> MatchContext:
    """Insert two teams, their players and one scheduled match; return the matching MatchContext."""
    session.add_all(
        [
            Team(id=TEAM1_ID, name="Alpha Wolves"),
            Team(id=TEAM2_ID, name="Bravo Foxes"),
            Team(id=3, name="Charlie Owls"),
        ]
    )
    session.flush()
    for pid in TEAM1_PLAYERS:
        session.add(Player(id=pid, nickname=f"a{pid}", current_team_id=TEAM1_ID))
    for pid in TEAM2_PLAYERS:
        session.add(Player(id=pid, nickname=f"b{pid}", current_team_id=TEAM2_ID))
    session.add(
        Match(
            id=10,
            team1_id=TEAM1_ID,
            team2_id=TEAM2_ID,
            scheduled_at=START,
            status=MatchStatus.SCHEDULED,
            format=MatchFormat.BO3,
            tier=Tier.S,
            in_scope=True,
        )
    )
    session.flush()
    return make_ctx(**ctx_overrides)


def add_news(
    session: Session,
    n: int,
    *,
    hours_ago: float = 5.0,
    team_ids: list[int] | None = None,
    player_ids: list[int] | None = None,
    label: str | None = None,
    relevance: float | None = None,
    published: bool = True,
    **columns: Any,
) -> NewsItem:
    when = NOW - timedelta(hours=hours_ago)
    row = NewsItem(
        source="hltv",
        url=f"https://www.hltv.org/news/{5000 + n}/item-{n}",
        title=columns.pop("title", f"Item {n}"),
        body=columns.pop("body", f"Text of item {n}."),
        published_at=when if published else None,
        team_ids=team_ids if team_ids is not None else [],
        player_ids=player_ids if player_ids is not None else [],
        triage_label=label,
        relevance=relevance,
        triaged_at=when if label is not None else None,
        created_at=when,
        updated_at=when,
        **columns,
    )
    session.add(row)
    session.flush()
    return row
