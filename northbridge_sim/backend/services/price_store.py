from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..redis_client import RedisClient

@dataclass
class Price:
    last: float
    bid: Optional[float]
    ask: Optional[float]
    ts: str
    venue: str

class PriceStore:
    def __init__(self, redis: RedisClient, redis_hash: str):
        self._prices: Dict[str, Price] = {}
        self.redis = redis
        self.redis_hash = redis_hash

    def get(self, key: str) -> Optional[Price]:
        return self._prices.get(key)

    def snapshot(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k, p in self._prices.items():
            out[k] = {"last": p.last, "bid": p.bid, "ask": p.ask, "ts": p.ts, "venue": p.venue}
        return out

    async def update(self, symbol: str, venue: str, last: float, ts: str, bid: float | None = None, ask: float | None = None) -> None:
        key = f"{symbol}@{venue}"
        self._prices[key] = Price(last=last, bid=bid, ask=ask, ts=ts, venue=venue)
        await self.redis.r.hset(self.redis_hash, key, json.dumps({"last": last, "bid": bid, "ask": ask, "ts": ts, "venue": venue}))
