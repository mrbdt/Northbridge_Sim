from __future__ import annotations

from .base import BaseAgent, AgentConfig, AgentContext
from ..models import TradeIntent

class CROAgent(BaseAgent):
    def __init__(self, cfg: AgentConfig, ctx: AgentContext):
        super().__init__(cfg, ctx)
        self._risk_q = ctx.bus.subscribe("risk")

    async def step(self) -> None:
        portfolio = self.ctx.services["portfolio"]
        risk = self.ctx.services["risk"]
        drained = 0
        while True:
            try:
                msg = self._risk_q.get_nowait()
            except Exception:
                break
            drained += 1
            meta = msg.get("meta", {})
            intent_raw = meta.get("intent")
            if not intent_raw:
                continue
            try:
                intent = TradeIntent(**intent_raw)
            except Exception:
                continue
            snap = await portfolio.snapshot()
            decision = risk.check_intent(snap, intent)
            await self.ctx.bus.publish("risk", self.agent_id, f"RISK_{decision.status.upper()}", meta={"intent": intent.model_dump(), "decision": decision.model_dump()})
            if decision.status == "ok":
                await self.ctx.bus.publish("execution", self.agent_id, "APPROVED_INTENT", meta={"intent": intent.model_dump()})
            elif decision.status == "resize" and decision.resized_qty is not None:
                intent.qty = float(decision.resized_qty)
                await self.ctx.bus.publish("execution", self.agent_id, "APPROVED_INTENT", meta={"intent": intent.model_dump(), "resized": True})
        await self.log_state({"state":"monitoring","drained":drained})
