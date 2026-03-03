from __future__ import annotations

import asyncio
from typing import List, Optional

import requests
from requests import Session

from .price_store import PriceStore
from .parquet_writer import ParquetTickWriter, Tick
from .universe import UniverseService
from ..utils import utcnow_iso


def _chunk(items: List[str], size: int) -> List[List[str]]:
    return [items[i:i+size] for i in range(0, len(items), size)]


class YahooUniversePoller:
    """
    Poll Yahoo Finance quote endpoint for any symbols marked as provider=yahoo in the universe.

    This can cover:
      - commodities futures (e.g., GC=F, CL=F)
      - FX rates (e.g., EURUSD=X)
      - indices (e.g., ^GSPC)
      - (and equities if you choose)
    """
    def __init__(
        self,
        universe: UniverseService,
        poll_interval_seconds: int,
        price_store: PriceStore,
        tick_writer: ParquetTickWriter,
        venue: str = "YAHOO",
        max_symbols_per_request: int = 40,
    ):
        self.universe = universe
        self.poll_interval = poll_interval_seconds
        self.price_store = price_store
        self.tick_writer = tick_writer
        self.venue = venue
        self.max_symbols_per_request = max_symbols_per_request
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._session: Session = requests.Session()
        self._session.headers.update({"User-Agent": "northbridge-sim/1.0"})

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="yahoo_universe_poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            ts = utcnow_iso()
            try:
                symbols = await self.universe.get_symbols(provider="yahoo")
                if symbols:
                    await self._poll_batch(symbols, ts)
            except Exception:
                pass
            await asyncio.sleep(self.poll_interval)

    async def _poll_batch(self, symbols: List[str], ts: str) -> None:
        # Yahoo endpoint supports multiple symbols separated by commas, but keep requests reasonably small.
        for chunk in _chunk(symbols, self.max_symbols_per_request):
            symbols_str = ",".join(chunk)
            url = "https://query1.finance.yahoo.com/v7/finance/quote"
            params = {"symbols": symbols_str}
            resp = await asyncio.to_thread(self._session.get, url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("quoteResponse", {}).get("result", [])
            for r in results:
                sym = (r.get("symbol") or "").upper()
                px = r.get("regularMarketPrice")
                if sym and px is not None:
                    last = float(px)
                    await self.price_store.update(symbol=sym, venue=self.venue, last=last, ts=ts)
                    await self.tick_writer.enqueue(Tick(ts=ts, symbol=sym, venue=self.venue, last=last))
