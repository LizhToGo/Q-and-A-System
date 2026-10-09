from __future__ import annotations

from types import SimpleNamespace

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
def test_assist_chat_fallback_remains_supported(monkeypatch):
    FakeClient.calls = []
    FakeClient.response_body = {"choices": [{"message": {"content": "{\"source_page\": 31}"}}]}
    monkeypatch.setattr(assist, "settings", _settings("https://example.test/v1/chat/completions"))
    monkeypatch.setattr(assist.httpx, "Client", FakeClient)

    result = assist._call_llm([{"role": "user", "content": "读取页面"}])

    assert result == {"source_page": 31}
    assert FakeClient.calls[0][0] == "https://example.test/v1/chat/completions"
