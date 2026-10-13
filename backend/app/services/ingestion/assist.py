from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ...config import settings
from ...models import ExtractedItem, IngestionJob, SourceDocument
from ...provider_config import ProviderConfig, get_provider_config
from .service import _invalidate_item_publication


class AssistUnavailable(RuntimeError):
    """A safe public error; upstream response bodies and credentials stay private."""

    def __init__(self, message: str, *, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class EmptyModelOutput(AssistUnavailable):
    pass


class UnsupportedImages(AssistUnavailable):
    pass


class IncompleteModelOutput(AssistUnavailable):
    pass


def _escape_math_backslashes(value: str) -> str:
    """Repair string escapes only, never complete missing JSON/page content.

    A bare TeX \\frac is valid JSON but would become form-feed + `rac`.
    Recognize TeX before decoding, and track actual math spans so a paragraph
    newline such as ``\\nNext`` after a formula stays a JSON newline.
    """
    result: list[str] = []
    quoted = False
    math_region = False
    ambiguous_tex = re.compile(
        r"^(?:frac|forall|beta|begin|bar|binom|boldsymbol|mathbf|boxed|big\w*|backslash|bmod|bot|"
        r"nabla|neq|newcommand|nu|neg|not\w*|nonumber|"
        r"right\w*|rho|mathrm|rm|rangle|rvert|rfloor|rceil|"
        r"text\w*|tau|theta|times|tfrac|tilde|top|tag|tiny|tan\w*|triangle\w*)$"
    )
    command_pattern = re.compile(r"[A-Za-z]+")
    index = 0
    while index < len(value):
        char = value[index]
        if char == '"':
            quoted = not quoted
            math_region = False
            result.append(char)
            index += 1
            continue
        if quoted and char == "$":
            length = 2 if value[index:index + 2] == "$$" else 1
            result.append(value[index:index + length])
            math_region = not math_region
            index += length
            continue
        if quoted and char == "\\" and index + 1 < len(value):
            following = value[index + 1]
            if following in {'"', "\\", "/"}:
                if following == "\\" and index + 2 < len(value):
                    if value[index + 2] in "([":
                        math_region = True
                    elif value[index + 2] in ")]":
                        math_region = False
                result.append(value[index:index + 2])
                index += 2
                continue
            if following in "([":
                math_region = True
            elif following in ")]":
                math_region = False
            if following == "u" and re.match(r"[0-9a-fA-F]{4}", value[index + 2:index + 6]):
                result.append(value[index:index + 6])
                index += 6
                continue
            command = command_pattern.match(value, index + 1)
            is_tex = bool(command and (
                following not in "bfnrtu" or ambiguous_tex.fullmatch(command.group())
                or (math_region and len(command.group()) > 1)
            ))
            if is_tex or following not in "bfnrtu":
                result.append("\\\\")
                index += 1
                continue
            result.append(value[index:index + 2])
            index += 2
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _json_content(content: str) -> dict[str, Any]:
    """Read complete top-level objects, allowing fences and surrounding prose.

    Nested objects from a truncated page result are never promoted to a result.
    No model-based repair is used: that could silently complete a missing page.
    """
    value = _escape_math_backslashes(content.strip())
    candidates: list[str] = []
    start: int | None = None
    depth = 0
    quoted = False
    escaped = False
    for index, char in enumerate(value):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"' and depth:
            quoted = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(value[start:index + 1])
                start = None
    if depth or quoted:
        raise ValueError("模型 JSON 不完整或已截断")
    decoded: list[dict[str, Any]] = []
    for candidate in candidates:
        parsed = json.loads(candidate, parse_constant=lambda _value: None)
        if isinstance(parsed, dict):
            decoded.append(parsed)
    page_results = [parsed for parsed in decoded if "source_page" in parsed or "normalized_text" in parsed]
    if len(page_results) == 1:
        return page_results[0]
    if len(page_results) > 1:
        raise ValueError("模型返回了多个页面 JSON 对象")
    if len(decoded) == 1:
        return decoded[0]
    raise ValueError("模型响应中没有唯一 JSON 对象")


def _require_llm(cfg: ProviderConfig) -> None:
    if not cfg.enabled or not cfg.base_url or not cfg.api_key or not cfg.model:
        raise AssistUnavailable("未配置可用的 LLM provider、模型或认证")
    if not cfg.base_url.rstrip("/").endswith("/responses"):
        raise AssistUnavailable("当前系统只支持 Responses API；请将 LLM_BASE_URL 设置为 /v1/responses")


def _responses_input(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    instructions = ""
    inputs: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if role == "system" and isinstance(content, str):
            instructions = content
            continue
        parts: list[dict[str, Any]] = []
        if isinstance(content, str):
            parts.append({"type": "input_text", "text": content})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    parts.append({"type": "input_text", "text": part["text"]})
                elif part.get("type") == "image_url":
                    image_url = part.get("image_url", {}).get("url") if isinstance(part.get("image_url"), dict) else None
                    if isinstance(image_url, str):
                        parts.append({"type": "input_image", "image_url": image_url})
        if parts:
            inputs.append({"role": role if role in {"user", "assistant"} else "user", "content": parts})
    return instructions, inputs


def _response_content(body: Any) -> str:
    if not isinstance(body, dict):
        raise TypeError("模型响应不是 JSON 对象")
    status = body.get("status")
    if status in {"incomplete", "failed", "cancelled", "queued", "in_progress"} or body.get("incomplete_details"):
        raise IncompleteModelOutput("Responses 未完整完成页面转写")
    choices = body.get("choices", [])
    if isinstance(choices, list) and any(
        isinstance(choice, dict) and choice.get("finish_reason") in {"length", "content_filter", "tool_calls"}
        for choice in choices
    ):
        raise IncompleteModelOutput("网关响应未完整完成页面转写")
    output = body.get("output", [])
    if not isinstance(output, list):
        raise TypeError("Responses output 不是列表")
    message_text: list[str] = []
    output_text: list[str] = []
    for entry in output:
        if not isinstance(entry, dict):
            continue
        if entry.get("status") in {"incomplete", "in_progress"}:
            raise IncompleteModelOutput("Responses 消息未完整完成页面转写")
        if entry.get("type") == "reasoning":
            continue
        for part in entry.get("content", []):
            if not isinstance(part, dict):
                continue
            if part.get("type") == "refusal":
                raise AssistUnavailable("模型拒绝转写来源页")
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                (message_text if entry.get("type") == "message" else output_text).append(part["text"])
    direct = body.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    if message_text or output_text:
        return "".join(message_text or output_text)
    choices = body.get("choices", [])
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
        if choice.get("finish_reason") in {"length", "content_filter", "tool_calls"}:
            raise IncompleteModelOutput("网关响应未完整完成页面转写")
        message = choice.get("message", {})
        if isinstance(message, dict) and message.get("refusal"):
            raise AssistUnavailable("模型拒绝转写来源页")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            parts = [part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)]
            if parts:
                return "\n".join(parts)
    raise EmptyModelOutput("Responses 响应缺少生成文本")


def _response_shape(body: Any) -> str:
    """Only known envelope fields, not arbitrary upstream keys or body values."""
    if not isinstance(body, dict):
        return f"type={type(body).__name__}"
    output = body.get("output")
    choices = body.get("choices")
    direct = body.get("output_text")
    return f"output={type(output).__name__}, choices={type(choices).__name__}, output_text={type(direct).__name__}"


def _responses_input_value(inputs: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """Retain the existing gateway-compatible string form for text-only input."""
    text_parts: list[str] = []
    for message in inputs:
        content = message.get("content", [])
        if not isinstance(content, list):
            return inputs
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "input_text" or not isinstance(part.get("text"), str):
                return inputs
            text_parts.append(part["text"])
    return "\n\n".join(text_parts)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After", "")
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _images_rejected(response: httpx.Response) -> bool:
    if response.status_code not in {400, 415, 422}:
        return False
    try:
        body = response.json()
        error = body.get("error", {}) if isinstance(body, dict) else {}
        if isinstance(error, dict):
            text = " ".join(str(error.get(key, "")) for key in ("code", "type", "message")).lower()
        else:
            text = str(error).lower()
    except (TypeError, ValueError):
        return False
    return any(word in text for word in ("image", "vision", "图片", "图像")) and any(
        word in text for word in ("unsupported", "not support", "does not support", "not supported", "不支持")
    )


def _request_content(messages: list[dict[str, Any]], *, config: ProviderConfig | None = None,
                     max_output_tokens: int = 8192, compatibility_retry: bool = False) -> str:
    try:
        cfg = config if config is not None else get_provider_config(settings)
    except ValueError as exc:
        raise AssistUnavailable("无法加载 Responses provider 配置") from exc
    _require_llm(cfg)
    instructions, inputs = _responses_input(messages)
    image_input = any(part.get("type") == "input_image" for entry in inputs for part in entry["content"])
    input_value = _responses_input_value(inputs)
    payload: dict[str, Any] = {"model": cfg.model, "max_output_tokens": max_output_tokens, "input": input_value}
    if not compatibility_retry:
        payload.update({"reasoning": {"effort": "none"}, "instructions": instructions})
    elif instructions:
        # The minimal envelope must not drop the source-data trust boundary.
        payload["input"] = [{"role": "system", "content": [{"type": "input_text", "text": instructions}]}, *inputs]
    if cfg.gateway_messages:
        payload["messages"] = messages
    try:
        with httpx.Client(timeout=180) as client:
            response = client.post(cfg.base_url, headers={"Authorization": f"Bearer {cfg.api_key}"}, json=payload)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        response = exc.response
        error_type = UnsupportedImages if image_input and _images_rejected(response) else AssistUnavailable
        message = "当前 provider 拒绝图像输入，不能进行视觉转写" if error_type is UnsupportedImages else f"模型请求失败：HTTP {response.status_code}"
        from ..answering import LLMAnswerer
        error = error_type(message, status_code=response.status_code, retry_after=_retry_after(response))
        error.upstream_diagnostics = LLMAnswerer._upstream_summary(response, cfg.api_key)
        raise error from exc
    except httpx.HTTPError as exc:
        raise AssistUnavailable(f"模型请求失败：{exc.__class__.__name__}") from exc
    try:
        body = response.json()
    except (TypeError, ValueError) as exc:
        raise AssistUnavailable("模型响应体不是合法 JSON") from exc
    try:
        return _response_content(body)
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        raise AssistUnavailable(f"模型响应结构无效；shape={_response_shape(body)}") from exc


def _call_llm(messages: list[dict[str, Any]], *, config: ProviderConfig | None = None,
              max_output_tokens: int = 8192) -> dict[str, Any]:
    try:
        content = _request_content(messages, config=config, max_output_tokens=max_output_tokens)
    except EmptyModelOutput:
        try:
            content = _request_content(messages, config=config, max_output_tokens=max_output_tokens, compatibility_retry=True)
        except EmptyModelOutput as exc:
            raise EmptyModelOutput("模型连续两次返回空文本；Responses 兼容重试仍无正文") from exc
    try:
        return _json_content(content)
    except (TypeError, ValueError) as exc:
        raise AssistUnavailable("模型输出不是完整合法的页面 JSON；未补造或摘要替代正文") from exc


def _get_item(db: Session, item_id: int) -> tuple[ExtractedItem, SourceDocument]:
    item = db.get(ExtractedItem, item_id)
    if not item:
        raise LookupError("解析项不存在")
    job = db.get(IngestionJob, item.ingestion_job_id)
    document = db.get(SourceDocument, job.source_document_id) if job else None
    if not document:
        raise LookupError("解析项缺少原始教材")
    return item, document


def _apply_result(db: Session, item: ExtractedItem, parsed: dict[str, Any]) -> ExtractedItem:
    if type(parsed.get("source_page")) is not int or parsed["source_page"] != item.page_number:
        raise AssistUnavailable("模型返回的来源页码未通过门禁")
    normalized = parsed.get("normalized_text")
    if not isinstance(normalized, str) or not normalized.strip():
        raise AssistUnavailable("模型未返回可审核的非空文本")
    model_payload = dict(parsed["payload"])
    original_payload = dict(item.payload or {})
    for protected_key in ("source_page", "source_bounds", "page_bounds", "quality_flags", "text_quality", "quality_review_cleared", "image_coverage", "images"):
        model_payload.pop(protected_key, None)
    original_payload.update(model_payload)
    original_payload["quality_review_cleared"] = False
    original_payload["llm_warnings"] = parsed["warnings"]
    original_payload["llm_provider"] = parsed["diagnostics"]["provider"]
    original_payload["llm_model"] = parsed["diagnostics"]["model"]
    original_payload["page_transcription"] = parsed["diagnostics"]
    item.item_type = parsed["item_type"]
    item.normalized_text = normalized
    # Both existing JSON and validated transcription metadata are already safe.
    # _safe_json would silently truncate long equations/table cells to 4000 chars.
    item.payload = original_payload
    item.confidence = parsed["confidence"]
    item.needs_review = True
    item.review_status = "pending"
    _invalidate_item_publication(db, item, "source_item_assist_pending_review")
    db.commit()
    db.refresh(item)
    return item


def assist_extracted_item(db: Session, item_id: int, mode: str = "auto") -> ExtractedItem:
    """Full-page assistance; model organization is never a human review."""
    from .page_transcription import transcribe_page

    item, document = _get_item(db, item_id)
    payload = item.payload or {}
    stored_ocr = payload.get("ocr_text", "")
    parsed = transcribe_page(
        document, item.page_number, native_text=item.raw_text or "",
        ocr_text=stored_ocr if isinstance(stored_ocr, str) else "", mode=mode,
    )
    return _apply_result(db, item, parsed)
