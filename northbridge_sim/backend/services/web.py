from __future__ import annotations

from typing import Any, Dict, Optional

import httpx


class WebClient:
    """Small async HTTP client wrapper for agents/services."""
    def __init__(self, timeout_seconds: int = 15):
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_text(self, url: str, headers: Optional[Dict[str, str]] = None) -> str:
        r = await self._client.get(url, headers=headers)
        r.raise_for_status()
        return r.text

    async def get_json(self, url: str, headers: Optional[Dict[str, str]] = None) -> Any:
        r = await self._client.get(url, headers=headers)
        r.raise_for_status()
        return r.json()
