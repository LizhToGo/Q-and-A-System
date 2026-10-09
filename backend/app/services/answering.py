from __future__ import annotations

import json

import httpx

from ..config import settings


class LLMAnswerer:
    """OpenAI-compatible 可选适配器；未配置或失败时返回 None，核心校验不受影响。"""

    def enabled(self) -> bool:
        return bool(settings.llm_provider != "none" and settings.llm_base_url and settings.llm_api_key)

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
        payload = {
            "model": settings.llm_model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": "只根据给定教材证据回答；网络补充必须明确标注。输出 JSON：answer、claims、evidence_ids。不能编造证据 ID、页码或 URL。"},
                {"role": "user", "content": f"问题：{question}\n教材证据：\n{context or '无'}\n网络补充：\n{web_context or '无'}"},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                valid_ids = {item.get("material_id") for item in evidence}
                parsed["evidence_ids"] = [item for item in parsed.get("evidence_ids", []) if item in valid_ids]
                return parsed if parsed.get("answer") and parsed["evidence_ids"] else None
        except Exception:
            return None


def build_local_answer(question: str, evidence: list[dict]) -> dict:
    if not evidence:
        return {"answer": "当前教材知识库中没有找到可定位依据。", "evidence_ids": [], "provider": "local-empty"}
    lines = [f"根据检索到的教材资料，问题“{question}”的相关依据如下："]
    for item in evidence[:5]:
        lines.append(f"- {item.get('snippet', '')}")
    return {"answer": "\n".join(lines), "evidence_ids": [item.get("material_id") for item in evidence[:5]], "provider": "local-rag"}
