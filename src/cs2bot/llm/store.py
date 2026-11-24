"""Persistence helpers for the context layer (flags, triage verdicts, news selection).

All functions ``flush()`` but never ``commit()``: the caller owns the transaction (use
``cs2bot.db.session.session_scope``). Writes are idempotent - repeating a call with the same data
leaves the database in the same state.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from cs2bot.db.models import ContextFlagRow, Match, NewsItem, Team
from cs2bot.domain.context import ContextFlag, NewsItemIn
from cs2bot.domain.enums import TeamSlot
from cs2bot.domain.match import MatchContext
from cs2bot.llm.schemas import TriageItem, TriageLabel
from cs2bot.llm.tuning import LLMTuning
from cs2bot.utils.logging import get_logger
from cs2bot.utils.time import ensure_utc, utcnow

log = get_logger(__name__)

EXTRA_KEY = "llm"  # our namespace inside the shared ``extra`` JSON columns
# ``context_flags.model`` value reserved for flags the owner sets by hand (e.g. a manual no-bet veto).
# :func:`persist_flags` never deactivates such rows; everything else in the table is ours to replace.
MANUAL_FLAG_MODEL = "manual"
_DEFAULT_TUNING = LLMTuning()
_UNTRIAGED_RANK = 0.5  # untriaged items sort between clearly relevant and marginal triaged ones


def merge_llm_extra(current: dict[str, Any] | None, updates: dict[str, Any]) -> dict[str, Any]:
    """Return a NEW ``extra`` dict with ``updates`` merged into its ``"llm"`` section.

    JSON columns do not track in-place mutation, so the result must be assigned back
    (``row.extra = merge_llm_extra(row.extra, {...})``). Other packages' sections are untouched.
    """
    base = dict(current or {})
    section = dict(base.get(EXTRA_KEY) or {})
    section.update(updates)
    base[EXTRA_KEY] = section
    return base


def llm_extra(row_extra: dict[str, Any] | None) -> dict[str, Any]:
    """The ``"llm"`` section of an ``extra`` column (empty dict when absent or malformed)."""
    section = (row_extra or {}).get(EXTRA_KEY)
    return dict(section) if isinstance(section, dict) else {}


# ---------------------------------------------------------------------------------------------
# Context flags
# ---------------------------------------------------------------------------------------------


def _team_id_for(slot: TeamSlot | None, team_ids: tuple[int, int]) -> int | None:
    if slot is TeamSlot.TEAM1:
        return team_ids[0]
    if slot is TeamSlot.TEAM2:
        return team_ids[1]
    return None


def _flag_signature(
    flag_type: str,
    team_id: int | None,
    severity: float,
    confidence: float,
    logit_shift: float,
    veto: bool,
    rationale: str,
    evidence: Iterable[Any],
    expires_at: datetime | None,
) -> tuple[Any, ...]:
    return (
        flag_type,
        team_id,
        round(float(severity), 6),
        round(float(confidence), 6),
        round(float(logit_shift), 6),
        bool(veto),
        rationale,
        tuple(str(e) for e in evidence),
        ensure_utc(expires_at).replace(microsecond=0) if expires_at is not None else None,
    )


def persist_flags(
    session: Session,
    match_id: int,
    flags: Sequence[ContextFlag],
    *,
    model: str | None,
    team_ids: tuple[int, int],
    now: datetime | None = None,
) -> list[ContextFlagRow]:
    """Replace the match's active context flags with ``flags``.

    Previously active rows are deactivated (kept for the audit trail) and one new active row is
    inserted per flag, with ``team_id`` resolved from the flag's ``TeamSlot`` through
    ``team_ids = (matches.team1_id, matches.team2_id)``. ``logit_shift`` stays in team1's perspective.

    Rows whose ``model`` is :data:`MANUAL_FLAG_MODEL` (flags the owner set by hand) are left alone:
    an analysis never switches off a manual veto.

    Idempotent: when the active rows already describe exactly the same flags (same content, same
    model), nothing is written and the existing rows are returned. Passing an empty list deactivates
    everything - only do that after a *successful* analysis that found nothing.
    """
    current = ensure_utc(now) if now is not None else utcnow()
    model_name = model[:64] if model else None
    if model_name == MANUAL_FLAG_MODEL:
        raise ValueError(f"model name {MANUAL_FLAG_MODEL!r} is reserved for flags set by hand")
    active = [
        row
        for row in session.scalars(
            select(ContextFlagRow)
            .where(ContextFlagRow.match_id == match_id, ContextFlagRow.active.is_(True))
            .order_by(ContextFlagRow.id)
        )
        if row.model != MANUAL_FLAG_MODEL  # the owner's own flags are never replaced by an analysis
    ]
    wanted = [
        _flag_signature(
            flag.flag_type.value,
            _team_id_for(flag.team, team_ids),
            flag.severity,
            flag.confidence,
            flag.logit_shift,
            flag.veto,
            flag.rationale,
            flag.evidence,
            flag.expires_at,
        )
        for flag in flags
    ]
    existing = [
        _flag_signature(
            row.flag_type.value,
            row.team_id,
            row.severity,
            row.confidence,
            row.logit_shift,
            row.veto,
            row.rationale,
            row.evidence or [],
            row.expires_at,
        )
        for row in active
    ]
    if sorted(map(repr, wanted)) == sorted(map(repr, existing)) and all(
        row.model == model_name for row in active
    ):
        return active
    for row in active:
        row.active = False
    rows: list[ContextFlagRow] = []
    for flag in flags:
        row = ContextFlagRow(
            match_id=match_id,
            team_id=_team_id_for(flag.team, team_ids),
            flag_type=flag.flag_type,
            severity=float(flag.severity),
            confidence=float(flag.confidence),
            logit_shift=float(flag.logit_shift),
            veto=bool(flag.veto),
            rationale=flag.rationale,
            evidence=[str(e) for e in flag.evidence],
            model=model_name,
            active=True,
            created_at=current,
            expires_at=ensure_utc(flag.expires_at) if flag.expires_at is not None else None,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def deactivate_flags_after_team_change(session: Session, match_id: int) -> int:
    """Neutralise the analysis flags of a match whose participants a source just replaced.

    ``logit_shift`` is stored from the perspective of the team that was ``team1`` when the flag was
    written. After a participant is replaced, a team that stays in the match can move to the other
    slot (PandaScore lists the new pairing in its own order), so a stored shift would push the
    probability the wrong way; the flags also describe a pairing that no longer exists. Plain flags
    are switched off; veto flags stay active with ``logit_shift = 0`` (a no-bet veto is
    orientation-free and blocking is always safe - the next analysis replaces it). Flags the owner set
    by hand (``MANUAL_FLAG_MODEL``) are kept. Returns the number of rows changed; flushes, never
    commits. Final review (Lens B).
    """
    rows = [
        row
        for row in session.scalars(
            select(ContextFlagRow).where(ContextFlagRow.match_id == match_id, ContextFlagRow.active.is_(True))
        )
        if row.model != MANUAL_FLAG_MODEL
    ]
    changed = 0
    for row in rows:
        if row.veto:
            if row.logit_shift != 0.0:
                row.logit_shift = 0.0
                changed += 1
        else:
            row.active = False
            changed += 1
    if changed:
        session.flush()
        log.info("match %s: %d context flags neutralised after a team change", match_id, changed)
    return changed


def load_active_flags(session: Session, match_id: int, *, now: datetime) -> list[ContextFlag]:
    """Active, unexpired flags of a match as domain objects (oldest first).

    A flag is expired when ``expires_at < now`` (same rule as ``combine_flags``). ``team_id`` is
    mapped back to a ``TeamSlot`` through the match row; a team id that is no longer one of the
    match's two teams (the match was re-linked) yields ``team=None`` and ``logit_shift=0`` (its veto
    bit is kept: blocking is always safe). See also :func:`deactivate_flags_after_team_change`.
    """
    current = ensure_utc(now)
    match = session.get(Match, match_id)
    team1_id = match.team1_id if match is not None else None
    team2_id = match.team2_id if match is not None else None
    rows = session.scalars(
        select(ContextFlagRow)
        .where(ContextFlagRow.match_id == match_id, ContextFlagRow.active.is_(True))
        .order_by(ContextFlagRow.id)
    )
    flags: list[ContextFlag] = []
    for row in rows:
        if row.expires_at is not None and row.expires_at < current:
            continue
        slot: TeamSlot | None = None
        foreign_team = False
        if row.team_id is not None:
            if row.team_id == team1_id:
                slot = TeamSlot.TEAM1
            elif row.team_id == team2_id:
                slot = TeamSlot.TEAM2
            else:
                log.warning(
                    "context flag %s refers to team %s, not a team of match %s: its adjustment is ignored",
                    row.id,
                    row.team_id,
                    match_id,
                )
                foreign_team = True
        try:
            flags.append(
                ContextFlag(
                    flag_type=row.flag_type,
                    team=slot,
                    severity=min(1.0, max(0.0, row.severity)),
                    confidence=min(1.0, max(0.0, row.confidence)),
                    # final review: a flag about a team that is no longer in the match (a source replaced
                    # a participant) must not move the new pairing's probability; a veto still blocks
                    logit_shift=0.0 if foreign_team else row.logit_shift,
                    veto=row.veto,
                    rationale=row.rationale,
                    evidence=[str(e) for e in (row.evidence or [])],
                    expires_at=row.expires_at,
                )
            )
        except ValueError:  # one corrupted row must not hide the others
            log.warning("context flag row %s is malformed and was skipped", row.id)
    return flags


# ---------------------------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------------------------


def store_triage(
    session: Session,
    news_item_id: int,
    item: TriageItem,
    *,
    now: datetime | None = None,
    model: str | None = None,
) -> bool:
    """Write a triage verdict onto its ``news_items`` row. Returns False when the row does not exist.

    Sets ``triage_label`` / ``relevance`` / ``triaged_at``, merges the verdict's team ids into
    ``team_ids`` (union with what the scraper detected; ids that are not in ``teams`` are ignored) and
    keeps the one-line summary under ``extra["llm"]["triage"]``. The verdict's team ids are stored
    there as well: the HLTV sync rewrites ``team_ids`` from its own detection when it refreshes an
    article, and :func:`select_news_for_match` reads both places.
    """
    row = session.get(NewsItem, news_item_id)
    if row is None:
        log.warning("triage verdict for unknown news item %s ignored", news_item_id)
        return False
    current = ensure_utc(now) if now is not None else utcnow()
    row.triage_label = item.label.value
    row.relevance = float(item.relevance)
    row.triaged_at = current
    suggested = {int(t) for t in item.team_ids}
    known: set[int] = set()
    original = _int_set(row.team_ids)
    previous = llm_extra(row.extra).get("triage")
    # Ids that only the verdict put into ``team_ids`` (kept across repeated calls), so that a replay at
    # an instant before ``triaged_at`` can take them out again (see select_news_for_match).
    added = _int_set(previous.get("added_team_ids")) if isinstance(previous, dict) else set()
    if suggested:
        known = set(session.scalars(select(Team.id).where(Team.id.in_(suggested))))
        merged = sorted(original | known)
        if merged != list(row.team_ids or []):
            row.team_ids = merged
    added |= known - original
    row.extra = merge_llm_extra(
        row.extra,
        {
            "triage": {
                "summary": item.summary,
                "team_names": list(item.team_names),
                "team_ids": sorted(known),
                "added_team_ids": sorted(added),
                "model": model,
            }
        },
    )
    session.flush()
    return True


# ---------------------------------------------------------------------------------------------
# News selection
# ---------------------------------------------------------------------------------------------


def news_row_to_input(row: NewsItem) -> NewsItemIn:
    """Convert a ``news_items`` row into the domain input type (body falls back to the HLTV lead)."""
    body = row.body
    if not body:
        hltv = (row.extra or {}).get("hltv")
        lead = hltv.get("lead") if isinstance(hltv, dict) else None
        body = lead if isinstance(lead, str) and lead.strip() else None
    return NewsItemIn(
        source=row.source,
        url=row.url,
        title=row.title,
        body=body,
        published_at=row.published_at,
        team_ids=sorted(_int_set(row.team_ids)),
        player_ids=sorted(_int_set(row.player_ids)),
    )


def _news_time(row: NewsItem) -> datetime | None:
    """When the item became known: its publication time, else when we ingested it."""
    return row.published_at or row.created_at


def select_news_for_match(
    session: Session,
    ctx: MatchContext,
    *,
    days: int = 14,
    limit: int = 25,
    now: datetime | None = None,
    min_relevance: float | None = None,
    include_untriaged: bool = True,
    max_global: int | None = None,
) -> list[NewsItemIn]:
    """News worth showing to the context model for ``ctx`` (oldest first, at most ``limit``).

    An item qualifies when it is

    * **recent** - published (or, without a publication time, ingested) within ``days`` before
      ``now`` and **not after** ``now`` (no look-ahead when ``now`` is a historical instant);
    * **about this match** - it mentions team1 / team2 (``team_ids``, or the teams a triage verdict
      attached) or one of the players in the context's lineups (``player_ids``);
    * **relevant** - triaged with a label other than ``irrelevant`` and ``relevance >= min_relevance``.
      Items that have not been triaged yet are included too (``include_untriaged``), so a triage
      outage cannot hide a fresh roster story from the context model. A verdict whose ``triaged_at``
      lies after ``now`` is ignored (the item counts as untriaged), so a historical ``now`` sees
      only what was known then.

    In addition, up to ``max_global`` recent ``meta_patch`` items (game updates / map-pool changes)
    are included although they mention neither team.

    When more than ``limit`` items qualify, the most relevant ones are kept (ties: newest first).
    Pass ``limit=settings.llm.max_news_items_per_match``.
    """
    current = ensure_utc(now) if now is not None else utcnow()
    since = current - timedelta(days=max(0, days))
    threshold = _DEFAULT_TUNING.min_relevance if min_relevance is None else min_relevance
    global_cap = _DEFAULT_TUNING.max_global_news if max_global is None else max(0, max_global)
    if limit <= 0:
        return []
    team_ids = {ctx.team1_id, ctx.team2_id}
    player_ids = {p.player_id for p in (*ctx.team1_lineup, *ctx.team2_lineup)}
    rows = session.scalars(
        select(NewsItem).where(
            or_(
                NewsItem.published_at >= since,
                and_(NewsItem.published_at.is_(None), NewsItem.created_at >= since),
            )
        )
    )
    specific: list[tuple[float, datetime, NewsItem]] = []
    general: list[tuple[float, datetime, NewsItem]] = []
    for row in rows:
        when = _news_time(row)
        if when is None or when > current or when < since:
            continue
        # A verdict written after ``now`` did not exist yet at ``now`` (historical replays): the item
        # is then treated exactly like an untriaged one, including the team ids the verdict attached.
        known_verdict = row.triage_label is not None and (
            row.triaged_at is None or ensure_utc(row.triaged_at) <= current
        )
        label = row.triage_label if known_verdict else None
        if label == TriageLabel.IRRELEVANT.value:
            continue
        triaged = label is not None
        relevance = float(row.relevance) if triaged and row.relevance is not None else 0.0
        if triaged and relevance < threshold:
            continue
        if not triaged and not include_untriaged:
            continue
        if triaged:
            mentioned = _mentioned_team_ids(row)
        elif row.triage_label is not None:  # verdict written after ``now``: without the ids it added
            mentioned = _int_set(row.team_ids) - _triage_added_team_ids(row)
        else:
            mentioned = _int_set(row.team_ids)
        mentions = bool(team_ids & mentioned) or bool(player_ids & _int_set(row.player_ids))
        rank = relevance if triaged else _UNTRIAGED_RANK
        if mentions:
            specific.append((rank, when, row))
        elif label == TriageLabel.META_PATCH.value:
            general.append((rank, when, row))
    general.sort(key=lambda entry: (entry[1], entry[2].id), reverse=True)  # newest first
    # Match-wide items get up to ``global_cap`` slots, but never crowd out news about the two teams
    # when ``limit`` is small: they then only fill what the team-specific items leave free (with
    # the default limit of 25 and cap of 3 the reservation always holds).
    general = general[: min(global_cap, max(limit - len(specific), limit // 4))]
    specific.sort(key=lambda entry: (entry[0], entry[1], entry[2].id), reverse=True)
    chosen = specific[: max(0, limit - len(general))] + general
    chosen.sort(key=lambda entry: (entry[1], entry[2].id))  # oldest first for the prompt
    return [news_row_to_input(row) for _, _, row in chosen]


def _mentioned_team_ids(row: NewsItem) -> set[int]:
    """Team ids on the row plus those the triage verdict attached (kept under ``extra["llm"]``)."""
    triage = llm_extra(row.extra).get("triage")
    from_triage = _int_set(triage.get("team_ids")) if isinstance(triage, dict) else set()
    return _int_set(row.team_ids) | from_triage


def _triage_added_team_ids(row: NewsItem) -> set[int]:
    """Team ids that only the triage verdict merged into ``team_ids`` (see :func:`store_triage`)."""
    triage = llm_extra(row.extra).get("triage")
    return _int_set(triage.get("added_team_ids")) if isinstance(triage, dict) else set()


def _int_set(values: Any) -> set[int]:
    if not isinstance(values, list | tuple | set):
        return set()
    return {int(v) for v in values if isinstance(v, int) and not isinstance(v, bool)}
