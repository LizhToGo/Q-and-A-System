from __future__ import annotations

import asyncio
from types import SimpleNamespace

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
    return SimpleNamespace(llm_provider="deepseek", llm_base_url=url, llm_api_key="test-key", llm_model="deepseek-v4.1-flash")


def _evidence():
    return [{"material_id": 7, "title": "采样定理", "source_anchor": "doc:1:p31", "snippet": "fmax = fs / 2"}]


def test_responses_api_uses_input_and_output_text(monkeypatch):
    FakeAsyncClient.calls = []
    FakeAsyncClient.response_body = {"output_text": "```json\n{\"answer\": \"4 kHz\", \"evidence_ids\": [\"教材证据 7\"]}\n```"}
    monkeypatch.setattr(answering, "settings", _settings("https://example.test/v1/responses"))
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(LLMAnswerer().complete("最高频率？", _evidence()))

    assert result == {"answer": "4 kHz", "evidence_ids": [7], "provider": "deepseek"}
    url, payload = FakeAsyncClient.calls[0]
    assert url == "https://example.test/v1/responses"
    assert "input" in payload and "messages" not in payload


def test_chat_completions_remains_supported(monkeypatch):
    FakeAsyncClient.calls = []
    FakeAsyncClient.response_body = {"choices": [{"message": {"content": "{\"answer\": \"4 kHz\", \"evidence_ids\": [7]}"}}]}
    monkeypatch.setattr(answering, "settings", _settings("https://example.test/v1/chat/completions"))
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(LLMAnswerer().complete("最高频率？", _evidence()))

    assert result == {"answer": "4 kHz", "evidence_ids": [7], "provider": "deepseek"}
    url, payload = FakeAsyncClient.calls[0]
    assert url == "https://example.test/v1/chat/completions"
    assert "messages" in payload and "input" not in payload

def test_model_text_cannot_forge_evidence_id_from_arbitrary_suffix(monkeypatch):
    FakeAsyncClient.calls = []
    FakeAsyncClient.response_body = {"choices": [{"message": {"content": "{\"answer\": \"4 kHz\", \"evidence_ids\": [\"我猜是 7\"]}"}}]}
    monkeypatch.setattr(answering, "settings", _settings("https://example.test/v1/chat/completions"))
    monkeypatch.setattr(answering.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(LLMAnswerer().complete("最高频率？", _evidence()))

    assert result is None


def test_json_parser_accepts_surrounding_model_text():
    assert LLMAnswerer._parse_json('结果如下：{"answer":"4 kHz","evidence_ids":[7]}谢谢') == {
        "answer": "4 kHz",
        "evidence_ids": [7],
    }
