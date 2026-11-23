"""Tunables of the Claude context layer that do not (yet) have a field in ``LLMSettings``.

Everything here has a sensible default; pass a customised :class:`LLMTuning` to
:class:`cs2bot.llm.context.ContextAnalyzer` (and to the pipeline helpers) to change it. Model names,
effort levels, the logit bounds, the request timeout and the per-match news cap live in
``cs2bot.config.LLMSettings`` and are *not* duplicated here.
"""

from __future__ import annotations

from dataclasses import dataclass

# A veto suggested by the model only counts when its confidence is at least this high
# (docs/dev/briefs/llm.md, item 3).
VETO_MIN_CONFIDENCE: float = 0.6

# Beta header required by the server-side refusal fallback in its ``fallbacks="default"`` form.
SERVER_FALLBACK_BETA: str = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class LLMTuning:
    """Knobs for request sizes, the cost guard and news selection."""

    # --- triage (Claude Haiku) ---
    triage_batch_size: int = 20  # news items per request
    triage_max_tokens: int = 8000  # thinking + ~20 short entries
    triage_body_chars: int = 3000  # per-item body budget; longer bodies are cut with a visible marker
    triage_max_attempts: int = 3  # per news item, for content-related failures (pipeline helper)

    # --- match context + rationale (Claude Opus) ---
    context_max_tokens: int = 16000
    context_body_chars: int = 8000
    rationale_max_tokens: int = 16000
    rationale_max_rejected: int = 25  # rejected decisions listed individually in the rationale prompt
    rationale_language: str = "English"
    max_rationale_chars: int = 600  # per-flag rationale kept in the DB / prediction payload
    max_evidence_items: int = 8  # per flag

    # --- request plumbing ---
    prompt_caching: bool = True  # cache_control on the (stable) system prompt
    fallback_beta: str = SERVER_FALLBACK_BETA

    # --- flag semantics ---
    veto_min_confidence: float = VETO_MIN_CONFIDENCE
    # A stand-in / illness / visa / travel / conflict / slump flag about one team can only count
    # against that team; a suggested shift pointing the other way (a sign slip) is not applied.
    enforce_adverse_direction: bool = True

    # --- cost guard (see cs2bot.llm.memo) ---
    reanalysis_interval_s: float = 6 * 3600.0  # same inputs are not re-analysed more often than this
    near_start_window_s: float = 3 * 3600.0  # ... except close to the match start,
    near_start_interval_s: float = 3600.0  # where "lineup still unknown" becomes time-sensitive
    # After a transient failure (network, 5xx, rate limit, rejected request) the same inputs are
    # retried after this long. A refusal / truncated / invalid answer is tied to the inputs, was
    # billed, and would most likely repeat: it is retried only after the re-analysis interval.
    failure_backoff_s: float = 900.0
    memo_max_entries: int = 512
    # Rationale: an unchanged picture (same bets, flags, veto state, lineups and a match-win
    # probability within the same ``rationale_probability_step`` band) reuses the previous text.
    rationale_reuse_interval_s: float = 6 * 3600.0
    rationale_probability_step: float = 0.025
    # Triage: stop sending further batches after this many consecutive transient failures.
    triage_max_transient_failures: int = 2

    # --- news selection ---
    min_relevance: float = 0.3  # triaged items below this are not shown to the context model
    max_global_news: int = 3  # match-wide "meta_patch" items added to every match's context
