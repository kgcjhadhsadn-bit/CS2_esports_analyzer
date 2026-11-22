"""Enumerations shared by every layer (DB stores the ``.value`` strings)."""

from __future__ import annotations

from enum import StrEnum


class Side(StrEnum):
    CT = "ct"
    T = "t"

    @property
    def other(self) -> Side:
        return Side.T if self is Side.CT else Side.CT


class TeamSlot(StrEnum):
    """Orientation inside *our* match record (``matches.team1_id`` / ``team2_id``)."""

    TEAM1 = "team1"
    TEAM2 = "team2"

    @property
    def other(self) -> TeamSlot:
        return TeamSlot.TEAM2 if self is TeamSlot.TEAM1 else TeamSlot.TEAM1


class MatchFormat(StrEnum):
    BO1 = "bo1"
    BO2 = "bo2"
    BO3 = "bo3"
    BO5 = "bo5"

    @property
    def n_maps(self) -> int:
        return {"bo1": 1, "bo2": 2, "bo3": 3, "bo5": 5}[self.value]

    @property
    def maps_to_win(self) -> int | None:
        """Maps needed to win the series; ``None`` for BO2 (draw possible)."""
        return {"bo1": 1, "bo2": None, "bo3": 2, "bo5": 3}[self.value]

    @property
    def min_maps(self) -> int:
        return {"bo1": 1, "bo2": 2, "bo3": 2, "bo5": 3}[self.value]


class MatchStatus(StrEnum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    FINISHED = "finished"
    CANCELED = "canceled"
    POSTPONED = "postponed"
    UNKNOWN = "unknown"


class Tier(StrEnum):
    S = "s"
    A = "a"
    B = "b"
    C = "c"
    D = "d"
    UNRANKED = "unranked"


class BuyType(StrEnum):
    PISTOL = "pistol"
    ECO = "eco"
    FORCE = "force"
    FULL = "full"


class RoundEndReason(StrEnum):
    ELIMINATION = "elimination"
    BOMB_EXPLODED = "bomb_exploded"
    BOMB_DEFUSED = "bomb_defused"
    TIME_EXPIRED = "time_expired"
    SURRENDER = "surrender"
    OTHER = "other"


class VetoAction(StrEnum):
    BAN = "ban"
    PICK = "pick"
    LEFTOVER = "leftover"  # decider / remaining map


class PlayerRole(StrEnum):
    AWPER = "awper"
    ENTRY = "entry"
    LURKER = "lurker"
    SUPPORT = "support"
    ANCHOR = "anchor"
    ROTATOR = "rotator"
    IGL = "igl"
    RIFLER = "rifler"
    UNKNOWN = "unknown"


class DemoStatus(StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    PARSED = "parsed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class LinkStatus(StrEnum):
    """How confidently a bookmaker event is linked to one of our matches."""

    AUTO = "auto"
    CONFIRMED = "confirmed"
    PENDING_REVIEW = "pending_review"
    REJECTED = "rejected"
    UNLINKED = "unlinked"


class BetStatus(StrEnum):
    PENDING = "pending"  # row created, not yet sent to the bookmaker
    PLACING = "placing"
    PLACED = "placed"
    WON = "won"
    LOST = "lost"
    VOID = "void"  # refunded (map not played, cancelled match, push ...)
    FAILED = "failed"  # placement failed - no money at risk
    CANCELLED = "cancelled"
    DRY_RUN = "dry_run"  # simulated placement, no money at risk

    @property
    def is_open(self) -> bool:
        return self in (BetStatus.PENDING, BetStatus.PLACING, BetStatus.PLACED)

    @property
    def is_settled(self) -> bool:
        return self in (BetStatus.WON, BetStatus.LOST, BetStatus.VOID)


class LedgerKind(StrEnum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    STAKE = "stake"
    PAYOUT = "payout"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"
    RECONCILIATION = "reconciliation"


class ContextFlagType(StrEnum):
    STAND_IN = "stand_in"
    ROSTER_CHANGE = "roster_change"
    ILLNESS = "illness"
    VISA_ISSUE = "visa_issue"
    TRAVEL_FATIGUE = "travel_fatigue"
    BOOTCAMP = "bootcamp"
    ELIMINATION_STAKES = "elimination_stakes"
    DEAD_RUBBER = "dead_rubber"
    INTERNAL_CONFLICT = "internal_conflict"
    COACH_CHANGE = "coach_change"
    PATCH_CHANGE = "patch_change"
    MAP_POOL_CHANGE = "map_pool_change"
    FORM_SLUMP = "form_slump"
    MOTIVATION = "motivation"
    OTHER = "other"


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class Bookmaker(StrEnum):
    BETCLIC = "betclic"


class DataSource(StrEnum):
    PANDASCORE = "pandascore"
    HLTV = "hltv"
    DEMO = "demo"
    BETCLIC = "betclic"
    LIQUIPEDIA = "liquipedia"
    MANUAL = "manual"
    SYNTHETIC = "synthetic"
