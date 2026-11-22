"""Pure matching rules for finding the ordered market / selection on the event page.

The rule is "never guess": a candidate is chosen only when its normalised name equals the wanted name.
External ids (from DOM data attributes) are used to disambiguate equal names and to detect conflicts;
they never override a name mismatch.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from cs2bot.executor.textutils import normalise_text


class MatchKind(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"  # several candidates fit equally well
    CONFLICT = "conflict"  # the element carrying the wanted id shows a different name


@dataclass(frozen=True)
class Candidate:
    index: int
    names: tuple[str, ...]  # normalised name variants shown for this element
    ids: tuple[str, ...] = ()  # raw values of the configured id attributes
    display: str = ""  # original text, for messages


@dataclass(frozen=True)
class MatchOutcome:
    kind: MatchKind
    index: int | None = None
    detail: str = ""


def id_matches(value: str | None, wanted: str | None) -> bool:
    """``wanted`` equals ``value`` or appears in it as a standalone token (``"market-123"`` ~ ``"123"``)."""
    if not value or not wanted:
        return False
    if value == wanted:
        return True
    return re.search(rf"(?<![0-9A-Za-z]){re.escape(wanted)}(?![0-9A-Za-z])", value) is not None


def pick_candidate(candidates: Sequence[Candidate], wanted_name: str, wanted_id: str = "") -> MatchOutcome:
    """Choose the candidate for ``wanted_name`` (raw text) / ``wanted_id`` (may be empty).

    * the normalised name must match exactly (any of the candidate's name variants);
    * if some element carries ``wanted_id``: exactly one id+name match -> FOUND, several -> AMBIGUOUS,
      none (the id sits on an element with another name) -> CONFLICT;
    * otherwise (ids not exposed in the DOM): exactly one name match -> FOUND, several -> AMBIGUOUS,
      none -> NOT_FOUND.
    """
    target = normalise_text(wanted_name)
    if not target:
        return MatchOutcome(MatchKind.NOT_FOUND, detail="empty wanted name")
    by_name = [c for c in candidates if target in c.names]
    if wanted_id:
        by_id = [c for c in candidates if any(id_matches(v, wanted_id) for v in c.ids)]
        if by_id:
            both = [c for c in by_id if c in by_name]
            if len(both) == 1:
                return MatchOutcome(MatchKind.FOUND, both[0].index, "matched by id and name")
            if len(both) > 1:
                return MatchOutcome(
                    MatchKind.AMBIGUOUS, detail=f"{len(both)} elements carry id {wanted_id!r} and the name"
                )
            shown = ", ".join(repr(c.display or "/".join(c.names)) for c in by_id[:3])
            return MatchOutcome(
                MatchKind.CONFLICT, detail=f"id {wanted_id!r} belongs to element(s) named {shown}"
            )
    if len(by_name) == 1:
        return MatchOutcome(MatchKind.FOUND, by_name[0].index, "matched by name")
    if len(by_name) > 1:
        return MatchOutcome(MatchKind.AMBIGUOUS, detail=f"{len(by_name)} elements are named {wanted_name!r}")
    return MatchOutcome(MatchKind.NOT_FOUND, detail=f"no element named {wanted_name!r}")
