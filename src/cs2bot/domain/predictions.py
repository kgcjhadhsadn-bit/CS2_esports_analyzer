"""Model outputs.

A :class:`MatchPrediction` holds *distributions* rich enough to price every market in
:mod:`cs2bot.domain.markets`. It contains a ``central`` estimate plus ``low``/``high`` scenarios in
which team1's strength is shifted by -1 / +1 standard deviation on every map simultaneously
(perfectly correlated). The pricing engine derives an outcome's uncertainty as
``sigma = max(sigma_floor, |p(high) - p(low)| / 2)``.

Dict keys are strings so everything round-trips through JSON:

* score keys: ``"13-9"`` (team1 rounds first), ``"2-1"`` (team1 maps first)
* pmf keys of integer-valued variables: ``"26"`` -> probability
* scope keys for multi-map player markets: ``"map:1"``, ``"maps:1,2"``, ``"match"``

Conventions shared by the simulator (producer) and the pricing engine (consumer)
---------------------------------------------------------------------------------
* Every distribution inside a :class:`MapSlotPrediction` is conditional on that map being played.
* ``player_kills_by_scope``: ``"map:k"`` is conditional on map k being played (the slot's ``players``
  dict is the primary source for single maps); ``"maps:1,2"`` is conditional on ALL listed maps being
  played (the bet is void otherwise); ``"match"`` is unconditional (kills over the maps actually played).
* ``top_fragger[scope]``: player id -> probability with dead heats already split evenly, same scope
  keys as above. Team-restricted markets use ``"<scope>|team1"`` / ``"<scope>|team2"``.
* ``p_team1_race_to["N"]`` = P(team1 is first to N rounds). For N <= 13 one team always gets there.
  For N > 13 ``p_neither_race_to["N"]`` = P(neither team reaches N); the market is void in that case
  and P(team2 first) = 1 - p_team1 - p_neither.
* ``p_team1_pistol`` keys are ``"1"`` / ``"13"``; ``p_team1_half`` and ``p_half_draw`` keys are
  ``"1"`` / ``"2"``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from cs2bot.domain.context import ContextFlag
from cs2bot.domain.enums import MatchFormat, TeamSlot


class MapSequenceProbability(BaseModel):
    """One possible ordered list of maps to be played, with who picked each."""

    maps: list[str]
    pickers: list[TeamSlot | None]  # None = decider / leftover
    probability: float


class PlayerProjection(BaseModel):
    player_id: int
    nickname: str | None = None
    team: TeamSlot
    kills_pmf: dict[str, float]  # "17" -> p
    kills_mean: float
    kills_var: float
    deaths_mean: float | None = None


class MapSlotPrediction(BaseModel):
    """Prediction for the k-th map of the series (map identity may still be uncertain).

    All distributions are **conditional on the map being played**; ``p_played`` is separate.
    """

    map_number: int
    p_played: float
    map_distribution: dict[str, float]  # map name -> P(this slot is that map | played)
    p_team1_win: float
    score_pmf: dict[str, float]  # joint final score "13-9" -> p (includes overtime scores)
    p_overtime: float
    p_team1_pistol: dict[str, float] = Field(default_factory=dict)  # "1" / "13" -> P(team1 wins round)
    p_team1_half: dict[str, float] = Field(default_factory=dict)  # "1" / "2" -> P(team1 wins half)
    p_half_draw: dict[str, float] = Field(default_factory=dict)  # "1" / "2" -> P(6-6 half)
    p_team1_race_to: dict[str, float] = Field(default_factory=dict)  # "5" -> P(team1 first to 5)
    p_neither_race_to: dict[str, float] = Field(default_factory=dict)  # "16" -> P(nobody reaches 16)
    players: dict[str, PlayerProjection] = Field(default_factory=dict)  # player id -> projection


class PredictionCore(BaseModel):
    """Everything needed to price markets under one strength scenario."""

    p_team1_win: float
    p_draw: float = 0.0  # BO2 only
    map_score_pmf: dict[str, float]  # "2-1" -> p
    slots: list[MapSlotPrediction]
    player_kills_by_scope: dict[str, dict[str, PlayerProjection]] = Field(default_factory=dict)
    top_fragger: dict[str, dict[str, float]] = Field(default_factory=dict)  # scope -> player id -> p


class MapWinEstimate(BaseModel):
    """Per-map probability from the rating/ensemble layer (before simulation)."""

    map_name: str
    p_team1_win: float
    sigma: float
    picked_by: TeamSlot | None = None
    components: dict[str, float] = Field(default_factory=dict)  # e.g. {"glicko": .62, "lgbm": .66}


class MatchPrediction(BaseModel):
    match_id: int
    created_at: datetime
    model_version: str
    format: MatchFormat
    team1_id: int
    team2_id: int
    central: PredictionCore
    low: PredictionCore | None = None
    high: PredictionCore | None = None
    sigma_team1_win: float
    map_estimates: dict[str, MapWinEstimate] = Field(default_factory=dict)
    veto_distribution: list[MapSequenceProbability] = Field(default_factory=list)
    veto_known: bool = False
    context_flags: list[ContextFlag] = Field(default_factory=list)
    logit_shift_applied: float = 0.0
    data_completeness: float = 0.0
    n_simulations: int = 0
    components: dict[str, Any] = Field(default_factory=dict)


class OutcomeProbability(BaseModel):
    """Model probability for one bettable outcome (unconditional probabilities)."""

    spec_key: str
    selection: str
    p_win: float
    p_push: float = 0.0
    p_void: float = 0.0
    sigma: float

    @property
    def p_lose(self) -> float:
        return max(0.0, 1.0 - self.p_win - self.p_push - self.p_void)

    @property
    def p_win_given_settled(self) -> float:
        """Probability of winning given the bet is not voided/pushed (used for the confidence check)."""
        denom = 1.0 - self.p_void - self.p_push
        return self.p_win / denom if denom > 1e-12 else 0.0
