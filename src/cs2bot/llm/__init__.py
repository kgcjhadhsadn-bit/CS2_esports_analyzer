"""cs2bot.llm - Claude news / context layer (see docs/dev/reports/llm.md for the public interface).

Claude reads news and match context and returns structured, bounded adjustments
(:class:`cs2bot.domain.context.ContextFlag`) plus a human-readable rationale. It never outputs the
win probabilities used for betting; every suggestion is clipped in code.
"""

from cs2bot.llm.combine import active_flags, combine_flags, flag_contribution
from cs2bot.llm.context import (
    ContextAnalyzer,
    LLMStatus,
    MatchAnalysisOutcome,
    RationaleOutcome,
    TriageOutcome,
    UsageTotals,
    suggestion_to_flag,
)
from cs2bot.llm.memo import AnalysisMemo, MemoEntry, context_fingerprint
from cs2bot.llm.pipeline import (
    ContextRefresh,
    TriageReport,
    load_known_teams,
    refresh_match_context,
    triage_pending_news,
)
from cs2bot.llm.rationale import RISK_DISCLAIMER, build_rationale_digest, render_template_rationale
from cs2bot.llm.schemas import (
    ContextFlagSuggestion,
    MatchContextResult,
    RationaleOutput,
    TriageBatch,
    TriageItem,
    TriageLabel,
)
from cs2bot.llm.store import (
    load_active_flags,
    news_row_to_input,
    persist_flags,
    select_news_for_match,
    store_triage,
)
from cs2bot.llm.tuning import VETO_MIN_CONFIDENCE, LLMTuning

__all__ = [
    "RISK_DISCLAIMER",
    "VETO_MIN_CONFIDENCE",
    "AnalysisMemo",
    "ContextAnalyzer",
    "ContextFlagSuggestion",
    "ContextRefresh",
    "LLMStatus",
    "LLMTuning",
    "MatchAnalysisOutcome",
    "MatchContextResult",
    "MemoEntry",
    "RationaleOutcome",
    "RationaleOutput",
    "TriageBatch",
    "TriageItem",
    "TriageLabel",
    "TriageOutcome",
    "TriageReport",
    "UsageTotals",
    "active_flags",
    "build_rationale_digest",
    "combine_flags",
    "context_fingerprint",
    "flag_contribution",
    "load_active_flags",
    "load_known_teams",
    "news_row_to_input",
    "persist_flags",
    "refresh_match_context",
    "render_template_rationale",
    "select_news_for_match",
    "store_triage",
    "suggestion_to_flag",
    "triage_pending_news",
]
