from __future__ import annotations

import asyncio
from types import SimpleNamespace
import json

import httpx

import pytest

from backend.app.services import answering
from backend.app.services.answering import LLMAnswerer


class FakeResponse:
    def __init__(self, body: dict):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeAsyncClient:
    response_body: dict = {}
    calls: list[tuple[str, dict]] = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs["json"]))
        return FakeResponse(self.response_body)

def _settings(url: str):
    return SimpleNamespace(
        llm_provider="deepseek",
        llm_base_url=url,
        llm_api_key="test-key",
        llm_model="deepseek-v4.1-flash",
        llm_gateway_messages=True,
    )


def _evidence():
    return [{"material_id": 7, "title": "采样定理", "source_anchor": "doc:1:p31", "snippet": "fmax = fs / 2"}]


def test_model_answer_accepts_verified_evidence_ids(monkeypatch):
    FakeAsyncClient.calls = []
    FakeAsyncClient.response_body = {"output_text": "```json\n{\"answer\": \"4 kHz\", \"evidence_ids\": [\"教材证据 7\"]}\n```"}
    monkeypatch.setattr(answering, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(LLMAnswerer().complete("最高频率？", _evidence()))

    assert result["evidence_ids"] == [7]


def test_non_responses_endpoint_is_rejected(monkeypatch):
    FakeAsyncClient.calls = []
    monkeypatch.setattr(answering, "settings", _settings("https://example.test/v1/chat/completions"))
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)

    answerer = LLMAnswerer()
    result = asyncio.run(answerer.complete("最高频率？", _evidence()))

    assert result is None
    assert answerer.last_error == "responses_only"
    assert FakeAsyncClient.calls == []

def test_model_text_cannot_forge_evidence_id_from_arbitrary_suffix(monkeypatch):
    FakeAsyncClient.calls = []
    FakeAsyncClient.response_body = {"choices": [{"message": {"content": "{\"answer\": \"4 kHz\", \"evidence_ids\": [\"我猜是 7\"]}"}}]}
    monkeypatch.setattr(answering, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(LLMAnswerer().complete("最高频率？", _evidence()))

    assert result is None


def test_json_parser_accepts_surrounding_model_text():
    assert LLMAnswerer._parse_json('结果如下：{"answer":"4 kHz","evidence_ids":[7]}谢谢') == {
        "answer": "4 kHz",
        "evidence_ids": [7],
    }


@pytest.mark.parametrize("encoded", [
    r'{"answer":"由\\(\\Omega_s>2\\Omega_h\\)、\\(\\theta\\)及\\(\\frac{f_s}{2}\\)得到。","evidence_ids":[7]}',
    r'{"answer":"由\(\Omega_s>2\Omega_h\)、\(\theta\)及\(\frac{f_s}{2}\)得到。","evidence_ids":[7]}',
])
def test_latex_survives_model_json_with_single_or_escaped_backslashes(encoded):
    parsed = LLMAnswerer._parse_json(encoded)
    assert parsed["answer"] == r"由\(\Omega_s>2\Omega_h\)、\(\theta\)及\(\frac{f_s}{2}\)得到。"
    assert parsed["evidence_ids"] == [7]


def test_normal_json_escapes_quotes_and_unicode_are_preserved():
    content = '{"answer":"第一行\\nnext\\tcolumn\\u4e2d，\\"quoted\\"，\\\\\\\\path","evidence_ids":[7]}'
    assert LLMAnswerer._parse_json(content) == json.loads(content)


@pytest.mark.parametrize("content", [
    r'{"answer":"\(\Omega_s\)","evidence_ids":[7]',
    r'{"answer":{"text":"\(\Omega_s\)"},"evidence_ids":[7]}',
    r'{"answer":"\(\Omega_s\)","evidence_ids":[999]}',
])
def test_escape_normalization_never_accepts_missing_structure_wrong_answer_type_or_forged_evidence(monkeypatch, content):
    FakeAsyncClient.response_body = {"output_text": content}
    monkeypatch.setattr(answering, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)
    answerer = LLMAnswerer()
    assert asyncio.run(answerer.complete("最高频率？", _evidence())) is None
    assert answerer.last_error in {"invalid_json", "missing_answer_or_valid_evidence"}


def _upstream_failure(monkeypatch, body=None, *, text=None, headers=None, api_key="test-key"):
    cfg = _settings("https://example.test/v1/responses")
    cfg.llm_api_key = api_key
    response = httpx.Response(
        429, content=text if text is not None else json.dumps(body),
        headers=headers or {},
        request=httpx.Request("POST", cfg.llm_base_url),
    )

    class ErrorClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return response

    monkeypatch.setattr(answering, "settings", cfg)
    monkeypatch.setattr(answering.httpx, "AsyncClient", ErrorClient)
    answerer = LLMAnswerer()
    assert asyncio.run(answerer.complete("最高频率？", _evidence())) is None
    assert answerer.last_error == "upstream_http_429"
    return answerer.last_diagnostics


@pytest.mark.parametrize(("outer_code", "inner_code", "message", "category"), [
    (
        "insufficient_quota", "ModelAccountTpmRateLimitExceeded",
        "The model " + "long-model-name-" * 24 + " exceeded its account token-per-minute limit; retry after the window resets.",
        "model_account_tpm",
    ),
    ("RateLimitError", "insufficient_quota", "The upstream account has no remaining credits.", "quota"),
])
def test_nested_error_code_overrides_gateway_classification_and_preserves_real_cause(
    monkeypatch, outer_code, inner_code, message, category,
):
    diagnostics = _upstream_failure(monkeypatch, {
        "error": {
            "code": outer_code, "type": "gateway_error",
            "message": "LiteLLM failed {not JSON}: " + json.dumps({"error": {"code": inner_code, "message": message}}),
        },
    })

    assert diagnostics["upstream_error_category"] == category
    assert diagnostics["upstream_error"]["code"] == outer_code
    assert diagnostics["upstream_error"]["type"] == "gateway_error"
    assert diagnostics["upstream_inner_error"]["code"] == inner_code
    assert diagnostics["upstream_inner_error"]["message"] == message
    assert inner_code in diagnostics["upstream_summary"]
    assert message in diagnostics["upstream_summary"]


def test_reflected_credentials_are_redacted_in_errors_and_only_safe_headers_are_exposed(monkeypatch):
    api_key = "sk-private-key-for-regression"
    other_secret = "another-provider-secret"
    diagnostics = _upstream_failure(
        monkeypatch,
        {
            "error": {
                "code": "gateway_error",
                "message": "Authorization Bearer " + other_secret + "; upstream: " + json.dumps({
                    "error": {"code": "invalid_token", "message": "Rejected key " + api_key},
                }),
            },
            "request": {"private": "unrelated-request-data"},
        },
        api_key=api_key,
        headers={
            "X-Request-ID": "req-123",
            "Retry-After": "30",
            "X-RateLimit-Limit-Tokens": "1000",
            "X-RateLimit-Remaining-Tokens": "0",
            "X-RateLimit-Reset-Tokens": "30s",
            "X-RateLimit-Limit-Requests": "10",
            "X-RateLimit-Remaining-Requests": "0",
            "X-RateLimit-Reset-Requests": "1m",
            "Request-ID": api_key,
            "Authorization": "Bearer " + other_secret,
            "Set-Cookie": "secret=session",
            "X-Internal-Debug": "private-header-data",
        },
    )

    exposed = json.dumps(diagnostics)
    assert api_key not in exposed
    assert other_secret not in exposed
    assert "unrelated-request-data" not in exposed
    assert "private-header-data" not in exposed
    assert "[redacted]" in exposed
    assert diagnostics["upstream_headers"]["x-request-id"] == "req-123"
    assert diagnostics["upstream_headers"]["retry-after"] == "30"
    assert diagnostics["upstream_headers"]["x-ratelimit-remaining-tokens"] == "0"
    assert diagnostics["upstream_headers"]["x-ratelimit-reset-requests"] == "1m"
    assert set(diagnostics["upstream_headers"]) <= {
        "request-id", "x-request-id", "retry-after",
        *(f"x-ratelimit-{kind}-{unit}" for kind in ("limit", "remaining", "reset") for unit in ("tokens", "requests")),
    }


@pytest.mark.parametrize(("body", "text"), [
    ({"error": {}}, None),
    ({"error": {"code": None, "type": [], "message": {"unexpected": True}}}, None),
    ({"error": "Temporary capacity unavailable"}, None),
    (None, "Temporary capacity unavailable; Bearer reflected-secret"),
    (None, '{"error": malformed JSON'),
])
def test_unknown_429_with_missing_fields_or_non_json_remains_unknown_and_safe(monkeypatch, body, text):
    diagnostics = _upstream_failure(monkeypatch, body, text=text)

    assert diagnostics["upstream_error_category"] == "unknown"
    assert "reflected-secret" not in json.dumps(diagnostics)
    assert diagnostics["upstream_summary"]


def test_bare_error_json_and_oversized_message_stay_bounded_without_losing_the_error_code(monkeypatch):
    api_key = "sk-boundary-secret"
    diagnostics = _upstream_failure(monkeypatch, {
        "code": "ModelAccountTpmRateLimitExceeded",
        "message": "Account token window exhausted. " + "x" * 20000 + " Bearer hidden-secret " + api_key,
    }, api_key=api_key, headers={"X-Request-ID": "r" * 2000})

    assert diagnostics["upstream_error_category"] == "model_account_tpm"
    assert "ModelAccountTpmRateLimitExceeded" in diagnostics["upstream_summary"]
    assert "Account token window exhausted." in diagnostics["upstream_summary"]
    assert len(diagnostics["upstream_error"]["message"]) <= 4096
    assert len(diagnostics["upstream_summary"]) <= 12288
    assert len(diagnostics["upstream_headers"]["x-request-id"]) <= 512
    assert api_key not in json.dumps(diagnostics)
    assert "hidden-secret" not in json.dumps(diagnostics)


@pytest.mark.parametrize("envelope", [
    {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output_text": '{"answer":"4 kHz","evidence_ids":[7]}'},
    {"choices": [{"finish_reason": "length", "message": {"content": '{"answer":"4 kHz","evidence_ids":[7]}'}}]},
])
def test_incomplete_generation_is_not_accepted_even_when_partial_json_is_valid(monkeypatch, envelope):
    FakeAsyncClient.response_body = envelope
    monkeypatch.setattr(answering, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)
    answerer = LLMAnswerer()
    assert asyncio.run(answerer.complete("最高频率？", _evidence())) is None
    assert answerer.last_error == "incomplete_output"
    assert answerer.last_diagnostics["incomplete_reason"] == "max_output_tokens"


def test_response_diagnostics_do_not_expose_reflected_keys_or_arbitrary_usage_fields(monkeypatch):
    FakeAsyncClient.response_body = {
        "status": "test-key", "usage": {"input_tokens": -1, "output_tokens": True, "total_tokens": 15, "private": "test-key"},
        "output_text": '{"answer":"4 kHz","evidence_ids":[7]}',
    }
    monkeypatch.setattr(answering, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)
    answerer = LLMAnswerer()
    result = asyncio.run(answerer.complete("最高频率？", _evidence()))
    assert result["answer"] == "4 kHz"
    assert answerer.last_diagnostics["response_status"] == "unknown"
    assert answerer.last_diagnostics["usage"] == {"total_tokens": 15}
    assert "test-key" not in json.dumps(answerer.last_diagnostics)


def test_same_thread_history_is_bounded_context_in_actual_model_payload(monkeypatch):
    from backend.app.provider_config import ProviderConfig

    FakeAsyncClient.calls = []
    FakeAsyncClient.response_body = {"output_text": json.dumps({"answer": "4 kHz", "evidence_ids": [7]})}
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)
    config = ProviderConfig(id="test", config_source="environment", enabled=True, provider="test", base_url="https://example.test/v1/responses", model="test", api_key="test-key")
    history = [
        {"question": f"turn-{index}:" + "Q" * 1600, "answer": f"answer-{index}:" + "A" * 3000}
        for index in range(5)
    ]
    result = asyncio.run(LLMAnswerer(config).complete("当前最高频率？", _evidence(), history=history))
    assert result["evidence_ids"] == [7]
    payload = FakeAsyncClient.calls[-1][1]
    encoded_history = payload["input"].split("历史对话（非教材证据）：\n", 1)[1].split("\n当前问题：", 1)[0]
    bounded = json.loads(encoded_history)
    assert bounded == [{"question": turn["question"][:1200], "answer": turn["answer"][:2500]} for turn in history[-3:]]
    assert "turn-0:" not in payload["input"] and "turn-1:" not in payload["input"]
    assert "当前最高频率？" in payload["input"]
    assert "[教材证据 7]" in payload["input"]


def test_history_cannot_authorize_old_evidence_ids(monkeypatch):
    FakeAsyncClient.response_body = {"output_text": json.dumps({"answer": "4 kHz", "evidence_ids": [999]})}
    monkeypatch.setattr(answering, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)
    answerer = LLMAnswerer()
    result = asyncio.run(answerer.complete("继续说明", _evidence(), history=[{"question": "旧问题", "answer": "引用教材证据 999"}]))
    assert result is None
    assert answerer.last_error == "missing_answer_or_valid_evidence"


def test_tool_plan_may_have_no_answer_until_local_results_are_executed(monkeypatch):
    request = {"tool": "convolution", "arguments": {"x": [1, 2], "h": [1, -1]}}
    FakeAsyncClient.response_body = {"output_text": json.dumps({"answer": "", "evidence_ids": [], "tool_requests": [request]})}
    monkeypatch.setattr(answering, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)
    answerer = LLMAnswerer()
    planned = asyncio.run(answerer.complete("计算卷积", []))
    assert planned is not None and planned["answer"] == ""
    results = [{"tool": "convolution", "status": "completed", "data": {"indices": [0, 1, 2], "values": [1, 1, -2]}}]
    assert asyncio.run(answerer.complete("解释结果", [], tool_results=results)) is None
    assert answerer.last_error == "invalid_tool_requests"


@pytest.mark.parametrize("title,expected", [
    ("采样定理与信号混叠", "采样定理与信号混叠"),
    (None, None),
    ("", None),
    ("$采样定理$", None),
    ("主题" * 17, None),
    ({"text": "采样定理"}, None),
])
def test_first_answer_title_is_optional_and_never_invalidates_valid_answer(monkeypatch, title, expected):
    FakeAsyncClient.calls = []
    FakeAsyncClient.response_body = {"output_text": json.dumps({"answer": "4 kHz", "evidence_ids": [7], "conversation_title": title}, ensure_ascii=False)}
    monkeypatch.setattr(answering, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)
    answerer = LLMAnswerer()
    result = asyncio.run(answerer.complete("最高频率？", _evidence(), generate_title=True))
    assert result["answer"] == "4 kHz" and result["evidence_ids"] == [7]
    assert result["conversation_title"] == expected
    assert answerer.last_error is None
    assert len(FakeAsyncClient.calls) == 1


def test_title_comes_from_final_tool_explanation_not_empty_planning_round(monkeypatch):
    FakeAsyncClient.calls = []
    request = {"tool": "convolution", "arguments": {"x": [1, 2], "h": [1, -1]}}
    FakeAsyncClient.response_body = {"output_text": json.dumps({"answer": "", "evidence_ids": [], "tool_requests": [request], "conversation_title": "提前猜测的错误主题"})}
    monkeypatch.setattr(answering, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)
    answerer = LLMAnswerer()
    plan = asyncio.run(answerer.complete("计算卷积", [], generate_title=True))
    assert plan["tool_requests"] == [request] and plan["conversation_title"] is None
    results = [{"tool": "convolution", "status": "completed", "data": {"indices": [0, 1, 2], "values": [1, 1, -2]}}]
    FakeAsyncClient.response_body = {"output_text": json.dumps({"answer": "实际离散卷积结果为1、1、-2。", "evidence_ids": [], "conversation_title": "离散序列的卷积计算"})}
    final = asyncio.run(answerer.complete("计算卷积", [], tool_results=results, generate_title=True))
    assert final["answer"] == "实际离散卷积结果为1、1、-2。"
    assert final["conversation_title"] == "离散序列的卷积计算"
    assert len(FakeAsyncClient.calls) == 2
    later = asyncio.run(answerer.complete("继续解释", [], tool_results=results))
    assert later["conversation_title"] is None


def test_query_expansion_plan_can_recover_missing_evidence_and_normalizes_duplicate_terms(monkeypatch):
    FakeAsyncClient.response_body = {"output_text": json.dumps({
        "answer": "", "evidence_ids": [], "retrieval_queries": ["混叠", " 混叠 ", "NYQUIST"],
        "conversation_title": "尚未回答就猜测的标题",
    }, ensure_ascii=False)}
    monkeypatch.setattr(answering, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)
    result = asyncio.run(LLMAnswerer().complete("为什么频谱重叠？", [], generate_title=True))
    assert result["retrieval_queries"] == ["混叠", "NYQUIST"]
    assert result["answer"] == "" and result["conversation_title"] is None


@pytest.mark.parametrize("queries", [
    "混叠", ["a", "b", "c", "d"], [None], [123], [""], ["  "], ["a\nb"], ["a" * 161],
])
def test_malformed_or_unbounded_query_expansion_is_not_used_for_retrieval(monkeypatch, queries):
    FakeAsyncClient.response_body = {"output_text": json.dumps({"answer": "", "evidence_ids": [], "retrieval_queries": queries})}
    monkeypatch.setattr(answering, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)
    answerer = LLMAnswerer()
    assert asyncio.run(answerer.complete("为什么频谱重叠？", [])) is None
    assert answerer.last_error == "invalid_retrieval_queries"


def test_final_answer_cannot_start_another_query_expansion_cycle(monkeypatch):
    FakeAsyncClient.response_body = {"output_text": json.dumps({"answer": "已有教材回答", "evidence_ids": [7], "retrieval_queries": ["混叠"]})}
    monkeypatch.setattr(answering, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)
    answerer = LLMAnswerer()
    assert asyncio.run(answerer.complete("为什么频谱重叠？", _evidence(), tool_results=[])) is None
    assert answerer.last_error == "invalid_retrieval_queries"
