from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, Dict, List, Optional

from .db import Database
from .redis_client import RedisClient
from .utils import utcnow_iso

class MessageBus:
    """
    In-process broadcast bus with persistence (SQLite) and live fanout (Redis Pub/Sub).
    SQLite is the source of truth; Redis Pub/Sub is best-effort for live UI.
    """
    def __init__(self, db: Database, redis: RedisClient, redis_prefix: str):
        self.db = db
        self.redis = redis
        self.redis_prefix = redis_prefix
        self._subs: Dict[str, List[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, channel: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        self._subs[channel].append(q)
        return q

    async def publish(self, channel: str, sender: str, message: str, meta: Optional[Dict[str, Any]] = None) -> None:
        ts = utcnow_iso()
        meta_json = json.dumps(meta or {})
        await self.db.execute(
            "INSERT INTO messages(ts, channel, sender, message, meta_json) VALUES(?,?,?,?,?)",
            (ts, channel, sender, message, meta_json),
        )
        try:
            await self.redis.r.publish(f"{self.redis_prefix}{channel}", json.dumps({
                "ts": ts, "channel": channel, "sender": sender, "message": message, "meta": meta or {}
            }))
        except Exception:
            pass

        for q in list(self._subs.get(channel, [])):
            if q.full():
                try:
                    _ = q.get_nowait()
                except Exception:
                    pass
            await q.put({"ts": ts, "channel": channel, "sender": sender, "message": message, "meta": meta or {}})

    async def tail(self, channel: str, limit: int = 200) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT id, ts, channel, sender, message, meta_json FROM messages WHERE channel=? ORDER BY id DESC LIMIT ?",
            (channel, limit),
        )
        out: List[Dict[str, Any]] = []
        for r in reversed(rows):
            out.append({
                "id": r["id"],
                "ts": r["ts"],
                "channel": r["channel"],
                "sender": r["sender"],
                "message": r["message"],
                "meta": json.loads(r["meta_json"] or "{}"),
            })
        return out
