from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable, List, Optional

import requests

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

from .price_store import PriceStore
from .parquet_writer import ParquetTickWriter, Tick
from ..utils import utcnow_iso


def _is_rate_limited(exc: Exception) -> bool:
    """
    Best-effort detector for Alpaca 429 errors across different exception types.
    We check common attributes and also fall back to string matching.
    """
    code = getattr(exc, "status_code", None)
    if code == 429:
        return True
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True

    s = str(exc)
    if "429" in s and ("Too Many Requests" in s or "too many requests" in s):
        return True
    if "429" in s and "Client Error" in s:
        return True
    return False


class AutoEquitiesPoller:
    """
    Poll equities prices. Primary: Alpaca. Automatic fallback: Yahoo on 429 rate limit.

    - Starts in Alpaca mode if ALPACA_API_KEY/ALPACA_SECRET_KEY are set.
    - If Alpaca returns 429 Too Many Requests, switches to Yahoo for the rest of the session.
    """
    def __init__(
        self,
        symbols: List[str],
        poll_interval_seconds: int,
        price_store: PriceStore,
        tick_writer: ParquetTickWriter,
        venue: str = "EQUITIES",
        on_event: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.symbols = [s.strip().upper() for s in symbols]
        self.poll_interval = poll_interval_seconds
        self.price_store = price_store
        self.tick_writer = tick_writer
        self.venue = venue
        self.on_event = on_event

        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

        self._alpaca_client: Optional[StockHistoricalDataClient] = None
        self._mode: str = "alpaca"  # alpaca | yahoo

    @property
    def mode(self) -> str:
        return self._mode

    def add_symbols(self, symbols: List[str]) -> None:
        for s in symbols:
            sym = s.strip().upper()
            if sym and sym not in self.symbols:
                self.symbols.append(sym)

    def remove_symbols(self, symbols: List[str]) -> None:
        remove_set = {s.strip().upper() for s in symbols}
        self.symbols = [s for s in self.symbols if s not in remove_set]

    async def start(self) -> None:
        api_key = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")

        if api_key and secret_key:
            self._alpaca_client = StockHistoricalDataClient(api_key, secret_key)
            self._mode = "alpaca"
        else:
            self._alpaca_client = None
            self._mode = "yahoo"
            if self.on_event:
                await self.on_event("Alpaca keys missing; equities provider set to Yahoo fallback.")

        self._task = asyncio.create_task(self._run(), name="equities_auto_poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            ts = utcnow_iso()
            try:
                if self._mode == "alpaca":
                    await self._poll_alpaca(ts)
                else:
                    await self._poll_yahoo(ts)
            except Exception:
                # swallow; infra agent will notice missing prices
                pass
            await asyncio.sleep(self.poll_interval)

    async def _poll_alpaca(self, ts: str) -> None:
        if self._alpaca_client is None:
            self._mode = "yahoo"
            if self.on_event:
                await self.on_event("Alpaca client not available; switching to Yahoo fallback.")
            await self._poll_yahoo(ts)
            return

        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=self.symbols)
            quotes = await asyncio.to_thread(self._alpaca_client.get_stock_latest_quote, req)
            for sym in self.symbols:
                q = quotes.get(sym)
                if q is None:
                    continue
                bid = float(getattr(q, "bid_price", None) or 0.0)
                ask = float(getattr(q, "ask_price", None) or 0.0)
                if bid > 0 and ask > 0:
                    mid = (bid + ask) / 2.0
                    await self.price_store.update(symbol=sym, venue=self.venue, last=mid, bid=bid, ask=ask, ts=ts)
                    await self.tick_writer.enqueue(Tick(ts=ts, symbol=sym, venue=self.venue, last=mid, bid=bid, ask=ask))
        except Exception as e:
            if _is_rate_limited(e):
                self._mode = "yahoo"
                if self.on_event:
                    await self.on_event("Alpaca rate limit hit (429). Switching equities provider to Yahoo fallback.")
                await self._poll_yahoo(ts)
            else:
                raise

    async def _poll_yahoo(self, ts: str) -> None:
        symbols_str = ",".join(self.symbols)
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols_str}"
        resp = await asyncio.to_thread(requests.get, url, 10)
        data = resp.json()
        results = data.get("quoteResponse", {}).get("result", [])
        for r in results:
            sym = r.get("symbol")
            px = r.get("regularMarketPrice")
            if sym and px is not None:
                last = float(px)
                await self.price_store.update(symbol=sym, venue=self.venue, last=last, ts=ts)
                await self.tick_writer.enqueue(Tick(ts=ts, symbol=sym, venue=self.venue, last=last))


class YahooPoller:
    """
    Explicit Yahoo-only poller (kept for compatibility / testing).
    """
    def __init__(self, symbols: List[str], poll_interval_seconds: int, price_store: PriceStore, tick_writer: ParquetTickWriter, venue: str = "EQUITIES"):
        self.symbols = [s.strip().upper() for s in symbols]
        self.poll_interval = poll_interval_seconds
        self.price_store = price_store
        self.tick_writer = tick_writer
        self.venue = venue
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="yahoo_poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            ts = utcnow_iso()
            try:
                symbols_str = ",".join(self.symbols)
                url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols_str}"
                resp = await asyncio.to_thread(requests.get, url, 10)
                data = resp.json()
                results = data.get("quoteResponse", {}).get("result", [])
                for r in results:
                    sym = r.get("symbol")
                    px = r.get("regularMarketPrice")
                    if sym and px is not None:
                        last = float(px)
                        await self.price_store.update(symbol=sym, venue=self.venue, last=last, ts=ts)
                        await self.tick_writer.enqueue(Tick(ts=ts, symbol=sym, venue=self.venue, last=last))
            except Exception:
                pass
            await asyncio.sleep(self.poll_interval)
