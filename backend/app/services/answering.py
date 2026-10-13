from __future__ import annotations

import json
import re

import httpx

from ..config import settings
from ..provider_config import ProviderConfig, get_provider_config


_CONTROL_ESCAPE_COMMANDS = frozenset({
    "bar", "beta", "bf", "begin", "bmod", "boldsymbol", "boxed",
    "frac", "forall", "nu", "ne", "neq", "nabla", "not", "nonumber",
    "right", "rm", "rightarrow", "rho", "theta", "times", "tan", "tau",
    "text", "textbf", "textit", "textrm",
})


def _normalize_json_backslashes(value: str) -> str:
    """Preserve bare LaTeX commands in model JSON; never repair structure or IDs."""
    output: list[str] = []
    in_string = False
    index = 0
    while index < len(value):
        char = value[index]
        if char == '"':
            in_string = not in_string
        if in_string and char == "\\" and index + 1 < len(value):
            following = value[index + 1]
            if following in '\\/"':
                output.append(value[index:index + 2])
                index += 2
                continue
            command = re.match(r"[A-Za-z]+", value[index + 1:])
            latex_control = command is not None and command.group() in _CONTROL_ESCAPE_COMMANDS
            unicode_escape = following == "u" and re.match(r"[0-9a-fA-F]{4}", value[index + 2:index + 6]) is not None
            if latex_control or following not in "bfnrtu" or (following == "u" and not unicode_escape):
                output.append("\\\\")
                index += 1
                continue
            output.append(value[index:index + 2])
            index += 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


class LLMAnswerer:
    """可选 OpenAI-compatible Responses API 适配器。"""

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self.config = config
        self.last_error: str | None = None
        self.last_diagnostics: dict[str, object] = {}

    def _failure(
        self, code: str, reason: str, cfg=None, *, status: int | None = None,
        upstream: dict[str, object] | None = None,
    ) -> None:
        self.last_error = code
        self.last_diagnostics.update({
            "failure_reason": reason,
            "provider": getattr(cfg, "provider", getattr(settings, "llm_provider", "none")),
            "model": getattr(cfg, "model", getattr(settings, "llm_model", "")),
            "base_url": getattr(cfg, "base_url", getattr(settings, "llm_base_url", None)),
            "config_source": getattr(cfg, "config_source", "environment"),
        })
        if status is not None:
            self.last_diagnostics["http_status"] = status
        if upstream:
            self.last_diagnostics.update(upstream)

    @staticmethod
    def _upstream_summary(response: httpx.Response, api_key: str) -> dict[str, object]:
        """Expose bounded error details only, never request data or arbitrary headers."""
        def safe_text(value: object, limit: int) -> str:
            text = str(value)
            if api_key:
                text = text.replace(api_key, "[redacted]")
                text = text.replace(json.dumps(api_key, ensure_ascii=True)[1:-1], "[redacted]")
            text = re.sub(r"(?i)\bBearer\s+[^\s\"'<>;,{}\[\]]+", "Bearer [redacted]", text)
            text = " ".join(text.split())
            return text if len(text) <= limit else text[:limit - 1] + "…"

        def error_fields(value: object) -> dict:
            if not isinstance(value, dict):
                return {}
            error = value.get("error")
            if isinstance(error, dict):
                value = error
            return {
                key: value[key] for key in ("code", "type", "message")
                if isinstance(value.get(key), (str, int, float)) and value[key] != ""
            }

        try:
            body = response.json()
        except (ValueError, TypeError):
            body = getattr(response, "text", "")
        outer = error_fields(body)
        inner = None
        current = outer
        decoder = json.JSONDecoder()
        for _depth in range(4):
            message = current.get("message")
            if not isinstance(message, str):
                break
            message = message[:65536]
            nested = {}
            # LiteLLM prefixes may precede a JSON error; invalid braces are skipped.
            for index, match in enumerate(re.finditer(r"\{", message)):
                if index >= 64:
                    break
                try:
                    candidate, _end = decoder.raw_decode(message, match.start())
                except ValueError:
                    continue
                nested = error_fields(candidate)
                if nested:
                    break
            if not nested:
                break
            inner = current = nested

        def sanitized(error: dict) -> dict[str, str]:
            return {
                key: safe_text(value, 4096 if key == "message" else 256)
                for key, value in error.items()
            }

        diagnostics: dict[str, object] = {}
        if outer:
            diagnostics["upstream_error"] = sanitized(outer)
        if inner:
            diagnostics["upstream_inner_error"] = sanitized(inner)
        parts = []
        for label, error in (("网关错误" if inner else "上游错误", outer), ("内层错误", inner)):
            if not error:
                continue
            fields = sanitized(error)
            if inner and error is outer:
                fields.pop("message", None)
            detail = "; ".join(f"{key}={value}" for key, value in fields.items())
            if detail:
                parts.append(f"{label}: {detail}")
        if parts:
            diagnostics["upstream_summary"] = safe_text(" | ".join(parts), 12288)
        elif isinstance(body, str):
            diagnostics["upstream_summary"] = safe_text(body, 4096)
        elif isinstance(body, dict) and isinstance(body.get("error"), str):
            diagnostics["upstream_summary"] = safe_text(body["error"], 4096)
        else:
            diagnostics["upstream_summary"] = "上游错误 JSON 未提供可识别的 code/type/message"
        allowed_headers = {
            "request-id", "x-request-id", "x-ms-request-id", "x-amzn-requestid", "retry-after",
            *(f"x-ratelimit-{kind}-{unit}" for kind in ("limit", "remaining", "reset") for unit in ("tokens", "requests")),
        }
        headers = {
            name.lower(): safe_text(value, 512)
            for name, value in response.headers.items() if name.lower() in allowed_headers
        }
        if headers:
            diagnostics["upstream_headers"] = headers
        return diagnostics

    def enabled(self, cfg=None) -> bool:
        cfg = cfg or get_provider_config(settings)
        return bool(cfg.enabled and cfg.base_url and cfg.api_key)

    @staticmethod
    def _is_responses_endpoint(url: str) -> bool:
        return url.rstrip("/").endswith("/responses")

    @staticmethod
    def _extract_content(body: dict) -> str | None:
        if not isinstance(body, dict):
            return None
        direct = body.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct
        message_text: list[str] = []
        output_text: list[str] = []
        for output in body.get("output", []):
            if not isinstance(output, dict):
                continue
            is_message = output.get("type") == "message"
            for content in output.get("content", []):
                if not isinstance(content, dict) or not isinstance(content.get("text"), str):
                    continue
                if is_message:
                    message_text.append(content["text"])
                elif content.get("type") == "output_text":
                    output_text.append(content["text"])
        if message_text:
            return message_text[0]
        if output_text:
            return output_text[0]
        choices = body.get("choices", [])
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message", {})
            content = message.get("content") if isinstance(message, dict) else None
            return content if isinstance(content, str) else None
        return None

    @staticmethod
    def _parse_json(content: str) -> dict | None:
        value = content.strip()
        if value.startswith("```"):
            lines = value.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            value = "\n".join(lines).strip()
        try:
            parsed = json.loads(_normalize_json_backslashes(value))
        except json.JSONDecodeError:
            start = value.find("{")
            if start < 0:
                raise
            parsed, _end = json.JSONDecoder().raw_decode(_normalize_json_backslashes(value[start:]))
        return parsed if isinstance(parsed, dict) else None

    async def complete(self, question: str, evidence: list[dict], web_results: list[dict] | None = None, history: list[dict[str, str]] | None = None) -> dict | None:
        self.last_error = None
        self.last_diagnostics = {}

        try:
            cfg = self.config or get_provider_config(settings)
        except ValueError:
            self._failure("responses_only", "配置的端点不是 Responses API")
            return None
        if not self.enabled(cfg):
            self._failure("not_configured", "LLM 未启用或缺少 URL/API key", cfg)
            return None
        context = "\n\n".join(
            f"[教材证据 {item.get('material_id')}] {item.get('title')} {item.get('source_anchor')}\n{item.get('snippet')}"
            for item in evidence
        )
        web_context = "\n\n".join(
            f"[网络补充] {item.get('title')} {item.get('url')}\n{item.get('snippet')}"
            for item in (web_results or [])
        )
        instructions = (
            "只根据给定教材证据回答；网络补充必须明确标注。输出一个 JSON 对象：answer（字符串）、evidence_ids（整数列表）。"
            "不能编造证据 ID、页码或 URL，evidence_ids 只能使用输入中出现的数字 material_id。"
            "answer 使用 Markdown；数学表达式只使用 KaTeX 兼容的 $$...$$、\\(...\\) 或 \\[...\\] 定界，禁止裸公式。"
            "JSON 字符串中的反斜杠必须正确转义。"
            "历史对话仅用于理解当前问题，不是权威教材证据；不能沿用历史证据 ID，引用只能来自本次教材证据。"
        )
        prompt = f"问题：{question}\n教材证据：\n{context or '无'}\n网络补充：\n{web_context or '无'}"
        if history:
            bounded_history = [
                {"question": turn["question"][:1200], "answer": turn["answer"][:2500]}
                for turn in history[-3:]
            ]
            prompt = "历史对话（非教材证据）：\n" + json.dumps(bounded_history, ensure_ascii=False) + "\n当前" + prompt
        messages = [{"role": "system", "content": instructions}, {"role": "user", "content": prompt}]
        endpoint = cfg.base_url.rstrip("/")
        if not self._is_responses_endpoint(endpoint):
            self._failure("responses_only", "配置的端点不是 Responses API", cfg)
            return None
        payload = {
            "model": cfg.model,
            "max_output_tokens": 4096,
            "reasoning": {"effort": "none"},
            "instructions": instructions,
            "input": prompt,
        }
        if cfg.gateway_messages:
            payload["messages"] = messages
        self.last_diagnostics = {
            "max_output_tokens": payload["max_output_tokens"],
            "input_characters": len(prompt),
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
                response = await client.post(endpoint, headers={"Authorization": f"Bearer {cfg.api_key}"}, json=payload)
                response.raise_for_status()
                self.last_diagnostics.update({
                    "http_status": int(getattr(response, "status_code", 200)),
                    "provider": cfg.provider, "model": cfg.model, "base_url": cfg.base_url,
                    "config_source": cfg.config_source,
                })
                try:
                    body = response.json()
                except (ValueError, TypeError, json.JSONDecodeError):
                    self._failure("invalid_json", "上游响应不是有效 JSON", cfg, status=int(getattr(response, "status_code", 200)))
                    return None
                if isinstance(body, dict):
                    response_status = body.get("status")
                    self.last_diagnostics["response_status"] = response_status if response_status in ("completed", "incomplete", "failed", "in_progress", "queued", "cancelled") else "unknown"
                    usage = body.get("usage")
                    if isinstance(usage, dict):
                        self.last_diagnostics["usage"] = {
                            name: usage[name] for name in ("input_tokens", "output_tokens", "total_tokens")
                            if isinstance(usage.get(name), int) and not isinstance(usage[name], bool) and usage[name] >= 0
                        }
                    incomplete = body.get("incomplete_details")
                    reason = incomplete.get("reason", "") if isinstance(incomplete, dict) else ""
                    choices = body.get("choices")
                    length_stop = isinstance(choices, list) and any(
                        isinstance(choice, dict) and choice.get("finish_reason") == "length" for choice in choices
                    )
                    if body.get("status") == "incomplete" or length_stop:
                        self.last_diagnostics["incomplete_reason"] = "max_output_tokens" if length_stop else reason if reason in ("max_output_tokens", "content_filter") else "unknown"
                        self._failure("incomplete_output", "上游输出未完成，未将截断内容当作完整回答", cfg, status=int(getattr(response, "status_code", 200)))
                        return None
                content = self._extract_content(body)
                if not content:
                    self._failure("empty_content", "上游响应没有可用文本内容", cfg, status=int(getattr(response, "status_code", 200)))
                    return None
                try:
                    parsed = self._parse_json(content)
                except (ValueError, TypeError, json.JSONDecodeError):
                    self._failure("invalid_json", "模型文本不是有效 JSON", cfg, status=int(getattr(response, "status_code", 200)))
                    return None
                if not parsed:
                    self._failure("invalid_json", "模型文本不是有效 JSON", cfg, status=int(getattr(response, "status_code", 200)))
                    return None
                valid_ids = {str(item.get("material_id")) for item in evidence}
                raw_ids = parsed.get("evidence_ids", [])
                if not isinstance(raw_ids, list):
                    self._failure("invalid_evidence_ids", "模型返回的 evidence_ids 不是列表", cfg, status=int(getattr(response, "status_code", 200)))
                    return None
                normalized_ids: list[int] = []
                for item in raw_ids:
                    text = str(item).strip()
                    if text.isdigit():
                        candidate = text
                    else:
                        match = re.fullmatch(r"\[?教材证据\s+(\d+)\]?", text)
                        candidate = match.group(1) if match else ""
                    if candidate in valid_ids:
                        normalized_ids.append(int(candidate))
                parsed["evidence_ids"] = list(dict.fromkeys(normalized_ids))
                if not isinstance(parsed.get("answer"), str) or not parsed["answer"].strip() or not parsed["evidence_ids"]:
                    self._failure("missing_answer_or_valid_evidence", "模型未提供回答或没有可验证的教材证据 ID", cfg, status=int(getattr(response, "status_code", 200)))
                    return None
                return {**parsed, "provider": cfg.provider}
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            upstream = self._upstream_summary(exc.response, cfg.api_key)
            if status == 429:
                error = upstream.get("upstream_inner_error") or upstream.get("upstream_error") or {}
                category = "unknown"
                code = str(error.get("code", "")).lower()
                detail = " ".join(str(error.get(field, "")) for field in ("type", "message")).lower()
                if not error:
                    detail = str(upstream.get("upstream_summary", "")).lower()
                if "modelaccounttpm" in code:
                    category = "model_account_tpm"
                    reason = "上游模型账户的每分钟 token 限流（HTTP 429），不等同于当前 API key 余额不足"
                elif any(term in code for term in ("quota", "billing", "insufficient", "credit", "budget")):
                    category = "quota"
                    reason = "上游报告配额或计费限制（HTTP 429），不能据此认定当前 API key 额度已用完"
                elif any(term in code for term in ("rate", "toomanyrequests", "tpm")):
                    category = "rate_limit"
                    reason = "上游请求或 token 速率限制（HTTP 429）"
                elif any(term in detail for term in ("quota", "billing", "insufficient", "credit", "budget", "配额")):
                    category = "quota"
                    reason = "上游报告配额或计费限制（HTTP 429），不能据此认定当前 API key 额度已用完"
                elif any(term in detail for term in ("rate limit", "ratelimit", "too many", "tpm", "频率", "限流")):
                    category = "rate_limit"
                    reason = "上游请求或 token 速率限制（HTTP 429）"
                else:
                    reason = "上游返回 HTTP 429，原因未明"
                upstream["upstream_error_category"] = category
            elif status in {401, 403}:
                reason = f"上游鉴权失败（HTTP {status}）"
            elif status >= 500:
                reason = f"上游服务错误（HTTP {status}）"
            else:
                reason = f"上游拒绝请求（HTTP {status}）"
            self._failure(f"upstream_http_{status}", reason, cfg, status=status, upstream=upstream)
            return None
        except httpx.TimeoutException:
            self._failure("timeout", "请求上游模型超时（120 秒）", cfg)
            return None
        except httpx.HTTPError as exc:
            self._failure(f"transport_{exc.__class__.__name__}", "连接上游模型失败", cfg)
            return None
        except (KeyError, TypeError, ValueError) as exc:
            self._failure(f"response_{exc.__class__.__name__}", "上游响应结构无法解析", cfg)
            return None

def build_local_answer(question: str, evidence: list[dict]) -> dict:
    if not evidence:
        return {"answer": "当前教材知识库中没有找到可定位依据。", "evidence_ids": [], "provider": "local-empty"}
    evidence_ids = [item.get("material_id") for item in evidence[:5] if item.get("material_id") is not None]
    return {
        "answer": (
            "当前模型服务不可用，系统未生成未经验证的自然语言回答。\n\n"
            f"已为“{question}”检索到 {len(evidence_ids)} 条教材依据；请展开“查看教材依据”阅读原文，"
            "模型服务恢复后可重新提交。"
        ),
        "evidence_ids": evidence_ids,
        "provider": "local-rag",
    }
