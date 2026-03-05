from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional

from .db import Database
from .redis_client import RedisClient
from .utils import utcnow_iso


_SENTINEL = object()


class MessageBus:
    """
    In-process broadcast bus with persistence (SQLite) and live fanout (Redis Pub/Sub).

    PERFORMANCE NOTE
    ----------------
    Persisting each message with its own SQLite commit is very slow once message volume ramps.
    We therefore:
      - Append immediately to an in-memory ring buffer (fast, used for tail())
      - Queue messages to a background task that batch-inserts into SQLite (few commits)
      - Best-effort publish to Redis for live UI (optional)

    SQLite remains the durable store; in-memory buffer improves responsiveness between flushes.
    """

    def __init__(
        self,
        db: Database,
        redis: RedisClient,
        redis_prefix: str,
        *,
        inmem_maxlen: int = 2000,
        persist_batch_size: int = 200,
        persist_flush_interval_s: float = 0.25,
        persist_queue_max: int = 50_000,
    ):
        self.db = db
        self.redis = redis
        self.redis_prefix = redis_prefix

        self._subs: Dict[str, List[asyncio.Queue]] = defaultdict(list)

        # In-memory ring buffer per channel (for fast tails + UI)
        self._inmem: Dict[str, Deque[Dict[str, Any]]] = defaultdict(lambda: deque(maxlen=inmem_maxlen))

        # Background persistence queue
        self._persist_q: asyncio.Queue = asyncio.Queue(maxsize=persist_queue_max)
        self._persist_task: Optional[asyncio.Task] = None
        self._closing = False

        self._persist_batch_size = int(persist_batch_size)
        self._persist_flush_interval_s = float(persist_flush_interval_s)

    async def start(self) -> None:
        """Start background persistence loop (safe to call multiple times)."""
        if self._persist_task is not None:
            return
        self._closing = False
        self._persist_task = asyncio.create_task(self._persist_loop(), name="bus_persist_loop")

    async def stop(self) -> None:
        """Stop background persistence loop and flush queued messages."""
        if self._persist_task is None:
            return
        self._closing = True
        # After shutdown starts, other tasks should already be stopped.
        await self._persist_q.put(_SENTINEL)
        try:
            await self._persist_task
        finally:
            self._persist_task = None

    def subscribe(self, channel: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        self._subs[channel].append(q)
        return q

    async def publish(self, channel: str, sender: str, message: str, meta: Optional[Dict[str, Any]] = None) -> None:
        # Lazy-start persistence loop so bus works even if start() wasn't called.
        if self._persist_task is None:
            await self.start()

        ts = utcnow_iso()
        payload = {
            "id": None,  # filled by DB once persisted; UI doesn't require it
            "ts": ts,
            "channel": channel,
            "sender": sender,
            "message": message,
            "meta": meta or {},
        }

        await self._emit_payload(payload)

        # Mirror internal bus traffic into chat so Internal Messaging becomes the
        # primary lens for agent coordination.
        for mirror in self._chat_mirrors(payload):
            await self._emit_payload(mirror)

    async def _emit_payload(self, payload: Dict[str, Any]) -> None:
        channel = payload["channel"]

        # 1) In-memory tail buffer (instant)
        self._inmem[channel].append(payload)

        # 2) Best-effort Redis Pub/Sub
        try:
            await self.redis.r.publish(f"{self.redis_prefix}{channel}", json.dumps(payload))
        except Exception:
            pass

        # 3) Fanout to in-process subscribers
        for q in list(self._subs.get(channel, [])):
            if q.full():
                try:
                    _ = q.get_nowait()
                except Exception:
                    pass
            try:
                await q.put(payload)
            except Exception:
                pass

        # 4) Queue for SQLite persistence (do not block UI if overwhelmed)
        if self._closing:
            return
        try:
            self._persist_q.put_nowait(payload)
        except asyncio.QueueFull:
            # Drop oldest pending items by making room (protects latency)
            try:
                _ = self._persist_q.get_nowait()
                self._persist_q.put_nowait(payload)
            except Exception:
                # If still failing, drop
                pass

    def _chat_mirrors(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        channel = str(payload.get("channel") or "")
        sender = str(payload.get("sender") or "")
        if not sender:
            return []

        # Avoid loops for already-chat channels.
        if channel.startswith("dm:") or channel.startswith("room:"):
            return []

        mirrored: List[Dict[str, Any]] = []
        base_meta = dict(payload.get("meta") or {})
        base_meta["_mirrored_from"] = channel

        # firmwide room mirror for visibility
        mirrored.append({
            "id": None,
            "ts": payload["ts"],
            "channel": "room:all",
            "sender": sender,
            "message": payload.get("message") or "",
            "meta": base_meta,
        })

        # direct channel mirrors for clearer handoffs
        recipient_by_channel = {
            "trade_ideas": "ceo",
            "ceo_inbox": "ceo",
            "risk": "cro",
            "execution": "exec",
        }
        recipient = recipient_by_channel.get(channel)
        if recipient and recipient != sender:
            a, b = sorted([sender, recipient])
            mirrored.append({
                "id": None,
                "ts": payload["ts"],
                "channel": f"dm:{a}:{b}",
                "sender": sender,
                "message": payload.get("message") or "",
                "meta": base_meta,
            })

        return mirrored

    async def tail(self, channel: str, limit: int = 200) -> List[Dict[str, Any]]:
        """
        Return last `limit` messages for `channel`.
        Prefer in-memory (fast). If not enough, backfill from DB and merge.
        """
        limit = int(limit)
        mem = list(self._inmem.get(channel, []))

        # If we have enough in memory, return that (fast path)
        if len(mem) >= limit:
            return mem[-limit:]

        # Backfill from DB
        rows = await self.db.fetchall(
            "SELECT id, ts, channel, sender, message, meta_json "
            "FROM messages WHERE channel=? ORDER BY id DESC LIMIT ?",
            (channel, limit),
        )
        db_msgs: List[Dict[str, Any]] = []
        for r in reversed(rows):
            db_msgs.append(
                {
                    "id": r["id"],
                    "ts": r["ts"],
                    "channel": r["channel"],
                    "sender": r["sender"],
                    "message": r["message"],
                    "meta": json.loads(r["meta_json"] or "{}"),
                }
            )

        # Merge (dedupe by stable tuple)
        combined = db_msgs + mem
        out: List[Dict[str, Any]] = []
        seen = set()
        for m in combined:
            k = (m.get("ts"), m.get("channel"), m.get("sender"), m.get("message"))
            if k in seen:
                continue
            seen.add(k)
            out.append(m)

        return out[-limit:]

    async def _persist_loop(self) -> None:
        """
        Batch insert messages into SQLite with far fewer commits.
        """
        insert_sql = "INSERT INTO messages(ts, channel, sender, message, meta_json) VALUES(?,?,?,?,?)"

        while True:
            item = await self._persist_q.get()
            if item is _SENTINEL:
                break

            batch = [item]
            start = time.monotonic()

            # Gather more for a short interval (or until batch is full)
            while len(batch) < self._persist_batch_size:
                remaining = self._persist_flush_interval_s - (time.monotonic() - start)
                if remaining <= 0:
                    break
                try:
                    nxt = await asyncio.wait_for(self._persist_q.get(), timeout=remaining)
                    if nxt is _SENTINEL:
                        # Put sentinel back for the outer loop to handle after flush
                        await self._persist_q.put(_SENTINEL)
                        break
                    batch.append(nxt)
                except asyncio.TimeoutError:
                    break

            rows = [
                (m["ts"], m["channel"], m["sender"], m["message"], json.dumps(m.get("meta") or {})) for m in batch
            ]

            try:
                await self.db.executemany(insert_sql, rows)
            except Exception:
                # Avoid crashing the whole backend if disk hiccups
                # (In worst case you'll lose some logs, but UI stays alive)
                pass

        # Drain anything left (best-effort flush)
        try:
            drained: List[Dict[str, Any]] = []
            while not self._persist_q.empty():
                nxt = self._persist_q.get_nowait()
                if nxt is _SENTINEL:
                    continue
                drained.append(nxt)

            if drained:
                rows = [
                    (m["ts"], m["channel"], m["sender"], m["message"], json.dumps(m.get("meta") or {}))
                    for m in drained
                ]
                await self.db.executemany(insert_sql, rows)
        except Exception:
            pass
