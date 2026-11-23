"""Demo parsing: one ``.dem`` file -> rounds, kills, per-player round stats and position samples.

How demoparser2 is queried (checked against the demoparser2 Rust source,
``src/parser/src/second_pass/game_events.rs`` and ``maps.rs``, against awpy, which wraps it, and
against six real demos - see "Validation" below):

* ``parse_events([...], player=[...], other=[...])`` returns ``[(event_name, DataFrame), ...]`` for
  the events that occur in the demo (``parse_event`` returns an empty *list* for an event that never
  occurs). ``player`` props are added per referenced player with the prefixes ``user_`` /
  ``attacker_`` / ``assister_`` (``user_X``, ``attacker_last_place_name``, ...); ``other`` props
  (game rules state) are added under their own name (``total_rounds_played``).
* ``round_start`` / ``round_end`` / ``round_officially_ended`` are synthesised by the parser from
  game-rules counters: ``round_end`` carries ``winner`` (``"CT"`` / ``"T"``) and ``reason``
  (``"ct_killed"``, ``"t_killed"``, ``"bomb_exploded"``, ``"bomb_defused"``, ``"time_ran_out"``,
  ``"t_surrender"``, ``"ct_surrender"``, ...); older parser versions / real game events use the
  numeric codes (winner 2 = T, 3 = CT; reason 1, 7, 8, 9, 12, 17, 18) - both forms are accepted.
  ``round_officially_ended`` shares its tick with the next ``round_start``. Demos open with a
  ``round_end`` that has neither winner nor reason, and a backup restore produces another one.
* Steam ids are strings in event frames and unsigned integers in tick frames; ``0`` means a bot.
* ``parse_ticks(props, ticks=[...])`` returns one row per player per requested tick with the
  columns ``tick``, ``steamid``, ``name`` plus the props. ``team_num`` is 2 for T and 3 for CT.
* GOTV demos (every HLTV demo) contain neither ``player_blind`` nor ``item_purchase``.

Validation
----------
No demo file ships with the repository, so the unit tests script a fake parser. The logic was
additionally run with the real demoparser2 (0.42.2) on the six public demos of the awpy-fixtures
project - Valve matchmaking, FACEIT (knife round, overtime), ESEA (knife round) and three HLTV
demos of professional matches, one of them with a round replayed after a backup restore. Scores,
round counts and every published per-player number (kills, deaths, ADR, KAST, headshots, multi-kill
rounds, enemies flashed) came out identical, except one player's ADR on csstats.gg (34.7 there,
37.7 from the game events; awpy reports the same residual).
``tests/ingest/demos/test_demos_real_fixtures.py`` repeats that check when the demos are downloaded.

Round cleaning (the part that decides whether the data can be trusted)
-----------------------------------------------------------------------
A round candidate is a ``round_end`` with a winner plus the *last* ``round_freeze_end`` since the
previous ``round_end`` (earlier freeze ends in between belong to attempts that were aborted by a
restart or a backup restore and never finished). For each candidate the game-rules values at freeze
end are known: ``is_warmup_period`` and ``total_rounds_played`` (``N - 1`` during round ``N``; it is
read at freeze end, never at the round-end tick, where it may or may not be incremented yet). Then:

1. warmup rounds are dropped;
2. a round that starts with ``total_rounds_played == 0`` after other live rounds marks a match
   restart and opens a new *segment* (this is how the knife round and "live on 3" attempts go away);
3. inside a segment, a round whose counter is not above the previous one replaces the rounds it
   repeats (a round replayed after a technical restore);
4. a first-of-segment round fought with knives only is dropped even when no restart follows it;
5. the last segment with at least :data:`MIN_MATCH_ROUNDS` rounds is the match (falling back to the
   longest), so neither the knife round nor a stray restart after the final round can win.

Round numbers are ``total_rounds_played + 1`` (minus a knife round that was counted), so a demo that
starts recording late still gets the true numbers - they decide pistol rounds and overtime.

Definitions used for the derived statistics (assumptions are spelled out here on purpose)
----------------------------------------------------------------------------------------
* A round owns the events from its freeze end until the next round starts: kills after ``round_end``
  (saving players hunted down, the bomb blast) count for that round, as on the in-game scoreboard
  and in awpy. Nothing that happens during freeze time counts: a backup restore "kills" all ten
  players on the tick the replayed round starts (seen in a real HLTV demo), and those are no deaths.
  ``survived`` means "no death in that whole window"; ``ct_survivors`` / ``t_survivors`` count players
  alive at the ``round_end`` tick.
* A *kill* credits the attacker only when the victim is an enemy (no suicides, no team kills).
* *Opening kill*: the first enemy kill of the round before the round-end tick.
* *Trade*: kill ``k2`` is a trade of kill ``k1`` when ``k2``'s victim is ``k1``'s attacker,
  ``k2``'s attacker is on ``k1``'s victim's side and ``0 <= tick2 - tick1 <= trade_window_s * tick
  rate`` (inclusive). Then ``k2.is_trade`` and ``k1.was_traded`` are set.
* *Damage* follows the victim's health: every hit is capped at the health the victim has left, so a
  victim can never give away more than 100 per round in total; team and self damage are excluded.
* *Utility damage*: damage with HE grenades and molotov / incendiary fire.
* *Enemies flashed*: a living enemy blinded for at least ``min_blind_duration_s``. GOTV demos (all
  HLTV demos) do not contain the ``player_blind`` event, so blinds are reconstructed the way awpy does
  it: a player's ``flash_duration`` jumps from its old value to the new blind duration on the very
  tick of a ``flashbang_detonate`` event, whose thrower gets the credit. ``player_blind`` is used
  when a demo does have it (matchmaking / POV demos).
* *Utility thrown* is counted from detonation events (``flashbang_detonate``, ``smokegrenade_detonate``,
  ``hegrenade_detonate``, ``inferno_startburn``): a grenade still in the air when the round is over,
  or a molotov that burns out mid-air, is not counted.
* *Assists* are the game's own attribution (``player_death.assister``), flash assists included, as
  in the scoreboard's assist column.
* *KAST*: kill, assist, survived, or death that was traded. As on HLTV a pure flash assist does not
  count as the "A" (checked against HLTV's KAST of a real match: with flash assists one player came
  out at 78.3 % instead of HLTV's 73.9 %).
* *Clutch*: the moment a side is reduced to one living player while ``N >= 1`` enemies live, that
  player is in a ``1vN``; it is won when the player's side wins the round.
* *Equipment value* is ``current_equip_value`` at freeze end, *money at round start* is
  ``start_balance`` (``balance + cash_spent_this_round`` as a fallback) at the same tick.
* *Buy type* per side from the average equipment value per player (:data:`ECO_MAX_AVG_EQUIPMENT`,
  :data:`FORCE_MAX_AVG_EQUIPMENT`, the same thresholds as ``models.round_model.classify_buy``);
  rounds 1 and 13 are pistol rounds, overtime has none.
* ``distance`` is the 3D distance between attacker and victim in game units (1 unit = 1 inch).
"""

from __future__ import annotations

import bisect
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol

import pandas as pd
from demoparser2 import DemoParser

from cs2bot.config import DemoSettings
from cs2bot.domain.enums import BuyType, RoundEndReason, Side
from cs2bot.ingest.demos._values import records, to_bool, to_float, to_int, to_steamid, to_str
from cs2bot.ingest.demos.errors import DemoError, DemoParseError
from cs2bot.utils.logging import get_logger
from cs2bot.utils.names import normalize_map_name

log = get_logger(__name__)

# ------------------------------------------------------------------------------------------ constants

T_TEAM_NUM = 2
CT_TEAM_NUM = 3
DEFAULT_TICK_RATE = 64  # CS2 servers and GOTV demos run at 64 ticks per second

# Buy classification on the average equipment value per player of a side at freeze end.
ECO_MAX_AVG_EQUIPMENT = 1500.0  # below: eco
FORCE_MAX_AVG_EQUIPMENT = 3800.0  # below: force / half buy; at or above: full buy
PISTOL_ROUNDS: frozenset[int] = frozenset({1, 13})  # MR12: first round of each regulation half
REGULATION_ROUNDS = 24
MIN_MATCH_ROUNDS = 13  # a map cannot end before one team has 13 rounds

POST_ROUND_SECONDS = 7.0  # mp_round_restart_delay: used when round_officially_ended is missing
UTILITY_DAMAGE_WEAPONS: frozenset[str] = frozenset({"hegrenade", "inferno", "molotov", "incgrenade"})

EVENT_NAMES: tuple[str, ...] = (
    "round_start",
    "round_freeze_end",
    "round_end",
    "round_officially_ended",
    "player_death",
    "player_hurt",
    "player_blind",
    "bomb_planted",
    "flashbang_detonate",
    "smokegrenade_detonate",
    "hegrenade_detonate",
    "inferno_startburn",
)
EVENT_PLAYER_PROPS: tuple[str, ...] = ("X", "Y", "Z", "last_place_name", "team_num")
EVENT_OTHER_PROPS: tuple[str, ...] = ("total_rounds_played", "is_warmup_period")
CORE_TICK_PROPS: tuple[str, ...] = (
    "team_num",
    "is_alive",
    "X",
    "Y",
    "Z",
    "last_place_name",
    "current_equip_value",
    "balance",
    "team_clan_name",
)
# Nice to have; if this parser version rejects one of them the query is retried without them.
OPTIONAL_TICK_PROPS: tuple[str, ...] = ("start_balance", "cash_spent_this_round", "health", "inventory")

# CSRoundEndReason codes -> our enum (see demoparser2 maps.rs ROUND_WIN_REASON).
_REASON_BY_CODE: dict[int, RoundEndReason] = {
    1: RoundEndReason.BOMB_EXPLODED,
    7: RoundEndReason.BOMB_DEFUSED,
    8: RoundEndReason.ELIMINATION,
    9: RoundEndReason.ELIMINATION,
    12: RoundEndReason.TIME_EXPIRED,
    17: RoundEndReason.SURRENDER,
    18: RoundEndReason.SURRENDER,
}
_REASON_BY_NAME: dict[str, RoundEndReason] = {
    "bomb_exploded": RoundEndReason.BOMB_EXPLODED,
    "target_bombed": RoundEndReason.BOMB_EXPLODED,
    "bomb_defused": RoundEndReason.BOMB_DEFUSED,
    "t_killed": RoundEndReason.ELIMINATION,
    "ct_killed": RoundEndReason.ELIMINATION,
    "terrorists_killed": RoundEndReason.ELIMINATION,
    "cts_killed": RoundEndReason.ELIMINATION,
    "time_ran_out": RoundEndReason.TIME_EXPIRED,
    "target_saved": RoundEndReason.TIME_EXPIRED,
    "t_surrender": RoundEndReason.SURRENDER,
    "ct_surrender": RoundEndReason.SURRENDER,
}
_WINNER_BY_CODE: dict[int, Side] = {
    1: Side.T,
    7: Side.CT,
    8: Side.CT,
    9: Side.T,
    12: Side.CT,
    17: Side.CT,
    18: Side.T,
}
_WINNER_BY_NAME: dict[str, Side] = {
    "bomb_exploded": Side.T,
    "target_bombed": Side.T,
    "bomb_defused": Side.CT,
    "t_killed": Side.CT,
    "terrorists_killed": Side.CT,
    "ct_killed": Side.T,
    "cts_killed": Side.T,
    "time_ran_out": Side.CT,
    "target_saved": Side.CT,
    "t_surrender": Side.CT,
    "ct_surrender": Side.T,
}

# Inventory display names (lower-cased, alphanumerics only) -> weapon names as used in kill events.
_PRIMARY_WEAPONS: dict[str, str] = {
    "awp": "awp",
    "ak47": "ak47",
    "m4a4": "m4a1",
    "m4a1s": "m4a1_silencer",
    "aug": "aug",
    "sg553": "sg556",
    "ssg08": "ssg08",
    "g3sg1": "g3sg1",
    "scar20": "scar20",
    "galilar": "galilar",
    "famas": "famas",
    "mp9": "mp9",
    "mac10": "mac10",
    "mp7": "mp7",
    "mp5sd": "mp5sd",
    "ump45": "ump45",
    "p90": "p90",
    "ppbizon": "bizon",
    "nova": "nova",
    "xm1014": "xm1014",
    "mag7": "mag7",
    "sawedoff": "sawedoff",
    "m249": "m249",
    "negev": "negev",
}
_PISTOLS: dict[str, str] = {
    "deserteagle": "deagle",
    "r8revolver": "revolver",
    "fiveseven": "fiveseven",
    "tec9": "tec9",
    "cz75auto": "cz75a",
    "p250": "p250",
    "dualberettas": "elite",
    "usps": "usp_silencer",
    "p2000": "hkp2000",
    "glock18": "glock",
}

ROUND_COLUMNS: tuple[str, ...] = (
    "round_number",
    "start_tick",
    "freeze_end_tick",
    "end_tick",
    "official_end_tick",
    "winner_side",
    "end_reason",
    "reason_raw",
    "ct_equipment_value",
    "t_equipment_value",
    "ct_money_start",
    "t_money_start",
    "ct_buy_type",
    "t_buy_type",
    "bomb_planted",
    "bomb_site",
    "ct_survivors",
    "t_survivors",
    "duration_s",
    "ct_players",
    "t_players",
    "ct_roster",
    "is_overtime",
)
KILL_COLUMNS: tuple[str, ...] = (
    "round_number",
    "tick",
    "seconds_into_round",
    "attacker_steamid",
    "victim_steamid",
    "assister_steamid",
    "attacker_side",
    "victim_side",
    "weapon",
    "headshot",
    "penetrated",
    "through_smoke",
    "noscope",
    "attacker_blind",
    "flash_assisted",
    "attacker_place",
    "victim_place",
    "attacker_x",
    "attacker_y",
    "attacker_z",
    "victim_x",
    "victim_y",
    "victim_z",
    "distance",
    "is_opening",
    "is_trade",
    "was_traded",
    "is_enemy_kill",
)
PLAYER_ROUND_COLUMNS: tuple[str, ...] = (
    "round_number",
    "steamid",
    "name",
    "side",
    "roster",
    "kills",
    "deaths",
    "assists",
    "damage",
    "utility_damage",
    "enemies_flashed",
    "flashes_thrown",
    "smokes_thrown",
    "he_thrown",
    "molotovs_thrown",
    "equipment_value",
    "money_start",
    "survived",
    "kast",
    "opening_kill",
    "opening_death",
    "clutch_situation",
    "clutch_won",
    "primary_weapon",
)
POSITION_COLUMNS: tuple[str, ...] = (
    "round_number",
    "steamid",
    "side",
    "seconds_into_round",
    "place",
    "x",
    "y",
    "z",
    "is_alive",
)
PLAYER_COLUMNS: tuple[str, ...] = ("steamid", "name", "team_name", "roster", "start_side", "rounds_played")


# ---------------------------------------------------------------------------------------- public types


class DemoParserLike(Protocol):
    """The part of ``demoparser2.DemoParser`` that :func:`parse_demo` uses (tests inject a fake)."""

    def parse_header(self) -> Mapping[str, Any]: ...

    def parse_event(
        self, event_name: str, *, player: Sequence[str] | None = None, other: Sequence[str] | None = None
    ) -> pd.DataFrame: ...

    def parse_events(
        self,
        event_name: Sequence[str],
        *,
        player: Sequence[str] | None = None,
        other: Sequence[str] | None = None,
    ) -> list[tuple[str, pd.DataFrame]]: ...

    def parse_ticks(
        self,
        wanted_props: Sequence[str],
        *,
        players: Sequence[int] | None = None,
        ticks: Sequence[int] | None = None,
    ) -> pd.DataFrame: ...


ParserFactory = Callable[[str], DemoParserLike]


@dataclass(frozen=True)
class ParseOptions:
    """Parsing knobs that are not in :class:`cs2bot.config.DemoSettings` (defaults suit CS2 demos)."""

    tick_rate: int = DEFAULT_TICK_RATE
    # Player state is requested for this many consecutive ticks per sample and the first one present
    # is used: broadcast demos do not necessarily contain every tick.
    tick_search_window: int = 4
    min_blind_duration_s: float = 1.0
    min_match_rounds: int = MIN_MATCH_ROUNDS


@dataclass
class ParsedDemo:
    """Everything extracted from one demo file.

    DataFrames (columns are the ``*_COLUMNS`` tuples of this module; sides are ``"ct"`` / ``"t"``,
    buy types / end reasons are the enum values, steam ids are Python ints):

    * ``players``: one row per human player seen on a side - ``steamid``, ``name``, ``team_name``
      (clan name in the demo), ``roster`` (``"a"`` = the five that were CT in the first parsed round,
      ``"b"`` = the others), ``start_side``, ``rounds_played``. The side per round is in
      ``player_rounds``.
    * ``rounds``, ``kills``, ``player_rounds``, ``positions``: see the module docstring.
    """

    map_name: str
    header: dict[str, Any]
    players: pd.DataFrame
    rounds: pd.DataFrame
    kills: pd.DataFrame
    player_rounds: pd.DataFrame
    positions: pd.DataFrame
    tick_rate: int = DEFAULT_TICK_RATE
    parser_version: str | None = None
    path: str | None = None
    roster_names: dict[str, str | None] = field(default_factory=dict)
    roster_scores: dict[str, int] = field(default_factory=dict)
    dropped_rounds: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def n_rounds(self) -> int:
        return len(self.rounds)

    @property
    def steamids(self) -> list[int]:
        return [int(s) for s in self.players["steamid"].tolist()]


# -------------------------------------------------------------------------------------- small helpers


def demoparser_version() -> str | None:
    """Installed demoparser2 version (stored in ``demos.parser_version``)."""
    try:
        return metadata.version("demoparser2")
    except metadata.PackageNotFoundError:
        return None


def _as_code(raw: Any) -> int | None:
    """Integer value of a numeric cell (``3``, ``3.0``, ``"3"``, ``"3.0"``); ``None`` for names / missing."""
    if isinstance(raw, bool):
        return None
    text = to_str(raw)
    if text is None:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if math.isfinite(number) and number == int(number) else None


def parse_side(raw: Any) -> Side | None:
    """``"CT"`` / ``3`` -> CT, ``"T"`` / ``"TERRORIST"`` / ``2`` -> T, anything else -> ``None``."""
    code = _as_code(raw)
    if code is not None:
        return {T_TEAM_NUM: Side.T, CT_TEAM_NUM: Side.CT}.get(code)
    text = (to_str(raw) or "").upper()
    if text in ("CT", "CTS", "COUNTER-TERRORIST", "COUNTERTERRORIST"):
        return Side.CT
    if text in ("T", "TERRORIST", "TERRORISTS"):
        return Side.T
    return None


def _reason_key(raw: Any) -> tuple[int | None, str | None]:
    """A round-end reason as ``(numeric code, None)`` or ``(None, lower-case name)``.

    A numeric column that also holds a missing value (the winner-less pseudo round end) arrives as
    floats (``9.0``), so codes are accepted in every numeric spelling.
    """
    code = _as_code(raw)
    if code is not None:
        return code, None
    text = to_str(raw)
    return (None, text.lower()) if text is not None else (None, None)


def map_round_end_reason(raw: Any) -> RoundEndReason:
    """Map a demoparser2 ``round_end.reason`` (name or numeric code) to :class:`RoundEndReason`."""
    code, name = _reason_key(raw)
    if code is not None:
        return _REASON_BY_CODE.get(code, RoundEndReason.OTHER)
    if name is not None:
        return _REASON_BY_NAME.get(name, RoundEndReason.OTHER)
    return RoundEndReason.OTHER


def _winner_from_reason(raw: Any) -> Side | None:
    code, name = _reason_key(raw)
    if code is not None:
        return _WINNER_BY_CODE.get(code)
    return _WINNER_BY_NAME.get(name) if name is not None else None


def classify_buy_type(avg_equipment_value: float | None, round_number: int) -> BuyType | None:
    """Buy type of one side from its average equipment value per player at freeze end."""
    if round_number in PISTOL_ROUNDS:
        return BuyType.PISTOL
    if avg_equipment_value is None:
        return None
    if avg_equipment_value < ECO_MAX_AVG_EQUIPMENT:
        return BuyType.ECO
    if avg_equipment_value < FORCE_MAX_AVG_EQUIPMENT:
        return BuyType.FORCE
    return BuyType.FULL


def is_knife_weapon(weapon: str | None) -> bool:
    """Kill / hurt event weapon names of knives: ``knife``, ``knife_t``, ``knife_karambit``, ``bayonet``."""
    return bool(weapon) and ("knife" in str(weapon) or "bayonet" in str(weapon))


def _clean_weapon(raw: Any) -> str | None:
    text = to_str(raw)
    if text is None:
        return None
    text = text.lower()
    return text[7:] if text.startswith("weapon_") else text


def _primary_weapon(inventory: Any) -> str | None:
    """Best weapon in an ``inventory`` list (a primary if there is one, else a pistol)."""
    if not isinstance(inventory, Iterable) or isinstance(inventory, str | bytes):
        return None
    keys = ["".join(ch for ch in str(item).lower() if ch.isalnum()) for item in inventory]
    for table in (_PRIMARY_WEAPONS, _PISTOLS):
        for name, weapon in table.items():
            if name in keys:
                return weapon
    return None


def _bomb_site_from_place(place: str | None) -> str | None:
    if not place:
        return None
    compact = "".join(ch for ch in place.lower() if ch.isalnum())
    if compact in ("bombsitea", "sitea", "a"):
        return "A"
    if compact in ("bombsiteb", "siteb", "b"):
        return "B"
    return None


# ------------------------------------------------------------------------------------ internal records


@dataclass
class _RawRound:
    start_tick: int | None
    freeze_tick: int
    end_tick: int
    official_tick: int  # last tick attributed to the round (inclusive)
    winner: Side
    reason_raw: str | None
    trp: int | None  # total_rounds_played at freeze end (None when unknown)
    warmup: bool
    has_freeze_end: bool
    trp_eff: int = 0
    number: int = 0


@dataclass
class _Death:
    tick: int
    attacker: int | None
    victim: int | None
    assister: int | None
    attacker_side: Side | None
    victim_side: Side | None
    assister_side: Side | None
    weapon: str | None
    attacker_name: str | None
    victim_name: str | None
    headshot: bool
    penetrated: bool | None
    through_smoke: bool | None
    noscope: bool | None
    attacker_blind: bool | None
    flash_assisted: bool | None
    attacker_place: str | None
    victim_place: str | None
    attacker_pos: tuple[float | None, float | None, float | None]
    victim_pos: tuple[float | None, float | None, float | None]
    enemy: bool = False
    is_opening: bool = False
    is_trade: bool = False
    was_traded: bool = False


@dataclass
class _Hurt:
    tick: int
    attacker: int | None
    victim: int | None
    attacker_side: Side | None
    victim_side: Side | None
    damage: int
    health_after: int | None
    weapon: str | None


@dataclass
class _Snap:
    """One player's state at one tick."""

    steamid: int
    name: str | None
    side: Side | None
    alive: bool
    x: float | None
    y: float | None
    z: float | None
    place: str | None
    equipment: int | None
    money_start: int | None
    clan: str | None
    primary_weapon: str | None


@dataclass
class _PlayerRound:
    steamid: int
    side: Side
    name: str | None = None
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    kast_assists: int = 0  # assists that are not pure flash assists
    damage: int = 0
    utility_damage: int = 0
    enemies_flashed: int = 0
    flashes_thrown: int = 0
    smokes_thrown: int = 0
    he_thrown: int = 0
    molotovs_thrown: int = 0
    equipment_value: int | None = None
    money_start: int | None = None
    traded: bool = False
    opening_kill: bool = False
    opening_death: bool = False
    clutch_situation: str | None = None
    clutch_won: bool | None = None
    primary_weapon: str | None = None


# ----------------------------------------------------------------------------------------- event load


def _load_events(parser: DemoParserLike, warnings: list[str]) -> dict[str, list[dict[str, Any]]]:
    """All needed events as lists of row dicts (events that never happen are empty lists)."""
    frames: dict[str, Any] = {}
    try:
        for name, frame in parser.parse_events(
            list(EVENT_NAMES), player=list(EVENT_PLAYER_PROPS), other=list(EVENT_OTHER_PROPS)
        ):
            frames[str(name)] = frame
    except Exception as exc:
        raise DemoParseError(f"demoparser2 could not read the events: {exc}") from exc
    # The synthesised round events are essential; ask again one by one if the batch call left them
    # out (awpy queries them separately for the same reason).
    for name in ("round_start", "round_end", "round_freeze_end", "round_officially_ended"):
        if not records(frames.get(name)):
            try:
                frames[name] = parser.parse_event(name, other=list(EVENT_OTHER_PROPS))
            except Exception as exc:
                warnings.append(f"event {name} unavailable: {exc}")
    return {name: records(frames.get(name)) for name in EVENT_NAMES}


def _sorted_ticks(rows: list[dict[str, Any]]) -> list[int]:
    return sorted({t for t in (to_int(r.get("tick")) for r in rows) if t is not None})


def _parse_deaths(rows: list[dict[str, Any]]) -> list[_Death]:
    out: list[_Death] = []
    for row in rows:
        tick = to_int(row.get("tick"))
        if tick is None:
            continue
        penetrated = to_int(row.get("penetrated"))
        out.append(
            _Death(
                tick=tick,
                attacker=to_steamid(row.get("attacker_steamid")),
                victim=to_steamid(row.get("user_steamid")),
                assister=to_steamid(row.get("assister_steamid")),
                attacker_side=parse_side(row.get("attacker_team_num")),
                victim_side=parse_side(row.get("user_team_num")),
                assister_side=parse_side(row.get("assister_team_num")),
                weapon=_clean_weapon(row.get("weapon")),
                attacker_name=to_str(row.get("attacker_name")),
                victim_name=to_str(row.get("user_name")),
                headshot=bool(to_bool(row.get("headshot"))),
                penetrated=None if penetrated is None else penetrated > 0,
                through_smoke=to_bool(row.get("thrusmoke")),
                noscope=to_bool(row.get("noscope")),
                attacker_blind=to_bool(row.get("attackerblind")),
                flash_assisted=to_bool(row.get("assistedflash")),
                attacker_place=to_str(row.get("attacker_last_place_name")),
                victim_place=to_str(row.get("user_last_place_name")),
                attacker_pos=(
                    to_float(row.get("attacker_X")),
                    to_float(row.get("attacker_Y")),
                    to_float(row.get("attacker_Z")),
                ),
                victim_pos=(
                    to_float(row.get("user_X")),
                    to_float(row.get("user_Y")),
                    to_float(row.get("user_Z")),
                ),
            )
        )
    out.sort(key=lambda d: d.tick)  # stable: same-tick kills keep their demo order
    return out


def _parse_hurts(rows: list[dict[str, Any]]) -> list[_Hurt]:
    out: list[_Hurt] = []
    for row in rows:
        tick = to_int(row.get("tick"))
        damage = to_int(row.get("dmg_health"))
        if tick is None or damage is None or damage <= 0:
            continue
        health = to_int(row.get("health"))
        out.append(
            _Hurt(
                tick=tick,
                attacker=to_steamid(row.get("attacker_steamid")),
                victim=to_steamid(row.get("user_steamid")),
                attacker_side=parse_side(row.get("attacker_team_num")),
                victim_side=parse_side(row.get("user_team_num")),
                damage=damage,
                health_after=health if health is not None and 0 <= health <= 100 else None,
                weapon=_clean_weapon(row.get("weapon")),
            )
        )
    out.sort(key=lambda h: h.tick)
    return out


# -------------------------------------------------------------------------------------- round cleaning


def _build_round_candidates(
    events: dict[str, list[dict[str, Any]]], tick_rate: int, warnings: list[str]
) -> list[_RawRound]:
    """Pair every ``round_end`` with the freeze end / start of its round (see module docstring)."""
    ends: dict[int, tuple[Side | None, str | None]] = {}
    for row in events["round_end"]:
        tick = to_int(row.get("tick"))
        if tick is None:
            continue
        winner = parse_side(row.get("winner")) or _winner_from_reason(row.get("reason"))
        ends[tick] = (winner, to_str(row.get("reason")))
    freezes: dict[int, tuple[int | None, bool]] = {}
    for row in events["round_freeze_end"]:
        tick = to_int(row.get("tick"))
        if tick is not None:
            freezes[tick] = (
                to_int(row.get("total_rounds_played")),
                bool(to_bool(row.get("is_warmup_period"))),
            )
    start_state: dict[int, tuple[int | None, bool]] = {}
    for row in events["round_start"]:
        tick = to_int(row.get("tick"))
        if tick is not None:
            start_state[tick] = (
                to_int(row.get("total_rounds_played")),
                bool(to_bool(row.get("is_warmup_period"))),
            )
    freeze_ticks = sorted(freezes)
    start_ticks = sorted(start_state)
    official_ticks = _sorted_ticks(events["round_officially_ended"])
    end_ticks = sorted(ends)

    def last_in(ticks: list[int], lo_exclusive: int, hi_inclusive: int) -> int | None:
        i = bisect.bisect_right(ticks, hi_inclusive) - 1
        return ticks[i] if i >= 0 and ticks[i] > lo_exclusive else None

    def first_after(ticks: list[int], lo_exclusive: int) -> int | None:
        i = bisect.bisect_right(ticks, lo_exclusive)
        return ticks[i] if i < len(ticks) else None

    post_round = round(POST_ROUND_SECONDS * tick_rate)
    out: list[_RawRound] = []
    prev_end = -1
    for end_tick in end_ticks:
        winner, reason_raw = ends[end_tick]
        freeze_tick = last_in(freeze_ticks, prev_end, end_tick)
        # ">= prev_end": demos open with a pseudo round_end (no winner) on the tick of the first round_start.
        start_tick = last_in(start_ticks, prev_end - 1, freeze_tick if freeze_tick is not None else end_tick)
        lower = prev_end
        prev_end = end_tick
        if winner is None:
            continue  # draw / "game start" pseudo round
        if freeze_tick is not None:
            trp, warmup = freezes[freeze_tick]
            has_freeze_end = True
        elif start_tick is not None:
            # No freeze end (e.g. a round ended by a surrender vote): time is measured from the start.
            freeze_tick = start_tick
            trp, warmup = start_state[start_tick]
            has_freeze_end = False
        else:
            warnings.append(f"round ending at tick {end_tick} has no start/freeze-end event (after {lower})")
            continue
        following = [
            t
            for t in (first_after(start_ticks, end_tick), first_after(freeze_ticks, end_tick))
            if t is not None
        ]
        next_boundary = min(following) if following else None
        official = first_after(official_ticks, end_tick)
        if official is None or (next_boundary is not None and official > next_boundary):
            official = end_tick + post_round
        if next_boundary is not None and official >= next_boundary:
            official = next_boundary - 1
        out.append(
            _RawRound(
                start_tick=start_tick,
                freeze_tick=freeze_tick,
                end_tick=end_tick,
                official_tick=max(official, end_tick),
                winner=winner,
                reason_raw=reason_raw,
                trp=trp,
                warmup=warmup,
                has_freeze_end=has_freeze_end,
            )
        )
    return out


def _is_knife_round(rnd: _RawRound, deaths: list[_Death], hurts: list[_Hurt]) -> bool:
    """A round in which every kill and every point of player-inflicted damage came from a knife."""
    weapons = [
        d.weapon for d in deaths if rnd.freeze_tick <= d.tick <= rnd.end_tick and d.attacker is not None
    ]
    weapons += [
        h.weapon for h in hurts if rnd.freeze_tick <= h.tick <= rnd.end_tick and h.attacker is not None
    ]
    return bool(weapons) and all(is_knife_weapon(w) for w in weapons)


def _clean_rounds(
    candidates: list[_RawRound],
    deaths: list[_Death],
    hurts: list[_Hurt],
    options: ParseOptions,
    warnings: list[str],
    dropped: dict[str, int],
) -> list[_RawRound]:
    """Keep only the real rounds of the match and number them (see module docstring)."""
    live = [r for r in candidates if not r.warmup]
    dropped["warmup"] = len(candidates) - len(live)
    if live and all(r.trp is None for r in live):
        warnings.append("total_rounds_played unavailable: rounds numbered sequentially, restarts undetected")

    segments: list[list[_RawRound]] = []
    current: list[_RawRound] = []
    replayed = 0
    for rnd in live:
        if rnd.trp is None:
            rnd.trp_eff = current[-1].trp_eff + 1 if current else 0
        else:
            rnd.trp_eff = rnd.trp
            if current and rnd.trp == 0:
                segments.append(current)  # match restart: whatever was played before does not count
                current = []
            else:
                while current and current[-1].trp_eff >= rnd.trp:
                    current.pop()  # this round replays (and replaces) rounds restored from a backup
                    replayed += 1
        current.append(rnd)
    if current:
        segments.append(current)
    dropped["replayed"] = replayed

    knife = 0
    cleaned: list[list[_RawRound]] = []
    for segment in segments:
        offset = 0
        if _is_knife_round(segment[0], deaths, hurts):
            segment = segment[1:]
            offset = 1
            knife += 1
        for rnd in segment:
            rnd.number = rnd.trp_eff + 1 - offset
        if segment:
            cleaned.append(segment)
    dropped["knife"] = knife
    if not cleaned:
        return []
    complete = [s for s in cleaned if len(s) >= options.min_match_rounds]
    if complete:
        chosen = complete[-1]
    else:
        chosen = max(reversed(cleaned), key=len)
        warnings.append(f"only {len(chosen)} rounds found - the demo looks incomplete")
    if chosen is not cleaned[-1]:
        warnings.append("rounds after the match (a later restart) were ignored")
    dropped["restarted"] = sum(len(s) for s in cleaned if s is not chosen)
    numbers = [r.number for r in chosen]
    if numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        warnings.append(f"round numbers are not consecutive: {numbers}")
    if numbers[0] != 1:
        warnings.append(f"the demo starts at round {numbers[0]}")
    return chosen


# ------------------------------------------------------------------------------------------ tick data


def _load_ticks(
    parser: DemoParserLike, wanted: list[int], warnings: list[str]
) -> dict[int, dict[int, _Snap]]:
    """``{tick: {steamid: snapshot}}`` for the requested ticks (ticks missing in the demo are absent)."""
    if not wanted:
        return {}
    try:
        frame = parser.parse_ticks([*CORE_TICK_PROPS, *OPTIONAL_TICK_PROPS], ticks=wanted)
    except Exception as exc:
        warnings.append(f"optional player props unavailable ({exc}); retrying with the core props")
        try:
            frame = parser.parse_ticks(list(CORE_TICK_PROPS), ticks=wanted)
        except Exception as exc2:
            raise DemoParseError(f"demoparser2 could not read player state: {exc2}") from exc2
    by_tick: dict[int, dict[int, _Snap]] = defaultdict(dict)
    for row in records(frame):
        tick = to_int(row.get("tick"))
        steamid = to_steamid(row.get("steamid"))
        if tick is None or steamid is None:
            continue  # steamid 0 = bot
        side = parse_side(row.get("team_num"))
        if side is None:
            side = parse_side(row.get("team_name"))
        alive = to_bool(row.get("is_alive"))
        if alive is None:
            health = to_int(row.get("health"))
            alive = True if health is None else health > 0
        money = to_int(row.get("start_balance"))
        if money is None:
            balance, spent = to_int(row.get("balance")), to_int(row.get("cash_spent_this_round"))
            money = balance + spent if balance is not None and spent is not None else None
        by_tick[tick][steamid] = _Snap(
            steamid=steamid,
            name=to_str(row.get("name")),
            side=side,
            alive=alive,
            x=to_float(row.get("X")),
            y=to_float(row.get("Y")),
            z=to_float(row.get("Z")),
            place=to_str(row.get("last_place_name")),
            equipment=to_int(row.get("current_equip_value")),
            money_start=money,
            clan=to_str(row.get("team_clan_name")),
            primary_weapon=_primary_weapon(row.get("inventory")),
        )
    return by_tick


def _snaps_at(by_tick: dict[int, dict[int, _Snap]], tick: int, window: int) -> dict[int, _Snap]:
    for t in range(tick, tick + max(1, window)):
        snaps = by_tick.get(t)
        if snaps:
            return snaps
    return {}


def _window(tick: int, window: int) -> range:
    return range(tick, tick + max(1, window))


def _blinds_from_flash_duration(
    parser: DemoParserLike,
    detonations: list[dict[str, Any]],
    options: ParseOptions,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Rebuild ``player_blind``-like rows from ``flash_duration`` around each flashbang detonation.

    For every detonation the players' ``flash_duration`` just before it (the last tick available in
    the ``tick_search_window`` before the detonation tick) is compared with the value at the
    detonation (the first tick available from the detonation tick on): a rise is a new blind of that
    length, caused by this flashbang. A rise that an earlier detonation within the same window was
    already credited with is not credited twice (two flashbangs popping on neighbouring ticks).
    """
    window = max(1, options.tick_search_window)
    pops: list[tuple[int, int | None]] = []
    for row in detonations:
        tick = to_int(row.get("tick"))
        if tick is not None:
            pops.append((tick, to_steamid(row.get("user_steamid"))))
    if not pops:
        return []
    pops.sort(key=lambda pop: pop[0])
    wanted = sorted({t for tick, _thrower in pops for t in range(tick - window, tick + window) if t >= 0})
    try:
        frame = parser.parse_ticks(["flash_duration"], ticks=wanted)
    except Exception as exc:
        warnings.append(f"flash_duration unavailable ({exc}): enemies flashed cannot be counted")
        return []
    durations: dict[int, dict[int, float]] = defaultdict(dict)
    for row in records(frame):
        tick, steamid = to_int(row.get("tick")), to_steamid(row.get("steamid"))
        if tick is not None and steamid is not None:
            durations[tick][steamid] = to_float(row.get("flash_duration")) or 0.0

    def first_available(ticks: Iterable[int]) -> dict[int, float] | None:
        return next((durations[t] for t in ticks if t in durations), None)

    out: list[dict[str, Any]] = []
    credited: dict[int, tuple[int, float]] = {}  # victim -> (detonation tick, duration) of the last credit
    for tick, thrower in pops:
        before = first_available(range(tick - 1, tick - window - 1, -1))
        after = first_available(range(tick, tick + window))
        if after is None or before is None:
            continue  # without a "before" value a rise cannot be told from an older blind
        for victim, duration in after.items():
            previous = before.get(victim, 0.0)
            if duration <= previous + 1e-3:
                continue  # not a rising edge: not blinded (again) by this flashbang
            last = credited.get(victim)
            if last is not None and tick - last[0] < window and abs(last[1] - duration) < 1e-3:
                continue
            credited[victim] = (tick, duration)
            out.append(
                {
                    "tick": tick,
                    "attacker_steamid": thrower,
                    "user_steamid": victim,
                    "blind_duration": duration,
                }
            )
    return out


# ------------------------------------------------------------------------------------- per-round logic


def _mark_trades(kills: list[_Death], window_ticks: float) -> None:
    """Set ``is_trade`` / ``was_traded`` on the enemy kills of one round (ordered by tick)."""
    enemy_kills = [k for k in kills if k.enemy and k.attacker is not None and k.victim is not None]
    for i, first in enumerate(enemy_kills):
        for later in enemy_kills[i + 1 :]:
            if later.tick - first.tick > window_ticks:
                break
            if (
                later.victim == first.attacker
                and later.attacker != first.victim
                and later.attacker_side is not None
                and later.attacker_side == first.victim_side
            ):
                first.was_traded = True
                later.is_trade = True


def _detect_clutches(
    members: dict[int, Side], deaths: list[_Death], winner: Side
) -> dict[int, tuple[str, bool]]:
    """``{steamid: ("1vN", won)}`` for the players who ended up alone against N living enemies."""
    alive: dict[Side, set[int]] = {Side.CT: set(), Side.T: set()}
    for steamid, side in members.items():
        alive[side].add(steamid)
    initial = {side: len(players) for side, players in alive.items()}
    out: dict[int, tuple[str, bool]] = {}
    flagged: set[Side] = set()
    for death in deaths:
        if death.victim is None:
            continue
        own = members.get(death.victim)
        if own is None or death.victim not in alive[own]:
            continue
        alive[own].discard(death.victim)
        enemies = len(alive[own.other])
        if len(alive[own]) == 1 and own not in flagged and initial[own] >= 2 and enemies >= 1:
            flagged.add(own)
            (last,) = alive[own]
            out[last] = (f"1v{enemies}", winner is own)
    return out


def _distance(a: tuple[float | None, ...], b: tuple[float | None, ...]) -> float | None:
    coords = [v for v in (*a, *b) if v is not None]
    if len(coords) != 6:
        return None
    return math.sqrt(sum((p - q) ** 2 for p, q in zip(coords[:3], coords[3:], strict=True)))


def _side_total(players: list[_PlayerRound], attribute: str) -> int | None:
    values = [getattr(pr, attribute) for pr in players if getattr(pr, attribute) is not None]
    return int(sum(values)) if values else None


def _side_buy(players: list[_PlayerRound], round_number: int) -> str | None:
    values = [pr.equipment_value for pr in players if pr.equipment_value is not None]
    buy_type = classify_buy_type(sum(values) / len(values) if values else None, round_number)
    return buy_type.value if buy_type is not None else None


def _frame(rows: list[dict[str, Any]], columns: Sequence[str], exact: Sequence[str] = ()) -> pd.DataFrame:
    """DataFrame with a fixed column order; ``exact`` columns keep Python objects.

    Steam ids are 17-digit integers: a nullable column would silently become float64 and lose
    the last digits, so those columns are built with ``dtype=object``.
    """
    data = {
        column: pd.Series([row.get(column) for row in rows], dtype=object if column in exact else None)
        for column in columns
    }
    return pd.DataFrame(data, columns=list(columns))


def _events_in(rows: list[dict[str, Any]], lo: int, hi: int) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        tick = to_int(row.get("tick"))
        if tick is not None and lo <= tick <= hi:
            out.append(row)
    return out


def _resolve_bomb_sites(plants: list[dict[str, Any]]) -> dict[int, str]:
    """``{site entity id: "A" | "B"}`` by majority vote over the planters' place names.

    ``bomb_planted.site`` is the entity index of the bomb-site trigger, which differs per map, while
    the planter's ``last_place_name`` is ``BombsiteA`` / ``BombsiteB`` for almost every plant. Voting
    per site id also labels the few plants made from the edge of a neighbouring place.
    """
    votes: dict[int, Counter[str]] = defaultdict(Counter)
    for row in plants:
        site_id = to_int(row.get("site"))
        letter = _bomb_site_from_place(to_str(row.get("user_last_place_name")))
        if site_id is not None and letter is not None:
            votes[site_id][letter] += 1
    return {site_id: counter.most_common(1)[0][0] for site_id, counter in votes.items()}


# ------------------------------------------------------------------------------------------- parsing


def parse_demo(
    path: Path | str,
    settings: DemoSettings,
    *,
    parser_factory: ParserFactory = DemoParser,
    options: ParseOptions | None = None,
) -> ParsedDemo:
    """Parse one demo file into a :class:`ParsedDemo`.

    ``settings.position_sample_seconds`` and ``settings.trade_window_s`` are honoured;
    ``parser_factory`` is ``demoparser2.DemoParser`` unless a test injects a fake. Raises
    :class:`DemoParseError` when the file cannot be parsed or contains no complete round.
    """
    options = options or ParseOptions()
    try:
        return _parse(Path(path), settings, parser_factory, options)
    except DemoError:
        raise
    except Exception as exc:  # demoparser2 raises plain Exception subclasses; never leak them raw
        raise DemoParseError(f"failed to parse {Path(path).name}: {type(exc).__name__}: {exc}") from exc


def _parse(
    path: Path, settings: DemoSettings, parser_factory: ParserFactory, options: ParseOptions
) -> ParsedDemo:
    warnings: list[str] = []
    dropped: dict[str, int] = {}
    tick_rate = options.tick_rate
    parser = parser_factory(str(path))
    header = {str(k): v for k, v in dict(parser.parse_header()).items()}
    map_name = normalize_map_name(str(header.get("map_name") or ""))
    if not map_name:
        warnings.append("demo header has no map name")

    events = _load_events(parser, warnings)
    deaths = _parse_deaths(events["player_death"])
    hurts = _parse_hurts(events["player_hurt"])
    candidates = _build_round_candidates(events, tick_rate, warnings)
    rounds = _clean_rounds(candidates, deaths, hurts, options, warnings, dropped)
    if not rounds:
        raise DemoParseError(f"{path.name}: no complete match round found ({len(candidates)} round ends)")

    sample_seconds = sorted({int(s) for s in settings.position_sample_seconds if int(s) >= 0})
    wanted: set[int] = set()
    for rnd in rounds:
        wanted.update(_window(rnd.freeze_tick, options.tick_search_window))
        for second in sample_seconds:
            tick = rnd.freeze_tick + second * tick_rate
            if tick <= rnd.end_tick:
                wanted.update(_window(tick, options.tick_search_window))
    by_tick = _load_ticks(parser, sorted(wanted), warnings)

    blind_rows = events["player_blind"]
    if not blind_rows:
        in_match = [
            row
            for row in events["flashbang_detonate"]
            if any(r.freeze_tick <= (to_int(row.get("tick")) or -1) <= r.official_tick for r in rounds)
        ]
        blind_rows = _blinds_from_flash_duration(parser, in_match, options, warnings)

    site_by_id = _resolve_bomb_sites(events["bomb_planted"])
    throw_events = (
        ("flashbang_detonate", "flashes_thrown"),
        ("smokegrenade_detonate", "smokes_thrown"),
        ("hegrenade_detonate", "he_thrown"),
        ("inferno_startburn", "molotovs_thrown"),
    )
    trade_ticks = settings.trade_window_s * tick_rate

    names: dict[int, Counter[str]] = defaultdict(Counter)
    clans: dict[int, Counter[str]] = defaultdict(Counter)
    roster: dict[int, str] = {}
    first_side: dict[int, Side] = {}
    rounds_played: Counter[int] = Counter()
    roster_scores = {"a": 0, "b": 0}
    previous_ct_roster: str | None = None

    round_rows: list[dict[str, Any]] = []
    kill_rows: list[dict[str, Any]] = []
    player_round_rows: list[dict[str, Any]] = []
    position_rows: list[dict[str, Any]] = []

    for rnd in rounds:
        lo, hi = rnd.freeze_tick, rnd.official_tick
        freeze_snaps = _snaps_at(by_tick, rnd.freeze_tick, options.tick_search_window)
        stats: dict[int, _PlayerRound] = {}
        for snap in freeze_snaps.values():
            if snap.side is None or not snap.alive:
                continue  # spectators, coaches, players that have not spawned
            stats[snap.steamid] = _PlayerRound(
                steamid=snap.steamid,
                side=snap.side,
                name=snap.name,
                equipment_value=snap.equipment,
                money_start=snap.money_start,
                primary_weapon=snap.primary_weapon,
            )
            if snap.name:
                names[snap.steamid][snap.name] += 1
            if snap.clan:
                clans[snap.steamid][snap.clan] += 1
        if not freeze_snaps:
            warnings.append(f"round {rnd.number}: no player state at freeze end (tick {rnd.freeze_tick})")

        round_deaths = [d for d in deaths if lo <= d.tick <= hi]
        round_hurts = [h for h in hurts if lo <= h.tick <= hi]

        for death in round_deaths:
            # A player missing from the freeze-end snapshot (e.g. reconnected) still gets a stat line.
            for steamid, side, name in (
                (death.attacker, death.attacker_side, death.attacker_name),
                (death.victim, death.victim_side, death.victim_name),
            ):
                if steamid is not None and side is not None and steamid not in stats:
                    stats[steamid] = _PlayerRound(steamid=steamid, side=side, name=name)
                    if name:
                        names[steamid][name] += 1
        side_of: dict[int, Side] = {sid: pr.side for sid, pr in stats.items()}

        # -- kills --------------------------------------------------------------------------------
        for death in round_deaths:
            if death.attacker is not None:
                death.attacker_side = side_of.get(death.attacker, death.attacker_side)
            if death.victim is not None:
                death.victim_side = side_of.get(death.victim, death.victim_side)
            if death.assister is not None:
                death.assister_side = side_of.get(death.assister, death.assister_side)
            death.enemy = (
                death.attacker is not None
                and death.attacker != death.victim
                and (
                    death.attacker_side is None
                    or death.victim_side is None
                    or death.attacker_side != death.victim_side
                )
            )
        opening = next((d for d in round_deaths if d.enemy and d.tick <= rnd.end_tick), None)
        if opening is not None:
            opening.is_opening = True
        _mark_trades(round_deaths, trade_ticks)

        for death in round_deaths:
            victim = stats.get(death.victim) if death.victim is not None else None
            if victim is not None:
                victim.deaths += 1
                victim.traded = victim.traded or death.was_traded
                victim.opening_death = victim.opening_death or death.is_opening
            attacker = stats.get(death.attacker) if death.attacker is not None else None
            if death.enemy and attacker is not None:
                attacker.kills += 1
                attacker.opening_kill = attacker.opening_kill or death.is_opening
            assister = stats.get(death.assister) if death.assister is not None else None
            if (
                assister is not None
                and death.assister != death.victim
                and (death.victim_side is None or assister.side != death.victim_side)
            ):
                assister.assists += 1
                if not death.flash_assisted:
                    assister.kast_assists += 1
            kill_rows.append(
                {
                    "round_number": rnd.number,
                    "tick": death.tick,
                    "seconds_into_round": max(0.0, (death.tick - rnd.freeze_tick) / tick_rate),
                    "attacker_steamid": death.attacker,
                    "victim_steamid": death.victim,
                    "assister_steamid": death.assister,
                    "attacker_side": death.attacker_side.value if death.attacker_side else None,
                    "victim_side": death.victim_side.value if death.victim_side else None,
                    "weapon": death.weapon,
                    "headshot": death.headshot,
                    "penetrated": death.penetrated,
                    "through_smoke": death.through_smoke,
                    "noscope": death.noscope,
                    "attacker_blind": death.attacker_blind,
                    "flash_assisted": death.flash_assisted,
                    "attacker_place": death.attacker_place if death.attacker is not None else None,
                    "victim_place": death.victim_place,
                    "attacker_x": death.attacker_pos[0],
                    "attacker_y": death.attacker_pos[1],
                    "attacker_z": death.attacker_pos[2],
                    "victim_x": death.victim_pos[0],
                    "victim_y": death.victim_pos[1],
                    "victim_z": death.victim_pos[2],
                    "distance": _distance(death.attacker_pos, death.victim_pos),
                    "is_opening": death.is_opening,
                    "is_trade": death.is_trade,
                    "was_traded": death.was_traded,
                    "is_enemy_kill": death.enemy,
                }
            )

        # -- damage (capped by the victim's remaining health) -----------------------------------------
        health_left: dict[int, int] = {}
        for hurt in round_hurts:
            remaining = health_left.get(hurt.victim, 100) if hurt.victim is not None else 100
            dealt = max(0, min(hurt.damage, remaining))
            if hurt.victim is not None:
                health_left[hurt.victim] = (
                    hurt.health_after if hurt.health_after is not None else remaining - dealt
                )
            attacker = stats.get(hurt.attacker) if hurt.attacker is not None else None
            if attacker is None or hurt.attacker == hurt.victim or dealt <= 0:
                continue
            victim_side = hurt.victim_side
            if hurt.victim is not None:
                victim_side = side_of.get(hurt.victim, victim_side)
            if victim_side is not None and victim_side == attacker.side:
                continue  # team damage
            attacker.damage += dealt
            if hurt.weapon in UTILITY_DAMAGE_WEAPONS:
                attacker.utility_damage += dealt

        # -- flashes and thrown utility ---------------------------------------------------------------
        death_tick = {d.victim: d.tick for d in reversed(round_deaths) if d.victim is not None}
        for row in _events_in(blind_rows, lo, hi):
            thrower = stats.get(to_steamid(row.get("attacker_steamid")) or 0)
            victim_id = to_steamid(row.get("user_steamid"))
            tick = to_int(row.get("tick")) or 0
            duration = to_float(row.get("blind_duration")) or 0.0
            if thrower is None or victim_id is None or victim_id == thrower.steamid:
                continue
            victim_side = side_of.get(victim_id) or parse_side(row.get("user_team_num"))
            if victim_side is None or victim_side == thrower.side:
                continue
            if duration < options.min_blind_duration_s or death_tick.get(victim_id, tick + 1) < tick:
                continue
            thrower.enemies_flashed += 1
        for event_name, attribute in throw_events:
            for row in _events_in(events[event_name], lo, hi):
                thrower = stats.get(to_steamid(row.get("user_steamid")) or 0)
                if thrower is not None:
                    setattr(thrower, attribute, getattr(thrower, attribute) + 1)

        # -- bomb ------------------------------------------------------------------------------------
        plants = _events_in(events["bomb_planted"], rnd.freeze_tick, hi)
        bomb_site: str | None = None
        if plants:
            site_id = to_int(plants[0].get("site"))
            bomb_site = _bomb_site_from_place(to_str(plants[0].get("user_last_place_name")))
            if bomb_site is None and site_id is not None:
                bomb_site = site_by_id.get(site_id)

        # -- clutches, survivors, economy ------------------------------------------------------------
        in_round_deaths = [d for d in round_deaths if d.tick <= rnd.end_tick]
        for steamid, (situation, won) in _detect_clutches(side_of, in_round_deaths, rnd.winner).items():
            stats[steamid].clutch_situation = situation
            stats[steamid].clutch_won = won
        dead_in_round = {d.victim for d in in_round_deaths if d.victim is not None}
        per_side: dict[Side, list[_PlayerRound]] = {Side.CT: [], Side.T: []}
        for pr in stats.values():
            per_side[pr.side].append(pr)

        # -- rosters ("a" = the players that were CT in the first parsed round) ----------------------
        known = [(roster[sid], side) for sid, side in side_of.items() if sid in roster]
        votes_a_ct = sum(1 for key, side in known if (key == "a") == (side is Side.CT))
        votes_b_ct = len(known) - votes_a_ct
        if votes_a_ct == votes_b_ct:
            ct_roster = previous_ct_roster or "a"
        else:
            ct_roster = "a" if votes_a_ct > votes_b_ct else "b"
        previous_ct_roster = ct_roster
        t_roster = "b" if ct_roster == "a" else "a"
        for steamid, side in side_of.items():
            roster.setdefault(steamid, ct_roster if side is Side.CT else t_roster)
            first_side.setdefault(steamid, side)
            rounds_played[steamid] += 1
        roster_scores[ct_roster if rnd.winner is Side.CT else t_roster] += 1

        round_rows.append(
            {
                "round_number": rnd.number,
                "start_tick": rnd.start_tick,
                "freeze_end_tick": rnd.freeze_tick,
                "end_tick": rnd.end_tick,
                "official_end_tick": rnd.official_tick,
                "winner_side": rnd.winner.value,
                "end_reason": map_round_end_reason(rnd.reason_raw).value,
                "reason_raw": rnd.reason_raw,
                "ct_equipment_value": _side_total(per_side[Side.CT], "equipment_value"),
                "t_equipment_value": _side_total(per_side[Side.T], "equipment_value"),
                "ct_money_start": _side_total(per_side[Side.CT], "money_start"),
                "t_money_start": _side_total(per_side[Side.T], "money_start"),
                "ct_buy_type": _side_buy(per_side[Side.CT], rnd.number),
                "t_buy_type": _side_buy(per_side[Side.T], rnd.number),
                "bomb_planted": bool(plants),
                "bomb_site": bomb_site,
                "ct_survivors": sum(1 for pr in per_side[Side.CT] if pr.steamid not in dead_in_round),
                "t_survivors": sum(1 for pr in per_side[Side.T] if pr.steamid not in dead_in_round),
                "duration_s": max(0.0, (rnd.end_tick - rnd.freeze_tick) / tick_rate),
                "ct_players": len(per_side[Side.CT]),
                "t_players": len(per_side[Side.T]),
                "ct_roster": ct_roster,
                "is_overtime": rnd.number > REGULATION_ROUNDS,
            }
        )
        for pr in stats.values():
            survived = pr.deaths == 0
            player_round_rows.append(
                {
                    "round_number": rnd.number,
                    "steamid": pr.steamid,
                    "name": pr.name,
                    "side": pr.side.value,
                    "roster": roster[pr.steamid],
                    "kills": pr.kills,
                    "deaths": pr.deaths,
                    "assists": pr.assists,
                    "damage": pr.damage,
                    "utility_damage": pr.utility_damage,
                    "enemies_flashed": pr.enemies_flashed,
                    "flashes_thrown": pr.flashes_thrown,
                    "smokes_thrown": pr.smokes_thrown,
                    "he_thrown": pr.he_thrown,
                    "molotovs_thrown": pr.molotovs_thrown,
                    "equipment_value": pr.equipment_value,
                    "money_start": pr.money_start,
                    "survived": survived,
                    "kast": pr.kills > 0 or pr.kast_assists > 0 or survived or pr.traded,
                    "opening_kill": pr.opening_kill,
                    "opening_death": pr.opening_death,
                    "clutch_situation": pr.clutch_situation,
                    "clutch_won": pr.clutch_won,
                    "primary_weapon": pr.primary_weapon,
                }
            )

        # -- position samples ------------------------------------------------------------------------
        for second in sample_seconds:
            tick = rnd.freeze_tick + second * tick_rate
            if tick > rnd.end_tick:
                continue
            for snap in _snaps_at(by_tick, tick, options.tick_search_window).values():
                side = side_of.get(snap.steamid)
                if side is None or not snap.alive or death_tick.get(snap.steamid, tick + 1) <= tick:
                    continue
                position_rows.append(
                    {
                        "round_number": rnd.number,
                        "steamid": snap.steamid,
                        "side": side.value,
                        "seconds_into_round": second,
                        "place": snap.place,
                        "x": snap.x,
                        "y": snap.y,
                        "z": snap.z,
                        "is_alive": True,
                    }
                )

    player_rows = [
        {
            "steamid": steamid,
            "name": names[steamid].most_common(1)[0][0] if names[steamid] else None,
            "team_name": clans[steamid].most_common(1)[0][0] if clans[steamid] else None,
            "roster": roster[steamid],
            "start_side": first_side[steamid].value,
            "rounds_played": rounds_played[steamid],
        }
        for steamid in sorted(roster, key=lambda s: (roster[s], s))
    ]
    roster_names: dict[str, str | None] = {}
    for key in ("a", "b"):
        counter: Counter[str] = Counter()
        for steamid, value in roster.items():
            if value == key:
                counter.update(clans[steamid])
        roster_names[key] = counter.most_common(1)[0][0] if counter else None
    for key in ("a", "b"):
        size = sum(1 for value in roster.values() if value == key)
        if size > 5:
            warnings.append(f"roster {key} has {size} players (substitution or a mis-assigned player)")
    for message in warnings:
        log.warning("%s: %s", path.name, message)

    return ParsedDemo(
        map_name=map_name,
        header=header,
        players=_frame(player_rows, PLAYER_COLUMNS, exact=("steamid",)),
        rounds=_frame(round_rows, ROUND_COLUMNS),
        kills=_frame(
            kill_rows, KILL_COLUMNS, exact=("attacker_steamid", "victim_steamid", "assister_steamid")
        ),
        player_rounds=_frame(player_round_rows, PLAYER_ROUND_COLUMNS, exact=("steamid",)),
        positions=_frame(position_rows, POSITION_COLUMNS, exact=("steamid",)),
        tick_rate=tick_rate,
        parser_version=demoparser_version(),
        path=str(path),
        roster_names=roster_names,
        roster_scores=roster_scores,
        dropped_rounds=dropped,
        warnings=warnings,
    )
