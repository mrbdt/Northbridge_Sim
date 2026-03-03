from __future__ import annotations

import random
from .base import BaseAgent, AgentConfig, AgentContext
from ..models import TradeIntent

class EventAgent(BaseAgent):
    async def step(self) -> None:
        equities = self.ctx.settings.data.get("equities", {}).get("symbols", [])
        if not equities:
            await self.log_state({"state":"idle"})
            return
        sym = random.choice(equities)
        intent = TradeIntent(
            symbol=sym,
            venue="EQUITIES",
            side="sell",
            qty=2.0,
            order_type="market",
            time_horizon_minutes=60,
            confidence=0.45,
            thesis=f"MVP event placeholder: fade short-term move in {sym}.",
            risk_notes="Replace with real event detection later.",
            tags=["event","mvp"]
        )
        await self.ctx.bus.publish("trade_ideas", self.agent_id, f"TRADE_INTENT {intent.symbol} {intent.side} qty={intent.qty} conf={intent.confidence:.2f}", meta={"intent": intent.model_dump()})
        await self.log_state({"state":"proposing","symbol":sym,"intent":intent.model_dump()})
