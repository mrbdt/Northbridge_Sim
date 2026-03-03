from __future__ import annotations

from .base import BaseAgent, AgentConfig, AgentContext

class InfraAgent(BaseAgent):
    async def step(self) -> None:
        prices = self.ctx.services["price_store"].snapshot()
        if not prices:
            await self.ctx.bus.publish("ops", self.agent_id, "No market data prices in store; check data hubs.")
            await self.log_state({"state":"alerting","issue":"no_prices"})
        else:
            await self.log_state({"state":"ok","n_prices":len(prices)})
