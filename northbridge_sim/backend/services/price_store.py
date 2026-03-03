from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

from ..redis_client import RedisClient


@dataclass
class Price:
    last: float
    bid: Optional[float]
    ask: Optional[float]
    ts: str
    venue: str


class PriceStore:
    """
    In-memory last price store + lightweight rolling history buffer.

    - Last prices are also mirrored into Redis hash for the UI.
    - History is kept in-memory only (for fast indicator calculations).
    """
    def __init__(self, redis: RedisClient, redis_hash: str, history_maxlen: int = 5000):
        self._prices: Dict[str, Price] = {}
        self._history: Dict[str, Deque[Tuple[str, float]]] = defaultdict(lambda: deque(maxlen=history_maxlen))
        self.redis = redis
        self.redis_hash = redis_hash
        self.history_maxlen = history_maxlen

    def get(self, key: str) -> Optional[Price]:
        return self._prices.get(key)

    def keys(self) -> List[str]:
        return list(self._prices.keys())

    def keys_for_symbol(self, symbol: str) -> List[str]:
        sym = symbol.upper()
        return [k for k in self._prices.keys() if k.startswith(sym + "@")]

    def best_key_for_symbol(self, symbol: str, preferred_venue: Optional[str] = None) -> Optional[str]:
        keys = self.keys_for_symbol(symbol)
        if not keys:
            return None
        if preferred_venue:
            pv = preferred_venue.upper()
            for k in keys:
                if k.endswith("@" + pv) or k.split("@", 1)[-1] == pv:
                    return k
        # pick the one with latest timestamp string (ISO sorts lexicographically)
        keys.sort(key=lambda k: (self._prices[k].ts if k in self._prices else ""), reverse=True)
        return keys[0]

    def snapshot(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k, p in self._prices.items():
            out[k] = {"last": p.last, "bid": p.bid, "ask": p.ask, "ts": p.ts, "venue": p.venue}
        return out

    def history(self, key: str, max_points: int = 300) -> List[Tuple[str, float]]:
        dq = self._history.get(key)
        if not dq:
            return []
        if max_points <= 0:
            return list(dq)
        return list(dq)[-max_points:]

    def history_for_symbol(self, symbol: str, preferred_venue: Optional[str] = None, max_points: int = 300) -> List[Tuple[str, float]]:
        key = self.best_key_for_symbol(symbol, preferred_venue=preferred_venue)
        if not key:
            return []
        return self.history(key, max_points=max_points)

    async def update(self, symbol: str, venue: str, last: float, ts: str, bid: float | None = None, ask: float | None = None) -> None:
        sym = symbol.upper()
        ven = venue.upper()
        key = f"{sym}@{ven}"
        self._prices[key] = Price(last=float(last), bid=bid, ask=ask, ts=ts, venue=ven)
        self._history[key].append((ts, float(last)))
        await self.redis.r.hset(self.redis_hash, key, json.dumps({"last": float(last), "bid": bid, "ask": ask, "ts": ts, "venue": ven}))
