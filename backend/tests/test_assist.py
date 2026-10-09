from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.services.ingestion import assist


class FakeResponse:
    def __init__(self, body: dict):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeClient:
    response_body: dict = {}
    calls: list[tuple[str, dict]] = []

    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs["json"]))
        return FakeResponse(self.response_body)


def _settings(url: str):
    return SimpleNamespace(llm_provider="deepseek", llm_base_url=url, llm_api_key="test-key", llm_model="test-model")


def test_assist_responses_payload_and_output(monkeypatch):
    FakeClient.calls = []
    FakeClient.response_body = {"output_text": "```json\n{\"source_page\": 31, \"normalized_text\": \"采样定理\"}\n```"}
    monkeypatch.setattr(assist, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(assist.httpx, "Client", FakeClient)

    result = assist._call_llm([
        {"role": "system", "content": "严格输出 JSON"},
        {"role": "user", "content": [{"type": "text", "text": "读取页面"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]},
    ])

    assert result["source_page"] == 31
    url, payload = FakeClient.calls[0]
    assert url == "https://example.test/v1/responses"
    assert payload["instructions"] == "严格输出 JSON"
    assert payload["input"][0]["content"][1]["type"] == "input_image"
    assert payload["messages"][1]["role"] == "user"



def test_assist_responses_gateway_choices_shape(monkeypatch):
    FakeClient.calls = []
    FakeClient.response_body = {"choices": [{"message": {"content": "{\"source_page\": 31}"}}]}
    monkeypatch.setattr(assist, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(assist.httpx, "Client", FakeClient)

    result = assist._call_llm([{"role": "user", "content": "读取页面"}])

    assert result == {"source_page": 31}
    assert "messages" in FakeClient.calls[0][1]


def test_assist_rejects_chat_endpoint(monkeypatch):
    monkeypatch.setattr(assist, "settings", _settings("https://example.test/v1/chat/completions"))

    with pytest.raises(assist.AssistUnavailable, match="只支持 Responses API"):
        assist._call_llm([{"role": "user", "content": "读取页面"}])


def test_assist_json_parser_skips_explanation_example():
    result = assist._json_content(
        '说明 {"example": "not the result"}\n```json\n{"source_page": 31, "normalized_text": "采样定理"}\n```'
    )

    assert result["source_page"] == 31
    assert result["normalized_text"] == "采样定理"


def test_assist_responses_prefers_message_over_reasoning():
    result = assist._response_content(
        {
            "output": [
                {"type": "reasoning", "content": [{"type": "output_text", "text": "内部推理"}]},
                {"type": "message", "content": [{"type": "output_text", "text": "{\"source_page\": 31}"}]},
            ]
        }
    )

    assert result == "{\"source_page\": 31}"


def test_assist_repairs_invalid_json_once(monkeypatch):
    responses = iter(["不是 JSON", "{\"source_page\": 31, \"normalized_text\": \"采样定理\"}"])
    captured: list[list[dict[str, str]]] = []

    def fake_request(messages):
        captured.append(messages)
        return next(responses)

    monkeypatch.setattr(assist, "_request_content", fake_request)
    result = assist._call_llm([{"role": "user", "content": "读取页面"}])

    assert result["source_page"] == 31
    assert len(captured) == 2
    assert "不是 JSON" in captured[1][1]["content"]


def test_assist_responses_text_input_uses_gateway_compatible_string(monkeypatch):
    FakeClient.calls = []
    FakeClient.response_body = {"output_text": "{\"source_page\": 31}"}
    monkeypatch.setattr(assist, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(assist.httpx, "Client", FakeClient)

    assist._call_llm([{"role": "user", "content": "读取页面"}])

    payload = FakeClient.calls[0][1]
    assert payload["input"] == "读取页面"


def test_assist_retries_responses_after_empty_output(monkeypatch):
    calls: list[bool] = []

    def fake_request(_messages, *, compatibility_retry=False):
        calls.append(compatibility_retry)
        if compatibility_retry:
            return "{\"source_page\": 31, \"normalized_text\": \"采样定理\"}"
        raise assist.EmptyModelOutput("empty")

    monkeypatch.setattr(assist, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(assist, "_request_content", fake_request)

    result = assist._call_llm([{"role": "user", "content": "读取页面"}])

    assert result["source_page"] == 31
    assert calls == [False, True]


def test_assist_reports_continuous_empty_output(monkeypatch):
    def fake_request(_messages, *, compatibility_retry=False):
        raise assist.EmptyModelOutput("empty")

    monkeypatch.setattr(assist, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(assist, "_request_content", fake_request)

    try:
        assist._call_llm([{"role": "user", "content": "读取页面"}])
    except assist.AssistUnavailable as exc:
        assert "连续两次返回空文本" in str(exc)
    else:
        raise AssertionError("expected AssistUnavailable")
