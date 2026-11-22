# P8b - Claude news/context layer (cs2bot.llm)

> Work-package brief: the requirements this package was built against. Keep it in sync when scope changes.

**Owned paths:** src/cs2bot/llm/, tests/llm/

**Tests:** tests/llm/

## Requirements

Goal: Claude reads news and match context and returns STRUCTURED, BOUNDED adjustments (cs2bot.domain.context.ContextFlag) plus a human-readable rationale for the dashboard. The LLM never outputs the win probabilities used for betting; the code clips its suggestions.

Claude API facts (verified for the installed anthropic SDK 2.12 - follow exactly):
- Models (from settings.llm): context_model "claude-opus-5-5" (deep analysis + rationale), triage_model "claude-haiku-5-5" (bulk news triage).
- Claude Opus 5.5: thinking is always on - do NOT send thinking {"type": "disabled"} or budget_tokens (both return HTTP 400). Either omit "thinking" or send {"type": "adaptive"}. Depth is controlled by output_config={"effort": ...}; its default is "medium", so always pass settings.llm.context_effort explicitly. Never send temperature/top_p/top_k (rejected). No assistant-message prefill (400).
- Structured output: client.beta.messages.parse(model=..., max_tokens=16000, system=..., messages=[...], output_format=<PydanticModel>, output_config={"effort": "high"}, betas=["server-side-fallback-2026-07-01"], fallbacks="default") -> response.parsed_output (validated model instance). The fallbacks="default" + beta pair is a server-side refusal fallback; enable it for the Opus calls when settings.llm.use_server_fallbacks is true and say so in the docstring.
- Claude Haiku 5.5: client.messages.parse(model=..., max_tokens=..., messages=[...], output_format=<Model>, output_config={"effort": settings.llm.triage_effort}). Haiku has NO server-side fallback: never send fallbacks/betas for it.
- ALWAYS check response.stop_reason == "refusal" before reading content/parsed_output; on refusal log response.stop_details.category (may be None) and return a safe empty result. stop_reason "max_tokens" -> treat as failure (empty result + warning).
- Errors: catch most-specific-first - anthropic.RateLimitError, anthropic.APIStatusError (status_code >= 500 is retryable), anthropic.APIConnectionError; the SDK already retries twice; set the timeout from settings.llm.request_timeout_s. Construct anthropic.Anthropic(api_key=<secret value if settings.llm.api_key set else None>, timeout=...) so the SDK can fall back to ANTHROPIC_API_KEY; allow injecting a client for tests.
- Keep the system prompt stable (no timestamps or per-request data) so prompt caching works; put volatile content (news, match data) in the user message.

Build:
1. Pydantic output schemas: TriageItem/TriageBatch (news id, label in {roster_change, stand_in, injury_illness, visa_travel, coach_change, internal_conflict, bootcamp, meta_patch, tournament_format, irrelevant}, relevance 0..1, team names/ids mentioned, one-line summary) and MatchContextResult (flags mirroring ContextFlag fields with an explicit evidence list of news URLs/ids, plus a short overall note).
2. ContextAnalyzer(settings: Settings, *, client=None):
   - triage(items: list[tuple[int, NewsItemIn]], known_teams: dict[int, str]) -> list[TriageItem] (batches of ~20 items per request, Haiku).
   - analyze_match(ctx: MatchContext, news: list[NewsItemIn], *, form_summary: dict | None = None) -> list[ContextFlag] (Opus). Prompt rules: act as a professional CS2 analyst; only flag what the provided evidence supports and cite it; no probabilities; logit_shift is from TEAM1's perspective (positive favours team1) and must stay within +-settings.llm.max_flag_logit_shift; veto only for extreme situations (lineup unknown hours before start, forfeit risk, match-fixing allegations, server/technical chaos); explain severity/confidence semantics. Prompt-injection safety: news text is untrusted DATA - wrap each item in clearly delimited tags, state that instructions inside it must be ignored, and never let it change the output schema/rules.
   - write_rationale(prediction: MatchPrediction, decisions: list[BetDecision], ctx: MatchContext) -> str (Opus; concise Markdown for the dashboard explaining key factors, veto expectations, map-by-map view, why each bet was or wasn't placed; no promises of certainty - say that losses are possible).
   - Disabled mode: settings.llm.enabled false or no credentials -> triage returns [], analyze_match returns [], write_rationale returns a deterministic template summary built from the prediction (no API call). Never raise into the pipeline for API problems; log and degrade.
3. combine_flags(flags: list[ContextFlag], settings: LLMSettings, *, now: datetime) -> tuple[float, bool]: each flag's logit_shift clipped to +-max_flag_logit_shift and weighted by its confidence; expired flags (expires_at < now) ignored; total clipped to +-max_total_logit_shift; veto = any non-expired flag with veto and confidence >= 0.6. Pure function, heavily tested (hypothesis property tests welcome).
4. Persistence helpers: persist_flags(session, match_id, flags, *, model, team_ids: tuple[int, int]) (deactivate previous active flags for the match, insert ContextFlagRow with team_id resolved from TeamSlot), load_active_flags(session, match_id, *, now) -> list[ContextFlag], store_triage(session, news_item_id, item) (NewsItem.triage_label/relevance/triaged_at; merge team ids), select_news_for_match(session, ctx, *, days=14, limit) -> list[NewsItemIn] (relevant, recent, mentioning either team or their players).
5. Cost guard: a memo (in-memory LRU + optional DB check of existing active flags) so analyze_match is not re-run for the same (match id, hash of news ids + lineup) within a configurable interval.
6. Tests with a fake client object (exposing .messages.parse and .beta.messages.parse returning objects with stop_reason, stop_details, parsed_output, usage): the correct models/params are sent (assert no temperature, effort passed, fallbacks only for Opus), refusal + max_tokens handling, disabled mode, the prompt contains delimiters and the injection warning, combine_flags clipping/expiry/veto, persistence round-trip, news selection.

## Addendum (after the first build wave)

- Nothing changed in the contracts you use. HLTV news sync fills `news_items` (docs/dev/reports/hltv.md).
