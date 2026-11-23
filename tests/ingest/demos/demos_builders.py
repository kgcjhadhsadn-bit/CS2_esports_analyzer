"""Scripted fake of ``demoparser2.DemoParser`` for the demo tests (no real demo file is available).

``DemoBuilder`` scripts a match round by round; ``build()`` returns a :class:`FakeParser` whose
``parse_header`` / ``parse_event`` / ``parse_events`` / ``parse_ticks`` return pandas DataFrames with
the same columns, dtypes and quirks as demoparser2 0.4x (checked against its Rust source and docs):

* event frames: ``tick`` plus the event's own fields; players are referenced as ``user_*`` /
  ``attacker_*`` / ``assister_*`` with ``*_name`` and ``*_steamid`` (**strings**, ``None`` for the
  world); requested ``player=`` props are added per referenced player (``user_X``,
  ``attacker_last_place_name``, ...) and ``other=`` props under their own name;
* ``round_start`` / ``round_end`` / ``round_officially_ended`` are synthesised: ``round_end`` has
  ``winner`` (``"CT"`` / ``"T"``) and ``reason`` (``"ct_killed"``, ``"bomb_defused"``, ...);
  ``round_officially_ended`` shares its tick with the next ``round_start`` and is not emitted before
  the first round of a (re)started match;
* tick frames: one row per player per requested tick with ``tick``, ``steamid`` (uint64), ``name``
  and the requested props; ``team_num`` is 2 for T and 3 for CT;
* the demo opens with a winner-less ``round_end`` on the tick of the first ``round_start``, and at a
  ``round_end`` tick ``total_rounds_played`` is already incremented (both as in real demos);
* like a GOTV demo (every HLTV demo), the fake has no ``player_blind`` event unless
  ``has_player_blind=True``: a blind shows as ``flash_duration`` rising on the tick of the
  ``flashbang_detonate`` (so script a ``N(t, "flash", thrower)`` together with every ``B(t, ...)``).

Times in the script are seconds after freeze end; one second is ``tick_rate`` ticks.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

TEAM_A = ("a1", "a2", "a3", "a4", "a5")
TEAM_B = ("b1", "b2", "b3", "b4", "b5")
# 17-digit ids; several are odd numbers above 2**53, i.e. not representable as float64.
STEAMIDS: dict[str, int] = {
    **{name: 76561198000000001 + 2 * i for i, name in enumerate(TEAM_A)},
    **{name: 76561198100000001 + 2 * i for i, name in enumerate(TEAM_B)},
}
SITE_A_ID, SITE_B_ID = 292, 299

OPTIONAL_PROPS = ("start_balance", "cash_spent_this_round", "health", "inventory")


def sid(name: str) -> int:
    return STEAMIDS[name]


@dataclass
class K:
    """A ``player_death``: ``attacker`` kills ``victim`` ``t`` seconds after freeze end."""

    t: float
    attacker: str | None
    victim: str
    weapon: str = "ak47"
    assister: str | None = None
    headshot: bool = False
    penetrated: int = 0
    thrusmoke: bool = False
    noscope: bool = False
    attackerblind: bool = False
    assistedflash: bool = False


@dataclass
class H:
    """A ``player_hurt``: ``health`` is what the victim has left after the hit."""

    t: float
    attacker: str | None
    victim: str
    dmg: int
    health: int | None = None
    weapon: str = "ak47"


@dataclass
class B:
    """A ``player_blind``."""

    t: float
    attacker: str
    victim: str
    duration: float = 2.5


@dataclass
class N:
    """A grenade detonation: ``kind`` is ``flash`` / ``smoke`` / ``he`` / ``molotov``."""

    t: float
    kind: str
    thrower: str


_NADE_EVENT = {
    "flash": "flashbang_detonate",
    "smoke": "smokegrenade_detonate",
    "he": "hegrenade_detonate",
    "molotov": "inferno_startburn",
}

# A position is (place, x, y, z); a timeline is [(seconds after freeze end, position), ...].
Position = tuple[str, float, float, float]


@dataclass
class RoundScript:
    winner: str | None
    reason: str | None
    duration: float
    kills: list[K]
    hurts: list[H]
    blinds: list[B]
    nades: list[N]
    plant: tuple[float, str, int] | None
    equip: dict[str, int]
    money: dict[str, int]
    positions: dict[str, Position | list[tuple[float, Position]]]
    inventory: dict[str, list[str]]
    warmup: bool
    trp: int
    ct_team: str
    absent: frozenset[str]
    freeze_end: bool
    round_end_event: bool
    restored: bool = False
    start_tick: int = 0
    freeze_tick: int = 0
    end_tick: int = 0
    next_start_tick: int | None = None


@dataclass
class FakeParser:
    """Stand-in for ``demoparser2.DemoParser`` built by :class:`DemoBuilder`."""

    header: dict[str, str]
    rounds: list[RoundScript]
    tick_rate: int
    teams: dict[str, tuple[str, tuple[str, ...]]]
    tick_step: int = 1
    fail_optional_props: bool = False
    batch_omits_round_events: bool = False
    numeric_reasons: bool = False
    fail_header: bool = False
    fail_events: bool = False
    has_player_blind: bool = False  # GOTV (HLTV) demos do not record the player_blind event
    pseudo_round_end: bool = (
        True  # real demos open with a winner-less round_end on the first round_start tick
    )
    fail_flash_duration: bool = False
    calls: list[tuple[str, Any]] = field(default_factory=list)

    # ----------------------------------------------------------------------------------- helpers

    def tick_of(self, rnd: RoundScript, seconds: float) -> int:
        return rnd.freeze_tick + round(seconds * self.tick_rate)

    def _team_of(self, name: str) -> str:
        return "A" if name in self.teams["A"][1] else "B"

    def _side_num(self, rnd: RoundScript, name: str) -> int:
        return 3 if self._team_of(name) == rnd.ct_team else 2

    def _round_at(self, tick: int) -> RoundScript | None:
        current: RoundScript | None = None
        for rnd in self.rounds:
            if rnd.start_tick <= tick:
                current = rnd
        return current

    def _death_tick(self, rnd: RoundScript, name: str) -> int | None:
        ticks = [self.tick_of(rnd, k.t) for k in rnd.kills if k.victim == name and k.t >= 0]
        return min(ticks) if ticks else None

    def _position(self, rnd: RoundScript, name: str, tick: int) -> Position:
        spec = rnd.positions.get(name)
        index = (self.teams["A"][1] + self.teams["B"][1]).index(name)
        is_ct = self._side_num(rnd, name) == 3
        default: Position = ("CTSpawn" if is_ct else "TSpawn", 100.0 * index, 50.0 if is_ct else -50.0, 0.0)
        if spec is None:
            return default
        if isinstance(spec, tuple):
            return spec
        current = default
        for seconds, position in sorted(spec, key=lambda item: item[0]):
            if self.tick_of(rnd, seconds) <= tick:
                current = position
        return current

    def player_state(self, name: str, tick: int) -> dict[str, Any] | None:
        """Everything ``parse_ticks`` / the ``player=`` extras can report for one player at one tick."""
        rnd = self._round_at(tick)
        if rnd is None or name in rnd.absent:
            return None
        died = self._death_tick(rnd, name)
        alive = died is None or tick < died
        place, x, y, z = self._position(rnd, name, tick)
        health = 100
        for hurt in rnd.hurts:
            if hurt.victim == name and self.tick_of(rnd, hurt.t) <= tick and hurt.health is not None:
                health = hurt.health
        flash_duration = 0.0
        for blind in rnd.blinds:
            onset = self.tick_of(rnd, blind.t)
            if blind.victim == name and onset <= tick < onset + round(blind.duration * self.tick_rate):
                flash_duration = blind.duration
        equip = rnd.equip.get(name, 4500)
        start_balance = rnd.money.get(name, 5000)
        spent = min(equip, start_balance)
        team_key = self._team_of(name)
        return {
            "team_num": self._side_num(rnd, name),
            "team_name": "CT" if self._side_num(rnd, name) == 3 else "TERRORIST",
            "is_alive": alive,
            "health": health if alive else 0,
            "X": x,
            "Y": y,
            "Z": z,
            "last_place_name": place,
            "current_equip_value": equip,
            "balance": start_balance - spent,
            "start_balance": start_balance,
            "cash_spent_this_round": spent,
            "team_clan_name": self.teams[team_key][0],
            "inventory": rnd.inventory.get(name, ["Knife", "Glock-18"]),
            "flash_duration": flash_duration,
            "total_rounds_played": rnd.trp,
            "is_warmup_period": rnd.warmup,
        }

    def _player_fields(
        self, prefix: str, name: str | None, tick: int, props: Sequence[str]
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            f"{prefix}_name": name,
            f"{prefix}_steamid": str(STEAMIDS[name]) if name is not None else None,
        }
        state = self.player_state(name, tick) if name is not None else None
        for prop in props:
            out[f"{prefix}_{prop}"] = state.get(prop) if state is not None else None
        return out

    def _other(self, rnd: RoundScript, other: Sequence[str], *, trp: int | None = None) -> dict[str, Any]:
        values = {"total_rounds_played": rnd.trp if trp is None else trp, "is_warmup_period": rnd.warmup}
        return {name: values.get(name) for name in other}

    # ------------------------------------------------------------------------------------ events

    def _event_rows(self, name: str, player: Sequence[str], other: Sequence[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for rnd in self.rounds:
            extra = self._other(rnd, other)
            if name == "round_start":
                rows.append({"round": rnd.trp + 1, "tick": rnd.start_tick, **extra})
            elif name == "round_freeze_end" and rnd.freeze_end:
                rows.append({"tick": rnd.freeze_tick, **extra})
            elif name == "round_end":
                if rnd.restored or (self.pseudo_round_end and rnd is self.rounds[0]):
                    # no winner, no reason, on the very tick of the round_start
                    rows.append(
                        {"round": rnd.trp, "reason": None, "winner": None, "tick": rnd.start_tick, **extra}
                    )
                if not rnd.round_end_event:
                    continue
                reason: Any = rnd.reason
                winner: Any = rnd.winner
                if self.numeric_reasons:
                    reason = _REASON_CODES.get(str(rnd.reason), rnd.reason)
                    winner = {"T": 2, "CT": 3}.get(str(rnd.winner), rnd.winner)
                # at the round_end tick the rounds counter has already been incremented
                after = self._other(rnd, other, trp=rnd.trp + (0 if rnd.warmup or winner is None else 1))
                rows.append(
                    {"round": rnd.trp + 1, "reason": reason, "winner": winner, "tick": rnd.end_tick, **after}
                )
            elif name == "round_officially_ended" and rnd.trp >= 1:
                # Emitted when the freeze period of round N > 1 starts, i.e. at this round's start tick.
                rows.append({"tick": rnd.start_tick, **extra})
            elif name == "player_death":
                for kill in rnd.kills:
                    tick = self.tick_of(rnd, kill.t)
                    rows.append(
                        {
                            "assistedflash": kill.assistedflash,
                            **self._player_fields("assister", kill.assister, tick, player),
                            **self._player_fields("attacker", kill.attacker, tick, player),
                            "attackerblind": kill.attackerblind,
                            "attackerinair": False,
                            "distance": 12.5,
                            "dmg_armor": 0,
                            "dmg_health": 100,
                            "dominated": 0,
                            "headshot": kill.headshot,
                            "hitgroup": "head" if kill.headshot else "chest",
                            "noreplay": False,
                            "noscope": kill.noscope,
                            "penetrated": kill.penetrated,
                            "revenge": 0,
                            "thrusmoke": kill.thrusmoke,
                            "tick": tick,
                            **self._player_fields("user", kill.victim, tick - 1, player),
                            "weapon": kill.weapon,
                            "weapon_fauxitemid": "",
                            "weapon_itemid": "0",
                            "weapon_originalowner_xuid": "",
                            "wipe": 0,
                            **extra,
                        }
                    )
            elif name == "player_hurt":
                for hurt in rnd.hurts:
                    tick = self.tick_of(rnd, hurt.t)
                    rows.append(
                        {
                            "armor": 100,
                            **self._player_fields("attacker", hurt.attacker, tick, player),
                            "dmg_armor": 0,
                            "dmg_health": hurt.dmg,
                            "health": hurt.health,
                            "hitgroup": "chest",
                            "tick": tick,
                            **self._player_fields("user", hurt.victim, tick - 1, player),
                            "weapon": hurt.weapon,
                            **extra,
                        }
                    )
            elif name == "player_blind" and self.has_player_blind:
                for blind in rnd.blinds:
                    tick = self.tick_of(rnd, blind.t)
                    rows.append(
                        {
                            **self._player_fields("attacker", blind.attacker, tick, player),
                            "blind_duration": blind.duration,
                            "entityid": 400,
                            "tick": tick,
                            **self._player_fields("user", blind.victim, tick, player),
                            **extra,
                        }
                    )
            elif name == "bomb_planted" and rnd.plant is not None:
                seconds, planter, site = rnd.plant
                tick = self.tick_of(rnd, seconds)
                rows.append(
                    {
                        "site": site,
                        "tick": tick,
                        **self._player_fields("user", planter, tick, player),
                        **extra,
                    }
                )
            elif name in _NADE_EVENT.values():
                for nade in rnd.nades:
                    if _NADE_EVENT[nade.kind] != name:
                        continue
                    tick = self.tick_of(rnd, nade.t)
                    rows.append(
                        {
                            "entityid": 500,
                            "tick": tick,
                            **self._player_fields("user", nade.thrower, tick, player),
                            "x": 1.0,
                            "y": 2.0,
                            "z": 3.0,
                            **extra,
                        }
                    )
        return rows

    # ------------------------------------------------------------------- demoparser2 interface

    def parse_header(self) -> dict[str, str]:
        self.calls.append(("parse_header", None))
        if self.fail_header:
            raise RuntimeError("demo file is truncated")
        return dict(self.header)

    def parse_event(
        self, event_name: str, *, player: Sequence[str] | None = None, other: Sequence[str] | None = None
    ) -> pd.DataFrame:
        self.calls.append(("parse_event", event_name))
        return pd.DataFrame(self._event_rows(event_name, list(player or ()), list(other or ())))

    def parse_events(
        self,
        event_name: Sequence[str],
        *,
        player: Sequence[str] | None = None,
        other: Sequence[str] | None = None,
    ) -> list[tuple[str, pd.DataFrame]]:
        self.calls.append(("parse_events", (tuple(event_name), tuple(player or ()), tuple(other or ()))))
        if self.fail_events:
            raise RuntimeError("OutOfBytesError")
        out: list[tuple[str, pd.DataFrame]] = []
        for name in event_name:
            if self.batch_omits_round_events and name.startswith("round_") and name != "round_freeze_end":
                continue
            rows = self._event_rows(name, list(player or ()), list(other or ()))
            if rows:  # demoparser2 only returns the events that occur in the demo
                out.append((name, pd.DataFrame(rows)))
        return out

    def parse_ticks(
        self,
        wanted_props: Sequence[str],
        *,
        players: Sequence[int] | None = None,
        ticks: Sequence[int] | None = None,
    ) -> pd.DataFrame:
        self.calls.append(("parse_ticks", (tuple(wanted_props), tuple(ticks or ()))))
        if self.fail_optional_props and any(prop in OPTIONAL_PROPS for prop in wanted_props):
            raise RuntimeError("prop not found: start_balance")
        if self.fail_flash_duration and "flash_duration" in wanted_props:
            raise RuntimeError("prop not found: flash_duration")
        rows: list[dict[str, Any]] = []
        names = self.teams["A"][1] + self.teams["B"][1]
        for tick in sorted(set(ticks or ())):
            if tick % self.tick_step != 0:
                continue  # this tick is not in the demo
            for name in names:
                state = self.player_state(name, tick)
                if state is None:
                    continue
                row = {prop: state.get(prop) for prop in wanted_props}
                row.update({"tick": tick, "steamid": STEAMIDS[name], "name": name})
                rows.append(row)
        frame = pd.DataFrame(rows, columns=[*wanted_props, "tick", "steamid", "name"])
        if rows:
            frame["steamid"] = frame["steamid"].astype(np.uint64)
            frame["tick"] = frame["tick"].astype(np.int32)
        return frame


_REASON_CODES = {
    "bomb_exploded": 1,
    "bomb_defused": 7,
    "t_killed": 8,
    "ct_killed": 9,
    "time_ran_out": 12,
    "t_surrender": 17,
    "ct_surrender": 18,
}
_DEFAULT_REASON = {"CT": "t_killed", "T": "ct_killed"}


class DemoBuilder:
    """Script a demo round by round, then :meth:`build` the fake parser."""

    def __init__(
        self,
        map_name: str = "de_mirage",
        *,
        tick_rate: int = 64,
        team_a: str = "Alpha",
        team_b: str = "Bravo",
        ct_team: str = "A",
        freeze_s: float = 15.0,
        post_round_s: float = 7.0,
        first_tick: int = 1000,
    ) -> None:
        self.map_name = map_name
        self.tick_rate = tick_rate
        self.teams = {"A": (team_a, TEAM_A), "B": (team_b, TEAM_B)}
        self.ct_team = ct_team
        self.freeze_s = freeze_s
        self.post_round_s = post_round_s
        self.cursor = first_tick
        self.trp = 0
        self.rounds: list[RoundScript] = []

    # ---------------------------------------------------------------------------------- control

    def swap_sides(self) -> DemoBuilder:
        self.ct_team = "B" if self.ct_team == "A" else "A"
        return self

    def restart(self) -> DemoBuilder:
        """``mp_restartgame``: the rounds counter goes back to zero."""
        self.trp = 0
        return self

    def restore(self, rounds_played: int) -> DemoBuilder:
        """Backup restore: the next round is round ``rounds_played + 1`` again."""
        self.trp = rounds_played
        return self

    def gap(self, seconds: float) -> DemoBuilder:
        """Idle time (pause, timeout, half-time) before the next round starts."""
        self.cursor += round(seconds * self.tick_rate)
        return self

    # ----------------------------------------------------------------------------------- rounds

    def add_round(
        self,
        winner: str | None = "CT",
        reason: str | None = None,
        *,
        duration: float = 80.0,
        kills: Sequence[K] = (),
        hurts: Sequence[H] = (),
        blinds: Sequence[B] = (),
        nades: Sequence[N] = (),
        plant: tuple[float, str, int] | None = None,
        equip: Mapping[str, int] | int | None = None,
        money: Mapping[str, int] | int | None = None,
        positions: Mapping[str, Position | list[tuple[float, Position]]] | None = None,
        inventory: Mapping[str, list[str]] | None = None,
        warmup: bool = False,
        absent: Sequence[str] = (),
        freeze_end: bool = True,
        round_end_event: bool = True,
        count: bool = True,
        restored: bool = False,
    ) -> RoundScript:
        """Append one round. ``count=False`` leaves the rounds counter alone (warmup, aborted rounds).

        ``restored=True`` reproduces what a backup restore looks like in a real HLTV demo: on the tick
        the replayed round starts there is a winner-less ``round_end`` and every player is "killed"
        by the world (and respawns for the round).
        """
        names = TEAM_A + TEAM_B
        if restored:
            at_start = -self.freeze_s
            kills = [*kills, *(K(at_start, name, name, weapon="world") for name in names)]
            hurts = [*hurts, *(H(at_start, None, name, 100, 0, weapon="world") for name in names)]

        def per_player(value: Mapping[str, int] | int | None) -> dict[str, int]:
            if value is None:
                return {}
            if isinstance(value, int):
                return dict.fromkeys(names, value)
            return dict(value)

        rnd = RoundScript(
            winner=winner,
            reason=reason if reason is not None else _DEFAULT_REASON.get(str(winner)),
            duration=duration,
            kills=sorted(kills, key=lambda k: k.t),
            hurts=sorted(hurts, key=lambda h: h.t),
            blinds=list(blinds),
            nades=list(nades),
            plant=plant,
            equip=per_player(equip),
            money=per_player(money),
            positions=dict(positions or {}),
            inventory=dict(inventory or {}),
            warmup=warmup,
            trp=self.trp,
            ct_team=self.ct_team,
            absent=frozenset(absent),
            freeze_end=freeze_end,
            round_end_event=round_end_event,
            restored=restored,
        )
        rnd.start_tick = self.cursor
        rnd.freeze_tick = rnd.start_tick + round(self.freeze_s * self.tick_rate)
        rnd.end_tick = rnd.freeze_tick + round(duration * self.tick_rate)
        self.cursor = rnd.end_tick + round(self.post_round_s * self.tick_rate)
        if self.rounds:
            self.rounds[-1].next_start_tick = rnd.start_tick
        self.rounds.append(rnd)
        if count and not warmup and round_end_event and winner is not None:
            self.trp += 1
        return rnd

    def add_warmup(self, **kwargs: Any) -> RoundScript:
        return self.add_round("CT", "t_killed", warmup=True, **kwargs)

    def add_knife_round(self, winner: str = "CT") -> RoundScript:
        """A knife round: knife damage and kills only."""
        loser = TEAM_B if (winner == "CT") == (self.ct_team == "A") else TEAM_A
        winners = TEAM_A if loser is TEAM_B else TEAM_B
        kills = [K(10.0 + i, winners[i], loser[i], weapon="knife") for i in range(5)]
        hurts = [H(9.5 + i, winners[i], loser[i], 55, 45, weapon="knife_t") for i in range(5)]
        return self.add_round(winner, kills=kills, hurts=hurts, duration=30.0, equip=200, money=0)

    def add_rounds(self, winners: str, **kwargs: Any) -> list[RoundScript]:
        """One plain round per character: ``"C"`` = CT wins, ``"T"`` = T wins."""
        return [self.add_round("CT" if ch == "C" else "T", **kwargs) for ch in winners]

    def build(self, **options: Any) -> FakeParser:
        return FakeParser(
            header={
                "map_name": self.map_name,
                "server_name": "fake GOTV",
                "client_name": "SourceTV Demo",
                "demo_version_name": "valve_demo_2",
                "network_protocol": "14000",
            },
            rounds=list(self.rounds),
            tick_rate=self.tick_rate,
            teams=self.teams,
            **options,
        )


def factory_for(parser: FakeParser) -> Callable[[str], FakeParser]:
    """``parser_factory`` returning always the same fake."""

    def make(_path: str) -> FakeParser:
        return parser

    return make


def standard_match(
    map_name: str = "de_mirage", *, first_half: str = "CCCCCCCTTTTT", second_half: str = "TTTTTT"
) -> DemoBuilder:
    """A complete map in which team A starts as CT; sides swap after round 12.

    ``first_half`` / ``second_half`` give the winning *side* per round (``"C"`` / ``"T"``). With the
    defaults team A wins 7 rounds as CT and 6 as T: final score A 13 - B 5.
    """
    builder = DemoBuilder(map_name)
    builder.add_rounds(first_half)
    builder.swap_sides()
    builder.add_rounds(second_half)
    return builder
