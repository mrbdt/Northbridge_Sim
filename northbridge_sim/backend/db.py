from __future__ import annotations

import asyncio
from typing import Any, Iterable, List, Optional, Sequence, Dict

import aiosqlite

class Database:
    def __init__(self, path: str):
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def executescript(self, script: str) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.executescript(script)
            await self._db.commit()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(sql, params)
            await self._db.commit()

    async def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.executemany(sql, seq_of_params)
            await self._db.commit()

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(sql, params)
            row = await cur.fetchone()
            await cur.close()
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
        return [dict(r) for r in rows]
