from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..db import Database
from ..utils import utcnow_iso


class SignalsStore:
    """SQLite-backed store for web/news signals."""
    def __init__(self, db: Database):
        self.db = db

    async def add_item(
        self,
        category: str,
        source: str,
        title: str,
        link: str,
        summary: str = "",
        meta: Optional[Dict[str, Any]] = None,
        ts: Optional[str] = None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO signals_items(ts, category, source, title, link, summary, meta_json)
                 VALUES(?,?,?,?,?,?,?)""",
            (ts or utcnow_iso(), category, source, title, link, summary, json.dumps(meta or {})),
        )

    async def recent(self, limit: int = 50, category: Optional[str] = None) -> List[Dict[str, Any]]:
        if category:
            rows = await self.db.fetchall(
                """SELECT id, ts, category, source, title, link, summary, meta_json
                     FROM signals_items WHERE category=? ORDER BY id DESC LIMIT ?""",
                (category, limit),
            )
        else:
            rows = await self.db.fetchall(
                """SELECT id, ts, category, source, title, link, summary, meta_json
                     FROM signals_items ORDER BY id DESC LIMIT ?""",
                (limit,),
            )
        out: List[Dict[str, Any]] = []
        for r in reversed(rows):
            out.append({
                "id": r["id"],
                "ts": r["ts"],
                "category": r["category"],
                "source": r.get("source"),
                "title": r.get("title"),
                "link": r.get("link"),
                "summary": r.get("summary"),
                "meta": json.loads(r.get("meta_json") or "{}"),
            })
        return out
