"""Match scope filter (PLAN.md section 4.1): Tier 1 / Tier 2 only.

A match is in scope when its PandaScore tournament tier is one of ``ScopeSettings.pandascore_tiers``
(default S, A, B) **or** either team is in the HLTV top ``ScopeSettings.hltv_top_n`` (default 50).

Both ingestion sources call into this module (``cs2bot.ingest.hltv`` imports :func:`is_in_scope` when
it is available), so the rule lives in exactly one place.

Design decisions
----------------
* **Sticky by default.** :func:`refresh_match_scope` only ever promotes a match into scope. A match that
  was in scope when it was analysed must not silently disappear from datasets because a team later
  dropped to rank 51, and HLTV may have promoted it for reasons this module cannot see (event stars).
  Pass ``sticky=False`` for a strict re-evaluation.
* **Ranks as of the match.** ``Team.hltv_rank`` is *today's* rank. For matches older than
  ``RANK_MAX_AGE_DAYS`` the rank is taken from the ``team_rankings`` history instead (latest HLTV ranking
  at or before the match day, at most ``RANK_MAX_AGE_DAYS`` old); without such a row the rank is unknown
  and only the tier decides. This keeps a backfill from pulling in 2023 matches of teams that only
  became good in 2026.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.config import ScopeSettings, Settings
from cs2bot.db.models import Match, Team, TeamRanking, Tournament
from cs2bot.domain.enums import DataSource, MatchStatus, Tier
from cs2bot.utils.time import utcnow

RANK_MAX_AGE_DAYS = 45

__all__ = [
    "RANK_MAX_AGE_DAYS",
    "is_in_scope",
    "match_tier",
    "refresh_match_scope",
    "refresh_upcoming_scope",
    "team_rank_as_of",
]


def _scope_settings(settings: ScopeSettings | Settings) -> ScopeSettings:
    return settings.scope if isinstance(settings, Settings) else settings


def is_in_scope(
    tier: Tier | str | None, team1_rank: int | None, team2_rank: int | None, settings: ScopeSettings
) -> bool:
    """True if ``tier`` is a configured tier or either team's HLTV rank is within the top N.

    ``tier`` may be a :class:`Tier`, its string value (any case) or None (= unranked). Ranks of 0 or
    below are treated as unknown.
    """
    tier_value = tier.value if isinstance(tier, Tier) else (tier or Tier.UNRANKED.value)
    wanted = {t.strip().lower() for t in settings.pandascore_tiers}
    if tier_value.strip().lower() in wanted:
        return True
    return any(rank is not None and 0 < rank <= settings.hltv_top_n for rank in (team1_rank, team2_rank))


def team_rank_as_of(
    session: Session,
    team: Team | None,
    when: datetime | None,
    *,
    now: datetime | None = None,
    max_age_days: int = RANK_MAX_AGE_DAYS,
) -> int | None:
    """HLTV rank of ``team`` that applies to a match played at ``when``.

    Upcoming and recent matches (``when`` unknown or within ``max_age_days`` of now) use the current
    ``Team.hltv_rank``. Older matches use the ranking history, or None when there is none.
    """
    if team is None:
        return None
    current = now or utcnow()
    if when is None or when >= current - timedelta(days=max_age_days):
        return team.hltv_rank
    day = when.date()
    row = session.scalar(
        select(TeamRanking)
        .where(
            TeamRanking.team_id == team.id,
            TeamRanking.source == DataSource.HLTV,
            TeamRanking.ranked_on <= day,
            TeamRanking.ranked_on >= day - timedelta(days=max_age_days),
        )
        .order_by(TeamRanking.ranked_on.desc())
        .limit(1)
    )
    return row.rank if row is not None else None


def match_tier(session: Session, match: Match) -> Tier:
    """Tier of a match: ``Match.tier``, falling back to its tournament's tier while that is unranked."""
    if match.tier is not None and match.tier is not Tier.UNRANKED:
        return match.tier
    if match.tournament_id is not None:
        tournament = session.get(Tournament, match.tournament_id)
        if tournament is not None and tournament.tier is not None:
            return tournament.tier
    return Tier.UNRANKED


def refresh_match_scope(
    session: Session,
    match: Match,
    settings: ScopeSettings | Settings,
    *,
    sticky: bool = True,
    now: datetime | None = None,
) -> bool:
    """Re-evaluate ``match.in_scope`` from its tier and both teams' HLTV ranks; returns the new value.

    With ``sticky`` (default) an in-scope match stays in scope. The row is only written when the value
    actually changes; the caller flushes / commits.
    """
    scope = _scope_settings(settings)
    team1 = session.get(Team, match.team1_id) if match.team1_id is not None else None
    team2 = session.get(Team, match.team2_id) if match.team2_id is not None else None
    rank1 = team_rank_as_of(session, team1, match.scheduled_at, now=now)
    rank2 = team_rank_as_of(session, team2, match.scheduled_at, now=now)
    value = is_in_scope(match_tier(session, match), rank1, rank2, scope)
    if sticky and match.in_scope:
        value = True
    if bool(match.in_scope) != value:
        match.in_scope = value
    return value


def refresh_upcoming_scope(
    session: Session,
    settings: ScopeSettings | Settings,
    *,
    statuses: Iterable[MatchStatus] = (MatchStatus.SCHEDULED, MatchStatus.POSTPONED, MatchStatus.LIVE),
    sticky: bool = True,
    now: datetime | None = None,
) -> int:
    """Re-evaluate the scope of every not-yet-finished match; returns how many flags changed.

    Call it after an HLTV ranking sync: a team entering the top N pulls its upcoming matches into scope.
    """
    changed = 0
    query = select(Match).where(Match.status.in_(list(statuses)))
    if sticky:
        query = query.where(Match.in_scope.is_(False))
    for match in session.scalars(query):
        before = bool(match.in_scope)
        if refresh_match_scope(session, match, settings, sticky=sticky, now=now) != before:
            changed += 1
    if changed:
        session.flush()
    return changed
