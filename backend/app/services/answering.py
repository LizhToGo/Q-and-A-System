from __future__ import annotations

import json
import re

import httpx

from ..config import settings


class LLMAnswerer:
    """可选 OpenAI-compatible 适配器，支持 Chat Completions 和 Responses API。"""

    def __init__(self) -> None:
        self.last_error: str | None = None

    def enabled(self) -> bool:
        return bool(settings.llm_provider != "none" and settings.llm_base_url and settings.llm_api_key)

    @staticmethod
    def _is_responses_endpoint(url: str) -> bool:
        return url.rstrip("/").endswith("/responses")
    @staticmethod
    def _chat_endpoint(url: str) -> str:
        endpoint = url.rstrip("/")
        return endpoint if endpoint.endswith("/chat/completions") else f"{endpoint}/chat/completions"


    @staticmethod
    def _extract_content(body: dict, responses_api: bool) -> str | None:
        if not isinstance(body, dict):
            return None
        if responses_api:
            direct = body.get("output_text")
            if isinstance(direct, str):
                return direct
            for output in body.get("output", []):
                if not isinstance(output, dict):
                    continue
                for content in output.get("content", []):
                    if isinstance(content, dict) and isinstance(content.get("text"), str):
                        return content["text"]
        choices = body.get("choices", [])
        if not choices or not isinstance(choices[0], dict):
            return None
        message = choices[0].get("message", {})
        content = message.get("content") if isinstance(message, dict) else None
        return content if isinstance(content, str) else None

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
            parsed = json.loads(value)
        except json.JSONDecodeError:
            start = value.find("{")
            if start < 0:
                raise
            parsed, _end = json.JSONDecoder().raw_decode(value[start:])
        return parsed if isinstance(parsed, dict) else None

    async def complete(self, question: str, evidence: list[dict], web_results: list[dict] | None = None) -> dict | None:
        self.last_error = None
        if not self.enabled():
            self.last_error = "not_configured"
            return None
        context = "\n\n".join(
            f"[教材证据 {item.get('material_id')}] {item.get('title')} {item.get('source_anchor')}\n{item.get('snippet')}"
            for item in evidence
        )
        web_context = "\n\n".join(
            f"[网络补充] {item.get('title')} {item.get('url')}\n{item.get('snippet')}"
            for item in (web_results or [])
        )
        instructions = "只根据给定教材证据回答；网络补充必须明确标注。输出 JSON：answer、claims、evidence_ids。不能编造证据 ID、页码或 URL。"
        prompt = f"问题：{question}\n教材证据：\n{context or '无'}\n网络补充：\n{web_context or '无'}"
        messages = [{"role": "system", "content": instructions}, {"role": "user", "content": prompt}]
        endpoint = settings.llm_base_url.rstrip("/")
        responses_api = self._is_responses_endpoint(endpoint)
        if responses_api:
            payload = {
                "model": settings.llm_model,
                "max_output_tokens": 1024,
                "reasoning": {"effort": "none"},
                "instructions": instructions,
                "input": prompt,
                # SDU's /responses gateway currently requires messages and returns choices.
                "messages": messages,
            }
        else:
            endpoint = LLMAnswerer._chat_endpoint(endpoint)
            payload = {
                "model": settings.llm_model,
                "max_tokens": 1024,
                "thinking": {"type": "disabled"},
                "messages": messages,
            }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(endpoint, headers={"Authorization": f"Bearer {settings.llm_api_key}"}, json=payload)
                response.raise_for_status()
                content = self._extract_content(response.json(), responses_api)
                if not content:
                    self.last_error = "empty_content"
                    return None
                parsed = self._parse_json(content)
                if not parsed:
                    self.last_error = "invalid_json"
                    return None
                valid_ids = {str(item.get("material_id")) for item in evidence}
                raw_ids = parsed.get("evidence_ids", [])
                if not isinstance(raw_ids, list):
                    self.last_error = "invalid_evidence_ids"
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
                if not parsed.get("answer") or not parsed["evidence_ids"]:
                    self.last_error = "missing_answer_or_valid_evidence"
                    return None
                return {**parsed, "provider": settings.llm_provider}
        except httpx.HTTPStatusError as exc:
            self.last_error = f"upstream_http_{exc.response.status_code}"
            return None
        except httpx.HTTPError as exc:
            self.last_error = f"transport_{exc.__class__.__name__}"
            return None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.last_error = f"response_{exc.__class__.__name__}"
            return None

def build_local_answer(question: str, evidence: list[dict]) -> dict:
    if not evidence:
        return {"answer": "当前教材知识库中没有找到可定位依据。", "evidence_ids": [], "provider": "local-empty"}
    lines = [f"根据检索到的教材资料，问题“{question}”的相关依据如下："]
    for item in evidence[:5]:
        lines.append(f"- {item.get('snippet', '')}")
    return {"answer": "\n".join(lines), "evidence_ids": [item.get("material_id") for item in evidence[:5]], "provider": "local-rag"}
