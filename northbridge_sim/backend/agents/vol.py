from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import BaseAgent
from ..models import TradeIntent
from ..services.indicators import realized_vol
from ..utils import clamp


class VolAgent(BaseAgent):
    """
    Vol / RV PM:

    - Monitors realized vol proxies (SPY and BTC) and firm leverage/drawdown.
    - When risk is elevated, proposes de-risking (reduce the largest gross position).
    """

    async def step(self) -> None:
        portfolio = self.ctx.services["portfolio"]
        price_store = self.ctx.services["price_store"]

        snap = await portfolio.snapshot()

        spy_hist = price_store.history_for_symbol("SPY", preferred_venue="EQUITIES", max_points=400)
        btc_hist = price_store.history_for_symbol("BTC-USDT", preferred_venue="BINANCE", max_points=400)
        spy_prices = [p for _, p in spy_hist]
        btc_prices = [p for _, p in btc_hist]
        vol_spy = realized_vol(spy_prices[-240:]) if len(spy_prices) >= 30 else 0.0
        vol_btc = realized_vol(btc_prices[-240:]) if len(btc_prices) >= 30 else 0.0

        # Simple risk flags (tune later)
        high_vol = (vol_spy > 0.010) or (vol_btc > 0.020)
        high_lev = snap.leverage > 1.5
        high_dd = snap.drawdown > 0.05

        if not (high_vol or high_lev or high_dd):
            await self.log_state({
                "state": "monitoring",
                "leverage": snap.leverage,
                "drawdown": snap.drawdown,
                "vol_spy": vol_spy,
                "vol_btc": vol_btc,
                "flags": {"high_vol": high_vol, "high_lev": high_lev, "high_dd": high_dd},
            })
            return

        # Find largest gross position to reduce
        if not snap.positions:
            await self.log_state({"state": "alert", "reason": "no positions to reduce", "flags": {"high_vol": high_vol, "high_lev": high_lev, "high_dd": high_dd}})
            return

        def pos_gross(p: Dict[str, Any]) -> float:
            return abs(float(p.get("qty", 0.0)) * float(p.get("last", 0.0)))

        biggest = max(snap.positions, key=pos_gross)
        sym = biggest["symbol"]
        qty0 = float(biggest["qty"])
        reduce_qty = abs(qty0) * 0.25  # reduce 25%
        if reduce_qty <= 0:
            await self.log_state({"state": "alert", "reason": "largest position qty=0", "flags": {"high_vol": high_vol, "high_lev": high_lev, "high_dd": high_dd}})
            return

        side = "sell" if qty0 > 0 else "buy"
        # Choose venue from best price key
        key = price_store.best_key_for_symbol(sym)
        venue = key.split("@", 1)[-1] if key else "EQUITIES"

        confidence = clamp(0.6 + (0.1 if high_dd else 0.0) + (0.1 if high_lev else 0.0) + (0.1 if high_vol else 0.0), 0.5, 0.9)

        thesis = (
            f"Risk elevated: lev={snap.leverage:.2f}, dd={snap.drawdown:.2%}, vol_spy={vol_spy:.4f}, vol_btc={vol_btc:.4f}.\n"
            f"De-risk: reduce largest position {sym} by 25% via {side.upper()} qty={reduce_qty:.4f}."
        )

        intent = TradeIntent(
            symbol=sym,
            venue=venue,
            side=side,
            qty=float(reduce_qty),
            order_type="market",
            time_horizon_minutes=30,
            confidence=float(confidence),
            thesis=thesis,
            risk_notes="This is a de-risk action. CRO limits apply and may resize further.",
            tags=["vol", "risk", "derisk"],
        )
        await self.ctx.bus.publish("trade_ideas", self.agent_id, "TRADE_INTENT", meta={"intent": intent.model_dump(), "flags": {"high_vol": high_vol, "high_lev": high_lev, "high_dd": high_dd}})
        await self.log_state({
            "state": "hedging",
            "symbol": sym,
            "venue": venue,
            "reduce_qty": reduce_qty,
            "side": side,
            "leverage": snap.leverage,
            "drawdown": snap.drawdown,
            "vol_spy": vol_spy,
            "vol_btc": vol_btc,
            "flags": {"high_vol": high_vol, "high_lev": high_lev, "high_dd": high_dd},
            "thinking": thesis,
        })
