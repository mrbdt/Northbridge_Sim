from __future__ import annotations

from typing import List, Optional
from decimal import Decimal

from cryptofeed import FeedHandler
from cryptofeed.defines import TRADES
from cryptofeed.exchanges import Binance, Coinbase

from .price_store import PriceStore
from .parquet_writer import ParquetTickWriter, Tick
from ..utils import utcnow_iso

EXCHANGE_MAP = {
    "BINANCE": Binance,
    "COINBASE": Coinbase,
}


class CryptoDataHub:
    """
    Crypto real-time via cryptofeed.

    IMPORTANT:
    Some exchanges (notably Coinbase, depending on upstream API behavior) may require
    authenticated REST calls for symbol mapping. If a venue fails to initialize
    (e.g., 401), we record the error and continue with other venues so the simulator
    can still boot.
    """
    def __init__(self, venues: List[str], symbols: List[str], price_store: PriceStore, tick_writer: ParquetTickWriter):
        self.venues = venues
        self.symbols = symbols
        self.price_store = price_store
        self.tick_writer = tick_writer
        self.fh: Optional[FeedHandler] = None
        self.startup_errors: list[str] = []

    async def start(self) -> None:
        self.fh = FeedHandler()
        for v in self.venues:
            ex = EXCHANGE_MAP.get(v.upper())
            if not ex:
                self.startup_errors.append(f"Unknown crypto venue: {v}")
                continue
            try:
                self.fh.add_feed(ex(symbols=self.symbols, channels=[TRADES], callbacks={TRADES: self._on_trade}))
            except Exception as e:
                self.startup_errors.append(f"{v.upper()} init failed: {e}")
                continue

        # Start without taking over the asyncio loop
        self.fh.run(start_loop=False, install_signal_handlers=False)

    async def stop(self) -> None:
        if self.fh:
            try:
                await self.fh.stop_async()
            except Exception:
                pass
            self.fh = None

    async def _on_trade(self, trade, receipt_timestamp: float):
        ts = utcnow_iso()
        last = float(trade.price) if isinstance(trade.price, Decimal) else float(trade.price)
        venue = str(trade.exchange).upper()
        symbol = str(trade.symbol)

        await self.price_store.update(symbol=symbol, venue=venue, last=last, ts=ts)
        await self.tick_writer.enqueue(Tick(ts=ts, symbol=symbol, venue=venue, last=last))
