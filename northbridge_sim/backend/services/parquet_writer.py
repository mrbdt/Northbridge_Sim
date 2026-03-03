from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

@dataclass
class Tick:
    ts: str
    symbol: str
    venue: str
    last: float
    bid: float | None = None
    ask: float | None = None

class ParquetTickWriter:
    """
    Writes ticks as small Parquet parts in a partitioned folder:
      parquet_root/ticks/symbol=.../venue=.../date=YYYY-MM-DD/part-<uuid>.parquet
    """
    def __init__(self, parquet_root: str, flush_every: int = 250, flush_seconds: int = 5):
        self.root = Path(parquet_root)
        self.flush_every = flush_every
        self.flush_seconds = flush_seconds
        self._q: asyncio.Queue[Tick] = asyncio.Queue(maxsize=200_000)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="parquet_tick_writer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def enqueue(self, tick: Tick) -> None:
        if self._q.full():
            try:
                _ = self._q.get_nowait()
            except Exception:
                pass
        await self._q.put(tick)

    async def _run(self) -> None:
        buf: list[Tick] = []
        while not self._stop.is_set():
            try:
                tick = await asyncio.wait_for(self._q.get(), timeout=self.flush_seconds)
                buf.append(tick)
            except asyncio.TimeoutError:
                pass

            if buf and (len(buf) >= self.flush_every):
                await self._flush(buf)
                buf = []

        if buf:
            await self._flush(buf)

    async def _flush(self, ticks: list[Tick]) -> None:
        rows = [{
            "ts": t.ts,
            "symbol": t.symbol,
            "venue": t.venue,
            "last": t.last,
            "bid": t.bid,
            "ask": t.ask,
        } for t in ticks]
        df = pd.DataFrame(rows)
        if df.empty:
            return

        date = df["ts"].iloc[0][:10]
        symbol = df["symbol"].iloc[0]
        venue = df["venue"].iloc[0]

        out_dir = self.root / "ticks" / f"symbol={symbol}" / f"venue={venue}" / f"date={date}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"part-{uuid.uuid4().hex}.parquet"

        table = pa.Table.from_pandas(df, preserve_index=False)
        pq.write_table(table, out_path)
