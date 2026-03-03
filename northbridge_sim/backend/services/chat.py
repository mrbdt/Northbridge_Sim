from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..bus import MessageBus
from ..db import Database
from ..utils import utcnow_iso


def _slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "room"


def dm_room(a: str, b: str) -> str:
    x, y = sorted([a, b])
    return f"dm:{x}:{y}"


@dataclass
class ChatRoom:
    room_id: str
    kind: str  # dm | group | system
    name: str
    created_by: str
    created_ts: str
    meta: Dict[str, Any]


class ChatService:
    """
    Lightweight internal messaging service.

    Storage:
      - Rooms/membership: SQLite (chat_rooms, chat_members)
      - Messages: existing messages table via MessageBus.publish(channel=room_id)
    """
    def __init__(self, db: Database, bus: MessageBus):
        self.db = db
        self.bus = bus

    async def ensure_room(self, room_id: str, kind: str, name: str, created_by: str = "system", meta: Optional[Dict[str, Any]] = None) -> None:
        ts = utcnow_iso()
        await self.db.execute(
            """INSERT OR IGNORE INTO chat_rooms(room_id, kind, name, created_by, created_ts, meta_json)
                 VALUES(?,?,?,?,?,?)""",
            (room_id, kind, name, created_by, ts, json.dumps(meta or {})),
        )

    async def ensure_members(self, room_id: str, members: Sequence[str], added_by: str = "system") -> None:
        ts = utcnow_iso()
        rows = [(room_id, m, added_by, ts, json.dumps({})) for m in members]
        await self.db.executemany(
            """INSERT OR IGNORE INTO chat_members(room_id, member_id, added_by, added_ts, meta_json)
                 VALUES(?,?,?,?,?)""",
            rows,
        )

    async def bootstrap(self, agent_ids: List[str]) -> None:
        # Firm-wide room
        await self.ensure_room("room:all", kind="system", name="ALL (Firmwide)", created_by="system")
        await self.ensure_members("room:all", agent_ids, added_by="system")

        # DMs between all agents
        for i in range(len(agent_ids)):
            for j in range(i + 1, len(agent_ids)):
                a, b = agent_ids[i], agent_ids[j]
                rid = dm_room(a, b)
                await self.ensure_room(rid, kind="dm", name=f"DM {a} ↔ {b}", created_by="system")
                await self.ensure_members(rid, [a, b], added_by="system")

        # User <-> CEO chat room (so dashboard can use same primitives)
        if "ceo" in agent_ids:
            rid = dm_room("ceo", "user")
            await self.ensure_room(rid, kind="dm", name="DM user ↔ ceo", created_by="system")
            await self.ensure_members(rid, ["ceo", "user"], added_by="system")

    async def list_rooms(self, member_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if member_id:
            rows = await self.db.fetchall(
                """SELECT r.room_id, r.kind, r.name, r.created_by, r.created_ts, r.meta_json
                     FROM chat_rooms r
                     JOIN chat_members m ON m.room_id = r.room_id
                     WHERE m.member_id = ?
                     ORDER BY r.room_id""",
                (member_id,),
            )
        else:
            rows = await self.db.fetchall(
                """SELECT room_id, kind, name, created_by, created_ts, meta_json
                     FROM chat_rooms ORDER BY room_id"""
            )

        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "room_id": r["room_id"],
                "kind": r["kind"],
                "name": r["name"],
                "created_by": r["created_by"],
                "created_ts": r["created_ts"],
                "meta": json.loads(r.get("meta_json") or "{}"),
            })
        return out

    async def members(self, room_id: str) -> List[str]:
        rows = await self.db.fetchall(
            """SELECT member_id FROM chat_members WHERE room_id = ? ORDER BY member_id""",
            (room_id,),
        )
        return [r["member_id"] for r in rows]

    async def create_group_room(self, name: str, members: Sequence[str], created_by: str = "ceo", meta: Optional[Dict[str, Any]] = None) -> str:
        # room id includes random suffix to avoid collisions
        rid = f"room:{_slugify(name)}-{uuid.uuid4().hex[:6]}"
        await self.ensure_room(rid, kind="group", name=name, created_by=created_by, meta=meta)
        await self.ensure_members(rid, members, added_by=created_by)
        await self.bus.publish("ops", created_by, f"Created group chat '{name}' ({rid})", meta={"room_id": rid, "members": list(members)})
        return rid

    async def add_member(self, room_id: str, member_id: str, actor: str = "ceo") -> None:
        await self.ensure_members(room_id, [member_id], added_by=actor)
        await self.bus.publish(room_id, actor, f"Added {member_id} to room.", meta={"event": "member_added", "member_id": member_id})

    async def remove_member(self, room_id: str, member_id: str, actor: str = "ceo") -> None:
        await self.db.execute("DELETE FROM chat_members WHERE room_id=? AND member_id=?", (room_id, member_id))
        await self.bus.publish(room_id, actor, f"Removed {member_id} from room.", meta={"event": "member_removed", "member_id": member_id})

    async def send(self, room_id: str, sender: str, message: str, meta: Optional[Dict[str, Any]] = None) -> None:
        # Soft membership check (doesn't enforce security; useful for debugging)
        members = await self.members(room_id)
        if sender not in members:
            # still allow, but tag
            meta = {**(meta or {}), "_warn": f"sender {sender} not in members"}
        await self.bus.publish(room_id, sender, message, meta=meta)

    async def tail(self, room_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        return await self.bus.tail(room_id, limit=limit)
