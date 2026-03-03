from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterable, List, Optional, Sequence

import aiosqlite


class Database:
    """
    SQLite wrapper optimized for mixed read/write workloads:
      - Separate connections for reads vs writes
      - WAL + busy_timeout
      - Independent locks so reads don't queue behind writes
    """

    def __init__(self, path: str, busy_timeout_ms: int = 5000):
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms

        self._db_r: Optional[aiosqlite.Connection] = None
        self._db_w: Optional[aiosqlite.Connection] = None

        self._r_lock = asyncio.Lock()
        self._w_lock = asyncio.Lock()

    async def connect(self) -> None:
        self._db_r = await aiosqlite.connect(self.path)
        self._db_w = await aiosqlite.connect(self.path)

        self._db_r.row_factory = aiosqlite.Row
        self._db_w.row_factory = aiosqlite.Row

        # Pragmas on both connections
        for conn in (self._db_r, self._db_w):
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute("PRAGMA temp_store=MEMORY;")
            await conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)};")
            await conn.commit()

    async def close(self) -> None:
        if self._db_r is not None:
            await self._db_r.close()
            self._db_r = None
        if self._db_w is not None:
            await self._db_w.close()
            self._db_w = None

    async def executescript(self, script: str) -> None:
        assert self._db_w is not None
        async with self._w_lock:
            await self._db_w.executescript(script)
            await self._db_w.commit()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        assert self._db_w is not None
        async with self._w_lock:
            await self._db_w.execute(sql, params)
            await self._db_w.commit()

    async def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> None:
        assert self._db_w is not None
        async with self._w_lock:
            await self._db_w.executemany(sql, seq_of_params)
            await self._db_w.commit()

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        assert self._db_r is not None
        async with self._r_lock:
            cur = await self._db_r.execute(sql, params)
            row = await cur.fetchone()
            await cur.close()
            return dict(row) if row else None

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        assert self._db_r is not None
        async with self._r_lock:
            cur = await self._db_r.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
            return [dict(r) for r in rows]