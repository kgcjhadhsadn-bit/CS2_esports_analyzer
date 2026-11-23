"""Database rows -> the plain training records of :mod:`cs2bot.domain.records`.

Conventions shared by every loader
----------------------------------
* **Time filter:** ``since <= t < until`` (``since`` inclusive, ``until`` exclusive) where ``t`` is the
  map's ``played_at`` falling back to the match's ``scheduled_at``. Naive datetimes are read as UTC.
  ``until=as_of`` therefore returns exactly "what was known strictly before ``as_of``".
* **Scope:** ``in_scope_only=True`` keeps matches with ``matches.in_scope`` (Tier 1/2, PLAN.md 4.1).
* **Orientation:** team1/team2 always mean ``matches.team1_id`` / ``team2_id``.
* **Order:** chronological (time, match id, map number[, round number / player id]) and stable.
* **Defensive:** cancelled matches, matches without both teams, maps without a decided score and
  rows that fail validation are skipped (and counted in a debug log line) - one malformed row never
  aborts a load.
* Only portable SQL (plain joins, ``COALESCE``, ``IN`` lists in chunks): runs on SQLite and PostgreSQL.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from datetime import datetime
from typing import Any, Final

import pandas as pd
from pydantic import BaseModel, ValidationError
from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import Session

from cs2bot.datasets._common import (
    apply_match_filters,
    fetch_in_chunks,
    map_time_expr,
    resolve_lan,
    resolve_tier,
    source_rank,
)
from cs2bot.db.models import (
    Kill,
    MapPlayed,
    Match,
    MatchLineup,
    Player,
    PlayerMapStats,
    Round,
    Tournament,
    Veto,
)
from cs2bot.domain.enums import DataSource, MatchFormat, MatchStatus, Side, TeamSlot, VetoAction
from cs2bot.domain.records import MapResultRecord, PlayerMapRecord, RoundRecord, VetoRecord, VetoStep
from cs2bot.utils.names import normalize_map_name
from cs2bot.utils.time import ensure_utc

log = logging.getLogger(__name__)

#: CS2 is MR12: a finished map has a winner with at least 13 rounds. Lower "final" scores are live
#: snapshots, abandoned maps or data errors and are useless as training outcomes.
MIN_WINNER_ROUNDS: Final[int] = 13
#: Regulation length (MR12): rounds beyond this are overtime.
REGULATION_ROUNDS: Final[int] = 24
#: The CS2 Active Duty pool always has seven maps; a veto listing seven distinct maps defines it.
ACTIVE_DUTY_POOL_SIZE: Final[int] = 7
#: ``kills.weapon`` spellings that mean the AWP (demoparser2 reports "awp"; some tools "weapon_awp").
AWP_WEAPON_NAMES: Final[frozenset[str]] = frozenset({"awp", "weapon_awp"})

_PLACEHOLDER_MAPS: Final[frozenset[str]] = frozenset({"", "tba", "tbd", "default", "unknown"})


def _map_is_finished(map_status: MatchStatus | None, match_status: MatchStatus | None) -> bool:
    """A map counts as finished when its own status says so, or when the whole match is finished.

    Ingestion sources set the map status differently (HLTV: FINISHED/LIVE, PandaScore: per game), so
    a finished match with a stale map status still qualifies - the score check does the rest.
    """
    if map_status is MatchStatus.CANCELED:
        return False
    return map_status is MatchStatus.FINISHED or match_status is MatchStatus.FINISHED


def _valid_score(team1_rounds: int | None, team2_rounds: int | None) -> bool:
    if team1_rounds is None or team2_rounds is None:
        return False
    if team1_rounds < 0 or team2_rounds < 0 or team1_rounds == team2_rounds:
        return False
    return max(team1_rounds, team2_rounds) >= MIN_WINNER_ROUNDS


def _clean_map_name(raw: str | None) -> str | None:
    if not raw:
        return None
    name = normalize_map_name(raw)
    return None if name in _PLACEHOLDER_MAPS else name


# ------------------------------------------------------------------------------------ map results


def _lineups_by_match(session: Session, match_ids: Iterable[int]) -> dict[int, dict[int, list[int]]]:
    """match id -> team id -> sorted player ids, from ``match_lineups``."""
    out: dict[int, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))

    def stmt(chunk: Sequence[int]) -> Select[Any]:
        return select(MatchLineup.match_id, MatchLineup.team_id, MatchLineup.player_id).where(
            MatchLineup.match_id.in_(chunk)
        )

    for match_id, team_id, player_id in fetch_in_chunks(session, stmt, match_ids):
        out[match_id][team_id].append(player_id)
    return out


def _players_by_map(session: Session, map_ids: Iterable[int]) -> dict[int, dict[int, set[int]]]:
    """map_played id -> team id -> player ids that have a stat line on that map."""
    out: dict[int, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))

    def stmt(chunk: Sequence[int]) -> Select[Any]:
        return select(PlayerMapStats.map_played_id, PlayerMapStats.team_id, PlayerMapStats.player_id).where(
            PlayerMapStats.map_played_id.in_(chunk)
        )

    for map_id, team_id, player_id in fetch_in_chunks(session, stmt, map_ids):
        out[map_id][team_id].add(player_id)
    return out


def load_map_results(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    in_scope_only: bool = True,
) -> list[MapResultRecord]:
    """Finished maps with a decided score, chronological.

    * ``picked_by``: ``maps_played.picked_by_team_id`` mapped to TEAM1/TEAM2; ``None`` for the decider
      and when the pick is unknown (the record type cannot tell these apart).
    * Lineups: ``match_lineups`` of the match (sorted player ids). When a match has no lineup rows for
      a team, the players with a stat line on that map are used instead; otherwise the tuple is empty.
    * ``is_lan`` / ``tier``: the match's value, falling back to the tournament's.
    * ``overtime_rounds``: the stored value, or ``total rounds - 24`` when it was left at 0.
    """
    ts = map_time_expr()
    stmt = (
        select(
            MapPlayed.id,
            MapPlayed.match_id,
            MapPlayed.map_number,
            MapPlayed.map_name,
            MapPlayed.status,
            MapPlayed.team1_rounds,
            MapPlayed.team2_rounds,
            MapPlayed.team1_start_side,
            MapPlayed.team1_ct_rounds,
            MapPlayed.team1_t_rounds,
            MapPlayed.team2_ct_rounds,
            MapPlayed.team2_t_rounds,
            MapPlayed.overtime_rounds,
            MapPlayed.picked_by_team_id,
            ts.label("played_at"),
            Match.team1_id,
            Match.team2_id,
            Match.status.label("match_status"),
            Match.format,
            Match.is_lan,
            Match.tier,
            Tournament.tier.label("tournament_tier"),
            Tournament.is_lan.label("tournament_is_lan"),
        )
        .join(Match, Match.id == MapPlayed.match_id)
        .outerjoin(Tournament, Tournament.id == Match.tournament_id)
        .where(MapPlayed.team1_rounds.is_not(None), MapPlayed.team2_rounds.is_not(None))
    )
    stmt = apply_match_filters(stmt, time_expr=ts, since=since, until=until, in_scope_only=in_scope_only)
    rows = [
        r
        for r in session.execute(stmt)
        if _map_is_finished(r.status, r.match_status) and _valid_score(r.team1_rounds, r.team2_rounds)
    ]
    lineups = _lineups_by_match(session, {r.match_id for r in rows})
    missing = [
        r.id
        for r in rows
        if not lineups.get(r.match_id, {}).get(r.team1_id) or not lineups.get(r.match_id, {}).get(r.team2_id)
    ]
    stat_players = _players_by_map(session, missing) if missing else {}

    def lineup(r: Any, team_id: int) -> tuple[int, ...]:
        ids = lineups.get(r.match_id, {}).get(team_id)
        if not ids:
            ids = list(stat_players.get(r.id, {}).get(team_id, ()))
        return tuple(sorted(set(ids)))

    out: list[MapResultRecord] = []
    skipped = 0
    for r in rows:
        map_name = _clean_map_name(r.map_name)
        if map_name is None:
            skipped += 1
            continue
        total = r.team1_rounds + r.team2_rounds
        overtime = r.overtime_rounds or max(0, total - REGULATION_ROUNDS)
        picked: TeamSlot | None = None
        if r.picked_by_team_id is not None:
            if r.picked_by_team_id == r.team1_id:
                picked = TeamSlot.TEAM1
            elif r.picked_by_team_id == r.team2_id:
                picked = TeamSlot.TEAM2
        try:
            out.append(
                MapResultRecord(
                    match_id=r.match_id,
                    map_number=r.map_number,
                    map_name=map_name,
                    played_at=ensure_utc(r.played_at),
                    team1_id=r.team1_id,
                    team2_id=r.team2_id,
                    team1_rounds=r.team1_rounds,
                    team2_rounds=r.team2_rounds,
                    team1_start_side=r.team1_start_side,
                    team1_ct_rounds=r.team1_ct_rounds,
                    team1_t_rounds=r.team1_t_rounds,
                    team2_ct_rounds=r.team2_ct_rounds,
                    team2_t_rounds=r.team2_t_rounds,
                    overtime_rounds=overtime,
                    picked_by=picked,
                    is_lan=resolve_lan(r.is_lan, r.tournament_is_lan),
                    tier=resolve_tier(r.tier, r.tournament_tier),
                    format=r.format,
                    team1_lineup=lineup(r, r.team1_id),
                    team2_lineup=lineup(r, r.team2_id),
                )
            )
        except ValidationError:
            skipped += 1
    if skipped:
        log.debug("load_map_results: skipped %d malformed map rows", skipped)
    out.sort(key=lambda m: (m.played_at, m.match_id, m.map_number))
    return out


# ------------------------------------------------------------------------------------------ vetoes


def _infer_format(n_picks: int, n_steps: int) -> MatchFormat | None:
    """Series format from a veto's shape (used only when ``matches.format`` is missing)."""
    if n_steps < 3:
        return None
    return {0: MatchFormat.BO1, 2: MatchFormat.BO3, 4: MatchFormat.BO5}.get(n_picks)


def load_veto_records(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    in_scope_only: bool = True,
    finished_only: bool = True,
) -> list[VetoRecord]:
    """One record per match that has veto rows, chronological by ``matches.scheduled_at``.

    * Steps are in team1/team2 orientation; a step whose ``team_id`` is NULL keeps ``team=None``
      (leftover / decider). A veto that names a team outside the match is dropped as corrupt.
    * ``map_pool`` is filled when the veto lists exactly seven distinct maps (every Active Duty map
      appears once in a complete veto); otherwise ``None`` and the consumer uses its default pool.
    * ``finished_only=True`` keeps finished matches only (training data). Pass ``False`` to also get
      published vetoes of upcoming / live matches.
    """
    stmt = (
        select(
            Veto.match_id,
            Veto.step,
            Veto.team_id,
            Veto.action,
            Veto.map_name,
            Match.scheduled_at,
            Match.format,
            Match.team1_id,
            Match.team2_id,
        )
        .join(Match, Match.id == Veto.match_id)
        .order_by(Match.scheduled_at, Veto.match_id, Veto.step)
    )
    stmt = apply_match_filters(
        stmt, time_expr=Match.scheduled_at, since=since, until=until, in_scope_only=in_scope_only
    )
    if finished_only:
        stmt = stmt.where(Match.status == MatchStatus.FINISHED)

    grouped: dict[int, list[Any]] = defaultdict(list)
    for row in session.execute(stmt):
        grouped[row.match_id].append(row)

    out: list[VetoRecord] = []
    skipped = 0
    for match_id, rows in grouped.items():
        head = rows[0]
        steps: list[VetoStep] = []
        ok = True
        seen_steps: set[int] = set()
        for r in sorted(rows, key=lambda x: x.step):
            map_name = _clean_map_name(r.map_name)
            if map_name is None or r.step in seen_steps:
                ok = False
                break
            seen_steps.add(r.step)
            team: TeamSlot | None
            if r.team_id is None:
                team = None
            elif r.team_id == head.team1_id:
                team = TeamSlot.TEAM1
            elif r.team_id == head.team2_id:
                team = TeamSlot.TEAM2
            else:
                ok = False
                break
            if team is None and r.action is not VetoAction.LEFTOVER:
                ok = False  # a ban / pick without a team cannot be attributed
                break
            steps.append(VetoStep(step=r.step, action=r.action, team=team, map_name=map_name))
        if not ok or not steps:
            skipped += 1
            continue
        fmt = head.format or _infer_format(
            sum(1 for s in steps if s.action is VetoAction.PICK), len(steps)
        )
        if fmt is None:
            skipped += 1
            continue
        maps = [s.map_name for s in steps]
        pool = (
            tuple(sorted(maps))
            if len(set(maps)) == len(maps) and len(maps) == ACTIVE_DUTY_POOL_SIZE
            else None
        )
        try:
            out.append(
                VetoRecord(
                    match_id=match_id,
                    played_at=ensure_utc(head.scheduled_at),
                    format=fmt,
                    team1_id=head.team1_id,
                    team2_id=head.team2_id,
                    steps=tuple(steps),
                    map_pool=pool,
                )
            )
        except ValidationError:
            skipped += 1
    if skipped:
        log.debug("load_veto_records: skipped %d malformed vetoes", skipped)
    out.sort(key=lambda v: (v.played_at, v.match_id))
    return out


# ------------------------------------------------------------------------------------------ rounds


def load_round_records(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    in_scope_only: bool = True,
    sources: Collection[DataSource] | None = None,
) -> list[RoundRecord]:
    """Rounds joined to their map and match, converted to team1 orientation, chronological.

    * ``winner`` is derived from ``winner_side`` vs ``team1_side`` (both NOT NULL in the schema).
    * Equipment value, money, buy type and survivors are stored per side (CT/T) and are assigned to
      team1/team2 by ``team1_side``. HLTV round-history rows (``rounds.source = HLTV``) only know the
      winner, side and end reason: their economy fields stay ``None``. DEMO and SYNTHETIC rows are
      complete. Use ``sources=[DataSource.DEMO]`` to restrict to one origin.
    * ``team1_score_before`` / ``team2_score_before`` are reconstructed cumulatively inside each map,
      in round order. They are only filled while the stored rounds are contiguous from round 1; after
      a gap the score cannot be known and both stay ``None`` for the rest of that map.
    """
    ts = map_time_expr()
    stmt = (
        select(
            Round.map_played_id,
            Round.round_number,
            Round.team1_side,
            Round.winner_side,
            Round.end_reason,
            Round.ct_equipment_value,
            Round.t_equipment_value,
            Round.ct_money_start,
            Round.t_money_start,
            Round.ct_buy_type,
            Round.t_buy_type,
            Round.bomb_planted,
            Round.ct_survivors,
            Round.t_survivors,
            MapPlayed.match_id,
            MapPlayed.map_number,
            MapPlayed.map_name,
            ts.label("played_at"),
            Match.team1_id,
            Match.team2_id,
            Match.is_lan,
            Tournament.is_lan.label("tournament_is_lan"),
        )
        .join(MapPlayed, MapPlayed.id == Round.map_played_id)
        .join(Match, Match.id == MapPlayed.match_id)
        .outerjoin(Tournament, Tournament.id == Match.tournament_id)
        .order_by(ts, MapPlayed.match_id, MapPlayed.map_number, Round.round_number)
    )
    stmt = apply_match_filters(stmt, time_expr=ts, since=since, until=until, in_scope_only=in_scope_only)
    if sources is not None:
        stmt = stmt.where(Round.source.in_(list(sources)))

    out: list[RoundRecord] = []
    skipped = 0
    current_map: int | None = None
    score1 = score2 = 0
    expected = 1
    contiguous = True
    map_name: str | None = None
    for r in session.execute(stmt):
        if r.map_played_id != current_map:
            current_map = r.map_played_id
            score1 = score2 = 0
            expected = 1
            contiguous = True
            map_name = _clean_map_name(r.map_name)
        if map_name is None:
            skipped += 1
            continue
        if r.round_number != expected:
            contiguous = False
        team1_ct = r.team1_side is Side.CT
        winner = TeamSlot.TEAM1 if r.winner_side is r.team1_side else TeamSlot.TEAM2
        try:
            out.append(
                RoundRecord(
                    match_id=r.match_id,
                    map_number=r.map_number,
                    map_name=map_name,
                    played_at=ensure_utc(r.played_at),
                    round_number=r.round_number,
                    team1_id=r.team1_id,
                    team2_id=r.team2_id,
                    team1_side=r.team1_side,
                    winner=winner,
                    end_reason=r.end_reason,
                    team1_equipment_value=r.ct_equipment_value if team1_ct else r.t_equipment_value,
                    team2_equipment_value=r.t_equipment_value if team1_ct else r.ct_equipment_value,
                    team1_money_start=r.ct_money_start if team1_ct else r.t_money_start,
                    team2_money_start=r.t_money_start if team1_ct else r.ct_money_start,
                    team1_buy=r.ct_buy_type if team1_ct else r.t_buy_type,
                    team2_buy=r.t_buy_type if team1_ct else r.ct_buy_type,
                    bomb_planted=r.bomb_planted,
                    team1_survivors=r.ct_survivors if team1_ct else r.t_survivors,
                    team2_survivors=r.t_survivors if team1_ct else r.ct_survivors,
                    team1_score_before=score1 if contiguous else None,
                    team2_score_before=score2 if contiguous else None,
                    is_lan=resolve_lan(r.is_lan, r.tournament_is_lan),
                )
            )
        except ValidationError:
            skipped += 1
            contiguous = False  # the missing round breaks the running score
            continue
        expected = r.round_number + 1
        if winner is TeamSlot.TEAM1:
            score1 += 1
        else:
            score2 += 1
    if skipped:
        log.debug("load_round_records: skipped %d malformed round rows", skipped)
    return out


# ---------------------------------------------------------------------------------- player records

_FILL_FIELDS: Final[tuple[str, ...]] = (
    "headshots",
    "rounds_played",
    "adr",
    "kast",
    "rating",
    "first_kills",
    "first_deaths",
    "awp_kills",
)


def _awp_kills_by_map(session: Session, map_ids: Iterable[int]) -> dict[int, dict[int, int]]:
    """map_played id -> player id -> AWP kills, for maps whose kill rows carry weapon names.

    A map appears in the result only when at least one of its kills has a weapon, so a player that
    is missing from a present map really had zero AWP kills. Team kills are not counted.
    """
    out: dict[int, dict[int, int]] = {}
    awp = case((func.lower(Kill.weapon).in_(sorted(AWP_WEAPON_NAMES)), 1), else_=0)

    def stmt(chunk: Sequence[int]) -> Select[Any]:
        return (
            select(Kill.map_played_id, Kill.attacker_player_id, func.sum(awp))
            .where(
                Kill.map_played_id.in_(chunk),
                Kill.weapon.is_not(None),
                or_(
                    Kill.attacker_side.is_(None),
                    Kill.victim_side.is_(None),
                    Kill.attacker_side != Kill.victim_side,
                ),
            )
            .group_by(Kill.map_played_id, Kill.attacker_player_id)
        )

    for map_id, player_id, n in fetch_in_chunks(session, stmt, map_ids):
        per_map = out.setdefault(map_id, {})
        if player_id is not None:
            per_map[player_id] = int(n or 0)
    return out


def _standins(session: Session, match_ids: Iterable[int]) -> set[tuple[int, int]]:
    """(match id, player id) pairs flagged as stand-ins in ``match_lineups``."""

    def stmt(chunk: Sequence[int]) -> Select[Any]:
        return select(MatchLineup.match_id, MatchLineup.player_id).where(
            MatchLineup.match_id.in_(chunk), MatchLineup.is_standin.is_(True)
        )

    return {(m, p) for m, p in fetch_in_chunks(session, stmt, match_ids)}


def load_player_map_records(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    in_scope_only: bool = True,
) -> list[PlayerMapRecord]:
    """One stat line per (map, player), chronological.

    * **Source preference** when a player has several rows for the same map: DEMO > HLTV >
      PANDASCORE > SYNTHETIC. Kills / deaths / assists and the team come from the preferred row;
      optional fields that the preferred row lacks (for example the HLTV ``rating`` next to a DEMO
      row) are filled from the next source that has them.
    * ``rounds_played`` falls back to the map's total rounds; ``team_rounds_won`` /
      ``opponent_rounds_won`` come from the map score in the player's team orientation.
    * ``awp_kills``: the stat-line value, else counted from the ``kills`` table when that map's kills
      carry weapon names (demo data), else ``None``.
    * ``role`` is ``players.primary_role``; ``is_standin`` comes from ``match_lineups``.
    * Rows whose team is neither side of the match, or without any round count, are skipped.
    """
    ts = map_time_expr()
    stmt = (
        select(
            PlayerMapStats.map_played_id,
            PlayerMapStats.player_id,
            PlayerMapStats.team_id,
            PlayerMapStats.source,
            PlayerMapStats.kills,
            PlayerMapStats.deaths,
            PlayerMapStats.assists,
            PlayerMapStats.headshots,
            PlayerMapStats.rounds_played,
            PlayerMapStats.adr,
            PlayerMapStats.kast,
            PlayerMapStats.rating,
            PlayerMapStats.first_kills,
            PlayerMapStats.first_deaths,
            PlayerMapStats.awp_kills,
            MapPlayed.match_id,
            MapPlayed.map_number,
            MapPlayed.map_name,
            MapPlayed.team1_rounds,
            MapPlayed.team2_rounds,
            ts.label("played_at"),
            Match.team1_id,
            Match.team2_id,
            Match.is_lan,
            Match.tier,
            Tournament.tier.label("tournament_tier"),
            Tournament.is_lan.label("tournament_is_lan"),
            Player.primary_role,
        )
        .join(MapPlayed, MapPlayed.id == PlayerMapStats.map_played_id)
        .join(Match, Match.id == MapPlayed.match_id)
        .join(Player, Player.id == PlayerMapStats.player_id)
        .outerjoin(Tournament, Tournament.id == Match.tournament_id)
    )
    stmt = apply_match_filters(stmt, time_expr=ts, since=since, until=until, in_scope_only=in_scope_only)

    grouped: dict[tuple[int, int], list[Any]] = defaultdict(list)
    for row in session.execute(stmt):
        grouped[(row.map_played_id, row.player_id)].append(row)

    merged: list[tuple[Any, dict[str, Any]]] = []
    need_awp: set[int] = set()
    for rows in grouped.values():
        rows.sort(key=lambda x: source_rank(x.source))
        best = rows[0]
        fields: dict[str, Any] = {}
        for name in _FILL_FIELDS:
            fields[name] = next((getattr(x, name) for x in rows if getattr(x, name) is not None), None)
        if fields["awp_kills"] is None:
            need_awp.add(best.map_played_id)
        merged.append((best, fields))

    awp_by_map = _awp_kills_by_map(session, need_awp) if need_awp else {}
    standins = _standins(session, {best.match_id for best, _ in merged})

    out: list[PlayerMapRecord] = []
    skipped = 0
    for best, fields in merged:
        map_name = _clean_map_name(best.map_name)
        if map_name is None or best.team_id not in (best.team1_id, best.team2_id):
            skipped += 1
            continue
        is_team1 = best.team_id == best.team1_id
        rounds_played = fields["rounds_played"]
        if rounds_played is None and best.team1_rounds is not None and best.team2_rounds is not None:
            rounds_played = best.team1_rounds + best.team2_rounds
        if not rounds_played or rounds_played <= 0:
            skipped += 1
            continue
        awp_kills = fields["awp_kills"]
        if awp_kills is None and best.map_played_id in awp_by_map:
            awp_kills = awp_by_map[best.map_played_id].get(best.player_id, 0)
        own, opp = (
            (best.team1_rounds, best.team2_rounds) if is_team1 else (best.team2_rounds, best.team1_rounds)
        )
        try:
            out.append(
                PlayerMapRecord(
                    match_id=best.match_id,
                    map_number=best.map_number,
                    map_name=map_name,
                    played_at=ensure_utc(best.played_at),
                    player_id=best.player_id,
                    team_id=best.team_id,
                    opponent_team_id=best.team2_id if is_team1 else best.team1_id,
                    kills=best.kills,
                    deaths=best.deaths,
                    assists=best.assists or 0,
                    rounds_played=rounds_played,
                    team_rounds_won=own,
                    opponent_rounds_won=opp,
                    headshots=fields["headshots"],
                    adr=fields["adr"],
                    kast=fields["kast"],
                    rating=fields["rating"],
                    first_kills=fields["first_kills"],
                    first_deaths=fields["first_deaths"],
                    awp_kills=awp_kills,
                    role=best.primary_role,
                    is_lan=resolve_lan(best.is_lan, best.tournament_is_lan),
                    tier=resolve_tier(best.tier, best.tournament_tier),
                    is_standin=(best.match_id, best.player_id) in standins,
                )
            )
        except ValidationError:
            skipped += 1
    if skipped:
        log.debug("load_player_map_records: skipped %d malformed stat lines", skipped)
    out.sort(key=lambda p: (p.played_at, p.match_id, p.map_number, p.team_id, p.player_id))
    return out


# ------------------------------------------------------------------------------------- DataFrames


def to_frame(records: Iterable[BaseModel], *, columns: Sequence[str] | None = None) -> pd.DataFrame:
    """Records -> ``pandas.DataFrame`` with one column per record field (same names).

    Enum values are stored as their plain strings (``"ct"``, ``"team1"``, ``"s"``), datetimes stay
    timezone-aware UTC, tuples (lineups, veto steps) stay Python objects. For an empty input pass
    ``columns`` (for example :data:`cs2bot.domain.records.MAP_RESULT_COLUMNS`) to get typed-by-name
    empty columns instead of a column-less frame.
    """
    rows = list(records)
    if not rows:
        return pd.DataFrame(columns=list(columns or ()))
    cols = list(columns) if columns is not None else list(type(rows[0]).model_fields)
    data: dict[str, list[Any]] = {}
    for col in cols:
        values = [getattr(r, col) for r in rows]
        data[col] = [v.value if hasattr(v, "value") and isinstance(v, str) else v for v in values]
    return pd.DataFrame(data, columns=cols)
