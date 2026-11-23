"""Entity resolution for PandaScore payloads (teams, players, tournaments) on top of ``cs2bot.db.repo``.

Why not call ``db.repo.resolve_team`` directly for unknown teams? Its fuzzy fallback scores names with
rapidfuzz ``token_set_ratio``, which is 100 whenever one name's words are a subset of the other's:
"MOUZ NXT" vs "MOUZ", "Spirit Academy" vs "Spirit", "FURIA fe" vs "FURIA". PandaScore lists many academy
and female rosters, so that fallback would glue their PandaScore id onto the main team created by HLTV
and the main team's matches could never be linked again. This module therefore resolves in this order:

1. ``teams.pandascore_id``,
2. an exact normalised alias of a team that has no PandaScore id yet,
3. a *strict* fuzzy match (character ratio, never token subsets) among teams without a PandaScore id,
4. a new team row,

and only then calls ``resolve_team`` (which now finds the row by id) to attach the alias and fill empty
columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.db.models import Player, Team, TeamAlias, Tournament
from cs2bot.db.repo import find_team_by_alias, resolve_player, resolve_team, resolve_tournament
from cs2bot.domain.enums import DataSource
from cs2bot.ingest.pandascore.mapping import (
    map_is_lan,
    map_tier,
    parse_prizepool_usd,
    tournament_display_name,
)
from cs2bot.ingest.pandascore.models import PSMatch, PSPlayer, PSTeam
from cs2bot.utils.names import normalize_player_name, normalize_team_name
from cs2bot.utils.time import parse_datetime

SOURCE = DataSource.PANDASCORE
EXTRA_KEY = "pandascore"

STRICT_FUZZY_THRESHOLD = 93.0  # character-level ratio on normalised names
STRICT_FUZZY_MARGIN = 3.0  # the best candidate must beat the runner-up by this much
STRICT_FUZZY_MIN_LENGTH = 5  # short names ("og", "big", "b8") are never fuzzy-matched

_PS_SEEN_KEY = "pandascore_team_seen_at"
_HLTV_SEEN_KEY = "hltv_team_seen_at"  # written by cs2bot.ingest.hltv


@dataclass
class ChangeTracker:
    """Records whether a sync step really changed something (drives created/updated/unchanged)."""

    changed: bool = False
    fields: list[str] = field(default_factory=list)

    def set(self, obj: Any, attr: str, value: Any) -> bool:
        """``setattr`` only when the value differs; returns True if it did."""
        if getattr(obj, attr) == value:
            return False
        setattr(obj, attr, value)
        self.touch(f"{type(obj).__name__}.{attr}")
        return True

    def fill(self, obj: Any, attr: str, value: Any) -> bool:
        """Set only when the current value is None (never overwrite another source's data)."""
        if value is None or getattr(obj, attr) is not None:
            return False
        return self.set(obj, attr, value)

    def touch(self, what: str) -> None:
        self.changed = True
        if what not in self.fields:
            self.fields.append(what)

    def merge_extra(self, obj: Any, updates: dict[str, Any], *, key: str = EXTRA_KEY) -> bool:
        new = merge_extra(obj.extra, updates, key=key)
        if new == (obj.extra or {}):
            return False
        obj.extra = new
        self.touch(f"{type(obj).__name__}.extra")
        return True


def merge_extra(
    current: dict[str, Any] | None, updates: dict[str, Any], *, key: str = EXTRA_KEY
) -> dict[str, Any]:
    """Return a NEW ``extra`` dict with ``updates`` merged under ``extra[key]``.

    JSON columns do not track in-place mutation, hence the copy. ``None`` values are skipped, so a
    field PandaScore stops sending keeps its last known value.
    """
    base = dict(current or {})
    section = dict(base.get(key) or {})
    section.update({k: v for k, v in updates.items() if v is not None})
    base[key] = section
    return base


def extra_section(obj: Any, key: str = EXTRA_KEY) -> dict[str, Any]:
    section = (getattr(obj, "extra", None) or {}).get(key)
    return section if isinstance(section, dict) else {}


# ------------------------------------------------------------------------------------------ teams


@dataclass
class TeamIndex:
    """Per-run cache: PandaScore team id -> DB id, plus the aliases of teams without a PandaScore id."""

    by_ps_id: dict[int, int] = field(default_factory=dict)
    _unlinked_aliases: list[tuple[str, int]] | None = None

    def unlinked_aliases(self, session: Session) -> list[tuple[str, int]]:
        if self._unlinked_aliases is None:
            rows = session.execute(
                select(TeamAlias.alias, TeamAlias.team_id)
                .join(Team, Team.id == TeamAlias.team_id)
                .where(Team.pandascore_id.is_(None))
            )
            self._unlinked_aliases = [(alias, team_id) for alias, team_id in rows]
        return self._unlinked_aliases

    def forget_unlinked(self, team_id: int) -> None:
        if self._unlinked_aliases is not None:
            self._unlinked_aliases = [(a, t) for a, t in self._unlinked_aliases if t != team_id]


def strict_fuzzy_team(session: Session, name: str, index: TeamIndex) -> Team | None:
    """Unambiguous near-identical name among teams without a PandaScore id, else None.

    Catches spelling variants ("GamerLegion" / "Gamer Legion", "9 Pandas" / "9Pandas") but, unlike the
    foundation's token-set matcher, never a main team for its academy / female roster.
    """
    target = normalize_team_name(name)
    compact = target.replace(" ", "")
    if len(compact) < STRICT_FUZZY_MIN_LENGTH:
        return None
    scores: dict[int, float] = {}
    for alias, team_id in index.unlinked_aliases(session):
        score = 100.0 if alias.replace(" ", "") == compact else float(fuzz.ratio(target, alias))
        if score > scores.get(team_id, 0.0):
            scores[team_id] = score
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_id, best = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    if best < STRICT_FUZZY_THRESHOLD or best - runner_up < STRICT_FUZZY_MARGIN:
        return None
    team = session.get(Team, best_id)
    return team if team is not None and team.pandascore_id is None else None


def resolve_ps_team(session: Session, ps_team: PSTeam, index: TeamIndex | None = None) -> Team:
    """Find or create the canonical team for a PandaScore team (see the module docstring)."""
    if ps_team.id is None or not ps_team.name:
        raise ValueError("PandaScore team without id or name")
    idx = index if index is not None else TeamIndex()
    name = ps_team.name.strip()[:128]
    cached = idx.by_ps_id.get(ps_team.id)
    if cached is not None:
        hit = session.get(Team, cached)
        if hit is not None and hit.pandascore_id == ps_team.id:
            return hit
    team = session.scalar(select(Team).where(Team.pandascore_id == ps_team.id))
    if team is None:
        candidate = find_team_by_alias(session, name)
        if candidate is None or candidate.pandascore_id is not None:
            candidate = strict_fuzzy_team(session, name, idx)
        if candidate is not None and candidate.pandascore_id is None:
            candidate.pandascore_id = ps_team.id
            idx.forget_unlinked(candidate.id)
            team = candidate
        else:
            team = Team(name=name, pandascore_id=ps_team.id)
            session.add(team)
        session.flush()
    resolved = resolve_team(
        session,
        name=name,
        source=SOURCE,
        external_id=ps_team.id,
        slug=ps_team.slug[:128] if ps_team.slug else None,
        country=ps_team.location[:64] if ps_team.location else None,
        logo_url=ps_team.image_url[:512] if ps_team.image_url else None,
    )
    result = resolved if resolved is not None else team
    acronym = ps_team.acronym
    if acronym and extra_section(result).get("acronym") != acronym:
        result.extra = merge_extra(result.extra, {"acronym": acronym})
    idx.by_ps_id[ps_team.id] = result.id
    return result


# ------------------------------------------------------------------------------------------ players


def _last_team_sighting(player: Player) -> datetime | None:
    extra = player.extra or {}
    seen = [parse_datetime(extra.get(key)) for key in (_PS_SEEN_KEY, _HLTV_SEEN_KEY)]
    known = [s for s in seen if s is not None]
    return max(known) if known else None


def resolve_ps_player(
    session: Session,
    ps_player: PSPlayer,
    team: Team,
    *,
    seen_at: datetime | None,
    move_team: bool,
    same_match_candidates: list[Player] | None = None,
    keep_team_player_ids: set[int] | frozenset[int] = frozenset(),
) -> Player | None:
    """Find or create the player of a PandaScore roster / stat line of ``team``.

    Resolution order: ``players.pandascore_id``; a player already in this match's lineup for the same
    team with the same normalised nickname and no PandaScore id (``same_match_candidates`` - this is how
    a stand-in created by HLTV is recognised although his ``current_team_id`` points elsewhere); then
    ``db.repo.resolve_player`` (same nickname among the team's current players, else a new row).

    ``Player.current_team_id`` only moves when ``move_team`` is set and this sighting (``seen_at``) is
    not older than the last one recorded by PandaScore or HLTV, so a backfill walking through old
    matches never drags players back to former teams. Players whose DB id is in
    ``keep_team_player_ids`` (stand-ins according to another source's lineup of this match) are never
    moved.
    """
    if ps_player.id is None or not ps_player.name:
        return None
    nickname = ps_player.name.strip()[:64]
    if not nickname:
        return None
    real_name = ps_player.real_name[:128] if ps_player.real_name else None
    country = ps_player.nationality[:64] if ps_player.nationality else None
    player = session.scalar(select(Player).where(Player.pandascore_id == ps_player.id))
    if player is None and same_match_candidates:
        wanted = normalize_player_name(nickname)
        for candidate in same_match_candidates:
            if candidate.pandascore_id is None and normalize_player_name(candidate.nickname) == wanted:
                candidate.pandascore_id = ps_player.id
                player = candidate
                break
    if player is None:
        player = resolve_player(
            session,
            nickname=nickname,
            source=SOURCE,
            external_id=ps_player.id,
            team_id=team.id,
            update_current_team=False,
            real_name=real_name,
            country=country,
        )
        if player is None:  # create=True never returns None; defensive
            return None
    else:
        if real_name and not player.real_name:
            player.real_name = real_name
        if country and not player.country:
            player.country = country
    if move_team and seen_at is not None and player.id not in keep_team_player_ids:
        last = _last_team_sighting(player)
        if last is None or seen_at >= last:
            if player.current_team_id != team.id:
                player.current_team_id = team.id
            stamp = seen_at.isoformat()
            if (player.extra or {}).get(_PS_SEEN_KEY) != stamp:
                player.extra = {**(player.extra or {}), _PS_SEEN_KEY: stamp}
    if ps_player.role and extra_section(player).get("role") != ps_player.role:
        player.extra = merge_extra(player.extra, {"role": ps_player.role})
    return player


# ------------------------------------------------------------------------------------------ tournaments


def upsert_tournament(
    session: Session,
    ps_match: PSMatch,
    tracker: ChangeTracker,
    cache: dict[int, tuple[int, tuple[Any, ...]]] | None = None,
) -> Tournament | None:
    """Create / update the tournament row of a match (one row per PandaScore tournament = stage).

    The display name is ``"<league> <serie> - <stage>"`` because PandaScore's own tournament name is
    only the stage ("Playoffs"). PandaScore is authoritative for tier, dates and prize pool of its own
    rows: later changes (a re-tiered event, moved dates) are applied, but a known tier is never replaced
    by "not rated". ``cache`` (per sync run) skips the work when the same stage payload was already
    applied.
    """
    ps = ps_match.tournament
    if ps is None or ps.id is None:
        return None
    league = ps_match.league or ps.league
    serie = ps_match.serie or ps.serie
    serie_name = (serie.full_name or serie.name) if serie is not None else None
    name = tournament_display_name(league.name if league else None, serie_name, ps.name)
    is_lan = map_is_lan(ps.type)
    prize = parse_prizepool_usd(ps.prizepool)
    serie_id = ps.serie_id or ps_match.serie_id or (serie.id if serie else None)
    league_id = ps.league_id or ps_match.league_id or (league.id if league else None)
    location = ps.country or ps.region or None
    fingerprint = (name, ps.tier, ps.type, ps.prizepool, ps.begin_at, ps.end_at, ps.slug, serie_id, location)
    if cache is not None and ps.id in cache and cache[ps.id][1] == fingerprint:
        hit = session.get(Tournament, cache[ps.id][0])
        if hit is not None and hit.pandascore_id == ps.id:
            return hit
    existed = session.scalar(select(Tournament.id).where(Tournament.pandascore_id == ps.id)) is not None
    row = resolve_tournament(
        session,
        name=name,
        source=SOURCE,
        external_id=ps.id,
        tier=map_tier(ps.tier) if ps.tier else None,
        slug=ps.slug[:256] if ps.slug else None,
        is_lan=is_lan,
        location=location[:128] if location else None,
        prize_pool_usd=prize,
        start_at=ps.begin_at,
        end_at=ps.end_at,
        pandascore_serie_id=serie_id,
    )
    if not existed:
        tracker.touch("Tournament.created")
    tracker.set(row, "name", name)
    if ps.tier:
        tracker.set(row, "tier", map_tier(ps.tier))
    if is_lan is not None:
        tracker.set(row, "is_lan", is_lan)
    for attr, value in (
        ("slug", ps.slug[:256] if ps.slug else None),
        ("start_at", ps.begin_at),
        ("end_at", ps.end_at),
        ("prize_pool_usd", prize),
        ("pandascore_serie_id", serie_id),
        ("location", location[:128] if location else None),
    ):
        if value is not None:
            tracker.set(row, attr, value)
    tracker.merge_extra(
        row,
        {
            "league_id": league_id,
            "league_name": league.name if league else None,
            "serie_id": serie_id,
            "serie_name": serie_name,
            "stage": ps.name,
            "type": ps.type,
            "region": ps.region,
            "country": ps.country,
            "prizepool": ps.prizepool,
            "tier": ps.tier,
        },
    )
    if cache is not None:
        cache[ps.id] = (row.id, fingerprint)
    return row
