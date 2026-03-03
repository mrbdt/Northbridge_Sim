from __future__ import annotations

from .base import BaseAgent, AgentConfig, AgentContext
from ..models import TradeIntent

class VolAgent(BaseAgent):
    async def step(self) -> None:
        snap = await self.ctx.services["portfolio"].snapshot()
        if snap.leverage > 1.5:
            intent = TradeIntent(
                symbol="SPY",
                venue="EQUITIES",
                side="sell",
                qty=1.0,
                order_type="market",
                time_horizon_minutes=60,
                confidence=0.7,
                thesis="MVP vol/risk: leverage elevated -> reduce risk.",
                risk_notes="Replace with realized vol later.",
                tags=["vol","mvp"]
            )
            await self.ctx.bus.publish("trade_ideas", self.agent_id, f"TRADE_INTENT {intent.symbol} {intent.side} qty={intent.qty} conf={intent.confidence:.2f}", meta={"intent": intent.model_dump()})
            await self.log_state({"state":"hedging","leverage":snap.leverage,"intent":intent.model_dump()})
        else:
            await self.log_state({"state":"monitoring","leverage":snap.leverage})
