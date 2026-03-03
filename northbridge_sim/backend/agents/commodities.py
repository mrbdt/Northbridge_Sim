from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .base import BaseAgent
from ..models import TradeIntent
from ..services.indicators import simple_momentum_signal
from ..utils import clamp


class CommoditiesAgent(BaseAgent):
    """Commodities PM: focuses on commodity-linked instruments (typically Yahoo futures tickers)."""

    async def step(self) -> None:
        universe = self.ctx.services["universe"]
        price_store = self.ctx.services["price_store"]
        portfolio = self.ctx.services["portfolio"]
        signals_store = self.ctx.services.get("signals_store")

        ins = await universe.list()
        commodities = [x for x in ins if x.get("asset_class") in ("commodity", "commodities") or str(x.get("symbol","")).upper().endswith("=F")]
        if not commodities:
            await self.log_state({"state": "idle", "reason": "no commodity instruments in universe"})
            return

        snap = await portfolio.snapshot()

        scored: List[Tuple[float, Dict[str, Any], float, float, float]] = []
        for inst in commodities:
            sym = inst["symbol"]
            pref = inst.get("meta", {}).get("preferred_venue")
            hist = price_store.history_for_symbol(sym, preferred_venue=pref, max_points=200)
            prices = [p for _, p in hist]
            if len(prices) < 10:
                continue
            mom, vol = simple_momentum_signal(prices, lookback=60)
            score = mom / (vol + 1e-6)
            last = prices[-1]
            scored.append((abs(score), inst, score, mom, vol))

        if not scored:
            await self.log_state({"state": "waiting_data", "tracked": len(commodities)})
            return

        scored.sort(key=lambda x: x[0], reverse=True)
        _, inst, score, mom, vol = scored[0]
        sym = inst["symbol"]
        venue = inst.get("meta", {}).get("preferred_venue") or ("YAHOO" if sym.endswith("=F") else "EQUITIES")

        side = "buy" if score > 0 else "sell"
        confidence = clamp(min(0.9, max(0.2, abs(score) / 3.0)), 0.2, 0.9)

        # Size to ~2% NAV notional, risk service will resize if needed
        last_px = price_store.get(f"{sym.upper()}@{venue.upper()}")
        px = last_px.last if last_px else None
        multiplier = float(inst.get("multiplier") or 1.0)
        target_notional = snap.nav * 0.02
        qty = 1.0 if not px or px <= 0 else max(0.001, target_notional / (px * multiplier))

        # pull 2-3 recent commodity signals
        sigs = []
        if signals_store:
            try:
                sigs = await signals_store.recent(limit=5, category="commodities")
            except Exception:
                sigs = []

        thesis = (
            f"Commodities momentum/risk-adjusted score={score:.3f} (mom={mom:.3%}, vol={vol:.4f}) on {sym}.\n"
            + ("Recent web signals:\n" + "\n".join([f"- {s.get('title')}" for s in sigs[-3:]]) if sigs else "")
        ).strip()

        intent = TradeIntent(
            symbol=sym,
            venue=venue,
            side=side,
            qty=float(qty),
            order_type="market",
            time_horizon_minutes=240,
            confidence=float(confidence),
            thesis=thesis,
            risk_notes="Sized ~2% NAV notional; CRO will enforce firm limits.",
            tags=["commodities", "momentum"],
        )
        await self.ctx.bus.publish("trade_ideas", self.agent_id, "TRADE_INTENT", meta={"intent": intent.model_dump(), "score": score, "mom": mom, "vol": vol})
        await self.log_state({
            "state": "proposing",
            "symbol": sym,
            "venue": venue,
            "score": score,
            "momentum": mom,
            "vol": vol,
            "confidence": confidence,
            "qty": qty,
            "thinking": thesis,
        })
