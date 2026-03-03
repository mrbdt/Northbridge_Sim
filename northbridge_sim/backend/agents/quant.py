from __future__ import annotations

from typing import List

from .base import BaseAgent, AgentConfig, AgentContext
from ..models import TradeIntent

class QuantAgent(BaseAgent):
    async def step(self) -> None:
        price_store = self.ctx.services["price_store"]
        settings = self.ctx.settings
        universe = settings.data.get("crypto", {}).get("symbols", []) + settings.data.get("equities", {}).get("symbols", [])
        prices = price_store.snapshot()

        candidates: List[TradeIntent] = []
        for sym in universe:
            px = None
            venue = None
            for k, v in prices.items():
                if k.startswith(sym + "@"):
                    px = float(v["last"])
                    venue = v["venue"]
                    break
            if px is None or venue is None:
                continue
            h = abs(hash(sym)) % 1000
            score = (h / 1000.0) * 2 - 1
            side = "buy" if score > 0 else "sell"
            conf = min(0.9, max(0.1, abs(score)))
            qty = 0.01 if "BTC" in sym else 0.2 if "ETH" in sym else 5.0
            if sym in settings.data.get("equities", {}).get("symbols", []):
                qty = 5.0
                venue = "EQUITIES"
            candidates.append(TradeIntent(
                symbol=sym,
                venue=venue,
                side=side,
                qty=qty,
                order_type="market",
                time_horizon_minutes=120,
                confidence=float(conf),
                thesis=f"MVP systematic placeholder score={score:.2f}.",
                risk_notes="Replace with real factors later.",
                tags=["quant","mvp"]
            ))
        candidates.sort(key=lambda x: x.confidence, reverse=True)
        for intent in candidates[:2]:
            await self.ctx.bus.publish("trade_ideas", self.agent_id, f"TRADE_INTENT {intent.symbol} {intent.side} qty={intent.qty} conf={intent.confidence:.2f}", meta={"intent": intent.model_dump()})
        top = [i.model_dump() for i in candidates[:2]]
        await self.log_state({"state":"proposing","ideas_sent":min(2,len(candidates)),"top_ideas":top,"universe_size":len(universe),"n_prices":len(prices)})
