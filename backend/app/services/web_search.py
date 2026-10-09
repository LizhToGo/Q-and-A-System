from __future__ import annotations

from typing import Any

import httpx

from ..config import settings


class WebSearchProvider:
    """可插拔搜索适配器；未配置时不伪造网络证据。"""

    def enabled(self) -> bool:
        return bool(settings.web_search_enabled and settings.web_search_url)

    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        if not self.enabled():
            return []
        headers = {"Authorization": f"Bearer {settings.web_search_api_key}"} if settings.web_search_api_key else {}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(settings.web_search_url, params={"q": query, "limit": limit}, headers=headers)
                response.raise_for_status()
                payload = response.json()
        except Exception:
            return []
        raw_results = payload.get("results", payload.get("organic", [])) if isinstance(payload, dict) else []
        if not isinstance(raw_results, list):
            return []
        return [
            {
                "title": str(item.get("title", "")),
                "url": str(item.get("url", item.get("link", ""))),
                "snippet": str(item.get("snippet", item.get("description", ""))),
                "source_type": "web",
            }
            for item in raw_results[:limit]
            if isinstance(item, dict) and item.get("url", item.get("link"))
        ]
