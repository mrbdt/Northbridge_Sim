from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional, Tuple

from ..db import Database
from ..models import Fill, Order
from ..utils import utcnow_iso
from .price_store import PriceStore

class FeeModel:
    def __init__(self, fees_config: Dict[str, Any]):
        self.fees = fees_config.get("fees", {})

    def estimate_fees(self, venue: str, notional: float) -> float:
        venue_cfg = self.fees.get(venue, {})
        bps = float(venue_cfg.get("commission_bps", 0.0))
        fee = abs(notional) * bps / 10_000.0
        min_comm = float(venue_cfg.get("min_commission", 0.0))
        return max(fee, min_comm) if min_comm > 0 else fee

    def spread_bps(self, venue: str) -> float:
        return float(self.fees.get(venue, {}).get("spread_bps", 0.0))

class BrokerSim:
    """
    Simulated broker:
      - fills instantly at mid +/- half_spread +/- slippage
      - applies simple fees
      - records orders/fills to SQLite
    """
    def __init__(self, db: Database, price_store: PriceStore, fee_model: FeeModel, base_slippage_bps: Dict[str, float]):
        self.db = db
        self.price_store = price_store
        self.fee_model = fee_model
        self.base_slippage_bps = base_slippage_bps

    def _instrument_key(self, symbol: str, venue: str) -> str:
        return f"{symbol.upper()}@{venue.upper()}"

    def _mid_from_price(self, p) -> float:
        if p.bid is not None and p.ask is not None and p.bid > 0 and p.ask > 0:
            return (p.bid + p.ask) / 2.0
        return p.last

    async def submit_order(self, order: Order) -> Tuple[Optional[Fill], Optional[str]]:
        await self.db.execute(
            "INSERT OR REPLACE INTO orders(order_id, ts, agent_id, symbol, venue, side, qty, order_type, limit_price, status, meta_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                order.order_id,
                order.ts,
                order.agent_id,
                order.symbol,
                order.venue,
                order.side,
                order.qty,
                order.order_type,
                order.limit_price,
                order.status,
                json.dumps(order.meta or {}),
            ),
        )

        pk = self._instrument_key(order.symbol, order.venue)
        price = self.price_store.get(pk)
        if price is None:
            await self._update_order_status(order.order_id, "rejected")
            return None, f"No price for {pk}"

        mid = self._mid_from_price(price)
        spread_bps = self.fee_model.spread_bps(order.venue)
        half_spread = mid * (spread_bps / 10_000.0) / 2.0

        slip_bps = float(self.base_slippage_bps.get(order.venue, 0.0))
        slippage = mid * (slip_bps / 10_000.0)

        if order.order_type == "limit":
            if order.limit_price is None:
                await self._update_order_status(order.order_id, "rejected")
                return None, "Limit order missing limit_price"
            if order.side == "buy" and order.limit_price < mid:
                await self._update_order_status(order.order_id, "rejected")
                return None, "Limit buy not marketable"
            if order.side == "sell" and order.limit_price > mid:
                await self._update_order_status(order.order_id, "rejected")
                return None, "Limit sell not marketable"

        fill_px = mid + half_spread + slippage if order.side == "buy" else mid - half_spread - slippage
        fill_px = max(1e-9, float(fill_px))
        notional = fill_px * order.qty
        fees = self.fee_model.estimate_fees(order.venue, notional)

        fill = Fill(
            fill_id=str(uuid.uuid4()),
            ts=utcnow_iso(),
            order_id=order.order_id,
            symbol=order.symbol,
            venue=order.venue,
            side=order.side,
            qty=order.qty,
            price=fill_px,
            fees=fees,
            meta={"mid": mid, "half_spread": half_spread, "slippage": slippage, "spread_bps": spread_bps, "slip_bps": slip_bps},
        )

        await self.db.execute(
            "INSERT INTO fills(fill_id, ts, order_id, symbol, venue, side, qty, price, fees, meta_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                fill.fill_id,
                fill.ts,
                fill.order_id,
                fill.symbol,
                fill.venue,
                fill.side,
                fill.qty,
                fill.price,
                fill.fees,
                json.dumps(fill.meta or {}),
            ),
        )
        await self._update_order_status(order.order_id, "filled")
        return fill, None

    async def _update_order_status(self, order_id: str, status: str) -> None:
        await self.db.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
