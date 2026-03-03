from __future__ import annotations

from .base import BaseAgent, AgentConfig, AgentContext

class OpsAgent(BaseAgent):
    async def step(self) -> None:
        snap = await self.ctx.services["portfolio"].snapshot()
        await self.ctx.bus.publish("ops", self.agent_id, f"NAV={snap.nav:.2f} lev={snap.leverage:.2f} dd={snap.drawdown:.2%}")
        await self.log_state({"state":"reporting"})
