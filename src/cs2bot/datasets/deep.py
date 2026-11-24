"""Loaders for the "deep" match factors (deep_data package).

Everything the extra feature blocks of :mod:`cs2bot.features.deep` need beyond the classic training
records, as small frozen dataclasses (fast to build in bulk, no validation overhead):

* :class:`MatchMetaRecord` - schedule, tournament, stage text and event location of **every** match
  with two known teams (also upcoming ones: a schedule, a stage name and a venue are public before
  the match; nothing in them is a result). Feature code only ever reads matches that started before
  its ``as_of``, plus the static facts of the match being predicted.
* :class:`TeamMetaRecord` - the team's country and its players' countries (home region / time zone).
* :class:`ClutchRecord` - one 1vX situation from a parsed demo (``player_round_stats``).
* :class:`DuelRecord` - one kill from a parsed demo with both players' teams and weapons, the
  opening / trade flags and the victim's map area.
* :class:`CoachChangeRecord` - the start of a coach period (``roster_periods.is_coach``).
* market history - Betclic match-winner price changes per match (:mod:`cs2bot.odds.movement`).

:class:`DeepData` bundles them; :meth:`DeepData.from_session` loads each part defensively: a part
that cannot be loaded (missing table on an old database, a broken row) is logged and left empty, the
features that depend on it are then NaN. Time filters follow the other loaders: ``since <= t < until``.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from cs2bot.datasets._common import apply_match_filters, map_time_expr, resolve_lan, resolve_tier
from cs2bot.datasets.loaders import _clean_map_name
from cs2bot.db.models import (
    Kill,
    MapPlayed,
    Match,
    Player,
    PlayerRoundStats,
    RosterPeriod,
    Team,
    Tournament,
)
from cs2bot.domain.enums import MatchFormat, MatchStatus, Side, Tier
from cs2bot.utils.time import ensure_utc

log = logging.getLogger(__name__)

_CLUTCH_RE: Final = re.compile(r"^\s*1\s*v\s*([1-5])\s*$", re.IGNORECASE)

__all__ = [
    "ClutchRecord",
    "CoachChangeRecord",
    "DeepData",
    "DuelRecord",
    "MarketPoint",
    "MatchMetaRecord",
    "TeamMetaRecord",
    "load_clutch_records",
    "load_coach_changes",
    "load_duel_records",
    "load_match_meta",
    "load_team_meta",
]


@dataclass(frozen=True, slots=True)
class MatchMetaRecord:
    """Static (pre-match) facts of one match, in ``matches.team1_id`` / ``team2_id`` orientation."""

    match_id: int
    scheduled_at: datetime | None
    team1_id: int
    team2_id: int
    status: MatchStatus = MatchStatus.SCHEDULED
    format: MatchFormat | None = None
    tier: Tier = Tier.UNRANKED
    is_lan: bool = False
    stage: str | None = None
    tournament_id: int | None = None
    tournament_name: str | None = None
    location: str | None = None


@dataclass(frozen=True, slots=True)
class TeamMetaRecord:
    team_id: int
    country: str | None = None
    player_countries: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClutchRecord:
    """A player alone against ``opponents`` enemies (1vX) in one round."""

    match_id: int
    map_number: int
    map_name: str
    played_at: datetime
    team_id: int
    player_id: int
    opponents: int
    won: bool


@dataclass(frozen=True, slots=True)
class DuelRecord:
    """One kill: ``attacker`` killed ``victim``. Teams / weapons are None when unknown.

    ``victim_weapon`` is the victim's best weapon of that round (``player_round_stats.primary_weapon``);
    an AWP duel is a kill where both the attacker's weapon and the victim's weapon are the AWP.
    """

    match_id: int
    map_number: int
    map_name: str
    played_at: datetime
    attacker_id: int
    victim_id: int
    attacker_team_id: int | None = None
    victim_team_id: int | None = None
    attacker_side: Side | None = None
    weapon: str | None = None
    victim_weapon: str | None = None
    is_opening: bool = False
    is_trade: bool = False
    was_traded: bool = False
    victim_place: str | None = None


@dataclass(frozen=True, slots=True)
class CoachChangeRecord:
    team_id: int
    started_on: date
    player_id: int | None = None


@dataclass(frozen=True, slots=True)
class MarketPoint:
    """One recorded match-winner price change (decimal odds, our orientation; None = not quoted)."""

    captured_at: datetime
    team1_odds: float | None
    team2_odds: float | None


@dataclass
class DeepData:
    """Inputs of :class:`cs2bot.features.deep.DeepFeatureIndex` (each part may be empty)."""

    match_meta: Sequence[MatchMetaRecord] = ()
    team_meta: Mapping[int, TeamMetaRecord] = field(default_factory=dict)
    clutches: Sequence[ClutchRecord] = ()
    duels: Sequence[DuelRecord] = ()
    coach_changes: Sequence[CoachChangeRecord] = ()
    #: match id -> chronological match-winner price changes (team1 / team2 in our orientation)
    market: Mapping[int, Sequence[MarketPoint]] = field(default_factory=dict)
    #: optional sharp-bookmaker benchmark: match id -> [(captured_at, de-vigged P(team1 wins))]
    sharp: Mapping[int, Sequence[tuple[datetime, float]]] = field(default_factory=dict)

    @classmethod
    def from_session(
        cls,
        session: Session,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        in_scope_only: bool = True,
        with_demo: bool = True,
        with_market: bool = True,
        sharp: Any = None,
    ) -> DeepData:
        """Load every part (a failing part is logged and left empty, never raised).

        ``until`` bounds the results-like parts (clutches, duels, coach changes, market history); the
        match metadata is static pre-match information and always loaded in full. ``sharp`` is an
        optional :class:`cs2bot.odds.sharp.SharpOddsSource`; its quotes (de-vigged team1 probability)
        are attached per match when it is enabled.
        """

        def safe[T](what: str, loader: Callable[[], T], empty: T) -> T:
            try:
                with session.begin_nested():
                    return loader()
            except Exception:  # defensive: deep factors are optional, predictions must not stop
                log.warning("deep data: could not load %s; those features will be NaN", what, exc_info=True)
                return empty

        market: Mapping[int, Sequence[MarketPoint]] = {}
        if with_market:
            from cs2bot.odds.movement import load_match_winner_history

            market = safe("market history", lambda: load_match_winner_history(session, until=until), {})
        no_meta: list[MatchMetaRecord] = []
        meta = safe("match metadata", lambda: load_match_meta(session), no_meta)
        sharp_quotes: dict[int, list[tuple[datetime, float]]] = {}
        if sharp is not None and bool(getattr(sharp, "enabled", False)):
            for m in meta:
                try:
                    qs = [(q.captured_at, q.p_team1) for q in sharp.quotes(m.match_id, until=until)]
                except Exception:  # an optional feed must never stop the store build
                    log.warning("sharp odds lookup failed for match %s", m.match_id, exc_info=True)
                    continue
                if qs:
                    sharp_quotes[m.match_id] = qs
        return cls(
            match_meta=meta,
            team_meta=safe("team metadata", lambda: load_team_meta(session), {}),
            clutches=safe(
                "clutches",
                lambda: load_clutch_records(session, since=since, until=until, in_scope_only=in_scope_only),
                [],
            )
            if with_demo
            else [],
            duels=safe(
                "duels",
                lambda: load_duel_records(session, since=since, until=until, in_scope_only=in_scope_only),
                [],
            )
            if with_demo
            else [],
            coach_changes=safe("coach changes", lambda: load_coach_changes(session, until=until), []),
            market=market,
            sharp=sharp_quotes,
        )

    def cut(self, until: datetime) -> DeepData:
        """A copy without anything observed at or after ``until`` (tests, walk-forward evaluation)."""
        t = ensure_utc(until)
        return DeepData(
            match_meta=self.match_meta,
            team_meta=self.team_meta,
            clutches=[c for c in self.clutches if ensure_utc(c.played_at) < t],
            duels=[d for d in self.duels if ensure_utc(d.played_at) < t],
            coach_changes=[c for c in self.coach_changes if c.started_on < t.date()],
            market={
                mid: [p for p in pts if ensure_utc(p.captured_at) < t] for mid, pts in self.market.items()
            },
            sharp={mid: [p for p in pts if ensure_utc(p[0]) < t] for mid, pts in self.sharp.items()},
        )


# --------------------------------------------------------------------------------------- loaders


def load_match_meta(session: Session) -> list[MatchMetaRecord]:
    """Every match with two different known teams that is not cancelled (any time, any scope)."""
    rows = session.execute(
        select(
            Match.id,
            Match.scheduled_at,
            Match.team1_id,
            Match.team2_id,
            Match.status,
            Match.format,
            Match.tier,
            Match.is_lan,
            Match.stage,
            Match.tournament_id,
            Tournament.name,
            Tournament.location,
            Tournament.tier,
            Tournament.is_lan,
        )
        .outerjoin(Tournament, Match.tournament_id == Tournament.id)
        .where(
            Match.team1_id.is_not(None),
            Match.team2_id.is_not(None),
            Match.team1_id != Match.team2_id,
            Match.status != MatchStatus.CANCELED,
        )
        .order_by(Match.scheduled_at, Match.id)
    ).all()
    out: list[MatchMetaRecord] = []
    for r in rows:
        try:
            out.append(
                MatchMetaRecord(
                    match_id=int(r[0]),
                    scheduled_at=ensure_utc(r[1]) if r[1] is not None else None,
                    team1_id=int(cast(int, r[2])),
                    team2_id=int(cast(int, r[3])),
                    status=r[4] or MatchStatus.SCHEDULED,
                    format=r[5],
                    tier=resolve_tier(r[6], r[12]),
                    is_lan=resolve_lan(r[7], r[13]),
                    stage=r[8],
                    tournament_id=r[9],
                    tournament_name=r[10],
                    location=r[11],
                )
            )
        except (TypeError, ValueError) as exc:  # one broken row never aborts the batch
            log.debug("skipping match meta row %r: %s", r[0], exc)
    return out


def load_team_meta(session: Session) -> dict[int, TeamMetaRecord]:
    """Team country plus the countries of the players currently attached to the team.

    ``Team.extra["hltv"]["country"]`` / ``["location"]`` are used when the column is empty (the HLTV
    team page lists a country flag).
    """
    players: dict[int, list[str]] = defaultdict(list)
    for team_id, country in session.execute(
        select(Player.current_team_id, Player.country).where(
            Player.current_team_id.is_not(None), Player.country.is_not(None)
        )
    ):
        if country and str(country).strip():
            players[int(cast(int, team_id))].append(str(country).strip())
    out: dict[int, TeamMetaRecord] = {}
    for team_id, country, extra in session.execute(select(Team.id, Team.country, Team.extra)):
        value = country
        if not value and isinstance(extra, dict):
            for section in (extra.get("hltv"), extra.get("pandascore"), extra):
                if isinstance(section, dict):
                    cand = section.get("country") or section.get("location")
                    if isinstance(cand, str) and cand.strip():
                        value = cand
                        break
        out[int(team_id)] = TeamMetaRecord(
            team_id=int(team_id),
            country=str(value).strip() if value else None,
            player_countries=tuple(players.get(int(team_id), ())),
        )
    return out


def _map_join_rows(
    session: Session,
    columns: Sequence[Any],
    table: Any,
    *,
    since: datetime | None,
    until: datetime | None,
    in_scope_only: bool,
    extra_where: Sequence[Any] = (),
    joins: Sequence[tuple[Any, Any]] = (),
) -> list[Any]:
    t = map_time_expr()
    stmt = (
        select(Match.id, MapPlayed.map_number, MapPlayed.map_name, t, *columns)
        .select_from(table)
        .join(MapPlayed, table.map_played_id == MapPlayed.id)
        .join(Match, MapPlayed.match_id == Match.id)
    )
    for target, on in joins:
        stmt = stmt.outerjoin(target, on)
    stmt = apply_match_filters(stmt, time_expr=t, since=since, until=until, in_scope_only=in_scope_only)
    for cond in extra_where:
        stmt = stmt.where(cond)
    return list(session.execute(stmt))


def load_clutch_records(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    in_scope_only: bool = True,
) -> list[ClutchRecord]:
    """1vX situations (``player_round_stats.clutch_situation`` like ``"1v2"``) with their outcome."""
    rows = _map_join_rows(
        session,
        [
            PlayerRoundStats.team_id,
            PlayerRoundStats.player_id,
            PlayerRoundStats.clutch_situation,
            PlayerRoundStats.clutch_won,
        ],
        PlayerRoundStats,
        since=since,
        until=until,
        in_scope_only=in_scope_only,
        extra_where=[PlayerRoundStats.clutch_situation.is_not(None), PlayerRoundStats.team_id.is_not(None)],
    )
    out: list[ClutchRecord] = []
    for mid, num, name, t, team_id, player_id, situation, won in rows:
        m = _CLUTCH_RE.match(str(situation or ""))
        map_name = _clean_map_name(name)
        if m is None or map_name is None or t is None or won is None:
            continue
        out.append(
            ClutchRecord(
                match_id=int(mid),
                map_number=int(num),
                map_name=map_name,
                played_at=ensure_utc(t),
                team_id=int(team_id),
                player_id=int(player_id),
                opponents=int(m.group(1)),
                won=bool(won),
            )
        )
    out.sort(key=lambda r: (r.played_at, r.match_id, r.map_number))
    return out


def load_duel_records(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    in_scope_only: bool = True,
) -> list[DuelRecord]:
    """Kills with both players known, enriched with their teams and the victim's weapon of the round.

    Team kills (same team on both sides) are dropped. Team ids come from ``player_round_stats``; a kill
    whose round has no per-player rows keeps ``None`` teams (feature code then ignores it for
    team-level aggregates but still uses it for player-vs-player records).
    """
    att = aliased(PlayerRoundStats)
    vic = aliased(PlayerRoundStats)
    rows = _map_join_rows(
        session,
        [
            Kill.attacker_player_id,
            Kill.victim_player_id,
            att.team_id,
            vic.team_id,
            Kill.attacker_side,
            Kill.weapon,
            vic.primary_weapon,
            Kill.is_opening,
            Kill.is_trade,
            Kill.was_traded,
            Kill.victim_place,
        ],
        Kill,
        since=since,
        until=until,
        in_scope_only=in_scope_only,
        extra_where=[Kill.attacker_player_id.is_not(None), Kill.victim_player_id.is_not(None)],
        joins=[
            (
                att,
                (att.map_played_id == Kill.map_played_id)
                & (att.round_number == Kill.round_number)
                & (att.player_id == Kill.attacker_player_id),
            ),
            (
                vic,
                (vic.map_played_id == Kill.map_played_id)
                & (vic.round_number == Kill.round_number)
                & (vic.player_id == Kill.victim_player_id),
            ),
        ],
    )
    out: list[DuelRecord] = []
    for r in rows:
        mid, num, name, t, a, v, a_team, v_team, side, weapon, v_weapon, opening, trade, traded, place = r
        map_name = _clean_map_name(name)
        if map_name is None or t is None or a == v:
            continue
        if a_team is not None and v_team is not None and a_team == v_team:
            continue  # team kill
        out.append(
            DuelRecord(
                match_id=int(mid),
                map_number=int(num),
                map_name=map_name,
                played_at=ensure_utc(t),
                attacker_id=int(a),
                victim_id=int(v),
                attacker_team_id=int(a_team) if a_team is not None else None,
                victim_team_id=int(v_team) if v_team is not None else None,
                attacker_side=side,
                weapon=str(weapon).lower() if weapon else None,
                victim_weapon=str(v_weapon).lower() if v_weapon else None,
                is_opening=bool(opening),
                is_trade=bool(trade),
                was_traded=bool(traded),
                victim_place=str(place) if place else None,
            )
        )
    out.sort(key=lambda r: (r.played_at, r.match_id, r.map_number))
    return out


def load_coach_changes(session: Session, *, until: datetime | None = None) -> list[CoachChangeRecord]:
    """Start days of coach periods (``roster_periods`` with ``is_coach``), oldest first."""
    stmt = select(RosterPeriod.team_id, RosterPeriod.started_on, RosterPeriod.player_id).where(
        RosterPeriod.is_coach.is_(True)
    )
    if until is not None:
        stmt = stmt.where(RosterPeriod.started_on < ensure_utc(until).date())
    out = [
        CoachChangeRecord(team_id=int(team), started_on=day, player_id=pid)
        for team, day, pid in session.execute(stmt)
        if day is not None
    ]
    out.sort(key=lambda r: (r.team_id, r.started_on))
    return out
