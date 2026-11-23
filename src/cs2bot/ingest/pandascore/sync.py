"""PandaScore -> database sync: tournaments, teams, players, matches, maps, lineups, scope.

All functions take an open session, flush but never commit (except ``sync_past`` / ``sync_modified``
when ``PandaScoreSyncOptions.commit_every`` is set) and never raise for API or data problems: those
end up in the returned :class:`cs2bot.domain.sync.SyncReport`. Each match is written inside its own
savepoint, so one malformed match cannot poison the rest. Everything is idempotent: a second run over
the same payloads changes nothing and reports ``created == updated == 0``.

One match row whichever source runs first
-----------------------------------------
HLTV (``cs2bot.ingest.hltv``) and PandaScore both describe the same real matches. A PandaScore match is
found by ``matches.pandascore_id``; failing that by :func:`cs2bot.db.repo.find_match` (same two teams
within ``match_link_window_hours``), which links a row HLTV created. HLTV does the mirror image, so
both orders end with one row. The division of labour is fixed, which makes the final state independent
of the sync order:

==========================  ========================================================================
PandaScore owns             ``scheduled_at`` (incl. rescheduling), ``format``, ``tier``, ``stage``,
                            ``tournament_id`` and - when HLTV has no value - ``is_lan``.
HLTV owns (once it has it)  final series score / winner, every map row it has written (name, rounds,
                            sides, picks), vetoes, its own lineup rows.
==========================  ========================================================================

So when HLTV already stored a finished result, PandaScore keeps it (and reports a warning if its own
numbers differ); map rows carrying HLTV data only get their empty columns filled; HLTV-sourced lineup
rows are never deleted.

Orientation: ``team1`` / ``team2`` always mean ``matches.team1_id`` / ``team2_id``. A linked match keeps
its existing orientation (:func:`cs2bot.db.repo.is_swapped`); scores, round counts and sides are
swapped accordingly. References by team id (winner, lineups) need no swapping.

Assumptions (no live token was available; see ``models.py`` for the payload sources)
-----------------------------------------------------------------------------------
* Match lists do not contain map names. Maps, round history and player lines come from
  ``/csgo/matches/{id}/games`` (historical-data plan), fetched once per finished in-scope match. On a
  plan without it (HTTP 403) the sync keeps working without map rows and says so in a warning.
* A finished game whose ``rounds_score`` adds up to 0 has no round data (the API example for games
  without detailed stats shows exactly that); the round columns then stay empty.
* ``canceled`` + ``forfeit`` (walkover) is stored as ``CANCELED`` with the walkover winner in
  ``Match.extra['pandascore']['forfeit_winner_team_id']`` - the same convention as the HLTV sync.
* Rosters come from ``/tournaments/{id}/rosters``. They may list more than five players (substitutes);
  a pre-match lineup is only written when exactly five (active) players are listed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from cs2bot.config import Settings
from cs2bot.db.models import MapPlayed, Match, MatchLineup, Player, PlayerMapStats, Team, Tournament
from cs2bot.db.repo import find_match, is_swapped
from cs2bot.domain.enums import MatchStatus, RoundEndReason, Side, TeamSlot, Tier
from cs2bot.domain.records import RoundRecord
from cs2bot.domain.sync import SyncReport
from cs2bot.ingest.pandascore.client import PandaScoreClient
from cs2bot.ingest.pandascore.entities import (
    SOURCE,
    ChangeTracker,
    TeamIndex,
    extra_section,
    resolve_ps_player,
    resolve_ps_team,
    upsert_tournament,
)
from cs2bot.ingest.pandascore.errors import (
    PandaScoreError,
    PandaScoreNotFoundError,
    PandaScorePlanError,
    PandaScoreRateLimitError,
)
from cs2bot.ingest.pandascore.mapping import (
    game_map_name,
    map_game_status,
    map_is_lan,
    map_match_format,
    map_match_status,
    map_round_outcome,
    map_tier,
    overtime_rounds,
    regulation_rounds,
    stage_label,
)
from cs2bot.ingest.pandascore.models import PSGame, PSMatch, PSPlayer
from cs2bot.ingest.scope import refresh_match_scope
from cs2bot.utils.time import utcnow

logger = logging.getLogger(__name__)

DetailMode = Literal["in_scope", "all", "never"]

_HLTV_KEY = "hltv"  # Match.extra / MapPlayed.extra section written by cs2bot.ingest.hltv
_OPEN_STATUSES = (MatchStatus.SCHEDULED, MatchStatus.LIVE, MatchStatus.POSTPONED)


@dataclass
class PandaScoreSyncOptions:
    """Sync knobs that are not part of :class:`cs2bot.config.Settings` (all have safe defaults)."""

    # ``filter[videogame_title]`` sent to the match lists; "cs-2" keeps CS:GO-era matches out.
    videogame_title: str | None = "cs-2"
    # Also drop payloads that explicitly carry another title (belt and braces for single-match calls).
    skip_other_titles: bool = True
    # db.repo.find_match window used to link a match HLTV created first.
    match_link_window_hours: float = 12.0
    # Keep out-of-scope matches too (cheap: no extra API calls). They may enter scope later.
    store_out_of_scope: bool = True
    # /csgo/matches/{id}/games (maps, rounds, player lines) for finished matches.
    game_details: DetailMode = "in_scope"
    # /tournaments/{id}/rosters for lineups.
    rosters: DetailMode = "in_scope"
    lineup_size: int = 5
    # Keep the PandaScore round-by-round history in maps_played.extra['pandascore']['rounds'].
    store_round_history: bool = True
    # Also write player_map_stats rows with source=PANDASCORE. OFF by default: HLTV rows describe the
    # same map and a consumer that does not filter by source would count every kill twice.
    write_player_stats: bool = False
    # sync_upcoming: also read /matches/running so started matches turn LIVE.
    include_running: bool = True
    # sync_upcoming: re-fetch open matches that vanished from the upcoming/running lists (finished,
    # cancelled, postponed or moved out of the window) - at most ``max_reconcile`` per run.
    reconcile_missing: bool = True
    reconcile_lookback_hours: float = 72.0
    max_reconcile: int = 40
    # Matches older than this never move Player.current_team_id (backfills).
    current_team_window_days: int = 30
    # A finished match whose stats are still marked incomplete is re-fetched until it is this old.
    games_final_after_hours: float = 48.0
    max_pages: int | None = None  # hard cap on list pages per call (None = all)
    commit_every: int | None = None  # sync_past / sync_modified: commit after every N matches


@dataclass
class _Run:
    """State of one sync call."""

    client: PandaScoreClient
    settings: Settings
    options: PandaScoreSyncOptions
    report: SyncReport
    now: datetime
    teams: TeamIndex = field(default_factory=TeamIndex)
    tournaments: dict[int, tuple[int, tuple[Any, ...]]] = field(default_factory=dict)
    rosters: dict[int, dict[int, list[PSPlayer]] | None] = field(default_factory=dict)
    seen_match_ids: set[int] = field(default_factory=set)
    games_unavailable: bool = False  # 403 on the games endpoint: stop asking in this run
    rosters_unavailable: bool = False
    stop: bool = False  # rate limit exhausted / auth failure: no further API calls
    processed: int = 0
    pages_before: int = 0  # client counters when the run started (a client may serve many runs)
    requests_before: int = 0

    def count(self, key: str, n: int = 1) -> None:
        self.report.details[key] = int(self.report.details.get(key, 0)) + n

    def api_failure(self, what: str, exc: PandaScoreError) -> None:
        """Record a failed API call; rate-limit exhaustion stops the run (``report.blocked``)."""
        if isinstance(exc, PandaScoreRateLimitError):
            self.stop = True
            self.report.blocked = True
            wait = f"; retry after {exc.retry_after_s:.0f} s" if exc.retry_after_s is not None else ""
            self.report.add_error(f"{what}: PandaScore rate limit reached{wait}")
        else:
            self.report.add_error(f"{what}: {exc}")


@dataclass
class _Core:
    """Result of the first (pure DB) half of persisting one match."""

    match: Match
    created: bool
    linked: bool  # this call attached the PandaScore id to a row another source created
    tracker: ChangeTracker
    ps_team1: int  # PandaScore id of OUR team1
    ps_team2: int
    team_by_ps: dict[int, Team]

    def db_team_id(self, ps_team_id: int | None) -> int | None:
        team = self.team_by_ps.get(ps_team_id) if ps_team_id is not None else None
        return team.id if team is not None else None


# ============================================================================================ public


def sync_upcoming(
    session: Session,
    client: PandaScoreClient,
    settings: Settings,
    *,
    options: PandaScoreSyncOptions | None = None,
    now: datetime | None = None,
) -> SyncReport:
    """Upsert not-started (next ``settings.scope.max_hours_ahead`` hours) and running matches.

    Matches with undecided (TBD) opponents are counted in ``report.skipped`` /
    ``report.details['tbd']`` and picked up by a later run once both teams are known. Open matches we
    already store that are missing from PandaScore's lists are re-fetched one by one (see
    ``PandaScoreSyncOptions.reconcile_missing``): that is how cancellations and far-away reschedules
    reach the database.
    """
    run = _start(client, settings, options, now, "pandascore.sync_upcoming")
    title = run.options.videogame_title
    hours = settings.scope.max_hours_ahead
    sources: list[tuple[str, Iterator[PSMatch]]] = [
        (
            "upcoming matches",
            client.iter_upcoming_matches(
                hours, videogame_title=title, max_pages=run.options.max_pages, on_invalid=_invalid(run)
            ),
        )
    ]
    if run.options.include_running:
        sources.append(
            (
                "running matches",
                client.iter_matches(
                    "running", {"sort": "begin_at"}, videogame_title=title, on_invalid=_invalid(run)
                ),
            )
        )
    lists_ok = True
    for what, matches in sources:
        lists_ok = _consume(session, run, what, matches) and lists_ok
    if lists_ok and run.options.reconcile_missing and not run.stop:
        _reconcile_missing(session, run, hours)
    return _finish(session, run)


def sync_past(
    session: Session,
    client: PandaScoreClient,
    settings: Settings,
    since: datetime,
    until: datetime | None = None,
    *,
    options: PandaScoreSyncOptions | None = None,
    now: datetime | None = None,
) -> SyncReport:
    """Upsert matches that began in ``[since, until]`` (results, maps, lineups); also the backfill.

    Pages are processed as they arrive (constant memory); set ``options.commit_every`` for long
    backfills so progress survives an interruption - the sync is idempotent, simply run it again.
    """
    run = _start(client, settings, options, now, "pandascore.sync_past")
    matches = client.iter_past_matches(
        since,
        until or run.now,
        videogame_title=run.options.videogame_title,
        max_pages=run.options.max_pages,
        on_invalid=_invalid(run),
    )
    _consume(session, run, "past matches", matches)
    return _finish(session, run)


def sync_modified(
    session: Session,
    client: PandaScoreClient,
    settings: Settings,
    since: datetime,
    until: datetime | None = None,
    *,
    options: PandaScoreSyncOptions | None = None,
    now: datetime | None = None,
) -> SyncReport:
    """Upsert every match (any status) PandaScore modified in ``[since, until]``.

    A cheap incremental catch-all: reschedules, cancellations and late result corrections all bump
    ``modified_at``.
    """
    run = _start(client, settings, options, now, "pandascore.sync_modified")
    matches = client.iter_modified_matches(
        since,
        until or run.now,
        videogame_title=run.options.videogame_title,
        max_pages=run.options.max_pages,
        on_invalid=_invalid(run),
    )
    _consume(session, run, "modified matches", matches)
    return _finish(session, run)


def sync_match(
    session: Session,
    client: PandaScoreClient,
    settings: Settings,
    pandascore_match_id: int,
    *,
    options: PandaScoreSyncOptions | None = None,
    now: datetime | None = None,
) -> SyncReport:
    """Fetch and upsert one match, with game details and rosters whatever its scope."""
    run = _start(client, settings, options, now, "pandascore.sync_match")
    what = f"match {pandascore_match_id}"
    try:
        ps_match = client.match(pandascore_match_id)
    except PandaScoreNotFoundError:
        run.report.skipped += 1
        run.report.add_warning(f"{what}: not found on PandaScore")
        return _finish(session, run)
    except PandaScoreError as exc:
        run.report.failed += 1
        run.api_failure(what, exc)
        return _finish(session, run)
    _process_match(session, run, ps_match, force_details=True)
    return _finish(session, run)


def pandascore_round_records(session: Session, map_played: MapPlayed) -> list[RoundRecord]:
    """Round history stored by this sync as :class:`RoundRecord` (winner, sides, end reason only).

    A fallback for maps without a parsed demo, in the same shape as
    ``cs2bot.ingest.hltv.hltv_round_records``. Empty unless the stored history is complete.
    """
    section = extra_section(map_played)
    rounds = section.get("rounds") or []
    if not section.get("rounds_complete") or not rounds:
        return []
    match = session.get(Match, map_played.match_id)
    if match is None or match.team1_id is None or match.team2_id is None:
        return []
    played_at = map_played.played_at or match.scheduled_at
    if played_at is None:
        return []
    out: list[RoundRecord] = []
    score1 = score2 = 0
    for entry in rounds:
        winner = TeamSlot(entry["winner"])
        out.append(
            RoundRecord(
                match_id=match.id,
                map_number=map_played.map_number,
                map_name=map_played.map_name,
                played_at=played_at,
                round_number=int(entry["round"]),
                team1_id=match.team1_id,
                team2_id=match.team2_id,
                team1_side=Side(entry["team1_side"]),
                winner=winner,
                end_reason=RoundEndReason(entry.get("end_reason", RoundEndReason.OTHER.value)),
                bomb_planted=entry.get("bomb_planted"),
                team1_score_before=score1,
                team2_score_before=score2,
                is_lan=bool(match.is_lan),
            )
        )
        if winner is TeamSlot.TEAM1:
            score1 += 1
        else:
            score2 += 1
    return out


# ============================================================================================ run plumbing


def _start(
    client: PandaScoreClient,
    settings: Settings,
    options: PandaScoreSyncOptions | None,
    now: datetime | None,
    job: str,
) -> _Run:
    return _Run(
        client=client,
        settings=settings,
        options=options or PandaScoreSyncOptions(),
        report=SyncReport(job=job),
        now=now or utcnow(),
        pages_before=client.stats.pages,
        requests_before=client.stats.requests,
    )


def _finish(session: Session, run: _Run) -> SyncReport:
    session.flush()
    run.report.fetched_pages = run.client.stats.pages - run.pages_before
    run.report.details.setdefault("unchanged", 0)
    run.report.details["api_requests"] = run.client.stats.requests - run.requests_before
    return run.report


def _invalid(run: _Run) -> Any:
    def handler(raw: dict[str, Any], exc: Exception) -> None:
        run.report.failed += 1
        count = exc.error_count() if isinstance(exc, ValidationError) else 1
        run.report.add_error(f"match {raw.get('id')!r}: malformed payload ({count} validation errors)")

    return handler


def _consume(session: Session, run: _Run, what: str, matches: Iterable[PSMatch]) -> bool:
    """Process a lazily fetched match list; an API failure ends the list (keeps what was done).

    Returns False when the list could not be read completely.
    """
    iterator = iter(matches)
    while True:
        if run.stop:
            return False
        try:
            ps_match = next(iterator)
        except StopIteration:
            return True
        except PandaScoreError as exc:
            run.report.failed += 1
            run.api_failure(what, exc)
            return False
        _process_match(session, run, ps_match)
        every = run.options.commit_every
        if every and run.processed % every == 0:
            session.commit()


# ============================================================================================ one match


def _process_match(session: Session, run: _Run, ps: PSMatch, *, force_details: bool = False) -> None:
    """Persist one match; never raises (errors are recorded and the savepoint is rolled back)."""
    run.processed += 1
    what = f"match {ps.id}"
    if ps.id is not None:
        run.seen_match_ids.add(ps.id)
    try:
        with session.begin_nested():
            core = _persist_core(session, run, ps)
    except Exception as exc:  # one broken match must never abort the sync
        logger.exception("persisting PandaScore match %s failed", ps.id)
        run.report.failed += 1
        run.report.add_error(f"{what}: {type(exc).__name__}: {exc}")
        # the savepoint rollback may have removed rows the per-run caches point at
        run.teams = TeamIndex()
        run.tournaments.clear()
        return
    if core is None:
        return
    match = core.match
    games: list[PSGame] | None = None
    if _wants_games(run, core, ps, force_details):
        games = _fetch_games(run, ps)
    rosters = None
    if _wants_rosters(session, run, core, ps, games, force_details):
        rosters = _tournament_rosters(run, ps)
    try:
        with session.begin_nested():
            _persist_details(session, run, core, ps, games, rosters)
    except Exception as exc:
        logger.exception("persisting details of PandaScore match %s failed", ps.id)
        run.report.failed += 1
        run.report.add_error(f"{what}: details: {type(exc).__name__}: {exc}")
    match.last_synced_at = run.now
    if core.created:
        run.report.created += 1
    elif core.tracker.changed:
        run.report.updated += 1
        logger.debug("PandaScore match %s updated: %s", ps.id, ", ".join(core.tracker.fields))
    else:
        run.count("unchanged")
    if match.id not in run.report.match_ids:
        run.report.match_ids.append(match.id)


def _persist_core(session: Session, run: _Run, ps: PSMatch) -> _Core | None:
    """Entities + the match row itself. Returns None when the match is skipped (already counted)."""
    if ps.id is None:
        raise ValueError("match payload without id")
    opts = run.options
    title = ps.videogame_title.slug if ps.videogame_title is not None else None
    if opts.skip_other_titles and opts.videogame_title and title and title != opts.videogame_title:
        run.report.skipped += 1
        run.count("other_title")
        return None
    if not ps.is_team_match:
        run.report.skipped += 1
        run.count("not_team_match")
        return None
    opponents = ps.team_opponents()
    if len(opponents) != 2 or len(ps.opponents) > 2:
        run.report.skipped += 1
        run.count("tbd")
        return None
    existing = session.scalar(select(Match).where(Match.pandascore_id == ps.id))
    if existing is None and not opts.store_out_of_scope and not _payload_in_scope(session, run, ps):
        run.report.skipped += 1
        run.count("out_of_scope")
        return None
    tracker = ChangeTracker()
    tournament = upsert_tournament(session, ps, tracker, run.tournaments)
    team_a = resolve_ps_team(session, opponents[0], run.teams)
    team_b = resolve_ps_team(session, opponents[1], run.teams)
    if team_a.id == team_b.id:
        raise ValueError(f"both opponents resolve to the same team ({team_a.name!r})")
    ps_a, ps_b = opponents[0].id, opponents[1].id
    assert ps_a is not None
    assert ps_b is not None
    match, created, linked = _find_or_create_match(session, run, ps, existing, team_a, team_b, tracker)
    if {match.team1_id, match.team2_id} != {team_a.id, team_b.id}:
        if match.hltv_id is not None:
            raise ValueError(f"match row {match.id} (also linked to HLTV) has different teams")
        _replace_teams(session, match, team_a, team_b, tracker)
    swapped = is_swapped(match, team_a.id)
    core = _Core(
        match=match,
        created=created,
        linked=linked,
        tracker=tracker,
        ps_team1=ps_b if swapped else ps_a,
        ps_team2=ps_a if swapped else ps_b,
        team_by_ps={ps_a: team_a, ps_b: team_b},
    )
    _apply_match_fields(run, core, ps, tournament)
    session.flush()
    before = bool(match.in_scope)
    if refresh_match_scope(session, match, run.settings.scope, now=run.now) != before:
        tracker.touch("Match.in_scope")
    session.flush()
    return core


def _payload_in_scope(session: Session, run: _Run, ps: PSMatch) -> bool:
    """Scope check before anything is stored (``store_out_of_scope=False``): tier or known team ranks."""
    scope = run.settings.scope
    tier = map_tier(ps.tournament.tier if ps.tournament else None)
    if tier.value in {t.lower() for t in scope.pandascore_tiers}:
        return True
    ids = [team.id for team in ps.team_opponents() if team.id is not None]
    ranks = session.scalars(select(Team.hltv_rank).where(Team.pandascore_id.in_(ids)))
    return any(rank is not None and 0 < rank <= scope.hltv_top_n for rank in ranks)


def _find_or_create_match(
    session: Session,
    run: _Run,
    ps: PSMatch,
    existing: Match | None,
    team_a: Team,
    team_b: Team,
    tracker: ChangeTracker,
) -> tuple[Match, bool, bool]:
    """Returns (match, created, linked): by PandaScore id, else an unclaimed row of the same teams."""
    if existing is not None:
        return existing, False, False
    window = run.options.match_link_window_hours
    times = [t for t in (ps.kickoff, ps.original_scheduled_at, ps.begin_at) if t is not None]
    for around in dict.fromkeys(times):
        candidate = _link_candidate(session, team_a.id, team_b.id, around, window)
        if candidate is not None:
            candidate.pandascore_id = ps.id
            tracker.touch("Match.pandascore_id")
            run.count("linked")
            return candidate, False, True
    match = Match(
        team1_id=team_a.id,
        team2_id=team_b.id,
        scheduled_at=ps.kickoff,
        status=map_match_status(ps.status),
        pandascore_id=ps.id,
        tier=Tier.UNRANKED,
        in_scope=False,
        extra={},
    )
    session.add(match)
    session.flush()
    return match, True, False


def _link_candidate(
    session: Session, team_a_id: int, team_b_id: int, around: datetime, window_hours: float
) -> Match | None:
    """A match of these two teams near ``around`` that no PandaScore match claimed yet."""
    candidate = find_match(session, team_a_id, team_b_id, around, window_hours=window_hours)
    if candidate is None or candidate.pandascore_id is None:
        return candidate
    # The closest row belongs to another PandaScore match (two meetings on one day): look further.
    lo, hi = around - timedelta(hours=window_hours), around + timedelta(hours=window_hours)
    rows = session.scalars(
        select(Match).where(
            or_(
                and_(Match.team1_id == team_a_id, Match.team2_id == team_b_id),
                and_(Match.team1_id == team_b_id, Match.team2_id == team_a_id),
            ),
            Match.pandascore_id.is_(None),
            Match.scheduled_at >= lo,
            Match.scheduled_at <= hi,
        )
    )
    free = [m for m in rows if m.scheduled_at is not None]
    if not free:
        return None
    return min(free, key=lambda m: abs((m.scheduled_at - around).total_seconds()))  # type: ignore[operator]


def _replace_teams(
    session: Session, match: Match, team_a: Team, team_b: Team, tracker: ChangeTracker
) -> None:
    """PandaScore replaced a participant of a match only it knows: follow it and drop stale children."""
    for lineup in list(session.scalars(select(MatchLineup).where(MatchLineup.match_id == match.id))):
        if lineup.team_id not in (team_a.id, team_b.id):
            session.delete(lineup)
    for row in list(session.scalars(select(MapPlayed).where(MapPlayed.match_id == match.id))):
        session.delete(row)
    match.team1_id, match.team2_id = team_a.id, team_b.id
    match.team1_score = match.team2_score = match.winner_team_id = None
    tracker.touch("Match.teams")
    session.flush()
    session.expire(match, ["maps", "lineups"])


def _merge_status(
    current: MatchStatus | None, new: MatchStatus, *, created: bool, hltv_linked: bool
) -> MatchStatus:
    """PandaScore's status wins, except that it never re-opens a match HLTV already saw finished."""
    if created or current is None:
        return new
    if new is MatchStatus.UNKNOWN:
        return current
    if hltv_linked and current is MatchStatus.FINISHED and new is not MatchStatus.FINISHED:
        return current
    return new


def _hltv_has_final_result(match: Match) -> bool:
    hltv = extra_section(match, _HLTV_KEY)
    return (
        match.hltv_id is not None
        and hltv.get("status") == "finished"
        and match.team1_score is not None
        and match.team2_score is not None
    )


def _apply_match_fields(run: _Run, core: _Core, ps: PSMatch, tournament: Tournament | None) -> None:
    match, tracker = core.match, core.tracker
    hltv_linked = match.hltv_id is not None
    ps_status = map_match_status(ps.status)
    tracker.set(
        match,
        "status",
        _merge_status(match.status, ps_status, created=core.created, hltv_linked=hltv_linked),
    )
    extra: dict[str, Any] = {}
    kickoff = ps.kickoff
    if kickoff is not None and match.scheduled_at != kickoff:
        previous = match.scheduled_at
        tracker.set(match, "scheduled_at", kickoff)
        if previous is not None and not core.created and not core.linked:
            extra["previous_scheduled_at"] = previous.isoformat()
            run.count("rescheduled")
    fmt = map_match_format(ps.match_type, ps.number_of_games)
    if fmt is not None:
        tracker.set(match, "format", fmt)
    ps_tournament = ps.tournament
    if tournament is not None:
        tracker.set(match, "tournament_id", tournament.id)
        if ps_tournament is not None and ps_tournament.tier:
            tracker.set(match, "tier", map_tier(ps_tournament.tier))
        elif match.tier is Tier.UNRANKED and tournament.tier is not Tier.UNRANKED:
            tracker.set(match, "tier", tournament.tier)
    stage = stage_label(ps_tournament.name if ps_tournament else None, ps.name)
    if stage:
        tracker.set(match, "stage", stage)
    is_lan = map_is_lan(ps_tournament.type if ps_tournament else None)
    if is_lan is not None and (match.is_lan is None or not hltv_linked):
        tracker.set(match, "is_lan", is_lan)

    winner_id = core.db_team_id(ps.winner_id) if not ps.draw else None
    if ps_status is MatchStatus.FINISHED:
        score1, score2 = ps.score_for(core.ps_team1), ps.score_for(core.ps_team2)
        if winner_id is None and score1 is not None and score2 is not None and score1 != score2:
            winner_id = match.team1_id if score1 > score2 else match.team2_id
        if _hltv_has_final_result(match):
            if score1 is not None and (score1, score2) != (match.team1_score, match.team2_score):
                run.report.add_warning(
                    f"match {ps.id}: PandaScore result {score1}-{score2} differs from the HLTV result "
                    f"{match.team1_score}-{match.team2_score} (kept HLTV)"
                )
        else:
            if score1 is not None and score2 is not None:
                tracker.set(match, "team1_score", score1)
                tracker.set(match, "team2_score", score2)
            if winner_id is not None or ps.draw:
                tracker.set(match, "winner_team_id", winner_id)
    extra.update(
        {
            "name": ps.name,
            "slug": ps.slug,
            "status": ps.status,
            "match_type": ps.match_type,
            "number_of_games": ps.number_of_games,
            "modified_at": ps.modified_at.isoformat() if ps.modified_at else None,
            "begin_at": ps.begin_at.isoformat() if ps.begin_at else None,
            "end_at": ps.end_at.isoformat() if ps.end_at else None,
            "original_scheduled_at": (
                ps.original_scheduled_at.isoformat() if ps.original_scheduled_at else None
            ),
            "rescheduled": ps.rescheduled,
            "forfeit": ps.forfeit,
            "draw": ps.draw,
            "detailed_stats": ps.detailed_stats,
            "league_id": ps.league_id or (ps.league.id if ps.league else None),
            "serie_id": ps.serie_id or (ps.serie.id if ps.serie else None),
            "tournament_id": ps.tournament_id or (ps_tournament.id if ps_tournament else None),
            "forfeit_winner_team_id": winner_id if ps.forfeit else None,
            "games": _games_summary(core, ps.games) or None,
        }
    )
    tracker.merge_extra(match, extra)


def _games_summary(core: _Core, games: list[PSGame]) -> list[dict[str, Any]]:
    """Per-map outcome as far as the match payload knows it (no map names there)."""
    out = [
        {
            "position": game.position,
            "id": game.id,
            "status": game.status,
            "forfeit": bool(game.forfeit),
            "winner_team_id": core.db_team_id(game.winner_team_id),
        }
        for game in games
        if game.position is not None
    ]
    return sorted(out, key=lambda g: g["position"])


# ============================================================================================ details


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "unknown"


def _wants_games(run: _Run, core: _Core, ps: PSMatch, force: bool) -> bool:
    mode = run.options.game_details
    if run.stop or run.games_unavailable or (mode == "never" and not force):
        return False
    if map_match_status(ps.status) is not MatchStatus.FINISHED:
        return False
    if not force and mode == "in_scope" and not core.match.in_scope:
        return False
    if ps.games and not any(map_game_status(g.status) is MatchStatus.FINISHED for g in ps.games):
        return False
    return extra_section(core.match).get("games_synced_for") != _iso(ps.modified_at)


def _fetch_games(run: _Run, ps: PSMatch) -> list[PSGame] | None:
    what = f"match {ps.id}: games"
    assert ps.id is not None
    try:
        games = run.client.games(ps.id)
    except PandaScorePlanError:
        run.games_unavailable = True
        run.report.add_warning(
            "game details (map names, rounds, player lines) are not part of this PandaScore plan; "
            "maps are not synced from PandaScore"
        )
        return None
    except PandaScoreNotFoundError:
        run.report.add_warning(f"{what}: not found")
        return None
    except PandaScoreError as exc:
        run.api_failure(what, exc)
        return None
    run.count("game_calls")
    return games


def _lineup_counts(session: Session, match: Match) -> dict[int, int]:
    counts: dict[int, int] = {}
    for lineup in session.scalars(select(MatchLineup).where(MatchLineup.match_id == match.id)):
        counts[lineup.team_id] = counts.get(lineup.team_id, 0) + 1
    return counts


def _hltv_has_lineups(session: Session, match: Match) -> bool:
    if extra_section(match, _HLTV_KEY).get("has_lineups"):
        return True
    foreign = session.scalar(
        select(MatchLineup.id).where(MatchLineup.match_id == match.id, MatchLineup.source != SOURCE).limit(1)
    )
    return foreign is not None


def _has_player_lines(games: list[PSGame] | None) -> bool:
    return bool(games) and any(
        line.player is not None and line.team is not None for game in games or [] for line in game.players
    )


def _wants_rosters(
    session: Session, run: _Run, core: _Core, ps: PSMatch, games: list[PSGame] | None, force: bool
) -> bool:
    mode = run.options.rosters
    match = core.match
    if run.stop or run.rosters_unavailable or (mode == "never" and not force):
        return False
    if ps.tournament is None or ps.tournament.id is None:
        return False
    if not force and mode == "in_scope" and not match.in_scope:
        return False
    if match.status is MatchStatus.CANCELED or _has_player_lines(games):
        return False
    if _hltv_has_lineups(session, match):
        return False  # HLTV publishes the real lineup; an expected roster adds nothing
    if match.status is MatchStatus.FINISHED:
        counts = _lineup_counts(session, match)
        size = run.options.lineup_size
        if all(counts.get(team_id, 0) >= size for team_id in (match.team1_id, match.team2_id) if team_id):
            return False
    return True


def _tournament_rosters(run: _Run, ps: PSMatch) -> dict[int, list[PSPlayer]] | None:
    """Rosters of the match's tournament stage keyed by PandaScore team id (one API call per stage)."""
    assert ps.tournament is not None
    assert ps.tournament.id is not None
    tournament_id = ps.tournament.id
    if tournament_id in run.rosters:
        return run.rosters[tournament_id]
    what = f"tournament {tournament_id}: rosters"
    result: dict[int, list[PSPlayer]] | None = None
    try:
        payload = run.client.tournament_rosters(tournament_id)
    except PandaScorePlanError:
        run.rosters_unavailable = True
        run.report.add_warning("tournament rosters are not part of this PandaScore plan; no lineups")
    except PandaScoreNotFoundError:
        run.report.add_warning(f"{what}: not found")
    except PandaScoreError as exc:
        run.api_failure(what, exc)
    else:
        run.count("roster_calls")
        result = {team.id: list(team.players) for team in payload.rosters if team.id is not None}
    run.rosters[tournament_id] = result
    return result


def _persist_details(
    session: Session,
    run: _Run,
    core: _Core,
    ps: PSMatch,
    games: list[PSGame] | None,
    rosters: dict[int, list[PSPlayer]] | None,
) -> None:
    match = core.match
    player_ids: dict[int, int] = {}
    if games is not None:
        rows = _sync_maps(session, run, core, ps, games)
        if _has_player_lines(games):
            player_ids = _sync_lineups(session, run, core, ps, _participants(games), actual=True)
            if run.options.write_player_stats:
                _sync_player_stats(session, core, games, rows, player_ids)
        if _games_final(run, ps, games):
            core.tracker.merge_extra(match, {"games_synced_for": _iso(ps.modified_at)})
    if rosters is not None and not player_ids:
        _sync_lineups(session, run, core, ps, rosters, actual=False)
    elif games is None and match.status in _OPEN_STATUSES:
        _prune_ambiguous_lineups(session, run, core)
    session.flush()


def _games_final(run: _Run, ps: PSMatch, games: list[PSGame]) -> bool:
    """True when the game data will not change any more, so it need not be fetched again."""
    finished = [g for g in games if map_game_status(g.status) is MatchStatus.FINISHED]
    if all(g.complete or g.detailed_stats is False for g in finished):
        return True
    ended = ps.end_at or ps.kickoff
    return ended is not None and run.now - ended > timedelta(hours=run.options.games_final_after_hours)


def _hltv_owns(row: MapPlayed) -> bool:
    return row.hltv_mapstats_id is not None or _HLTV_KEY in (row.extra or {})


def _round_history(core: _Core, game: PSGame) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """Round list in our orientation + side statistics, or None when the history is unusable."""
    rounds = sorted((r for r in game.rounds if r.round is not None), key=lambda r: r.round or 0)
    if not rounds:
        return None
    history: list[dict[str, Any]] = []
    wins = {(slot, side): 0 for slot in TeamSlot for side in Side}
    for entry in rounds:
        if entry.ct == core.ps_team1 and entry.terrorists == core.ps_team2:
            team1_side = Side.CT
        elif entry.terrorists == core.ps_team1 and entry.ct == core.ps_team2:
            team1_side = Side.T
        else:
            return None
        if entry.winner_team == core.ps_team1:
            winner = TeamSlot.TEAM1
        elif entry.winner_team == core.ps_team2:
            winner = TeamSlot.TEAM2
        elif entry.winner_side in ("ct", "terrorists"):
            ct_won = entry.winner_side == "ct"
            winner = TeamSlot.TEAM1 if (team1_side is Side.CT) == ct_won else TeamSlot.TEAM2
        else:
            return None
        winner_side = team1_side if winner is TeamSlot.TEAM1 else team1_side.other
        wins[(winner, winner_side)] += 1
        outcome = (entry.outcome or "").lower()
        history.append(
            {
                "round": entry.round,
                "team1_side": team1_side.value,
                "winner": winner.value,
                "end_reason": map_round_outcome(entry.outcome).value,
                "bomb_planted": (
                    True
                    if outcome in ("exploded", "defused", "planted_eliminated")
                    else (False if outcome in ("eliminated", "timeout") else None)
                ),
            }
        )
    numbers = [h["round"] for h in history]
    if numbers != list(range(1, len(numbers) + 1)):
        return None
    stats = {
        "team1_start_side": Side(history[0]["team1_side"]),
        "team1_ct_rounds": wins[(TeamSlot.TEAM1, Side.CT)],
        "team1_t_rounds": wins[(TeamSlot.TEAM1, Side.T)],
        "team2_ct_rounds": wins[(TeamSlot.TEAM2, Side.CT)],
        "team2_t_rounds": wins[(TeamSlot.TEAM2, Side.T)],
    }
    return history, stats


def _sync_maps(
    session: Session, run: _Run, core: _Core, ps: PSMatch, games: list[PSGame]
) -> dict[int, MapPlayed]:
    """Upsert ``maps_played`` rows by map number; returns {PandaScore game id: row}."""
    match, tracker = core.match, core.tracker
    existing = {
        row.map_number: row
        for row in session.scalars(select(MapPlayed).where(MapPlayed.match_id == match.id))
    }
    regulation = regulation_rounds(ps)
    by_game: dict[int, MapPlayed] = {}
    for game in games:
        status = map_game_status(game.status)
        if game.position is None or game.forfeit or status not in (MatchStatus.FINISHED, MatchStatus.LIVE):
            continue  # unplayed, walkover or not started: no map row (same rule as the HLTV sync)
        name = game_map_name(game)
        row = existing.get(game.position)
        if row is None:
            if name is None:
                run.count("maps_without_name")
                continue
            row = MapPlayed(match_id=match.id, map_number=game.position, map_name=name, extra={})
            session.add(row)
            existing[game.position] = row
            tracker.touch("MapPlayed.created")
        owned_by_hltv = _hltv_owns(row)
        if name is not None and row.map_name != name:
            if owned_by_hltv:
                run.report.add_warning(
                    f"match {ps.id} map {game.position}: PandaScore says {name!r}, HLTV {row.map_name!r} "
                    "(kept HLTV)"
                )
            else:
                tracker.set(row, "map_name", name)
        finished = status is MatchStatus.FINISHED
        rounds1, rounds2 = game.rounds_for(core.ps_team1), game.rounds_for(core.ps_team2)
        if rounds1 is None or rounds2 is None or (finished and rounds1 + rounds2 == 0):
            rounds1 = rounds2 = None  # no round data for this game (see the module docstring)
        values: dict[str, Any] = {
            "status": status,
            "team1_rounds": rounds1,
            "team2_rounds": rounds2,
            "winner_team_id": core.db_team_id(game.winner_team_id) if finished else None,
            "played_at": game.begin_at or match.scheduled_at,
        }
        if finished and rounds1 is not None and rounds2 is not None:
            values["overtime_rounds"] = overtime_rounds(rounds1, rounds2, regulation)
        history = _round_history(core, game) if finished else None
        rounds_complete = False
        if history is not None and rounds1 is not None and rounds2 is not None:
            rounds_complete = len(history[0]) == rounds1 + rounds2
            if rounds_complete:
                values.update(history[1])
        for attr, value in values.items():
            if value is None:
                continue
            if owned_by_hltv:
                if attr in ("status", "overtime_rounds"):
                    continue  # non-nullable columns HLTV already decided
                tracker.fill(row, attr, value)
            else:
                tracker.set(row, attr, value)
        extra: dict[str, Any] = {
            "game_id": game.id,
            "status": game.status,
            "length_s": game.length,
            "complete": game.complete,
            "detailed_stats": game.detailed_stats,
            "end_at": game.end_at.isoformat() if game.end_at else None,
        }
        if run.options.store_round_history and history is not None and rounds_complete:
            extra["rounds"] = history[0]
            extra["rounds_complete"] = True
        tracker.merge_extra(row, extra)
        if game.id is not None:
            by_game[game.id] = row
    session.flush()
    session.expire(match, ["maps"])
    return by_game


def _participants(games: list[PSGame]) -> dict[int, list[PSPlayer]]:
    """Players who actually played, per PandaScore team id (union over all maps)."""
    out: dict[int, dict[int, PSPlayer]] = {}
    for game in games:
        for line in game.players:
            if line.player is None or line.team is None or line.team.id is None or line.player.id is None:
                continue
            out.setdefault(line.team.id, {}).setdefault(line.player.id, line.player)
    return {team_id: list(players.values()) for team_id, players in out.items()}


def _sync_lineups(
    session: Session,
    run: _Run,
    core: _Core,
    ps: PSMatch,
    rosters: dict[int, list[PSPlayer]],
    *,
    actual: bool,
) -> dict[int, int]:
    """Write ``match_lineups`` rows with source=PANDASCORE; returns {PandaScore player id: DB player id}.

    ``actual`` = the players come from the match's own stat lines (they did play). Otherwise it is the
    tournament roster, used as the *expected* lineup only when it lists exactly ``lineup_size`` (active)
    players. Rows written by other sources are never touched; a team that has such rows gets no new
    PandaScore rows (identity mismatches between sources would otherwise produce a sixth player).
    """
    match, tracker = core.match, core.tracker
    size = run.options.lineup_size
    kickoff = match.scheduled_at
    recent = kickoff is None or kickoff >= run.now - timedelta(days=run.options.current_team_window_days)
    seen_at = min(kickoff, run.now) if kickoff is not None else run.now
    all_rows = list(session.scalars(select(MatchLineup).where(MatchLineup.match_id == match.id)))
    hltv_has_lineups = _hltv_has_lineups(session, match)
    resolved: dict[int, int] = {}
    for ps_team_id, team in core.team_by_ps.items():
        roster = rosters.get(ps_team_id)
        if not roster:
            continue
        team_rows = [row for row in all_rows if row.team_id == team.id]
        candidates = [p for p in (session.get(Player, row.player_id) for row in team_rows) if p is not None]
        wanted: dict[int, PSPlayer] = {}
        for ps_player in roster:
            player = resolve_ps_player(
                session,
                ps_player,
                team,
                seen_at=seen_at,
                move_team=recent,
                same_match_candidates=candidates,
            )
            if player is None or ps_player.id is None:
                continue
            resolved[ps_player.id] = player.id
            wanted.setdefault(player.id, ps_player)
        if actual:
            lineup_ids = set(wanted)
        else:
            if hltv_has_lineups:
                continue
            active = {pid for pid, p in wanted.items() if p.active is not False}
            if len(active) == size:
                lineup_ids = active
            elif len(wanted) == size:
                lineup_ids = set(wanted)
            else:
                run.count("ambiguous_rosters")
                continue
        foreign = [row for row in team_rows if row.source != SOURCE]
        taken = {row.player_id for row in all_rows}
        for row in team_rows:
            if row.source == SOURCE and row.player_id not in lineup_ids:
                session.delete(row)
                all_rows.remove(row)
                tracker.touch("MatchLineup.deleted")
        if foreign:
            continue
        for player_id in sorted(lineup_ids - taken):
            row = MatchLineup(
                match_id=match.id, team_id=team.id, player_id=player_id, is_standin=False, source=SOURCE
            )
            session.add(row)
            all_rows.append(row)
            tracker.touch("MatchLineup.created")
    session.flush()
    session.expire(match, ["lineups"])
    return resolved


def _prune_ambiguous_lineups(session: Session, run: _Run, core: _Core) -> None:
    """Before the match: drop PandaScore rows of a team whose lineup HLTV has (partly) taken over.

    HLTV's lineup sync adds the players it lists but leaves rows of other sources alone, so a stand-in
    on HLTV turns a five-man PandaScore lineup into six rows. Which PandaScore rows HLTV confirmed
    cannot be told apart, hence all of them go; HLTV's next sync of the match re-adds the confirmed
    players under its own source.
    """
    match = core.match
    size = run.options.lineup_size
    rows = list(session.scalars(select(MatchLineup).where(MatchLineup.match_id == match.id)))
    for team_id in (match.team1_id, match.team2_id):
        team_rows = [row for row in rows if row.team_id == team_id]
        ours = [row for row in team_rows if row.source == SOURCE]
        if len(team_rows) > size and ours and len(ours) < len(team_rows):
            for row in ours:
                session.delete(row)
            core.tracker.touch("MatchLineup.deleted")
            run.count("lineups_pruned")
    session.flush()
    session.expire(match, ["lineups"])


def _sync_player_stats(
    session: Session,
    core: _Core,
    games: list[PSGame],
    rows: dict[int, MapPlayed],
    player_ids: dict[int, int],
) -> None:
    """Optional ``player_map_stats`` rows (source=PANDASCORE) from the per-game player lines."""
    tracker = core.tracker
    for game in games:
        row = rows.get(game.id) if game.id is not None else None
        if row is None:
            continue
        total_rounds = (
            row.team1_rounds + row.team2_rounds
            if row.team1_rounds is not None and row.team2_rounds is not None
            else None
        )
        for line in game.players:
            ps_player_id = line.pandascore_player_id
            player_id = player_ids.get(ps_player_id) if ps_player_id is not None else None
            team_id = core.db_team_id(line.team.id if line.team else None)
            if player_id is None or team_id is None or line.kills is None or line.deaths is None:
                continue
            stats = session.scalar(
                select(PlayerMapStats).where(
                    PlayerMapStats.map_played_id == row.id,
                    PlayerMapStats.player_id == player_id,
                    PlayerMapStats.source == SOURCE,
                )
            )
            if stats is None:
                stats = PlayerMapStats(
                    map_played_id=row.id,
                    player_id=player_id,
                    team_id=team_id,
                    source=SOURCE,
                    kills=line.kills,
                    deaths=line.deaths,
                    assists=line.assists or 0,
                    extra={},
                )
                session.add(stats)
                tracker.touch("PlayerMapStats.created")
            for attr, value in (
                ("team_id", team_id),
                ("kills", line.kills),
                ("deaths", line.deaths),
                ("assists", line.assists),
                ("headshots", line.headshots),
                ("rounds_played", total_rounds),
                ("adr", line.adr),
                ("kast", line.kast),
                ("rating", line.rating),
                ("flash_assists", int(line.flash_assists) if line.flash_assists is not None else None),
            ):
                if value is not None:
                    tracker.set(stats, attr, value)
    session.flush()


# ============================================================================================ reconcile


def _reconcile_missing(session: Session, run: _Run, hours_ahead: int) -> None:
    """Re-fetch open PandaScore matches near now that the upcoming / running lists no longer contain."""
    opts = run.options
    lo = run.now - timedelta(hours=opts.reconcile_lookback_hours)
    hi = run.now + timedelta(hours=hours_ahead)
    query = (
        select(Match)
        .where(
            Match.pandascore_id.is_not(None),
            Match.status.in_(list(_OPEN_STATUSES)),
            Match.scheduled_at >= lo,
            Match.scheduled_at <= hi,
        )
        .order_by(Match.in_scope.desc(), Match.scheduled_at)
    )
    stale = [m for m in session.scalars(query) if m.pandascore_id not in run.seen_match_ids]
    for match in stale[: max(0, opts.max_reconcile)]:
        if run.stop:
            return
        ps_id = match.pandascore_id
        assert ps_id is not None
        what = f"match {ps_id}"
        try:
            ps_match = run.client.match(ps_id)
        except PandaScoreNotFoundError:
            run.count("reconciled")
            if match.hltv_id is None:
                match.status = MatchStatus.CANCELED  # PandaScore deleted the match it alone knew
                ChangeTracker().merge_extra(match, {"deleted": True})
                match.last_synced_at = run.now
                run.report.updated += 1
                run.report.add_warning(f"{what}: no longer exists on PandaScore; marked canceled")
            else:
                run.report.add_warning(f"{what}: no longer exists on PandaScore (HLTV-linked; left as is)")
            continue
        except PandaScoreError as exc:
            run.report.failed += 1
            run.api_failure(what, exc)
            continue
        run.count("reconciled")
        _process_match(session, run, ps_match)
