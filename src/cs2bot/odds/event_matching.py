"""Link Betclic events to our matches (PLAN.md section 4.4).

Candidates are matches with status scheduled/live whose ``scheduled_at`` lies within
``+-window_hours`` (default 12 h) of the event's ``starts_at``, or on the same local
(Europe/Warsaw) day as ``now`` when the event has no start time. Each candidate gets

* **team score** - both orientations are tried; per side the best similarity against the team's
  name and all its ``team_aliases`` (exact normalised match = 1.0, else rapidfuzz ``ratio`` /
  ``token_sort_ratio``; deliberately *not* ``token_set_ratio``, which scores "Spirit" vs "Spirit
  Academy" as 100). The two sides are combined with a geometric mean so one weak side drags the
  score down. The better orientation decides ``teams_swapped``;
* **time score** - 1.0 within 30 min, falling linearly to 0.5 at 6 h and 0 at 12 h (0.4 when the
  event has no start time, which keeps such links below the AUTO threshold);
* **competition score** - token-set similarity of the competition name vs the tournament name
  after removing words like "Counter-Strike"/"CS2" (0.5 when either is unknown).

``confidence = 0.70 team + 0.20 time + 0.10 competition``. The best candidate is linked
``AUTO`` when ``confidence >= decision.min_event_link_confidence`` (dashboard overrides apply) and
it beats the runner-up by ``min_margin`` and its team order is clear (the chosen orientation's team
score beats the swapped one by ``min_orientation_margin``); otherwise ``PENDING_REVIEW`` (never bet
on). In-scope matches win ties. Rows set to ``CONFIRMED`` / ``REJECTED`` from the dashboard are never changed.
Betclic team names are stored as ``TeamAlias(source=BETCLIC)`` for AUTO/CONFIRMED links.

Two safety rules on top of the scoring:

* **other e-sports** - Betclic lists every e-sport in one section and organisations such as G2 or
  Fnatic field teams in several games. An event whose sport label (``extra["sport_raw"]``, stored
  by the collector), competition or URL section names another game (League of Legends, Dota 2,
  Valorant ...) and not CS2 is never linked (``UNLINKED``), however well names and time fit;
* **finished matches** - candidates are scheduled/live matches only, so once our match is
  ``finished`` it would drop out and the event would be unlinked the next time Betclic still
  lists it (results often reach us before the bookmaker removes the event). An ``AUTO`` link to
  a finished match is therefore frozen: the link is history (closing-line and settlement
  analysis need it) and nothing can be bet on it any more.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from cs2bot.config import Settings
from cs2bot.db.models import BookmakerEvent, Match, Team, TeamAlias
from cs2bot.db.state import effective_decision_settings
from cs2bot.domain.enums import DataSource, LinkStatus, MatchStatus
from cs2bot.odds.source_config import is_other_game, url_section
from cs2bot.odds.text import fold_text
from cs2bot.utils.names import normalize_team_name
from cs2bot.utils.time import utcnow

logger = logging.getLogger(__name__)

MANUAL_STATUSES = frozenset({LinkStatus.CONFIRMED, LinkStatus.REJECTED})
LINKED_STATUSES = frozenset({LinkStatus.AUTO, LinkStatus.CONFIRMED})

_GAME_NAME_RE = re.compile(r"counter[\s\-_]*strike(?:\s*2)?|\bcs\s*:?\s*(?:go|2)\b|\be-?sports?\b")
_COMPETITION_NOISE = {
    "counter",
    "strike",
    "counterstrike",
    "cs",
    "cs2",
    "csgo",
    "go",
    "e",
    "sport",
    "sports",
    "esport",
    "esports",
}


@dataclass(frozen=True)
class LinkConfig:
    window_hours: float = 12.0
    team_weight: float = 0.70
    time_weight: float = 0.20
    competition_weight: float = 0.10
    min_margin: float = 0.05
    # AUTO also needs the chosen orientation to beat the other one by this much (team score):
    # a wrong ``teams_swapped`` would put every bet on the other team (review round 1)
    min_orientation_margin: float = 0.10
    missing_start_time_score: float = 0.4
    unknown_competition_score: float = 0.5
    in_scope_tiebreak: float = 0.005  # ranking only; never changes the stored confidence
    statuses: tuple[MatchStatus, ...] = (MatchStatus.SCHEDULED, MatchStatus.LIVE)


class LinkCandidate(BaseModel):
    match_id: int
    confidence: float
    teams_swapped: bool
    team_score: float
    time_score: float
    competition_score: float
    in_scope: bool
    orientation_margin: float = 1.0  # team score of the chosen orientation minus the other one


class LinkResult(BaseModel):
    event_id: int
    match_id: int | None
    status: LinkStatus
    confidence: float | None
    teams_swapped: bool
    changed: bool = False  # match_id or teams_swapped differ from before this call
    status_changed: bool = False
    candidates: list[LinkCandidate] = Field(default_factory=list)
    reason: str = ""


# --------------------------------------------------------------------------- scoring helpers


def team_name_score(raw: str, names: list[str]) -> float:
    """0..1 similarity of a bookmaker team name to any of a team's names/aliases."""
    target = normalize_team_name(raw)
    if not target:
        return 0.0
    best = 0.0
    for name in names:
        candidate = normalize_team_name(name)
        if not candidate:
            continue
        if candidate == target:
            return 1.0
        score = max(fuzz.ratio(target, candidate), fuzz.token_sort_ratio(target, candidate)) / 100.0
        best = max(best, score)
    return best


def time_score(delta: timedelta | None, missing: float = 0.4) -> float:
    if delta is None:
        return missing
    hours = abs(delta.total_seconds()) / 3600.0
    if hours <= 0.5:
        return 1.0
    if hours <= 6.0:
        return 1.0 - 0.5 * (hours - 0.5) / 5.5
    if hours <= 12.0:
        return 0.5 - 0.5 * (hours - 6.0) / 6.0
    return 0.0


def competition_score(event_competition: str | None, tournament_name: str | None) -> float | None:
    def tokens(text: str) -> str:
        folded = _GAME_NAME_RE.sub(" ", fold_text(text))
        return " ".join(t for t in folded.replace("-", " ").split() if t not in _COMPETITION_NOISE)

    if not event_competition or not tournament_name:
        return None
    a, b = tokens(event_competition), tokens(tournament_name)
    if not a or not b:
        return None
    return fuzz.token_set_ratio(a, b) / 100.0


def _team_names(team: Team | None) -> list[str]:
    if team is None:
        return []
    names = [team.name]
    if team.slug:
        names.append(team.slug.replace("-", " "))
    names.extend(alias.alias for alias in team.aliases)
    return names


def score_candidate(event: BookmakerEvent, match: Match, cfg: LinkConfig) -> LinkCandidate:
    names1, names2 = _team_names(match.team1), _team_names(match.team2)
    straight = math.sqrt(team_name_score(event.team1_raw, names1) * team_name_score(event.team2_raw, names2))
    swapped = math.sqrt(team_name_score(event.team1_raw, names2) * team_name_score(event.team2_raw, names1))
    is_swapped = swapped > straight
    team = max(straight, swapped)
    delta = (match.scheduled_at - event.starts_at) if (event.starts_at and match.scheduled_at) else None
    t_score = time_score(delta, cfg.missing_start_time_score)
    comp = competition_score(event.competition_raw, match.tournament.name if match.tournament else None)
    c_score = cfg.unknown_competition_score if comp is None else comp
    confidence = cfg.team_weight * team + cfg.time_weight * t_score + cfg.competition_weight * c_score
    return LinkCandidate(
        match_id=match.id,
        confidence=round(min(1.0, max(0.0, confidence)), 4),
        teams_swapped=is_swapped,
        team_score=round(team, 4),
        time_score=round(t_score, 4),
        competition_score=round(c_score, 4),
        in_scope=bool(match.in_scope),
        orientation_margin=round(abs(straight - swapped), 4),
    )


def _local_day_bounds(now: datetime, tz_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    local_date = now.astimezone(tz).date()
    start = datetime.combine(local_date, time.min, tzinfo=tz)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def candidate_matches(
    session: Session,
    event: BookmakerEvent,
    *,
    settings: Settings,
    now: datetime,
    config: LinkConfig | None = None,
) -> list[Match]:
    cfg = config or LinkConfig()
    query = (
        select(Match)
        .options(
            selectinload(Match.team1).selectinload(Team.aliases),
            selectinload(Match.team2).selectinload(Team.aliases),
            selectinload(Match.tournament),
        )
        .where(
            Match.status.in_(cfg.statuses),
            Match.team1_id.is_not(None),
            Match.team2_id.is_not(None),
            Match.scheduled_at.is_not(None),
        )
    )
    if event.starts_at is not None:
        window = timedelta(hours=cfg.window_hours)
        query = query.where(
            Match.scheduled_at >= event.starts_at - window, Match.scheduled_at <= event.starts_at + window
        )
    else:
        lo, hi = _local_day_bounds(now, settings.timezone)
        query = query.where(Match.scheduled_at >= lo, Match.scheduled_at < hi)
    return list(session.scalars(query))


# --------------------------------------------------------------------------- aliases


def store_betclic_aliases(session: Session, event: BookmakerEvent) -> int:
    """Remember the Betclic spelling of both teams (``TeamAlias(source=BETCLIC)``); returns rows added."""
    if event.match_id is None:
        return 0
    match = session.get(Match, event.match_id)
    if match is None:
        return 0
    first, second = (
        (match.team2_id, match.team1_id) if event.teams_swapped else (match.team1_id, match.team2_id)
    )
    added = 0
    for raw, team_id in ((event.team1_raw, first), (event.team2_raw, second)):
        alias = normalize_team_name(raw)
        if team_id is None or not alias:
            continue
        existing = session.scalar(
            select(TeamAlias).where(TeamAlias.alias == alias, TeamAlias.source == DataSource.BETCLIC)
        )
        if existing is None:
            session.add(TeamAlias(team_id=team_id, alias=alias, source=DataSource.BETCLIC))
            session.flush()
            added += 1
        elif existing.team_id != team_id:
            logger.warning(
                "Betclic alias %r already points to team %s, not %s (event %s) - left unchanged",
                alias,
                existing.team_id,
                team_id,
                event.external_id,
            )
    return added


# --------------------------------------------------------------------------- linking


def link_event(
    session: Session,
    event_row: BookmakerEvent,
    *,
    settings: Settings,
    now: datetime | None = None,
    config: LinkConfig | None = None,
) -> LinkResult:
    """(Re)link one bookmaker event; updates ``match_id`` / ``teams_swapped`` / confidence / status."""
    cfg = config or LinkConfig()
    now = now or utcnow()
    if event_row.id is None:
        session.flush()
    before = (event_row.match_id, bool(event_row.teams_swapped))
    status_before = event_row.link_status

    if event_row.link_status in MANUAL_STATUSES:
        if event_row.link_status is LinkStatus.CONFIRMED:
            store_betclic_aliases(session, event_row)
        return LinkResult(
            event_id=event_row.id,
            match_id=event_row.match_id,
            status=event_row.link_status,
            confidence=event_row.link_confidence,
            teams_swapped=bool(event_row.teams_swapped),
            reason="manual decision preserved",
        )

    extra = event_row.extra or {}
    sport_raw = extra.get("sport_raw")
    hints = [sport_raw if isinstance(sport_raw, str) else None, event_row.competition_raw]
    hints.append(url_section(event_row.url))
    if is_other_game(hints):
        event_row.match_id = None
        event_row.teams_swapped = False
        event_row.link_confidence = None
        event_row.link_status = LinkStatus.UNLINKED
        session.flush()
        return LinkResult(
            event_id=event_row.id,
            match_id=None,
            status=LinkStatus.UNLINKED,
            confidence=None,
            teams_swapped=False,
            changed=before != (None, False),
            status_changed=status_before is not LinkStatus.UNLINKED,
            reason="another e-sport (not CS2) according to its sport / competition / URL",
        )

    if event_row.link_status is LinkStatus.AUTO and event_row.match_id is not None:
        linked = session.get(Match, event_row.match_id)
        if linked is not None and linked.status is MatchStatus.FINISHED:
            return LinkResult(
                event_id=event_row.id,
                match_id=event_row.match_id,
                status=event_row.link_status,
                confidence=event_row.link_confidence,
                teams_swapped=bool(event_row.teams_swapped),
                reason="linked match is finished: link frozen",
            )

    threshold = effective_decision_settings(session, settings).min_event_link_confidence
    scored = [
        score_candidate(event_row, m, cfg)
        for m in candidate_matches(session, event_row, settings=settings, now=now, config=cfg)
    ]
    scored.sort(key=lambda c: c.confidence + (cfg.in_scope_tiebreak if c.in_scope else 0.0), reverse=True)

    if not scored:
        event_row.match_id = None
        event_row.teams_swapped = False
        event_row.link_confidence = None
        event_row.link_status = LinkStatus.UNLINKED
        reason = "no scheduled/live match within the time window"
    else:
        best = scored[0]
        runner_up = scored[1].confidence if len(scored) > 1 else 0.0
        margin = best.confidence - runner_up
        orientation_clear = best.orientation_margin >= cfg.min_orientation_margin
        auto = best.confidence >= threshold and margin >= cfg.min_margin and orientation_clear
        event_row.match_id = best.match_id
        event_row.teams_swapped = best.teams_swapped
        event_row.link_confidence = best.confidence
        event_row.link_status = LinkStatus.AUTO if auto else LinkStatus.PENDING_REVIEW
        if auto:
            reason = f"confidence {best.confidence:.3f} >= {threshold:.2f}, margin {margin:.3f}"
        elif best.confidence < threshold:
            reason = f"confidence {best.confidence:.3f} below {threshold:.2f}"
        elif not orientation_clear:
            reason = f"team order unclear (orientation margin {best.orientation_margin:.3f})"
        else:
            reason = f"ambiguous: runner-up within {margin:.3f}"
    session.flush()
    if event_row.link_status is LinkStatus.AUTO:
        store_betclic_aliases(session, event_row)
    after = (event_row.match_id, bool(event_row.teams_swapped))
    return LinkResult(
        event_id=event_row.id,
        match_id=event_row.match_id,
        status=event_row.link_status,
        confidence=event_row.link_confidence,
        teams_swapped=bool(event_row.teams_swapped),
        changed=after != before,
        status_changed=event_row.link_status != status_before,
        candidates=scored[:5],
        reason=reason,
    )
