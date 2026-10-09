from __future__ import annotations

import json

import httpx

from ..config import settings


class LLMAnswerer:
    """可选 OpenAI-compatible 适配器，支持 Chat Completions 和 Responses API。"""

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
            return None
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
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None

    async def complete(self, question: str, evidence: list[dict], web_results: list[dict] | None = None) -> dict | None:
        if not self.enabled():
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
        endpoint = settings.llm_base_url.rstrip("/")
        responses_api = self._is_responses_endpoint(endpoint)
        if responses_api:
            payload = {"model": settings.llm_model, "temperature": 0.1, "instructions": instructions, "input": prompt}
        else:
            endpoint = LLMAnswerer._chat_endpoint(endpoint)
            payload = {
                "model": settings.llm_model,
                "temperature": 0.1,
                "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": prompt}],
            }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(endpoint, headers={"Authorization": f"Bearer {settings.llm_api_key}"}, json=payload)
                response.raise_for_status()
                content = self._extract_content(response.json(), responses_api)
                if not content:
                    return None
                parsed = self._parse_json(content)
                if not parsed:
                    return None
                valid_ids = {str(item.get("material_id")) for item in evidence}
                raw_ids = parsed.get("evidence_ids", [])
                parsed["evidence_ids"] = [int(item) for item in raw_ids if str(item) in valid_ids]
                return {**parsed, "provider": settings.llm_provider} if parsed.get("answer") and parsed["evidence_ids"] else None
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

def build_local_answer(question: str, evidence: list[dict]) -> dict:
    if not evidence:
        return {"answer": "当前教材知识库中没有找到可定位依据。", "evidence_ids": [], "provider": "local-empty"}
    lines = [f"根据检索到的教材资料，问题“{question}”的相关依据如下："]
    for item in evidence[:5]:
        lines.append(f"- {item.get('snippet', '')}")
    return {"answer": "\n".join(lines), "evidence_ids": [item.get("material_id") for item in evidence[:5]], "provider": "local-rag"}
