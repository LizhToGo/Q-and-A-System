from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from backend.app.provider_config import ProviderConfig
from backend.app.services.ingestion import assist, page_transcription as transcription


PAGE = 31
TEXT = "# 2.1 采样定理\n\n若信号带限，采样频率必须满足条件。\n\n\\[X(s)=\\frac{1}{s+a},\\quad a>0\\]"
FORMULA = r"\[X(s)=\frac{1}{s+a},\quad a>0\]"


def _config(*, gateway_messages=False):
    return ProviderConfig(id="test", config_source="environment", enabled=True, provider="test-provider",
                          base_url="https://example.test/v1/responses", model="test-model",
                          api_key="test-only-credential", gateway_messages=gateway_messages)


def _result(text=TEXT, *, page=PAGE, image_observed=False):
    return {
        "source_page": page,
        "transcription_complete": True,
        "image_observed": image_observed,
        "item_type": "text",
        "normalized_text": text,
        "payload": {"section": "2.1 采样定理", "chapter": "", "content_format": "markdown",
                    "figures": [], "tables": [],
                    "uncertainties": ["须核对原页字形"]},
        "confidence": 0.9,
        "warnings": [],
    }


class FakeClient:
    responses: list[dict | httpx.Response] = []
    calls: list[tuple[str, dict]] = []

    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs["json"]))
        response = self.responses.pop(0)
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response, request=httpx.Request("POST", url))


@pytest.fixture
def model(monkeypatch):
    cfg = _config()
    FakeClient.calls = []
    FakeClient.responses = []
    monkeypatch.setattr(assist.httpx, "Client", FakeClient)
    monkeypatch.setattr(transcription, "get_provider_config", lambda: cfg)
    monkeypatch.setattr(assist, "get_provider_config", lambda *_args: cfg)
    return FakeClient


@pytest.fixture
def document():
    return SimpleNamespace(id=2, page_count=379, path="original.pdf")


def _respond(model, result):
    model.responses.append({"status": "completed", "output_text": json.dumps(result, ensure_ascii=False)})


def test_full_text_preserves_paragraphs_equations_conditions_and_no_image(model, document):
    result = _result()
    _respond(model, result)

    actual = transcription.transcribe_page(document, PAGE, native_text=TEXT, mode="text")

    assert actual["source_page"] == PAGE
    assert actual["diagnostics"]["human_reviewed"] is False
    assert actual["diagnostics"]["vision_capability"] == "not_used"


def test_auto_with_actual_ocr_never_renders_or_claims_image(model, document, monkeypatch):
    def no_render(*_args):
        raise AssertionError("text-only auto must not render")

    monkeypatch.setattr(transcription, "render_page_bytes", no_render)
    _respond(model, _result())
    actual = transcription.transcribe_page(document, PAGE, ocr_text=TEXT)
    assert actual["diagnostics"]["mode"] == "text"
    assert actual["diagnostics"]["image_observed"] is False


def test_text_without_source_does_not_call_provider(model, document):
    with pytest.raises(assist.AssistUnavailable, match="没有实际"):
        transcription.transcribe_page(document, PAGE, mode="text")
    assert not model.calls


def test_vision_renders_exact_original_page_and_standard_image(model, document, monkeypatch):
    rendered = []

    def render(source, page):
        rendered.append((source, page))
        return b"original-page-png"

    monkeypatch.setattr(transcription, "render_page_bytes", render)
    _respond(model, _result(image_observed=True))
    actual = transcription.transcribe_page(document, PAGE, mode="vision", config=_config(gateway_messages=True))

    assert rendered == [(document, PAGE)]
    assert actual["diagnostics"]["image_input"] is True
    assert actual["diagnostics"]["human_reviewed"] is False
    url, payload = model.calls[0]
    assert url.endswith("/responses")
    image_parts = [part for entry in payload["input"] for part in entry["content"] if part["type"] == "input_image"]
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"].startswith("data:image/png;base64,")
    assert "messages" in payload


def test_auto_without_text_uses_real_page_image(model, document, monkeypatch):
    monkeypatch.setattr(transcription, "render_page_bytes", lambda *_args: b"page")
    _respond(model, _result(image_observed=True))
    actual = transcription.transcribe_page(document, PAGE)
    assert actual["diagnostics"]["mode"] == "vision"


def test_unsupported_images_are_not_successful_text_fallback(model, document, monkeypatch):
    monkeypatch.setattr(transcription, "render_page_bytes", lambda *_args: b"page")
    model.responses.append(httpx.Response(
        400, json={"error": {"code": "unsupported_image", "message": "Model does not support image input"}},
        request=httpx.Request("POST", "https://example.test/v1/responses"),
    ))
    with pytest.raises(assist.UnsupportedImages) as error:
        transcription.transcribe_page(document, PAGE)
    assert error.value.status_code == 400
    assert len(model.calls) == 1


def test_model_admitting_unseen_image_is_rejected(model, document, monkeypatch):
    monkeypatch.setattr(transcription, "render_page_bytes", lambda *_args: b"page")
    result = _result(image_observed=False)
    result["transcription_complete"] = False
    _respond(model, result)
    with pytest.raises(assist.UnsupportedImages):
        transcription.transcribe_page(document, PAGE, mode="vision")


@pytest.mark.parametrize("change", [
    {"source_page": 32}, {"source_page": "31"}, {"source_page": True},
    {"normalized_text": ""}, {"normalized_text": None}, {"transcription_complete": False},
    {"item_type": "unknown"}, {"confidence": float("inf")},
])
def test_wrong_page_empty_unknown_or_incomplete_never_succeeds(model, document, change):
    result = _result()
    result.update(change)
    _respond(model, result)
    with pytest.raises(assist.AssistUnavailable):
        transcription.transcribe_page(document, PAGE, native_text=TEXT, mode="text")


def test_summary_is_rejected_even_if_model_claims_complete(model, document):
    _respond(model, _result("本页主要介绍采样定理。"))
    with pytest.raises(assist.IncompleteModelOutput, match="摘要"):
        transcription.transcribe_page(document, PAGE, ocr_text="原文完整段落与公式。" * 150)


@pytest.mark.parametrize("body", [
    {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output_text": json.dumps(_result())},
    {"output": [{"type": "message", "status": "incomplete", "content": [{"type": "output_text", "text": json.dumps(_result())}]}]},
    {"output_text": json.dumps(_result()), "choices": [{"finish_reason": "length", "message": {"content": json.dumps(_result())}}]},
])
def test_truncated_response_envelope_rejects_even_complete_json(model, document, body):
    model.responses.append(body)
    with pytest.raises(assist.IncompleteModelOutput):
        transcription.transcribe_page(document, PAGE, ocr_text=TEXT)
    assert len(model.calls) == 1


@pytest.mark.parametrize("content", [
    "不是 JSON", '{"source_page":31,"normalized_text":"正文", "payload": {"source_page":31}',
    '{"source_page":31,"normalized_text":"正文',
])
def test_invalid_or_truncated_json_is_not_model_repaired(model, document, content):
    model.responses.append({"output_text": content})
    with pytest.raises(assist.AssistUnavailable, match="完整合法"):
        transcription.transcribe_page(document, PAGE, ocr_text=TEXT)
    assert len(model.calls) == 1


def test_latex_json_escapes_preserve_frac_beta_unknown_and_linebreaks():
    parsed = assist._json_content(
        r'{"source_page":31,"normalized_text":"\(\frac{\beta}{2}+\unknown{x}\)\nNext paragraph\n\n最后一段"}'
    )
    assert parsed["normalized_text"] == "\\(\\frac{\\beta}{2}+\\unknown{x}\\)\nNext paragraph\n\n最后一段"
    assert "\f" not in parsed["normalized_text"]
    assert "\b" not in parsed["normalized_text"]


def test_already_valid_escaped_math_and_unicode_are_unchanged():
    text = "\\[\\text{a}=\\frac{1}{2}\\]\nThe next paragraph.\n\n末段"
    assert assist._json_content(json.dumps({"source_page": PAGE, "normalized_text": text}))["normalized_text"] == text
    assert assist._json_content(r'{"source_page":31,"normalized_text":"\u91c7\u6837"}')["normalized_text"] == "采样"


def test_parser_skips_explanation_example_but_rejects_multiple_pages():
    parsed = assist._json_content('说明 {"example":"not the result"}\n```json\n{"source_page":31,"normalized_text":"正文"}\n```')
    assert parsed["source_page"] == PAGE
    with pytest.raises(ValueError, match="多个"):
        assist._json_content('{"source_page":31}{"source_page":32}')


def test_response_excludes_reasoning_and_preserves_all_text_parts():
    body = {"output": [
        {"type": "reasoning", "content": [{"type": "output_text", "text": "不是正文"}]},
        {"type": "message", "content": [{"type": "output_text", "text": '{"source_page":'}, {"type": "output_text", "text": "31}"}]},
    ]}
    assert assist._response_content(body) == '{"source_page":31}'


def test_gateway_choices_are_still_readable(model, document):
    model.responses.append({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(_result())}}]})
    assert transcription.transcribe_page(document, PAGE, native_text=TEXT)["normalized_text"] == TEXT


def test_empty_compatibility_retry_keeps_full_page_budget_and_trust_instructions(model, document):
    model.responses.append({"output": []})
    _respond(model, _result())
    actual = transcription.transcribe_page(document, PAGE, ocr_text=TEXT)
    assert actual["normalized_text"] == TEXT
    assert len(model.calls) == 2


def test_continuous_empty_output_fails(model, document):
    model.responses.extend([{"output": []}, {"output": []}])
    with pytest.raises(assist.EmptyModelOutput, match="连续两次"):
        transcription.transcribe_page(document, PAGE, ocr_text=TEXT)
    assert len(model.calls) == 2


def test_rate_limit_keeps_safe_retry_attributes_not_upstream_body(model, document):
    model.responses.append(httpx.Response(
        429, headers={"Retry-After": "12"}, json={"error": {"message": "sensitive upstream error body"}},
        request=httpx.Request("POST", "https://example.test/v1/responses"),
    ))
    with pytest.raises(assist.AssistUnavailable) as error:
        transcription.transcribe_page(document, PAGE, ocr_text=TEXT)
    assert error.value.status_code == 429
    assert error.value.retry_after == 12
    assert "sensitive" not in str(error.value)
    assert "test-only-credential" not in str(error.value)


def test_chat_endpoint_rejected_without_provider_request(model, document):
    cfg = SimpleNamespace(enabled=True, base_url="https://example.test/v1/chat/completions", api_key="test", model="test")
    with pytest.raises(assist.AssistUnavailable, match="只支持 Responses"):
        transcription.transcribe_page(document, PAGE, ocr_text=TEXT, config=cfg)
    assert not model.calls


def test_text_bbox_is_not_retained_and_foreign_figure_page_is_rejected(model, document):
    result = _result(TEXT + "\n\n图2-1 采样波形")
    result["payload"]["figures"] = [{"label": "图2-1", "caption": "采样波形", "page_number": PAGE, "bbox": [0.1, 0.2, 0.8, 0.9]}]
    _respond(model, result)
    actual = transcription.transcribe_page(document, PAGE, ocr_text=TEXT)
    assert "bbox" not in actual["payload"]["figures"][0]
    assert any("无法确认 bbox" in value for value in actual["payload"]["uncertainties"])
    result["payload"]["figures"][0]["page_number"] = 32
    _respond(model, result)
    with pytest.raises(assist.AssistUnavailable, match="来源页码"):
        transcription.transcribe_page(document, PAGE, ocr_text=TEXT)


def test_table_body_must_not_be_omitted_from_full_text(model, document):
    result = _result()
    result["payload"]["tables"] = [{"label": "", "caption": "", "page_number": PAGE, "rows": [["missing original cell"]]}]
    _respond(model, result)
    with pytest.raises(assist.IncompleteModelOutput, match="单元格"):
        transcription.transcribe_page(document, PAGE, ocr_text=TEXT)


def test_formula_index_preserves_complete_array_and_excludes_literal_code(model, document):
    array = r"\begin{array}{cccc}(011&110.&010&111)_2\\(3&6.&2&7)_8\end{array}"
    result = _result("进制转换：\\[" + array + "\\]\n\n`\\(y=not_math\\)`")
    # Side lists are not an independent transcription source.
    result["payload"]["reviewed_formulas"] = [{"source": "y=wrong", "conditions": "invented"}]
    _respond(model, result)
    actual = transcription.transcribe_page(document, PAGE, native_text="进制转换")
    assert actual["payload"]["formulas"] == [array]
    assert actual["payload"]["reviewed_formulas"] == [{"source": array, "conditions": ""}]


def test_dollar_math_normalizes_without_changing_code(model, document):
    _respond(model, _result("完整正文 $x=1$\n\n$$y=2$$\n\n`$literal$`"))
    actual = transcription.transcribe_page(document, PAGE, ocr_text="完整正文")
    assert actual["normalized_text"] == "完整正文 \\(x=1\\)\n\n\\[y=2\\]\n\n`$literal$`"


def test_incomplete_math_delimiters_fail(model, document):
    _respond(model, _result(r"完整正文 \[\frac{1}{2}"))
    with pytest.raises(assist.IncompleteModelOutput, match="截断"):
        transcription.transcribe_page(document, PAGE, ocr_text="完整正文")


def test_single_item_cutover_preserves_original_text_bounds_and_long_payload(model, document, monkeypatch):
    long_formula = "\\(" + "x+" * 2600 + "1\\)"
    complete = ("完整段落，条件明确。\n\n" * 500) + long_formula
    result = _result(complete)
    result["payload"]["source_bounds"] = [9, 9, 9, 9]
    result["payload"]["review_status"] = "approved"
    result["payload"]["uncertainties"] = ["不能自动审核"]
    _respond(model, result)
    item = SimpleNamespace(id=10, page_number=PAGE, raw_text="原始文字永远保留", normalized_text="旧正文",
                           payload={"source_bounds": [0, 0, 600, 800], "source_page": PAGE, "ocr_text": complete},
                           source_anchor="doc:2:p31", review_status="approved", needs_review=False)
    committed = []
    db = SimpleNamespace(commit=lambda: committed.append(True), refresh=lambda _item: None)
    monkeypatch.setattr(assist, "_get_item", lambda *_args: (item, document))
    monkeypatch.setattr(assist, "_invalidate_item_publication", lambda *_args: None)

    actual = assist.assist_extracted_item(db, item.id)

    assert actual.raw_text == "原始文字永远保留"
    assert actual.source_anchor == "doc:2:p31"
    assert actual.payload["source_bounds"] == [0, 0, 600, 800]
    assert actual.payload["source_page"] == PAGE
    assert actual.normalized_text == complete
    assert actual.payload["formulas"][0].endswith("+1")
    assert actual.payload["ocr_text"] == complete
    assert actual.review_status == "pending"
    assert actual.needs_review is True
    assert actual.payload["quality_review_cleared"] is False
    assert "review_status" not in actual.payload


@pytest.mark.parametrize("page", [0, -1, 380, True, "31"])
def test_invalid_source_page_does_not_call_provider(model, document, page):
    with pytest.raises(ValueError):
        transcription.transcribe_page(document, page, ocr_text=TEXT)
    assert not model.calls


def test_unreadable_whole_page_placeholder_is_not_success(model, document, monkeypatch):
    monkeypatch.setattr(transcription, "render_page_bytes", lambda *_args: b"page")
    _respond(model, _result("[无法辨认]", image_observed=True))
    with pytest.raises(assist.IncompleteModelOutput, match="占位"):
        transcription.transcribe_page(document, PAGE, mode="vision")


def test_vision_figures_tables_are_same_page_candidates_with_all_cells(model, document, monkeypatch):
    monkeypatch.setattr(transcription, "render_page_bytes", lambda *_args: b"page")
    body = TEXT + "\n\n图2-1 采样波形\n\n表2-1 对照\n\n|输入|输出|\n|---|---|\n|1|2|"
    result = _result(body, image_observed=True)
    result["payload"]["figures"] = [{"label": "图2-1", "caption": "采样波形", "page_number": PAGE, "bbox": [0.1, 0.2, 0.8, 0.6]}]
    result["payload"]["tables"] = [{"label": "表2-1", "caption": "对照", "page_number": PAGE,
                                     "columns": ["输入", "输出"], "rows": [["1", "2"]], "bbox": [0.1, 0.7, 0.8, 0.9]}]
    result["payload"]["printed_page"] = "15"
    _respond(model, result)
    actual = transcription.transcribe_page(document, PAGE, mode="vision")
    assert actual["normalized_text"] == body
    assert actual["payload"]["tables"][0]["rows"] == [["1", "2"]]
    assert actual["payload"]["figures"][0]["bbox"] == [0.1, 0.2, 0.8, 0.6]
    assert actual["payload"]["figures"][0]["page_number"] == PAGE
    assert actual["source_page"] == PAGE
    assert actual["payload"]["printed_page"] == "15"
    assert any("定位提议" in value for value in actual["payload"]["uncertainties"])
    assert actual["diagnostics"]["human_reviewed"] is False


def test_failed_assistance_never_changes_existing_item_or_commits(model, document, monkeypatch):
    item = SimpleNamespace(id=10, page_number=PAGE, raw_text=TEXT, normalized_text="旧正文",
                           payload={"quality_review_cleared": True}, review_status="approved", needs_review=False)
    monkeypatch.setattr(assist, "_get_item", lambda *_args: (item, document))
    committed = []
    db = SimpleNamespace(commit=lambda: committed.append(True), refresh=lambda _item: None)
    _respond(model, _result(page=32))
    with pytest.raises(assist.AssistUnavailable, match="来源页码"):
        assist.assist_extracted_item(db, item.id)
    assert item.raw_text == TEXT
    assert item.normalized_text == "旧正文"
    assert item.payload == {"quality_review_cleared": True}
    assert item.review_status == "approved"
    assert committed == []
