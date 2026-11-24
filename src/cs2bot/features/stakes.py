"""Stakes of a match from the stage text (HLTV / PandaScore) and the team's record in the event.

HLTV writes the stage into the match page ("Grand final", "Lower bracket semi-final", "Group A decider
match", "Swiss round 5", "Elimination match", "Opening match", "Showmatch", "Last chance qualifier
final" ...); PandaScore uses similar English labels. :func:`classify_stage` maps such free text to
flags with tolerant regular expressions; unknown text gives all-zero flags (not NaN: "no special
stakes recognised").

Swiss stages (Majors, BLAST Austin-style events): with a 3-win / 3-loss format a team at 2 wins plays
for qualification, at 2 losses for its life, at 2-2 both ("decider"). :func:`swiss_situation` derives
that from the record *before* the match (passed in by the feature code, leakage-safe).

Dead rubbers are rare in modern CS2 formats (Swiss, GSL and double elimination have none). They are
only flagged when the text says so ("showmatch", "third place" / "3rd place decider", "all-star"),
which marks low stakes; round-robin standings are not reconstructed (see the report).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

_FLAGS = re.IGNORECASE


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, _FLAGS)


_GRAND_FINAL: Final = _rx(r"\bgrand[\s\-]*finals?\b")
_FINAL: Final = _rx(r"(?<![\-\s]semi)(?<!quarter)(?<!semi)\bfinals?\b")
_SEMI: Final = _rx(r"\bsemi[\s\-]*finals?\b|\bsf\b")
_QUARTER: Final = _rx(r"\bquarter[\s\-]*finals?\b|\bqf\b")
_LOWER: Final = _rx(r"\blower\b|\blosers?\b|\blb\b")
_UPPER: Final = _rx(r"\bupper\b|\bwinners?\b|\bub\b")
# "Elimination stage" is the name of a Major's Swiss stage, not an elimination match
_ELIM: Final = _rx(
    r"\belimination\b(?![\s\-]*stage)|\blosers?[\s\-]*match\b|\bknock[\s\-]*out\b|\bsudden death\b"
)
_DECIDER: Final = _rx(r"\bdecider\b|\bdeciding\b")
_QUALIFY: Final = _rx(
    r"\bqualif\w*\b|\bqualification\b|\blast[\s\-]*chance\b|\blcq\b|\bplay[\s\-]*in\b|\bpromotion\b|\brelegation\b"
)
_PLAYOFF: Final = _rx(
    r"\bplay[\s\-]*offs?\b|\bbracket\b|\bfinals?\b|\bchampions?\s*stage\b|\blegends?\s*stage\b"
)
_SWISS: Final = _rx(r"\bswiss\b|\bopening\s*stage\b|\bchallengers?\s*stage\b|\belimination\s*stage\b")
_GROUP: Final = _rx(r"\bgroups?\b|\bround[\s\-]*robin\b|\bgsl\b")
_OPENING: Final = _rx(r"\bopening\s*match(es)?\b|\binitial\s*match\b|\bround\s*1\b")
_LOW_STAKES: Final = _rx(
    r"\bshow[\s\-]*match\b|\ball[\s\-]*stars?\b|\b(3rd|third)[\s\-]*place\b|\bexhibition\b"
)

#: The stake features, all 0/1 except ``stakes_level`` (0 = ordinary .. 5 = grand final).
STAGE_FEATURES: Final[tuple[str, ...]] = (
    "stage_grand_final",
    "stage_final",
    "stage_semi",
    "stage_quarter",
    "stage_playoff",
    "stage_lower_bracket",
    "stage_upper_bracket",
    "stage_elimination",
    "stage_decider",
    "stage_qualification",
    "stage_swiss",
    "stage_group",
    "stage_opening",
    "stage_low_stakes",
    "stakes_level",
)


@dataclass(frozen=True)
class StageInfo:
    grand_final: bool = False
    final: bool = False
    semi: bool = False
    quarter: bool = False
    playoff: bool = False
    lower_bracket: bool = False
    upper_bracket: bool = False
    elimination: bool = False
    decider: bool = False
    qualification: bool = False
    swiss: bool = False
    group: bool = False
    opening: bool = False
    low_stakes: bool = False

    @property
    def stakes_level(self) -> float:
        """Ordinal summary: 5 grand final, 4 final / elimination in playoffs, 3 semi / decider /
        elimination, 2 quarter / qualification, 1 playoff or group/swiss, 0 unknown; low stakes = 0."""
        if self.low_stakes:
            return 0.0
        if self.grand_final:
            return 5.0
        if self.final or (self.elimination and self.playoff):
            return 4.0
        if self.semi or self.decider or self.elimination:
            return 3.0
        if self.quarter or self.qualification:
            return 2.0
        if self.playoff or self.group or self.swiss or self.opening:
            return 1.0
        return 0.0

    def features(self) -> dict[str, float]:
        out = {
            "stage_grand_final": float(self.grand_final),
            "stage_final": float(self.final),
            "stage_semi": float(self.semi),
            "stage_quarter": float(self.quarter),
            "stage_playoff": float(self.playoff),
            "stage_lower_bracket": float(self.lower_bracket),
            "stage_upper_bracket": float(self.upper_bracket),
            "stage_elimination": float(self.elimination),
            "stage_decider": float(self.decider),
            "stage_qualification": float(self.qualification),
            "stage_swiss": float(self.swiss),
            "stage_group": float(self.group),
            "stage_opening": float(self.opening),
            "stage_low_stakes": float(self.low_stakes),
        }
        out["stakes_level"] = self.stakes_level
        return out


def classify_stage(text: str | None) -> StageInfo:
    """Stage flags from free text (None / empty -> all False)."""
    if not text or not text.strip():
        return StageInfo()
    s = " ".join(text.replace("_", " ").split())
    grand = bool(_GRAND_FINAL.search(s))
    semi = bool(_SEMI.search(s))
    quarter = bool(_QUARTER.search(s))
    final = grand or (bool(_FINAL.search(s)) and not semi and not quarter)
    lower = bool(_LOWER.search(s))
    upper = bool(_UPPER.search(s))
    group = bool(_GROUP.search(s))
    # every lower-bracket / losers' match of a double-elimination bracket or GSL group is an
    # elimination match; winners' / losers' matches of a GSL *group* are not playoff matches
    elimination = bool(_ELIM.search(s)) or lower
    return StageInfo(
        grand_final=grand,
        final=final,
        semi=semi,
        quarter=quarter,
        playoff=bool(_PLAYOFF.search(s)) or grand or semi or quarter or ((lower or upper) and not group),
        lower_bracket=lower,
        upper_bracket=upper,
        elimination=elimination,
        decider=bool(_DECIDER.search(s)),
        qualification=bool(_QUALIFY.search(s)),
        swiss=bool(_SWISS.search(s)),
        group=group,
        opening=bool(_OPENING.search(s)) and not (lower or upper),  # "lower bracket round 1" is no opener
        low_stakes=bool(_LOW_STAKES.search(s)),
    )


def swiss_situation(
    wins: int, losses: int, *, wins_needed: int = 3, losses_needed: int = 3
) -> tuple[bool, bool]:
    """(elimination match, qualification match) for a Swiss record before the match."""
    return losses == losses_needed - 1, wins == wins_needed - 1
