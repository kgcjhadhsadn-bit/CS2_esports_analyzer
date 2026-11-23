# llm: implementation report

**Tests:** `.venv/bin/python -m pytest tests/llm -q` - 200 passed, 0 failed (about 7 s with `-n 2`; lint/type clean: True)

## Summary

P8b is complete: Claude Haiku triages news, Claude Opus turns the relevant news plus the match context into bounded `ContextFlag`s and writes the dashboard rationale. The model never produces a probability; every number it suggests is validated and clipped in code. Definition of done is met: 200/200 tests pass, `ruff check`, `ruff format --check` and `mypy` are clean on `src/cs2bot/llm` and `tests/llm`, and the 37 foundation tests still pass. I wrote only inside my owned paths.

No request has been sent to the real API (there is no key on this machine). What is verified is the request the installed SDK (anthropic 2.12.0) puts on the wire and how the code reacts to each kind of response; prompt quality against live models is untested.

State I found: all modules and 149 passing tests already existed, with 40 lint errors in the tests. I re-verified the code against the real SDK client over a mock HTTP transport and kept the design. What I changed or added:

- **Truncated answers were mis-reported.** The SDK's `parse()` validates the response text inside the call, so an answer cut off at `max_tokens` raises a `pydantic.ValidationError` before `stop_reason` can be read. That is now recognised (`json_invalid`) and reported as `TRUNCATED` with a clear warning. A refusal that arrives after partial output looks the same and is handled the same way (see Known limitations).
- **A rejected request no longer burns the news queue.** HTTP 400/404/413/422 (unknown model, exhausted credit, changed API) used to be treated as a problem with the articles: the batch was split item by item (about 2N requests) and every item was counted as a failed attempt, so after three runs all pending news was given up on for good. Such a response now stops the run and defers the items. Two consecutive network/server errors stop the run too.
- **Refused or truncated inputs are not re-sent every 15 minutes.** These answers are billed and repeat for identical inputs. They are retried after the re-analysis interval (6 h, 1 h close to the start); transient failures still retry after 15 minutes. New news or a lineup change is analysed at once either way.
- **Rationale cost guard (new).** `write_rationale` reuses the previous text while the picture is unchanged (same accepted bets, flags, veto state, lineups, model version, and a match-win probability within the same 2.5 pp band) for up to 6 h. Without it, a caller that re-predicts every 15 minutes would send one Opus request per match per cycle.
- **Direction check (new).** `logit_shift` is from team1's perspective whichever team the flag is about, which invites sign slips. A stand-in / illness / visa / travel / conflict / slump flag about one team whose shift would help that team is kept, but its shift is set to 0 and the rationale says so. The prompt states the rule.
- Triage verdict team ids are also kept in `news_items.extra["llm"]["triage"]`, because the HLTV sync rewrites `team_ids` when it refreshes an article; news selection reads both.
- Given-up news items can no longer starve newer pending items in `triage_pending_news`.
- `sanitize_markdown` also reduces links with a non-`http(s)` target (`javascript:`, `data:`, `//host`) to their text.
- `ContextAnalyzer.close()`.
- 51 new tests, including 20 that drive the analyzer through the real SDK client (`tests/llm/test_llm_wire.py`).

Design in brief:

| Module | Content |
|---|---|
| `context.py` | `ContextAnalyzer` (requests, error handling, disabled mode), suggestion -> flag conversion with all bounds |
| `combine.py` | `combine_flags` (pure) |
| `schemas.py` | Structured-output models: `TriageItem`/`TriageBatch`, `ContextFlagSuggestion`/`MatchContextResult`, `RationaleOutput` |
| `prompts.py` | Stable system prompts; user messages with escaped, tagged untrusted text |
| `rationale.py` | Prediction digest, deterministic template, Markdown sanitising |
| `memo.py` | Cost guard: fingerprints and a thread-safe LRU |
| `store.py` | `persist_flags`, `load_active_flags`, `store_triage`, `select_news_for_match` |
| `pipeline.py` | `triage_pending_news`, `refresh_match_context` (what the scheduler jobs call) |
| `tuning.py` | `LLMTuning`: knobs that have no field in `LLMSettings` |

How the bounds are enforced, in order:

1. Evidence must point at a supplied news item (`news-<n>` or its URL) or at `"context"`. A flag with no valid evidence is dropped.
2. Severity and confidence are clamped to [0, 1]; `logit_shift` is clipped to `+-max_flag_logit_shift`; the direction check applies.
3. A veto with confidence below 0.6 is stored as a plain flag. `engine.decision` blocks a match on any active veto row whatever its confidence, so this keeps it consistent with `combine_flags`.
4. At most one flag per (type, team): the strongest is kept, so one story cannot be split to stack adjustments.
5. `combine_flags` clips each flag again, weights by confidence, ignores expired flags and clips the total to `+-max_total_logit_shift` (0.35, about 8.7 pp at even odds).

Claude API usage, as verified on the wire:

- Opus (`settings.llm.context_model`): `client.beta.messages.parse(model, max_tokens=16000, system=[cacheable block], messages=[user], output_format=<Model>, output_config={"effort": settings.llm.context_effort}, betas=["server-side-fallback-2026-07-01"], fallbacks="default")`. With `use_server_fallbacks` off: `client.messages.parse` without `betas`/`fallbacks`.
- Haiku (`settings.llm.triage_model`): `client.messages.parse(..., output_config={"effort": settings.llm.triage_effort})`, never `betas`/`fallbacks`.
- Never sent: `temperature`, `top_p`, `top_k`, `thinking`, assistant prefill.
- `stop_reason` is checked before `parsed_output`: `refusal` (category logged) and `max_tokens` give an empty result.
- Errors: `RateLimitError`, then `APIStatusError` (401/403 switch the layer off until restart, 5xx is transient, other 4xx is a rejected request), then `APIConnectionError`. Nothing raises into the pipeline.

## Public API

Everything below is importable from `cs2bot.llm` unless a module is named.

### ContextAnalyzer

```python
ContextAnalyzer(settings: Settings, *, client: Any | None = None,
                tuning: LLMTuning | None = None, memo: AnalysisMemo | None = None)

.enabled -> bool                      # False: disabled, no credentials, or auth failed
.usage -> UsageTotals                 # requests, input/output/cache tokens, refusals, failures
.settings -> Settings   .tuning -> LLMTuning
.close() -> None                      # closes a client the analyzer created itself

.triage(items: list[tuple[int, NewsItemIn]], known_teams: dict[int, str]) -> list[TriageItem]
.triage_detailed(items, known_teams) -> TriageOutcome
.analyze_match(ctx: MatchContext, news: list[NewsItemIn], *, form_summary: dict | None = None,
               now: datetime | None = None) -> list[ContextFlag]
.analyze_match_detailed(ctx, news, *, form_summary=None, now=None, use_memo=True) -> MatchAnalysisOutcome
.write_rationale(prediction: MatchPrediction, decisions: list[BetDecision], ctx: MatchContext, *,
                 now: datetime | None = None) -> str
.write_rationale_detailed(prediction, decisions, ctx, *, now=None, use_memo=True) -> RationaleOutcome
.reanalysis_interval(ctx: MatchContext, now: datetime) -> timedelta
```

- Create one analyzer per process and share it (thread-safe; it holds the cost-guard memos). The client is built lazily as `anthropic.Anthropic(api_key=<settings.llm.api_key or None>, timeout=settings.llm.request_timeout_s)`; with no key in the settings the SDK resolves `ANTHROPIC_API_KEY` itself.
- Disabled mode (`settings.llm.enabled` false, or no credentials): `triage` and `analyze_match` return `[]`, `write_rationale` returns the deterministic template, no request is made.
- `analyze_match` returning `[]` does not tell "nothing to flag" from "could not analyse". Use `refresh_match_context` (below) or `analyze_match_detailed`, so a failed call never wipes stored flags.
- `LLMStatus`: `OK`, `DISABLED`, `REFUSED`, `TRUNCATED`, `FAILED`, `BACKOFF` (same inputs failed recently, no call made).
- `MatchAnalysisOutcome`: `status`, `flags`, `note`, `model`, `fingerprint`, `from_cache`, `n_news`, `n_dropped`, `detail`, `analyzed_at`, `.ok`.
- `RationaleOutcome`: `text`, `status`, `model` (`None` = template; store it in `match_analyses.rationale_model`), `from_cache`, `written_at`.
- `TriageOutcome`: `status`, `items`, `unanswered_ids` (content-related), `deferred_ids` (transient), `model`, `.complete`.

### Combining flags

```python
combine_flags(flags: Iterable[ContextFlag], settings: LLMSettings, *, now: datetime,
              veto_min_confidence: float = 0.6) -> tuple[float, bool]   # (logit shift for team1, veto)
active_flags(flags, *, now) -> list[ContextFlag]
flag_contribution(flag: ContextFlag, settings: LLMSettings) -> float
```

`now` must be timezone-aware. Also importable as `cs2bot.llm.context.combine_flags`.

### Persistence (`flush()`, never `commit()`)

```python
persist_flags(session, match_id: int, flags: Sequence[ContextFlag], *, model: str | None,
              team_ids: tuple[int, int], now: datetime | None = None) -> list[ContextFlagRow]
load_active_flags(session, match_id: int, *, now: datetime) -> list[ContextFlag]
store_triage(session, news_item_id: int, item: TriageItem, *, now=None, model=None) -> bool
select_news_for_match(session, ctx: MatchContext, *, days: int = 14, limit: int = 25, now=None,
                      min_relevance: float | None = None, include_untriaged: bool = True,
                      max_global: int | None = None) -> list[NewsItemIn]     # oldest first
news_row_to_input(row: NewsItem) -> NewsItemIn
```

- `persist_flags` deactivates the match's active rows and inserts the new ones; identical flags are a no-op. An empty list deactivates everything, so pass it only after a successful analysis.
- `select_news_for_match` never returns items published after `now`. Untriaged items are included, so a triage outage cannot hide a fresh roster story.

### Pipeline helpers (what the jobs should call)

```python
load_known_teams(session) -> dict[int, str]
triage_pending_news(session, analyzer, *, limit: int = 200, max_age_days: int = 30,
                    now=None) -> TriageReport      # status, candidates, triaged, unanswered, deferred
refresh_match_context(session, analyzer, ctx: MatchContext, *, now=None, form_summary=None,
                      days: int = 14, force: bool = False) -> ContextRefresh
```

`ContextRefresh`: `flags` (active, unexpired, to use now), `status`, `source` (`"llm"`, `"memo"`, `"db"`, or `"stale"` = analysis unavailable, stored flags kept), `note`, `n_news`, `fingerprint`. The fingerprint of the last successful analysis is stored in `matches.extra["llm"]["context"]`, so unchanged inputs cost no request after a restart either.

### Other

- `LLMTuning` (frozen dataclass; all fields have defaults): batch size 20, token limits, body cut-offs, `prompt_caching`, `veto_min_confidence`, `enforce_adverse_direction`, re-analysis intervals, `failure_backoff_s`, `rationale_reuse_interval_s`, `rationale_probability_step`, `rationale_language`, `min_relevance`, `max_global_news`.
- `render_template_rationale(prediction, decisions, ctx) -> str`, `build_rationale_digest(...) -> dict`, `RISK_DISCLAIMER`.
- `context_fingerprint(ctx, news) -> str`, `rationale_fingerprint(prediction, decisions, ctx, *, probability_step=0.025) -> str`, `AnalysisMemo`, `MemoEntry`.
- `suggestion_to_flag(...)`, `ADVERSE_FLAG_TYPES`, `VETO_MIN_CONFIDENCE`.
- Schemas: `TriageLabel`, `TriageItem`, `TriageBatch`, `ContextFlagSuggestion`, `MatchContextResult`, `RationaleOutput`.
- `cs2bot.llm.rationale.sanitize_markdown(text) -> str` for any other LLM-derived text shown in the dashboard.

### Wiring example

I ran this sequence against a synthetic database together with `datasets.load_match_context` (fake client).

```python
from cs2bot.datasets.context import load_match_context
from cs2bot.llm import ContextAnalyzer, combine_flags, refresh_match_context, triage_pending_news

analyzer = ContextAnalyzer(settings)            # once per process; analyzer.close() at shutdown

# news job (after hltv sync_news)
with session_scope(session_factory) as session:
    report = triage_pending_news(session, analyzer)

# prediction service, per match
ctx = load_match_context(session, match_id, settings=settings, now=now, include_context_flags=False)
refresh = refresh_match_context(session, analyzer, ctx, now=now)
logit_shift, veto = combine_flags(refresh.flags, settings.llm, now=now)
prediction = build_match_prediction(..., logit_shift=logit_shift, context_flags=refresh.flags)

# after pricing and decisions
outcome = analyzer.write_rationale_detailed(prediction, decisions, ctx, now=now)
analysis_row.rationale, analysis_row.rationale_model = outcome.text, outcome.model
```

`veto` needs no extra handling for betting: `engine.decision` reads active veto rows and `prediction.context_flags` itself.

## Known limitations

- **Not run against the live API.** Prompts, effort levels and token limits are untested with real models. Check the first live runs: `analyzer.usage`, the stored `note`, and the warnings.
- **Refusal after partial output.** The SDK raises before the response is returned, so this case is reported as `TRUNCATED` and its refusal category cannot be logged. A refusal with no output is detected normally. Reading the stop reason in this case would need `messages.create` plus manual parsing instead of the `parse()` call the brief prescribes.
- **Cost guards are partly in memory.** The match analysis survives a restart through the database marker; the rationale memo and the failure back-off do not, so each match's rationale is written once more after a restart.
- **Reused rationale text can be stale.** It quotes the numbers of `written_at`: odds, EV and reasons for rejected outcomes may have moved, and the win probability by up to 2.5 pp. `use_memo=False` forces a rewrite.
- **The direction check can discard a legitimate case**, for example a stand-in who is an upgrade. The prompt tells the model to report that as `roster_change` or `other`.
- **The effect of flags is not backtested.** There is no historical news, so the size of the bounds rests on judgement, not on measurement.
- **Triage team matching is precision-first.** Candidates are exact, case-sensitive whole-word name matches plus ids the scraper attached. A team mentioned only by a nickname gets no id, and the item then reaches a match only through its player ids.
- 401/403 switch the layer off until the process restarts.
- `select_news_for_match` filters the 14-day window in Python (a few hundred rows).
- `LLMTuning` values cannot be set from `.env`; see the change request below.

## Foundation change requests

None is required. Two optional ones:

- `docs/ARCHITECTURE.md`, P8 line: the signature is `combine_flags(flags, settings.llm, *, now) -> (logit_shift, veto)`; the text omits the required `now`.
- `src/cs2bot/config.py`, `LLMSettings`: add the knobs the owner is most likely to tune, with the defaults of `LLMTuning`: `reanalysis_interval_s: float = 21600.0`, `rationale_reuse_interval_s: float = 21600.0`, `rationale_language: str = "English"`, `min_news_relevance: float = 0.3`. Until then, pass `tuning=LLMTuning(...)` to `ContextAnalyzer`.

## New dependency requests

None.
