"""Claude news / context layer: triage (Haiku), match-context flags and rationale (Opus).

The LLM never produces the probabilities used for betting. It returns *suggestions* in a fixed
schema; this module validates their evidence and clips every number, and
:func:`cs2bot.llm.combine.combine_flags` bounds the combined effect again.

Claude API usage (anthropic SDK 2.x)
------------------------------------
* Structured output via ``messages.parse(output_format=<pydantic model>)`` -> ``response.parsed_output``.
* Claude Opus 5.5 (``settings.llm.context_model``): thinking is always on and is controlled only by
  ``output_config={"effort": ...}``; the API default is ``medium``, so ``settings.llm.context_effort``
  is always sent. ``thinking``, ``temperature``, ``top_p``, ``top_k`` and assistant prefill are never
  sent (the API rejects them or they are unnecessary).
* **Server-side refusal fallback** - when ``settings.llm.use_server_fallbacks`` is true, Opus requests
  go through ``client.beta.messages.parse(..., betas=["server-side-fallback-2026-07-01"],
  fallbacks="default")``: if Opus declines a request for policy reasons, Anthropic re-runs it on its
  recommended fallback model inside the same call. With the setting off, the plain
  ``client.messages.parse`` endpoint is used.
* Claude Haiku 5.5 (``settings.llm.triage_model``) has no server-side fallback: triage requests never
  carry ``fallbacks`` / ``betas``.
* ``stop_reason`` is checked before the parsed output is read: ``"refusal"`` and ``"max_tokens"`` both
  yield an empty result (logged).
* Errors are caught most-specific-first (``RateLimitError``, ``APIStatusError``,
  ``APIConnectionError``); the SDK has already retried twice by then. Nothing here raises into the
  pipeline: every failure degrades to "no flags" / "template rationale".
* System prompts are stable and marked cacheable; volatile data lives in the user message.

Disabled mode: ``settings.llm.enabled`` false, or no credentials (neither ``settings.llm.api_key``
nor anything the SDK can resolve itself, e.g. ``ANTHROPIC_API_KEY``) -> no API call is ever made;
``triage`` / ``analyze_match`` return ``[]`` and ``write_rationale`` returns the deterministic template.
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import anthropic
import pydantic

from cs2bot.config import LLMSettings, Settings
from cs2bot.domain.betting import BetDecision
from cs2bot.domain.context import ContextFlag, NewsItemIn
from cs2bot.domain.match import MatchContext
from cs2bot.domain.predictions import MatchPrediction
from cs2bot.llm import prompts
from cs2bot.llm.combine import clip, combine_flags
from cs2bot.llm.memo import AnalysisMemo, MemoEntry, context_fingerprint
from cs2bot.llm.rationale import (
    RISK_DISCLAIMER,
    build_rationale_digest,
    finalize_rationale,
    render_template_rationale,
    sanitize_markdown,
)
from cs2bot.llm.schemas import (
    ContextFlagSuggestion,
    MatchContextResult,
    RationaleOutput,
    TriageBatch,
    TriageItem,
    TriageLabel,
    clamp01,
)
from cs2bot.llm.tuning import LLMTuning
from cs2bot.utils.logging import get_logger
from cs2bot.utils.names import normalize_team_name
from cs2bot.utils.time import ensure_utc, parse_datetime, utcnow

log = get_logger(__name__)

__all__ = [
    "ContextAnalyzer",
    "LLMStatus",
    "MatchAnalysisOutcome",
    "RationaleOutcome",
    "TriageOutcome",
    "UsageTotals",
    "build_evidence_index",
    "combine_flags",
    "dedupe_flags",
    "resolve_evidence",
    "suggestion_to_flag",
    "supports_server_fallback",
]

CONTEXT_EVIDENCE = "context"  # evidence token for "this flag rests on the match context itself"
_MAX_FLAGS = 10
_MAX_CANDIDATES_PER_ITEM = 15
_IRRELEVANT_MAX_RELEVANCE = 0.2
_NEWS_REF = re.compile(r"^(?:news)?[\s:_#-]*(\d+)$", re.IGNORECASE)
_CONTEXT_TOKENS = {"context", "match_context", "match context", "match-context"}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class LLMStatus(StrEnum):
    """How a request (or a cached lookup) ended."""

    OK = "ok"
    DISABLED = "disabled"  # LLM switched off or no credentials - no call made
    REFUSED = "refused"  # stop_reason == "refusal"
    TRUNCATED = "truncated"  # stop_reason == "max_tokens"
    FAILED = "failed"  # API / connection / validation error
    BACKOFF = "backoff"  # the same inputs failed recently - no call made


@dataclass
class UsageTotals:
    """Running token counters of one analyzer (for logs / health pages; not a billing record)."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    refusals: int = 0
    failures: int = 0


@dataclass(frozen=True)
class TriageOutcome:
    """Result of :meth:`ContextAnalyzer.triage_detailed`.

    ``unanswered_ids``: the request(s) covering these items ended without a usable verdict for a
    content-related reason (refusal, truncation, invalid output, or the model skipped the item).
    ``deferred_ids``: not attempted or failed for a transient reason (disabled, rate limit, network).
    """

    status: LLMStatus
    items: list[TriageItem] = field(default_factory=list)
    unanswered_ids: list[int] = field(default_factory=list)
    deferred_ids: list[int] = field(default_factory=list)
    model: str | None = None

    @property
    def complete(self) -> bool:
        return not self.unanswered_ids and not self.deferred_ids


@dataclass(frozen=True)
class MatchAnalysisOutcome:
    """Result of :meth:`ContextAnalyzer.analyze_match_detailed`.

    Only ``status == OK`` means "the model looked and these are the flags" (possibly none). For any
    other status ``flags`` is empty and callers must keep whatever flags they already had.
    """

    status: LLMStatus
    flags: list[ContextFlag] = field(default_factory=list)
    note: str = ""
    model: str | None = None
    fingerprint: str = ""
    from_cache: bool = False
    n_news: int = 0
    n_dropped: int = 0  # suggestions discarded (no valid evidence / duplicates)
    detail: str = ""
    analyzed_at: datetime | None = None  # when the model produced this result (cached results keep theirs)

    @property
    def ok(self) -> bool:
        return self.status is LLMStatus.OK


@dataclass(frozen=True)
class RationaleOutcome:
    """Result of :meth:`ContextAnalyzer.write_rationale_detailed`. ``model`` is None for the template."""

    text: str
    status: LLMStatus
    model: str | None = None


@dataclass(frozen=True)
class _CallResult:
    status: LLMStatus
    parsed: Any = None
    model: str | None = None
    detail: str = ""
    content_related: bool = False  # the failure is tied to this request's content (do not just retry)
    stop_all: bool = False  # further requests are pointless right now (auth failure, rate limit)


def supports_server_fallback(model: str) -> bool:
    """Whether ``fallbacks`` may be sent for ``model`` (Claude Haiku has no server-side fallback)."""
    return "haiku" not in model.lower()


# ---------------------------------------------------------------------------------------------
# Suggestion -> ContextFlag (all bounds are enforced here)
# ---------------------------------------------------------------------------------------------


def build_evidence_index(news: Sequence[NewsItemIn]) -> dict[str, str]:
    """Map every accepted evidence reference (``news-<n>``, the item URL) to the item's URL."""
    index: dict[str, str] = {}
    for position, item in enumerate(news, start=1):
        index[prompts.news_ref(position)] = item.url
        index.setdefault(item.url.strip().casefold(), item.url)
    return index


def resolve_evidence(raw: Sequence[str], index: Mapping[str, str], *, limit: int = 8) -> list[str]:
    """Keep only evidence that refers to supplied material; news references become URLs.

    Accepted: ``news-<n>`` (also ``news:3``, ``#3``, ``3``), the exact URL of a supplied item, and
    ``context``. Everything else - invented URLs, free text, out-of-range numbers - is dropped.
    """
    resolved: list[str] = []
    for entry in raw:
        token = " ".join(str(entry).split()).strip("\"'[]() ").casefold()
        target: str | None = None
        if token in _CONTEXT_TOKENS:
            target = CONTEXT_EVIDENCE
        elif (match := _NEWS_REF.match(token)) is not None:
            target = index.get(prompts.news_ref(int(match.group(1))))
        else:
            target = index.get(token)
        if target is not None and target not in resolved:
            resolved.append(target)
    return resolved[:limit]


def _parse_expiry(raw: str | None, now: datetime) -> datetime | None:
    """Parse the model's ``expires_at``. Unparseable or already-past values count as "no expiry".

    A timestamp in the past would switch the flag (including a veto) off immediately; that is far
    more likely a date-arithmetic slip by the model than its intent, so the flag stays in force.
    """
    if raw is None or not str(raw).strip():
        return None
    try:
        parsed = parse_datetime(str(raw).strip())
    except (ValueError, OverflowError, TypeError):
        log.warning("context flag with unparseable expires_at %r - treated as no expiry", str(raw)[:40])
        return None
    if parsed is None or parsed <= now:
        return None
    return parsed


def suggestion_to_flag(
    suggestion: ContextFlagSuggestion,
    *,
    settings: LLMSettings,
    evidence_index: Mapping[str, str],
    now: datetime,
    tuning: LLMTuning | None = None,
) -> ContextFlag | None:
    """Validate and bound one model suggestion. Returns ``None`` when it has no valid evidence.

    * evidence must point at supplied news items or the match context (anything else is dropped);
    * severity / confidence are clamped to [0, 1], ``logit_shift`` to ``+-max_flag_logit_shift``;
    * a veto below ``tuning.veto_min_confidence`` is demoted to a plain flag (and the rationale says
      so), so that every consumer of the stored ``veto`` bit agrees with ``combine_flags``.
    """
    tune = tuning or LLMTuning()
    evidence = resolve_evidence(suggestion.evidence, evidence_index, limit=tune.max_evidence_items)
    if not evidence:
        return None
    confidence = clamp01(suggestion.confidence)
    rationale = " ".join(sanitize_markdown(suggestion.rationale).split())
    if len(rationale) > tune.max_rationale_chars:
        rationale = rationale[: tune.max_rationale_chars - 1].rstrip() + "…"
    veto = bool(suggestion.veto)
    if veto and confidence < tune.veto_min_confidence:
        veto = False
        rationale = (
            f"{rationale} [No-bet veto suggested but not enforced: confidence {confidence:.2f} "
            f"is below {tune.veto_min_confidence:.2f}.]"
        ).strip()
    return ContextFlag(
        flag_type=suggestion.flag_type,
        team=suggestion.team,
        severity=clamp01(suggestion.severity),
        confidence=confidence,
        logit_shift=clip(suggestion.logit_shift, settings.max_flag_logit_shift),
        veto=veto,
        rationale=rationale or "(no rationale given)",
        evidence=evidence,
        expires_at=_parse_expiry(suggestion.expires_at, now),
    )


def _strength(flag: ContextFlag) -> tuple[int, float, float]:
    return (int(flag.veto), abs(flag.logit_shift) * flag.confidence, flag.confidence)


def dedupe_flags(flags: Sequence[ContextFlag], *, limit: int = _MAX_FLAGS) -> list[ContextFlag]:
    """At most one flag per ``(flag_type, team)``: keep the strongest and merge the evidence.

    Splitting one story into several flags of the same kind would stack their adjustments; the
    prompt forbids it and this enforces it. Order of first appearance is preserved.
    """
    best: dict[tuple[str, str | None], ContextFlag] = {}
    for flag in flags:
        key = (flag.flag_type.value, flag.team.value if flag.team else None)
        current = best.get(key)
        if current is None:
            best[key] = flag
            continue
        keep, other = (flag, current) if _strength(flag) > _strength(current) else (current, flag)
        merged = list(dict.fromkeys([*keep.evidence, *other.evidence]))
        best[key] = keep.model_copy(update={"evidence": merged})
    return list(best.values())[:limit]


# ---------------------------------------------------------------------------------------------
# Team candidates for triage
# ---------------------------------------------------------------------------------------------


class _TeamIndex:
    """Finds which known teams a news text may be about (cheap, precision-oriented)."""

    def __init__(self, known_teams: Mapping[int, str]) -> None:
        self._names: dict[int, str] = {}
        self._by_norm: dict[str, set[int]] = {}
        for team_id, name in known_teams.items():
            cleaned = " ".join(str(name).split())
            if len(cleaned) < 2:
                continue
            self._names[int(team_id)] = cleaned
            norm = normalize_team_name(cleaned)
            if norm:
                self._by_norm.setdefault(norm, set()).add(int(team_id))

    def name(self, team_id: int) -> str | None:
        return self._names.get(team_id)

    def detect(self, text: str) -> list[int]:
        """Team ids whose exact (case-sensitive) name occurs in ``text`` as a whole word.

        Case-sensitive on purpose: "BIG", "Heroic" or "Spirit" are also ordinary words. The result is
        only a list of *candidates* offered to the model.
        """
        found: list[int] = []
        for team_id, name in self._names.items():
            if name in text and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text):
                found.append(team_id)
        return found

    def resolve(self, names: Sequence[str]) -> set[int]:
        """Ids of teams whose normalised name equals one of ``names`` (ambiguous names are skipped)."""
        ids: set[int] = set()
        for name in names:
            matches = self._by_norm.get(normalize_team_name(name), set())
            if len(matches) == 1:
                ids |= matches
        return ids


# ---------------------------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------------------------


class ContextAnalyzer:
    """Claude-backed news triage, match-context flags and bet rationale.

    ``client`` may be injected (tests, custom transports); otherwise an ``anthropic.Anthropic`` client
    is created lazily on first use with ``api_key=<settings.llm.api_key or None>`` (``None`` lets the
    SDK resolve ``ANTHROPIC_API_KEY`` etc. itself) and ``timeout=settings.llm.request_timeout_s``.

    The analyzer is safe to share between threads. Public methods never raise for API problems.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        tuning: LLMTuning | None = None,
        memo: AnalysisMemo | None = None,
    ) -> None:
        self._settings = settings
        self._llm: LLMSettings = settings.llm
        self._tuning = tuning or LLMTuning()
        self._client: Any | None = client
        self._client_resolved = client is not None
        self._auth_failed = False
        self._lock = threading.Lock()
        self.memo = memo if memo is not None else AnalysisMemo(self._tuning.memo_max_entries)
        self.usage = UsageTotals()

    # -- availability ---------------------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def tuning(self) -> LLMTuning:
        return self._tuning

    @property
    def enabled(self) -> bool:
        """True when requests would actually be sent (enabled, credentials present, auth not failed)."""
        return self._get_client() is not None

    def _get_client(self) -> Any | None:
        if not self._llm.enabled or self._auth_failed:
            return None
        with self._lock:
            if self._client_resolved:
                return self._client
            self._client_resolved = True
            key = self._llm.api_key
            try:
                client = anthropic.Anthropic(
                    api_key=key.get_secret_value() if key is not None else None,
                    timeout=self._llm.request_timeout_s,
                )
            except Exception as exc:  # the SDK raises for unusable credential configuration
                log.warning(
                    "Claude client could not be created (%s) - LLM layer disabled", type(exc).__name__
                )
                return None
            if not _has_credentials(client):
                log.info("no Anthropic credentials configured - LLM layer disabled (template rationale only)")
                _close_quietly(client)
                return None
            self._client = client
            return client

    # -- low-level request ----------------------------------------------------------------

    def _request(
        self,
        kind: str,
        *,
        client: Any,
        model: str,
        effort: str,
        max_tokens: int,
        system: str,
        user: str,
        output_format: type[pydantic.BaseModel],
        allow_fallback: bool,
    ) -> _CallResult:
        """Send one structured-output request and classify the result. Never raises."""
        system_param: Any = system
        if self._tuning.prompt_caching:
            system_param = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_param,
            "messages": [{"role": "user", "content": user}],
            "output_format": output_format,
            "output_config": {"effort": effort},
        }
        try:
            if allow_fallback and self._llm.use_server_fallbacks and supports_server_fallback(model):
                response = client.beta.messages.parse(
                    **kwargs, betas=[self._tuning.fallback_beta], fallbacks="default"
                )
            else:
                response = client.messages.parse(**kwargs)
        except anthropic.RateLimitError as exc:
            self.usage.failures += 1
            log.warning("Claude %s request rate-limited after SDK retries: %s", kind, _brief(exc))
            return _CallResult(LLMStatus.FAILED, detail="rate_limited", stop_all=True)
        except anthropic.APIStatusError as exc:
            self.usage.failures += 1
            status_code = getattr(exc, "status_code", 0) or 0
            if status_code in (401, 403):
                self._auth_failed = True
                log.error(
                    "Claude %s request rejected (HTTP %s) - check the API key; LLM layer disabled "
                    "until restart",
                    kind,
                    status_code,
                )
                return _CallResult(LLMStatus.FAILED, detail=f"auth_{status_code}", stop_all=True)
            if status_code >= 500:
                log.warning("Claude %s request failed with server error %s (retryable)", kind, status_code)
                return _CallResult(LLMStatus.FAILED, detail=f"server_{status_code}")
            log.error("Claude %s request rejected (HTTP %s): %s", kind, status_code, _brief(exc))
            return _CallResult(LLMStatus.FAILED, detail=f"http_{status_code}", content_related=True)
        except anthropic.APIConnectionError as exc:
            self.usage.failures += 1
            log.warning("Claude %s request failed (%s: connection/timeout)", kind, type(exc).__name__)
            return _CallResult(LLMStatus.FAILED, detail="connection")
        except pydantic.ValidationError as exc:
            self.usage.failures += 1
            log.warning("Claude %s response did not match the schema (%d errors)", kind, exc.error_count())
            return _CallResult(LLMStatus.FAILED, detail="invalid_output", content_related=True)
        except Exception as exc:  # never raise into the pipeline (SDK parsing errors, bad fakes, ...)
            self.usage.failures += 1
            log.exception("Claude %s request failed unexpectedly (%s)", kind, type(exc).__name__)
            return _CallResult(LLMStatus.FAILED, detail=f"unexpected_{type(exc).__name__}")

        self._record_usage(response)
        served_model = getattr(response, "model", None) or model
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            self.usage.refusals += 1
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            log.warning("Claude %s request was refused (category=%s)", kind, category)
            return _CallResult(
                LLMStatus.REFUSED, model=served_model, detail=f"refusal:{category}", content_related=True
            )
        if stop_reason == "max_tokens":
            self.usage.failures += 1
            log.warning("Claude %s response hit max_tokens=%d - discarded", kind, max_tokens)
            return _CallResult(
                LLMStatus.TRUNCATED, model=served_model, detail="max_tokens", content_related=True
            )
        parsed = getattr(response, "parsed_output", None)
        if not isinstance(parsed, output_format):
            self.usage.failures += 1
            log.warning("Claude %s response carried no parsed output (stop_reason=%s)", kind, stop_reason)
            return _CallResult(
                LLMStatus.FAILED, model=served_model, detail="no_parsed_output", content_related=True
            )
        return _CallResult(LLMStatus.OK, parsed=parsed, model=served_model)

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        self.usage.requests += 1
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            value = getattr(usage, name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(self.usage, name, getattr(self.usage, name) + value)

    # -- triage (Haiku) -------------------------------------------------------------------

    def triage(self, items: list[tuple[int, NewsItemIn]], known_teams: dict[int, str]) -> list[TriageItem]:
        """Classify news items given as ``(news_items.id, NewsItemIn)``; verdicts come back in input order.

        Returns ``[]`` in disabled mode. Items the model could not classify are simply missing from the
        result (see :meth:`triage_detailed` for the reasons).
        """
        return self.triage_detailed(items, known_teams).items

    def triage_detailed(
        self, items: Sequence[tuple[int, NewsItemIn]], known_teams: Mapping[int, str]
    ) -> TriageOutcome:
        """Like :meth:`triage`, plus which items stayed unanswered / were deferred.

        Requests carry ``tuning.triage_batch_size`` items (~20). A batch that is refused, truncated or
        returns invalid output is split in half and retried, so one problematic article cannot block
        the others. A rate limit or authentication failure stops the remaining batches (deferred).

        Team ids in the verdicts are restricted to the candidates offered for that item (ids already
        attached to it plus exact name matches from ``known_teams``) and to names the model returned
        that match a known team exactly after normalisation; the model cannot introduce other ids.
        """
        unique: dict[int, NewsItemIn] = {}
        for news_id, item in items:
            unique.setdefault(int(news_id), item)
        order = list(unique)
        if not order:
            return TriageOutcome(LLMStatus.OK)
        try:
            client = self._get_client()
            if client is None:
                return TriageOutcome(LLMStatus.DISABLED, deferred_ids=order)
            index = _TeamIndex(known_teams)
            candidates = {nid: self._candidates(unique[nid], index) for nid in order}
            verdicts: dict[int, TriageItem] = {}
            unanswered: list[int] = []
            deferred: list[int] = []
            model_used: str | None = None
            size = max(1, self._tuning.triage_batch_size)
            queue = [order[i : i + size] for i in range(0, len(order), size)]
            stopped = False
            while queue:
                batch_ids = queue.pop(0)
                if stopped:
                    deferred.extend(batch_ids)
                    continue
                result = self._request(
                    "triage",
                    client=client,
                    model=self._llm.triage_model,
                    effort=self._llm.triage_effort,
                    max_tokens=self._tuning.triage_max_tokens,
                    system=prompts.TRIAGE_SYSTEM_PROMPT,
                    user=prompts.build_triage_message(
                        [(nid, unique[nid]) for nid in batch_ids],
                        {nid: [(tid, index.name(tid) or "") for tid in candidates[nid]] for nid in batch_ids},
                        body_chars=self._tuning.triage_body_chars,
                    ),
                    output_format=TriageBatch,
                    allow_fallback=False,  # Haiku: never send fallbacks / betas
                )
                if result.status is LLMStatus.OK:
                    model_used = result.model or model_used
                    answered = self._accept_verdicts(result.parsed, batch_ids, candidates, index, verdicts)
                    unanswered.extend(nid for nid in batch_ids if nid not in answered)
                elif result.stop_all:
                    stopped = True
                    deferred.extend(batch_ids)
                elif result.content_related and len(batch_ids) > 1:
                    middle = len(batch_ids) // 2
                    queue[:0] = [batch_ids[:middle], batch_ids[middle:]]
                elif result.content_related:
                    unanswered.extend(batch_ids)
                else:
                    deferred.extend(batch_ids)
            ordered = [verdicts[nid] for nid in order if nid in verdicts]
            status = LLMStatus.OK if ordered or not (unanswered or deferred) else LLMStatus.FAILED
            if unanswered or deferred:
                log.info(
                    "news triage: %d classified, %d unanswered, %d deferred",
                    len(ordered),
                    len(unanswered),
                    len(deferred),
                )
            return TriageOutcome(status, ordered, sorted(unanswered), sorted(deferred), model_used)
        except Exception:  # defensive: a triage bug must not take the news job down
            log.exception("news triage failed unexpectedly")
            return TriageOutcome(LLMStatus.FAILED, deferred_ids=order)

    @staticmethod
    def _candidates(item: NewsItemIn, index: _TeamIndex) -> list[int]:
        text = f"{item.title}\n{item.body or ''}"
        ordered = [tid for tid in item.team_ids if index.name(tid) is not None]
        ordered += [tid for tid in index.detect(text) if tid not in ordered]
        return list(dict.fromkeys(ordered))[:_MAX_CANDIDATES_PER_ITEM]

    @staticmethod
    def _accept_verdicts(
        batch: TriageBatch,
        batch_ids: Sequence[int],
        candidates: Mapping[int, Sequence[int]],
        index: _TeamIndex,
        verdicts: dict[int, TriageItem],
    ) -> set[int]:
        """Validate one batch's verdicts into ``verdicts``; returns the news ids that were answered."""
        expected = set(batch_ids)
        answered: set[int] = set()
        for entry in batch.items:
            if entry.news_id not in expected or entry.news_id in answered:
                log.debug("triage verdict for unexpected/duplicate news id %s ignored", entry.news_id)
                continue
            allowed = set(candidates.get(entry.news_id, ()))
            team_ids = sorted((set(entry.team_ids) & allowed) | index.resolve(entry.team_names))
            relevance = entry.relevance
            if entry.label is TriageLabel.IRRELEVANT:
                relevance = min(relevance, _IRRELEVANT_MAX_RELEVANCE)
            verdicts[entry.news_id] = entry.model_copy(update={"team_ids": team_ids, "relevance": relevance})
            answered.add(entry.news_id)
        return answered

    # -- match context (Opus) -------------------------------------------------------------

    def analyze_match(
        self,
        ctx: MatchContext,
        news: list[NewsItemIn],
        *,
        form_summary: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> list[ContextFlag]:
        """Return bounded context flags for one match (``[]`` when disabled, refused or failed).

        An empty list does not tell "nothing to flag" apart from "could not analyse"; pipelines that
        persist flags should use :meth:`analyze_match_detailed` (or
        :func:`cs2bot.llm.pipeline.refresh_match_context`) so a failed call does not wipe earlier flags.
        """
        return self.analyze_match_detailed(ctx, news, form_summary=form_summary, now=now).flags

    def analyze_match_detailed(
        self,
        ctx: MatchContext,
        news: Sequence[NewsItemIn],
        *,
        form_summary: Mapping[str, Any] | None = None,
        now: datetime | None = None,
        use_memo: bool = True,
    ) -> MatchAnalysisOutcome:
        """Analyse one match with Opus (structured output, cost-guarded by the memo).

        ``news`` should be oldest-first (as :func:`cs2bot.llm.store.select_news_for_match` returns it);
        at most ``settings.llm.max_news_items_per_match`` items are sent - if more are passed, the most
        recent ones are kept and a warning is logged.

        The memo returns the previous result for identical inputs (same news URLs, lineups, veto)
        while it is younger than ``tuning.reanalysis_interval_s`` (``near_start_interval_s`` inside the
        last ``near_start_window_s`` before the start, when "lineup still unknown" becomes urgent).
        A failed / refused analysis of the same inputs is not retried for ``failure_backoff_s``.
        """
        current = ensure_utc(now) if now is not None else utcnow()
        fingerprint = ""
        try:
            selected = self._cap_news(news)
            fingerprint = context_fingerprint(ctx, selected)
            client = self._get_client()
            if client is None:
                return MatchAnalysisOutcome(LLMStatus.DISABLED, fingerprint=fingerprint, n_news=len(selected))
            if use_memo:
                cached = self._from_memo(ctx, fingerprint, current, len(selected))
                if cached is not None:
                    return cached
            result = self._request(
                "match-context",
                client=client,
                model=self._llm.context_model,
                effort=self._llm.context_effort,
                max_tokens=self._tuning.context_max_tokens,
                system=prompts.context_system_prompt(
                    self._llm.max_flag_logit_shift, self._tuning.veto_min_confidence
                ),
                user=prompts.build_context_message(
                    ctx,
                    selected,
                    now=current,
                    form_summary=form_summary,
                    body_chars=self._tuning.context_body_chars,
                ),
                output_format=MatchContextResult,
                allow_fallback=True,
            )
            if result.status is not LLMStatus.OK:
                if not self._auth_failed:
                    self.memo.put(
                        MemoEntry(ctx.match_id, fingerprint, current, ok=False, status=result.status.value)
                    )
                return MatchAnalysisOutcome(
                    result.status,
                    model=result.model,
                    fingerprint=fingerprint,
                    n_news=len(selected),
                    detail=result.detail,
                )
            parsed: MatchContextResult = result.parsed
            flags, dropped = self._convert_flags(parsed, selected, current)
            note = " ".join(sanitize_markdown(parsed.note).split())
            self.memo.put(
                MemoEntry(
                    ctx.match_id,
                    fingerprint,
                    current,
                    ok=True,
                    flags=tuple(flags),
                    note=note,
                    model=result.model,
                )
            )
            log.info(
                "match %s context: %d flag(s), %d dropped, %d news item(s), model=%s",
                ctx.match_id,
                len(flags),
                dropped,
                len(selected),
                result.model,
            )
            return MatchAnalysisOutcome(
                LLMStatus.OK,
                flags=flags,
                note=note,
                model=result.model,
                fingerprint=fingerprint,
                n_news=len(selected),
                n_dropped=dropped,
                analyzed_at=current,
            )
        except Exception:  # defensive: never raise into the prediction pipeline
            log.exception("match-context analysis failed unexpectedly for match %s", ctx.match_id)
            return MatchAnalysisOutcome(LLMStatus.FAILED, fingerprint=fingerprint, detail="unexpected")

    def reanalysis_interval(self, ctx: MatchContext, now: datetime) -> timedelta:
        """How long an analysis of unchanged inputs stays valid at ``now`` (shorter close to the start)."""
        seconds_to_start = (ensure_utc(ctx.scheduled_at) - now).total_seconds()
        if 0 <= seconds_to_start <= self._tuning.near_start_window_s:
            return timedelta(
                seconds=min(self._tuning.near_start_interval_s, self._tuning.reanalysis_interval_s)
            )
        return timedelta(seconds=self._tuning.reanalysis_interval_s)

    def _from_memo(
        self, ctx: MatchContext, fingerprint: str, now: datetime, n_news: int
    ) -> MatchAnalysisOutcome | None:
        entry = self.memo.lookup(ctx.match_id, fingerprint)
        if entry is None:
            return None
        age = now - entry.analyzed_at
        if age < timedelta(0):
            return None
        if entry.ok and age < self.reanalysis_interval(ctx, now):
            return MatchAnalysisOutcome(
                LLMStatus.OK,
                flags=list(entry.flags),
                note=entry.note,
                model=entry.model,
                fingerprint=fingerprint,
                from_cache=True,
                n_news=n_news,
                analyzed_at=entry.analyzed_at,
            )
        if not entry.ok and age < timedelta(seconds=self._tuning.failure_backoff_s):
            return MatchAnalysisOutcome(
                LLMStatus.BACKOFF,
                fingerprint=fingerprint,
                from_cache=True,
                n_news=n_news,
                detail=f"previous attempt: {entry.status}",
            )
        return None

    def _cap_news(self, news: Sequence[NewsItemIn]) -> list[NewsItemIn]:
        limit = max(0, self._llm.max_news_items_per_match)
        items = list(news)
        if len(items) <= limit:
            return items
        log.warning(
            "match context: %d news items passed, only the %d most recent are sent", len(items), limit
        )
        ranked = sorted(
            enumerate(items),
            key=lambda pair: (ensure_utc(pair[1].published_at) if pair[1].published_at else _EPOCH, pair[0]),
        )
        keep = sorted(position for position, _ in ranked[len(items) - limit :]) if limit else []
        return [items[position] for position in keep]

    def _convert_flags(
        self, parsed: MatchContextResult, news: Sequence[NewsItemIn], now: datetime
    ) -> tuple[list[ContextFlag], int]:
        index = build_evidence_index(news)
        flags: list[ContextFlag] = []
        dropped = 0
        for suggestion in parsed.flags:
            if not (math.isfinite(suggestion.logit_shift) and math.isfinite(suggestion.confidence)):
                dropped += 1
                continue
            flag = suggestion_to_flag(
                suggestion, settings=self._llm, evidence_index=index, now=now, tuning=self._tuning
            )
            if flag is None:
                dropped += 1
                log.warning(
                    "context flag %s dropped: it cites no supplied evidence (%s)",
                    suggestion.flag_type.value,
                    ", ".join(str(e)[:60] for e in suggestion.evidence[:4]) or "none",
                )
                continue
            flags.append(flag)
        deduped = dedupe_flags(flags)
        return deduped, dropped + (len(flags) - len(deduped))

    # -- rationale (Opus) -----------------------------------------------------------------

    def write_rationale(
        self, prediction: MatchPrediction, decisions: list[BetDecision], ctx: MatchContext
    ) -> str:
        """Markdown rationale for the dashboard (the deterministic template when Claude is unavailable)."""
        return self.write_rationale_detailed(prediction, decisions, ctx).text

    def write_rationale_detailed(
        self, prediction: MatchPrediction, decisions: Sequence[BetDecision], ctx: MatchContext
    ) -> RationaleOutcome:
        """Like :meth:`write_rationale`, plus the status and the model that wrote the text.

        Store ``outcome.model`` in ``match_analyses.rationale_model`` (``None`` = template).
        The text never promises certainty: the prompt forbids it, and a sentence stating that losses
        are possible is appended if the model's text lacks one.
        """
        try:
            template = render_template_rationale(prediction, decisions, ctx)
        except Exception:  # malformed prediction payload - still return something readable
            log.exception("template rationale failed for match %s", prediction.match_id)
            template = f"Analysis summary unavailable for match {prediction.match_id}.\n\n*{RISK_DISCLAIMER}*"
        try:
            client = self._get_client()
            if client is None:
                return RationaleOutcome(template, LLMStatus.DISABLED)
            digest = build_rationale_digest(
                prediction, decisions, ctx, max_rejected=self._tuning.rationale_max_rejected
            )
            result = self._request(
                "rationale",
                client=client,
                model=self._llm.context_model,
                effort=self._llm.context_effort,
                max_tokens=self._tuning.rationale_max_tokens,
                system=prompts.rationale_system_prompt(self._tuning.rationale_language),
                user=prompts.build_rationale_message(digest),
                output_format=RationaleOutput,
                allow_fallback=True,
            )
            if result.status is not LLMStatus.OK:
                return RationaleOutcome(template, result.status)
            text = finalize_rationale(result.parsed.markdown)
            if not text:
                log.warning(
                    "Claude returned an empty rationale for match %s - using the template", ctx.match_id
                )
                return RationaleOutcome(template, LLMStatus.FAILED)
            return RationaleOutcome(text, LLMStatus.OK, result.model)
        except Exception:  # defensive: the rationale is cosmetic and must never break a bet run
            log.exception("rationale generation failed unexpectedly for match %s", prediction.match_id)
            return RationaleOutcome(template, LLMStatus.FAILED)


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------


def _has_credentials(client: Any) -> bool:
    """Whether the SDK client resolved any credential (API key, auth token or a token provider)."""
    return any(getattr(client, name, None) for name in ("api_key", "auth_token", "credentials"))


def _close_quietly(client: Any) -> None:
    try:
        client.close()
    except Exception:  # pragma: no cover - best effort
        return


def _brief(exc: Exception) -> str:
    """Short, single-line error text for logs (API error messages never contain credentials)."""
    message = getattr(exc, "message", None) or str(exc)
    return " ".join(str(message).split())[:300]
