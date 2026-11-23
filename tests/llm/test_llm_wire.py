"""The analyzer driven through the REAL anthropic SDK client over a mock HTTP transport.

The other test modules use a hand-written fake client, which proves what the analyzer *asks* the SDK
for. These tests prove that the installed SDK accepts those calls and turns them into the HTTP
request the brief describes (body fields, beta header), and that the SDK's actual behaviour for
refusals, truncated answers and HTTP errors is handled. No network: every request is answered by
``httpx2.MockTransport``.

The response bodies follow the Messages API response shape (``id``, ``type``, ``role``, ``model``,
``content``, ``stop_reason``, ``stop_details``, ``usage``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
import httpx2
import pytest

from cs2bot.domain.enums import ContextFlagType, TeamSlot
from cs2bot.llm.context import ContextAnalyzer, LLMStatus
from cs2bot.llm.schemas import TriageLabel
from cs2bot.llm.tuning import SERVER_FALLBACK_BETA
from tests.llm.llm_factories import (
    NOW,
    make_ctx,
    make_decision,
    make_news,
    make_prediction,
    make_settings,
)

FORBIDDEN_BODY_KEYS = ("temperature", "top_p", "top_k", "thinking", "tool_choice", "tools")


class Wire:
    """Real ``anthropic.Anthropic`` client whose HTTP layer replays scripted responses."""

    def __init__(self, *responses: tuple[int, dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.requests: list[httpx2.Request] = []
        self.client = anthropic.Anthropic(
            api_key="sk-test-not-a-real-key",
            base_url="https://api.anthropic.com",
            max_retries=0,  # the SDK's own retries are not under test (and would slow the suite down)
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self._handle)),
        )

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError(f"unexpected HTTP request #{len(self.requests)}")
        status, body = self._responses.pop(0)
        return httpx2.Response(status, json=body)

    def body(self, index: int = -1) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.requests[index].content)
        return loaded

    def betas(self, index: int = -1) -> list[str]:
        header = self.requests[index].headers.get("anthropic-beta", "")
        return [part.strip() for part in header.split(",") if part.strip()]


def message(
    payload: Any = None,
    *,
    text: str | None = None,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5-5",
    stop_details: dict[str, Any] | None = None,
    thinking: bool = True,
) -> tuple[int, dict[str, Any]]:
    """A 200 response. ``payload`` is JSON-encoded into the text block; ``text`` sets it verbatim."""
    content: list[dict[str, Any]] = []
    if thinking:  # thinking is always on for these models; its text is omitted by default
        content.append({"type": "thinking", "thinking": "", "signature": "sig"})
    if payload is not None or text is not None:
        content.append({"type": "text", "text": text if text is not None else json.dumps(payload)})
    return 200, {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": {
            "input_tokens": 900,
            "output_tokens": 150,
            "cache_read_input_tokens": 700,
            "cache_creation_input_tokens": 0,
        },
    }


def api_error(status: int, kind: str = "api_error") -> tuple[int, dict[str, Any]]:
    return status, {"type": "error", "error": {"type": kind, "message": f"simulated {kind}"}}


FLAG = {
    "flag_type": "stand_in",
    "team": "team2",
    "severity": 0.6,
    "confidence": 0.9,
    "logit_shift": 0.5,
    "veto": False,
    "rationale": "Bravo Foxes play with an emergency stand-in.",
    "evidence": ["news-1"],
    "expires_at": None,
}


def _analyzer(wire: Wire, **llm: Any) -> ContextAnalyzer:
    return ContextAnalyzer(make_settings(**llm), client=wire.client)


def _assert_body_rules(body: dict[str, Any]) -> None:
    for key in FORBIDDEN_BODY_KEYS:
        assert key not in body
    assert [m["role"] for m in body["messages"]] == ["user"]  # no assistant prefill
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "stream" not in body or body["stream"] is False


def test_opus_context_request_on_the_wire() -> None:
    wire = Wire(message({"flags": [FLAG], "note": "One stand-in reported."}))
    analyzer = _analyzer(wire)

    outcome = analyzer.analyze_match_detailed(make_ctx(), [make_news(1)], now=NOW)

    request = wire.requests[0]
    assert request.method == "POST" and request.url.path == "/v1/messages"
    assert SERVER_FALLBACK_BETA in wire.betas()
    body = wire.body()
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 16000
    assert body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "high"
    schema = body["output_config"]["format"]["schema"]
    assert set(schema["properties"]) == {"flags", "note"}
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "<news_item " in body["messages"][0]["content"]
    _assert_body_rules(body)

    assert outcome.status is LLMStatus.OK and outcome.model == "claude-opus-5-5"
    (flag,) = outcome.flags
    assert flag.flag_type is ContextFlagType.STAND_IN and flag.team is TeamSlot.TEAM2
    assert flag.logit_shift == pytest.approx(0.20)  # the model said 0.5; clipped in code
    assert flag.evidence == [make_news(1).url]
    assert (analyzer.usage.requests, analyzer.usage.input_tokens, analyzer.usage.cache_read_input_tokens) == (
        1,
        900,
        700,
    )


def test_opus_request_without_server_fallback_carries_no_beta() -> None:
    wire = Wire(message({"flags": [], "note": "Nothing notable."}))
    outcome = _analyzer(wire, use_server_fallbacks=False, context_effort="xhigh").analyze_match_detailed(
        make_ctx(), [], now=NOW
    )
    assert outcome.ok and outcome.flags == []
    body = wire.body()
    assert "fallbacks" not in body and wire.betas() == []
    assert body["output_config"]["effort"] == "xhigh"
    _assert_body_rules(body)


def test_haiku_triage_request_on_the_wire() -> None:
    verdict = {
        "news_id": 7,
        "label": "stand_in",
        "relevance": 1.7,  # out of range: clamped, not rejected
        "team_names": ["Bravo Foxes"],
        "team_ids": [2, 999],
        "summary": "Bravo Foxes field a stand-in.",
    }
    wire = Wire(message({"items": [verdict]}, model="claude-haiku-5-5"))
    analyzer = _analyzer(wire)

    (item,) = analyzer.triage([(7, make_news(1, title="Bravo Foxes field a stand-in"))], {2: "Bravo Foxes"})

    body = wire.body()
    assert body["model"] == "claude-haiku-5-5"
    assert body["output_config"]["effort"] == "low"
    assert "fallbacks" not in body and wire.betas() == []  # Haiku has no server-side fallback
    assert 'news_id="7"' in body["messages"][0]["content"]
    _assert_body_rules(body)
    assert item.label is TriageLabel.STAND_IN and item.relevance == 1.0 and item.team_ids == [2]


def test_rationale_request_on_the_wire() -> None:
    markdown = "## Summary\nAlpha Wolves are favoured.\n\n## Risks\nLosses are possible."
    wire = Wire(message({"markdown": markdown}))
    outcome = _analyzer(wire).write_rationale_detailed(
        make_prediction(), [make_decision(accepted=True)], make_ctx(), now=NOW
    )
    assert outcome.status is LLMStatus.OK and outcome.text == markdown
    body = wire.body()
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "high"
    assert "<analysis_data>" in body["messages"][0]["content"]
    _assert_body_rules(body)


def test_model_that_actually_served_the_request_is_reported() -> None:
    """After a server-side fallback ``response.model`` names the fallback model: that is what is stored."""
    wire = Wire(message({"flags": [], "note": "ok"}, model="claude-opus-4-8"))
    assert _analyzer(wire).analyze_match_detailed(make_ctx(), [], now=NOW).model == "claude-opus-4-8"


def test_real_refusal_is_detected_and_its_category_logged(caplog: pytest.LogCaptureFixture) -> None:
    details = {"type": "refusal", "category": "cyber", "explanation": "declined"}
    wire = Wire(message(stop_reason="refusal", stop_details=details, thinking=False))
    analyzer = _analyzer(wire)
    with caplog.at_level(logging.WARNING):
        outcome = analyzer.analyze_match_detailed(make_ctx(), [make_news(1)], now=NOW)
    assert outcome.status is LLMStatus.REFUSED and outcome.flags == []
    assert "category=cyber" in caplog.text and analyzer.usage.refusals == 1


def test_real_refusal_without_a_category() -> None:
    details = {"type": "refusal", "category": None, "explanation": None}
    wire = Wire(message(stop_reason="refusal", stop_details=details))
    assert _analyzer(wire).analyze_match_detailed(make_ctx(), [], now=NOW).status is LLMStatus.REFUSED


def test_real_truncated_answer_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    """The SDK raises a ValidationError from inside parse() for cut-off JSON; that is a truncation."""
    wire = Wire(message(text='{"flags": [{"flag_type": "stand_in", "tea', stop_reason="max_tokens"))
    with caplog.at_level(logging.WARNING):
        outcome = _analyzer(wire).analyze_match_detailed(make_ctx(), [make_news(1)], now=NOW)
    assert outcome.status is LLMStatus.TRUNCATED and outcome.flags == []
    assert "max_tokens" in caplog.text


def test_real_truncation_before_any_text_is_read_from_the_stop_reason() -> None:
    wire = Wire(message(stop_reason="max_tokens"))  # thinking used up the whole budget
    outcome = _analyzer(wire).analyze_match_detailed(make_ctx(), [], now=NOW)
    assert outcome.status is LLMStatus.TRUNCATED and outcome.detail == "max_tokens"


def test_refusal_after_partial_output_degrades_safely() -> None:
    """Partial text cannot be parsed, so the SDK raises before the stop reason is visible."""
    details = {"type": "refusal", "category": None, "explanation": None}
    wire = Wire(message(text='{"markdown": "## Summ', stop_reason="refusal", stop_details=details))
    outcome = _analyzer(wire).write_rationale_detailed(
        make_prediction(), [make_decision(accepted=True)], make_ctx(), now=NOW
    )
    assert outcome.status is LLMStatus.TRUNCATED and outcome.model is None
    assert "Automatic summary" in outcome.text  # the deterministic template


@pytest.mark.parametrize(
    ("status", "kind", "detail"),
    [
        (429, "rate_limit_error", "rate_limited"),
        (500, "api_error", "server_500"),
        (529, "overloaded_error", "server_529"),
        (400, "invalid_request_error", "http_400"),
        (404, "not_found_error", "http_404"),
    ],
)
def test_real_http_errors_degrade_to_an_empty_result(status: int, kind: str, detail: str) -> None:
    wire = Wire(api_error(status, kind))
    analyzer = _analyzer(wire)
    outcome = analyzer.analyze_match_detailed(make_ctx(), [make_news(1)], now=NOW)
    assert outcome.status is LLMStatus.FAILED and outcome.flags == [] and outcome.detail == detail
    assert analyzer.enabled and analyzer.usage.failures == 1


@pytest.mark.parametrize(("status", "kind"), [(401, "authentication_error"), (403, "permission_error")])
def test_real_auth_errors_switch_the_layer_off(
    status: int, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    wire = Wire(api_error(status, kind))
    analyzer = _analyzer(wire)
    with caplog.at_level(logging.DEBUG):
        assert analyzer.analyze_match(make_ctx(), [make_news(1)], now=NOW) == []
        assert not analyzer.enabled
        assert analyzer.triage([(1, make_news(1))], {}) == []
    assert len(wire.requests) == 1
    assert "sk-test" not in caplog.text


def test_real_connection_failure_degrades() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route to host", request=request)

    client = anthropic.Anthropic(
        api_key="sk-test-not-a-real-key",
        base_url="https://api.anthropic.com",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(refuse)),
    )
    analyzer = ContextAnalyzer(make_settings(), client=client)
    outcome = analyzer.analyze_match_detailed(make_ctx(), [make_news(1)], now=NOW)
    assert outcome.status is LLMStatus.FAILED and outcome.detail == "connection"
    assert analyzer.triage([(1, make_news(1))], {}) == []


def test_client_built_from_settings_uses_the_configured_timeout_and_sdk_retries() -> None:
    analyzer = ContextAnalyzer(make_settings(request_timeout_s=123.0))
    assert analyzer.enabled
    client = analyzer._get_client()
    assert isinstance(client, anthropic.Anthropic)
    assert client.timeout == 123.0 and client.max_retries == 2
    assert client.api_key == "sk-test-not-a-real-key"
    analyzer.close()
    assert client.is_closed()
    assert analyzer.enabled  # a new client is created on demand


def test_close_leaves_an_injected_client_alone() -> None:
    wire = Wire(message({"flags": [], "note": "ok"}))
    analyzer = _analyzer(wire)
    analyzer.close()
    assert not wire.client.is_closed()
    assert analyzer.analyze_match_detailed(make_ctx(), [], now=NOW).ok
