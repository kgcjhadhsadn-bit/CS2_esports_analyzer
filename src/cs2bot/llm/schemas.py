"""Pydantic schemas for Claude's structured outputs.

These classes are handed to ``client.messages.parse(output_format=...)``. The API guarantees that
the response matches the JSON schema, but JSON-schema *value* constraints (``minimum`` /
``maximum``) are not enforced by structured outputs. Therefore no hard ``ge`` / ``le`` constraints
are used here: numeric fields are clamped by validators instead, so one slightly out-of-range
number can never invalidate a whole batch. Everything the model suggests is bounded again in code
(:func:`cs2bot.llm.context.suggestion_to_flag`, :func:`cs2bot.llm.combine.combine_flags`).

Schema restrictions respected (structured outputs): no recursive types, no free-form dicts, every
field required (no defaults), enums as plain string enums.
"""

from __future__ import annotations

import math
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from cs2bot.domain.enums import ContextFlagType, TeamSlot

_MAX_SUMMARY_CHARS = 300
_MAX_NOTE_CHARS = 1200


def clamp01(value: float) -> float:
    """Clamp to [0, 1]; NaN / infinities become 0 (unknown -> no weight)."""
    if not isinstance(value, int | float) or not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, float(value)))


def _one_line(text: str, limit: int) -> str:
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = " ".join(str(value).split())
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            out.append(cleaned)
    return out


class TriageLabel(StrEnum):
    """What a news item is about (stored in ``news_items.triage_label``)."""

    ROSTER_CHANGE = "roster_change"
    STAND_IN = "stand_in"
    INJURY_ILLNESS = "injury_illness"
    VISA_TRAVEL = "visa_travel"
    COACH_CHANGE = "coach_change"
    INTERNAL_CONFLICT = "internal_conflict"
    BOOTCAMP = "bootcamp"
    META_PATCH = "meta_patch"
    TOURNAMENT_FORMAT = "tournament_format"
    IRRELEVANT = "irrelevant"


# NOTE: class docstrings below end up as "description" in the JSON schema sent to the model, so they
# are written for the model; developer notes are kept in comments.


class TriageItem(BaseModel):
    """Triage verdict for one news item."""

    news_id: int = Field(description="The news_id attribute of the item this entry classifies.")
    label: TriageLabel = Field(description="The single best-fitting category.")
    relevance: float = Field(
        description=(
            "0..1: how much this item could change what to expect from a team in its matches over "
            "the next two weeks. 0 = no bearing, 1 = decisive (e.g. a confirmed emergency stand-in)."
        )
    )
    team_names: list[str] = Field(description="Names of the CS2 teams the item is materially about.")
    team_ids: list[int] = Field(
        description="Ids of those teams, taken only from the item's candidate_teams list. May be empty."
    )
    summary: str = Field(description="One factual sentence (max ~25 words) stating what happened.")

    @field_validator("relevance")
    @classmethod
    def _clamp_relevance(cls, value: float) -> float:
        return clamp01(value)

    @field_validator("team_names")
    @classmethod
    def _clean_names(cls, value: list[str]) -> list[str]:
        return _unique(value)

    @field_validator("team_ids")
    @classmethod
    def _clean_ids(cls, value: list[int]) -> list[int]:
        return sorted(set(value))

    @field_validator("summary")
    @classmethod
    def _clean_summary(cls, value: str) -> str:
        return _one_line(value, _MAX_SUMMARY_CHARS)


class TriageBatch(BaseModel):
    """Triage verdicts for a batch of news items."""

    items: list[TriageItem] = Field(description="Exactly one entry per news item in the request.")


class ContextFlagSuggestion(BaseModel):
    """One situational flag for the match, supported by cited evidence."""

    # Mirrors cs2bot.domain.context.ContextFlag, but is only a *suggestion*: it is never used directly.
    # cs2bot.llm.context.suggestion_to_flag validates the evidence, clips the numbers and converts it.

    flag_type: ContextFlagType = Field(description="Category of the situation.")
    team: TeamSlot | None = Field(
        description="Which team the situation concerns: team1, team2, or null when it is match-wide."
    )
    severity: float = Field(description="0..1: how much the situation matters if it is true.")
    confidence: float = Field(
        description="0..1: how sure the evidence makes you that it is true and applies to this match."
    )
    logit_shift: float = Field(
        description=(
            "Suggested adjustment to TEAM1's per-map win log-odds. Positive favours team1, negative "
            "favours team2. 0 for purely informational flags."
        )
    )
    veto: bool = Field(description="True only for the extreme situations in which no bet should be placed.")
    rationale: str = Field(description="One or two sentences: what the evidence says and why it matters.")
    evidence: list[str] = Field(
        description='Ids of the supporting news items (e.g. "news-3"), or "context" for the match context.'
    )
    expires_at: str | None = Field(
        description="ISO-8601 UTC time after which the flag stops applying, or null (holds until the match)."
    )


class MatchContextResult(BaseModel):
    """Situational flags for one match plus a short overall note."""

    flags: list[ContextFlagSuggestion] = Field(
        description="Zero or more flags. An empty list is the normal result when nothing notable is reported."
    )
    note: str = Field(description="One to three sentences summarising the situational picture.")

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: str) -> str:
        return _one_line(value, _MAX_NOTE_CHARS)


class RationaleOutput(BaseModel):
    """The dashboard note for one match."""

    markdown: str = Field(description="The complete rationale as Markdown.")
