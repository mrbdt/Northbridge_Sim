from __future__ import annotations

from typing import Optional

import redis.asyncio as redis

class RedisClient:
    def __init__(self, url: str):
        self.url = url
        self._r: Optional[redis.Redis] = None

    async def connect(self) -> None:
        self._r = redis.Redis.from_url(self.url, decode_responses=True)

    @property
    def r(self) -> redis.Redis:
        assert self._r is not None
        return self._r

    async def close(self) -> None:
        if self._r is not None:
            await self._r.aclose()
            self._r = None
