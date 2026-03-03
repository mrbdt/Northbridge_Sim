from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Union

import httpx

class OllamaLLM:
    """
    Thin async client for Ollama's /api/chat.
    Uses a semaphore to cap concurrent generations across all agents.
    """
    def __init__(
        self,
        base_url: str,
        max_concurrent: int = 2,
        timeout_seconds: int = 120,
        default_keep_alive: str = "30m",
        default_options: Optional[Dict[str, Any]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._sem = asyncio.Semaphore(max_concurrent)
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))
        self.default_keep_alive = default_keep_alive
        self.default_options = default_options or {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        model: str,
        messages: List[Dict[str, str]],
        format: Optional[Union[str, Dict[str, Any]]] = None,
        options: Optional[Dict[str, Any]] = None,
        keep_alive: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": keep_alive or self.default_keep_alive,
            "options": {**self.default_options, **(options or {})},
        }
        if format is not None:
            payload["format"] = format

        async with self._sem:
            r = await self._client.post(f"{self.base_url}/api/chat", json=payload)
            r.raise_for_status()
            return r.json()
