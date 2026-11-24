"""Prediction service: one upcoming match -> :class:`MatchPrediction` + priced outcomes in the DB.

For one match (:func:`predict_match`):

1. ``datasets.load_match_context`` (lineups, map pool, published veto steps, start sides);
2. data completeness (``datasets.data_completeness``), also written to ``matches.data_completeness``;
3. context flags: with an :class:`~cs2bot.llm.ContextAnalyzer`, ``llm.refresh_match_context`` (news ->
   Claude flags, cached); without one, the stored active flags. ``llm.combine_flags`` turns them into
   a bounded logit shift (the veto flag is read by the decision engine itself);
4. veto distribution from the veto model, conditioned on the published steps;
5. per-map estimates for every map still possible: the ensemble when it is fitted, else the ratings;
6. ``models.match_engine.build_match_prediction`` (round simulation, every market);
7. persistence: a ``match_analyses`` row (payload + rationale; older rows lose ``is_latest``) and one
   ``predictions`` row per currently quoted outcome (``odds.latest_quotes`` + ``engine.pricing``).

:func:`predict_upcoming` runs this for every in-scope scheduled match inside the betting window and
re-simulates only when something that changes the prediction changed (the *fingerprint*: model
version, format, lineups, published veto, start sides, context flags, data completeness) or the
prediction is older than ``max_age``. Otherwise only the ``predictions`` rows of newly quoted outcomes
are added (pricing only, no simulation). Predictions are never written for a match that has started.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from cs2bot import datasets
from cs2bot.config import Settings
from cs2bot.db.models import Match, MatchAnalysis, Prediction
from cs2bot.domain.context import ContextFlag
from cs2bot.domain.enums import MatchStatus, VetoAction
from cs2bot.domain.match import MatchContext
from cs2bot.domain.predictions import MapSequenceProbability, MapWinEstimate, MatchPrediction
from cs2bot.engine.pricing import price_quotes_report
from cs2bot.llm import combine_flags, load_active_flags, refresh_match_context, render_template_rationale
from cs2bot.models.match_engine import build_match_prediction, context_kwargs, engine_options
from cs2bot.odds import latest_quotes
from cs2bot.services.registry import ModelBundle
from cs2bot.utils.time import ensure_utc, utcnow

log = logging.getLogger(__name__)

#: Key of the bookkeeping entry in ``matches.extra``.
EXTRA_KEY = "prediction"
#: Key inside ``matches.extra["prediction"]`` set when the stored prediction no longer describes the match
#: (its inputs changed but no new prediction could be made: teams / lineups gone, an error). The betting
#: cycle never bets a match carrying it; the next successful prediction (or an unchanged fingerprint)
#: clears it. Final review: before it, a failed re-prediction left the old analysis "latest" and bettable
#: for up to ``max_prediction_age`` (8 h).
STALE_KEY = "stale"
#: Re-simulate a prediction older than this even when nothing visible changed (ratings move daily).
DEFAULT_MAX_AGE = timedelta(hours=6)


class AnalyzerLike:  # pragma: no cover - documentation only
    """Structural type of :class:`cs2bot.llm.ContextAnalyzer` used here (``refresh_match_context``,
    ``write_rationale_detailed``)."""


@dataclass
class PreparedMatch:
    """Everything known about a match before simulating it."""

    ctx: MatchContext
    flags: list[ContextFlag]
    logit_shift: float
    context_veto: bool
    completeness: float
    fingerprint: str
    flag_source: str


@dataclass
class MatchPredictOutcome:
    match_id: int
    status: str  # predicted / unchanged / skipped / failed
    reason: str = ""
    analysis_id: int | None = None
    outcomes_written: int = 0


@dataclass
class PredictReport:
    started_at: datetime
    model_version: str | None
    considered: int = 0
    predicted: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    outcomes_written: int = 0
    rating_updates: int = 0
    results: list[MatchPredictOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def items(self) -> int:
        return self.predicted + self.unchanged

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "model_version": self.model_version,
            "considered": self.considered,
            "predicted": self.predicted,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "failed": self.failed,
            "outcomes_written": self.outcomes_written,
            "rating_updates": self.rating_updates,
            "skipped_reasons": {r.match_id: r.reason for r in self.results if r.status == "skipped"},
            "errors": self.errors[:20],
        }


# ------------------------------------------------------------------------------------------ helpers


def _veto_complete(ctx: MatchContext) -> bool:
    """True when the published steps fix the maps to be played (all picks and the decider known)."""
    if not ctx.known_veto:
        return False
    if ctx.veto_complete:
        return True
    played = [s for s in ctx.known_veto if s.action in (VetoAction.PICK, VetoAction.LEFTOVER)]
    return len(played) >= ctx.format.n_maps


def _flag_signature(flag: ContextFlag) -> list[Any]:
    return [
        flag.flag_type.value,
        flag.team.value if flag.team else None,
        round(flag.logit_shift, 4),
        flag.veto,
        round(flag.severity, 3),
        round(flag.confidence, 3),
    ]


def compute_fingerprint(
    ctx: MatchContext, flags: Sequence[ContextFlag], completeness: float, model_version: str
) -> str:
    """Hash of every input that changes the prediction (see the module docstring)."""
    payload = {
        "model": model_version,
        "format": ctx.format.value,
        "teams": [ctx.team1_id, ctx.team2_id],
        "lineups": [
            sorted((p.player_id, p.is_standin) for p in ctx.team1_lineup),
            sorted((p.player_id, p.is_standin) for p in ctx.team2_lineup),
        ],
        "pool": sorted(ctx.map_pool),
        "veto": [
            (s.step, s.action.value, s.team.value if s.team else None, s.map_name) for s in ctx.known_veto
        ],
        "first_veto": ctx.first_veto_team.value if ctx.first_veto_team else None,
        "sides": sorted((k, v.value if v else None) for k, v in ctx.start_sides.items()),
        "flags": sorted(_flag_signature(f) for f in flags),
        "completeness": round(completeness, 3),
        "lan": ctx.is_lan,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32]


def prepare_match(
    session: Session,
    match_id: int,
    bundle: ModelBundle,
    settings: Settings,
    *,
    analyzer: Any | None = None,
    now: datetime | None = None,
) -> PreparedMatch:
    """Context, flags, completeness and fingerprint of one match (raises ``MatchNotFoundError``)."""
    now = ensure_utc(now or utcnow())
    ctx = datasets.load_match_context(
        session, match_id, settings=settings, now=now, include_context_flags=False
    )
    flags: list[ContextFlag]
    source = "db"
    if analyzer is not None:
        try:
            refresh = refresh_match_context(session, analyzer, ctx, now=now)
            flags = list(refresh.flags)
            source = refresh.source
        except Exception as exc:  # the LLM layer must never stop predictions
            log.warning("context refresh failed for match %s: %s", match_id, exc)
            flags = load_active_flags(session, match_id, now=now)
    else:
        flags = load_active_flags(session, match_id, now=now)
    ctx = ctx.model_copy(update={"context_flags": flags})
    shift, veto = combine_flags(flags, settings.llm, now=now)
    completeness = datasets.data_completeness(session, ctx, now=now)
    fingerprint = compute_fingerprint(ctx, flags, completeness, bundle.version)
    return PreparedMatch(
        ctx=ctx,
        flags=flags,
        logit_shift=shift,
        context_veto=veto,
        completeness=completeness,
        fingerprint=fingerprint,
        flag_source=source,
    )


def veto_distribution(
    bundle: ModelBundle, ctx: MatchContext, *, now: datetime
) -> tuple[list[MapSequenceProbability], bool]:
    """Veto sequences (conditioned on the published steps) and whether the maps are already known."""
    sequences = bundle.veto.predict_sequences(
        ctx.team1_id,
        ctx.team2_id,
        ctx.format,
        ctx.map_pool,
        first_veto=ctx.first_veto_team,
        known_steps=ctx.known_veto,
        as_of=now,
        map_advantage=bundle.ratings.map_advantage,
    )
    return sequences, _veto_complete(ctx) or len(sequences) == 1


def map_estimates(
    session: Session,
    bundle: ModelBundle,
    ctx: MatchContext,
    sequences: Sequence[MapSequenceProbability],
    *,
    now: datetime,
) -> tuple[dict[str, MapWinEstimate], str]:
    """Per-map estimates for every map in the veto distribution: ensemble, else ratings."""
    if bundle.uses_ensemble and bundle.ensemble is not None:
        try:
            store = bundle.feature_store(session, now=now)
            est = bundle.ensemble.predict_for_sequences(
                ctx, sequences, as_of=now, store=store, rating_provider=bundle.ratings
            )
            return est, "ensemble"
        except Exception as exc:
            log.warning("ensemble failed for match %s, using the ratings: %s", ctx.match_id, exc)
    est = bundle.ratings.predict_for_sequences(
        ctx.team1_id,
        ctx.team2_id,
        sequences,
        now,
        is_lan=ctx.is_lan,
        team1_lineup=[p.player_id for p in ctx.team1_lineup],
        team2_lineup=[p.player_id for p in ctx.team2_lineup],
    )
    return est, "ratings"


def simulate(
    session: Session,
    prepared: PreparedMatch,
    bundle: ModelBundle,
    settings: Settings,
    *,
    now: datetime,
) -> MatchPrediction:
    """Build the :class:`MatchPrediction` of a prepared match (the expensive step)."""
    ctx = prepared.ctx
    with bundle.lock:
        sequences, veto_known = veto_distribution(bundle, ctx, now=now)
        estimates, source = map_estimates(session, bundle, ctx, sequences, now=now)
    kwargs = context_kwargs(ctx)
    kwargs["context_flags"] = list(prepared.flags)
    kill_model = bundle.kill_model
    if not kwargs.get("players") or kill_model is None:
        kwargs["players"] = None
        kill_model = None
    prediction = build_match_prediction(
        **kwargs,
        **engine_options(settings.models, match_id=ctx.match_id),
        veto_distribution=sequences,
        map_estimates=estimates,
        round_model=bundle.round_model,
        kill_model=kill_model,
        logit_shift=prepared.logit_shift,
        model_version=bundle.version,
        data_completeness=prepared.completeness,
        veto_known=veto_known,
        created_at=now,
        as_of=now,
    )
    prediction.components["map_estimate_source"] = source
    prediction.components["flag_source"] = prepared.flag_source
    prediction.components["fingerprint"] = prepared.fingerprint
    return prediction


def write_rationale(
    prediction: MatchPrediction,
    decisions: Sequence[Any],
    ctx: MatchContext,
    *,
    analyzer: Any | None,
    now: datetime,
) -> tuple[str, str | None]:
    """(text, model) of the rationale: Claude when an analyzer is available, else the template."""
    if analyzer is not None:
        try:
            out = analyzer.write_rationale_detailed(prediction, list(decisions), ctx, now=now)
            return str(out.text), out.model
        except Exception as exc:
            log.warning("rationale failed for match %s: %s", prediction.match_id, exc)
    try:
        return render_template_rationale(prediction, list(decisions), ctx), None
    except Exception as exc:  # defensive: never lose a prediction over its text
        log.warning("template rationale failed for match %s: %s", prediction.match_id, exc)
        return "", None


def write_outcome_predictions(
    session: Session,
    prediction: MatchPrediction,
    settings: Settings,
    *,
    analysis_id: int | None,
    model_run_id: int | None,
    now: datetime,
    replace: bool,
) -> int:
    """One ``predictions`` row per currently quoted outcome; returns the number of rows written.

    ``replace=True`` (a new prediction) first clears ``is_latest`` on every older row of the match;
    otherwise only outcomes without a latest row of this analysis are added.
    """
    match_id = prediction.match_id
    quotes = latest_quotes(session, match_id, max_age_s=settings.betclic.odds_max_age_s, now=now)
    if replace:
        session.execute(
            update(Prediction)
            .where(Prediction.match_id == match_id, Prediction.is_latest.is_(True))
            .values(is_latest=False)
        )
        existing: set[tuple[str, str]] = set()
    else:
        rows = session.execute(
            select(Prediction.spec_key, Prediction.selection_key, Prediction.components).where(
                Prediction.match_id == match_id, Prediction.is_latest.is_(True)
            )
        ).all()
        existing = {
            (spec_key, sel)
            for spec_key, sel, comps in rows
            if (comps or {}).get("analysis_id") == analysis_id
        }
        if any((comps or {}).get("analysis_id") != analysis_id for _, _, comps in rows):
            session.execute(
                update(Prediction)
                .where(
                    Prediction.match_id == match_id,
                    Prediction.is_latest.is_(True),
                    Prediction.created_at < prediction.created_at,
                )
                .values(is_latest=False)
            )
    if not quotes:
        session.flush()
        return 0
    report = price_quotes_report(
        prediction, quotes, sigma_floor=settings.models.sigma_floor, betclic=settings.betclic
    )
    written = 0
    for outcome, quote in report.priced:
        key = (outcome.spec_key, outcome.selection)
        if key in existing:
            continue
        existing.add(key)
        session.add(
            Prediction(
                match_id=match_id,
                model_run_id=model_run_id,
                model_version=prediction.model_version,
                created_at=now,
                market_type=quote.spec.market_type.value,
                spec=quote.spec.model_dump(mode="json"),
                spec_key=outcome.spec_key,
                selection_key=outcome.selection,
                p_win=outcome.p_win,
                p_push=outcome.p_push,
                p_void=outcome.p_void,
                sigma=outcome.sigma,
                data_completeness=prediction.data_completeness,
                components={
                    "analysis_id": analysis_id,
                    "selection_id": quote.selection_id,
                    "p_win_settled": outcome.p_win_given_settled,
                    "prediction_created_at": prediction.created_at.isoformat(),
                },
                is_latest=True,
            )
        )
        written += 1
    session.flush()
    return written


def persist_prediction(
    session: Session,
    prediction: MatchPrediction,
    prepared: PreparedMatch,
    bundle: ModelBundle,
    settings: Settings,
    *,
    analyzer: Any | None = None,
    now: datetime,
) -> tuple[MatchAnalysis, int]:
    """Write the analysis row, the outcome rows and the match bookkeeping (flushes, no commit)."""
    match_id = prediction.match_id
    rationale, rationale_model = write_rationale(prediction, [], prepared.ctx, analyzer=analyzer, now=now)
    session.execute(
        update(MatchAnalysis)
        .where(MatchAnalysis.match_id == match_id, MatchAnalysis.is_latest.is_(True))
        .values(is_latest=False)
    )
    analysis = MatchAnalysis(
        match_id=match_id,
        created_at=now,
        model_version=prediction.model_version,
        payload=prediction.model_dump(mode="json"),
        rationale=rationale or None,
        rationale_model=rationale_model,
        is_latest=True,
    )
    session.add(analysis)
    session.flush()
    n = write_outcome_predictions(
        session,
        prediction,
        settings,
        analysis_id=analysis.id,
        model_run_id=bundle.model_run_id,
        now=now,
        replace=True,
    )
    match = session.get(Match, match_id)
    if match is not None:
        match.data_completeness = prepared.completeness
        extra = dict(match.extra or {})
        extra[EXTRA_KEY] = {
            "fingerprint": prepared.fingerprint,
            "model_version": prediction.model_version,
            "created_at": now.isoformat(),
            "analysis_id": analysis.id,
        }
        match.extra = extra
    session.flush()
    return analysis, n


# ----------------------------------------------------------------------------------------- public


def predict_match(
    session: Session,
    match_id: int,
    bundle: ModelBundle,
    settings: Settings,
    *,
    analyzer: Any | None = None,
    now: datetime | None = None,
    persist: bool = True,
) -> MatchPrediction | None:
    """Predict one match and (``persist``) store it; None when the match has no two known teams.

    Flushes but never commits (the caller owns the transaction).
    """
    now = ensure_utc(now or utcnow())
    try:
        prepared = prepare_match(session, match_id, bundle, settings, analyzer=analyzer, now=now)
    except datasets.MatchNotFoundError as exc:
        log.info("cannot predict match %s: %s", match_id, exc)
        return None
    prediction = simulate(session, prepared, bundle, settings, now=now)
    if persist:
        persist_prediction(session, prediction, prepared, bundle, settings, analyzer=analyzer, now=now)
    return prediction


def mark_prediction_stale(session: Session, match_id: int, reason: str, *, now: datetime) -> bool:
    """Flag the match's stored prediction as not bettable (``matches.extra["prediction"]["stale"]``).

    Returns False when the match does not exist. Flushes, never commits.
    """
    match = session.get(Match, match_id)
    if match is None:
        return False
    extra = dict(match.extra or {})
    meta = dict(extra.get(EXTRA_KEY) or {})
    meta[STALE_KEY] = {"reason": reason[:300], "at": now.isoformat()}
    extra[EXTRA_KEY] = meta
    match.extra = extra  # reassigned so the JSON change is persisted
    session.flush()
    return True


def stale_reason(match: Match) -> str | None:
    """Why the stored prediction of ``match`` must not be bet (None = it may be)."""
    meta = (match.extra or {}).get(EXTRA_KEY) or {}
    stale = meta.get(STALE_KEY) if isinstance(meta, dict) else None
    if not stale:
        return None
    if isinstance(stale, dict):
        return str(stale.get("reason") or "the stored prediction is out of date")
    return str(stale)


def _clear_stale(match: Match) -> None:
    meta = (match.extra or {}).get(EXTRA_KEY)
    if isinstance(meta, dict) and STALE_KEY in meta:
        extra = dict(match.extra or {})
        extra[EXTRA_KEY] = {k: v for k, v in meta.items() if k != STALE_KEY}
        match.extra = extra


def upcoming_match_ids(session: Session, settings: Settings, *, now: datetime) -> list[int]:
    """In-scope, scheduled matches with both teams that start inside the betting window."""
    horizon = now + timedelta(hours=max(settings.decision.max_hours_before_start, 1))
    rows = session.scalars(
        select(Match.id)
        .where(
            Match.in_scope.is_(True),
            Match.status.in_([MatchStatus.SCHEDULED, MatchStatus.UNKNOWN]),
            Match.team1_id.is_not(None),
            Match.team2_id.is_not(None),
            Match.scheduled_at.is_not(None),
            Match.scheduled_at > now,
            Match.scheduled_at <= horizon,
        )
        .order_by(Match.scheduled_at, Match.id)
    )
    return list(rows)


def latest_analysis(session: Session, match_id: int) -> MatchAnalysis | None:
    return session.scalar(
        select(MatchAnalysis)
        .where(MatchAnalysis.match_id == match_id, MatchAnalysis.is_latest.is_(True))
        .order_by(MatchAnalysis.created_at.desc(), MatchAnalysis.id.desc())
        .limit(1)
    )


def _needs_new_prediction(
    match: Match, analysis: MatchAnalysis | None, fingerprint: str, *, now: datetime, max_age: timedelta
) -> str | None:
    """Why the match must be re-simulated, or None when the stored prediction is still valid."""
    if analysis is None:
        return "no prediction yet"
    meta = (match.extra or {}).get(EXTRA_KEY) or {}
    if meta.get("analysis_id") != analysis.id:
        return "prediction bookkeeping missing"
    if meta.get("fingerprint") != fingerprint:
        return "inputs changed"
    if now - ensure_utc(analysis.created_at) >= max_age:
        return "prediction too old"
    return None


def predict_upcoming(
    session_factory: sessionmaker[Session],
    settings: Settings,
    bundle: ModelBundle,
    *,
    analyzer: Any | None = None,
    now: datetime | None = None,
    force: bool = False,
    max_age: timedelta = DEFAULT_MAX_AGE,
    match_ids: Sequence[int] | None = None,
) -> PredictReport:
    """Predict every upcoming in-scope match (see the module docstring); one transaction per match.

    Matches without both teams or without a lineup for each team (confirmed or carried over from the
    previous match) are skipped. One failing match never stops the others.
    """
    now = ensure_utc(now or utcnow())
    report = PredictReport(started_at=now, model_version=bundle.version)
    with session_factory() as session:
        report.rating_updates = bundle.refresh(session, now=now)
        ids = list(match_ids) if match_ids is not None else upcoming_match_ids(session, settings, now=now)
    for match_id in ids:
        report.considered += 1
        outcome = MatchPredictOutcome(match_id=match_id, status="failed")
        try:
            with session_factory() as session:
                outcome = _predict_one(
                    session,
                    match_id,
                    bundle,
                    settings,
                    analyzer=analyzer,
                    now=now,
                    force=force,
                    max_age=max_age,
                )
                session.commit()
        except Exception as exc:
            log.exception("prediction failed for match %s", match_id)
            outcome = MatchPredictOutcome(
                match_id=match_id, status="failed", reason=f"{type(exc).__name__}: {exc}"
            )
            report.errors.append(f"match {match_id}: {type(exc).__name__}: {exc}")
            _mark_stale_after_failure(
                session_factory, match_id, f"prediction failed: {type(exc).__name__}", now
            )
        report.results.append(outcome)
        report.outcomes_written += outcome.outcomes_written
        if outcome.status == "predicted":
            report.predicted += 1
        elif outcome.status == "unchanged":
            report.unchanged += 1
        elif outcome.status == "skipped":
            report.skipped += 1
        else:
            report.failed += 1
    return report


def _mark_stale_after_failure(
    session_factory: sessionmaker[Session], match_id: int, reason: str, now: datetime
) -> None:
    """Best effort: a match whose prediction could not be refreshed is not bet on its old prediction."""
    try:
        with session_factory() as session:
            if mark_prediction_stale(session, match_id, reason, now=now):
                session.commit()
    except Exception:
        log.exception("could not flag the prediction of match %s as stale", match_id)


def _skip(session: Session, match: Match, reason: str, now: datetime) -> MatchPredictOutcome:
    """A match that cannot be predicted any more: its stored prediction (if any) is no longer bettable."""
    if latest_analysis(session, match.id) is not None:
        mark_prediction_stale(session, match.id, reason, now=now)
    return MatchPredictOutcome(match.id, "skipped", reason)


def _predict_one(
    session: Session,
    match_id: int,
    bundle: ModelBundle,
    settings: Settings,
    *,
    analyzer: Any | None,
    now: datetime,
    force: bool,
    max_age: timedelta,
) -> MatchPredictOutcome:
    match = session.get(Match, match_id)
    if match is None:
        return MatchPredictOutcome(match_id, "skipped", "match not found")
    if match.team1_id is None or match.team2_id is None:
        return _skip(session, match, "teams not known yet", now)
    if match.scheduled_at is None or ensure_utc(match.scheduled_at) <= now:
        return MatchPredictOutcome(match_id, "skipped", "match already started")
    try:
        prepared = prepare_match(session, match_id, bundle, settings, analyzer=analyzer, now=now)
    except datasets.MatchNotFoundError as exc:
        return _skip(session, match, str(exc), now)
    if not prepared.ctx.team1_lineup or not prepared.ctx.team2_lineup:
        return _skip(session, match, "lineups not known", now)
    analysis = latest_analysis(session, match_id)
    why = (
        "forced"
        if force
        else _needs_new_prediction(match, analysis, prepared.fingerprint, now=now, max_age=max_age)
    )
    if why is None and analysis is not None:
        prediction = MatchPrediction.model_validate(analysis.payload)
        n = write_outcome_predictions(
            session,
            prediction,
            settings,
            analysis_id=analysis.id,
            model_run_id=bundle.model_run_id,
            now=now,
            replace=False,
        )
        _clear_stale(match)  # the stored prediction matches the current inputs again
        return MatchPredictOutcome(match_id, "unchanged", analysis_id=analysis.id, outcomes_written=n)
    prediction = simulate(session, prepared, bundle, settings, now=now)
    row, n = persist_prediction(session, prediction, prepared, bundle, settings, analyzer=analyzer, now=now)
    log.info("predicted match %s (%s): p(team1)=%.3f", match_id, why, prediction.central.p_team1_win)
    return MatchPredictOutcome(match_id, "predicted", why or "", analysis_id=row.id, outcomes_written=n)
