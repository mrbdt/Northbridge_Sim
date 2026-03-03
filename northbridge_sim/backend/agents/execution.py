from __future__ import annotations

import uuid
from .base import BaseAgent, AgentConfig, AgentContext
from ..models import TradeIntent, Order
from ..utils import utcnow_iso

class ExecutionAgent(BaseAgent):
    def __init__(self, cfg: AgentConfig, ctx: AgentContext):
        super().__init__(cfg, ctx)
        self._exec_q = ctx.bus.subscribe("execution")

    async def step(self) -> None:
        broker = self.ctx.services["broker"]
        portfolio = self.ctx.services["portfolio"]
        executed = 0
        last_fills = []
        while True:
            try:
                msg = self._exec_q.get_nowait()
            except Exception:
                break
            meta = msg.get("meta", {})
            intent_raw = meta.get("intent")
            if not intent_raw:
                continue
            try:
                intent = TradeIntent(**intent_raw)
            except Exception:
                continue

            order = Order(
                order_id=str(uuid.uuid4()),
                ts=utcnow_iso(),
                agent_id=self.agent_id,
                symbol=intent.symbol,
                venue=intent.venue,
                side=intent.side,
                qty=float(intent.qty),
                order_type=intent.order_type,
                limit_price=intent.limit_price,
                meta={"source_intent": intent.model_dump()},
            )
            fill, err = await broker.submit_order(order)
            if err:
                await self.ctx.bus.publish("execution", self.agent_id, f"ORDER_REJECTED: {err}", meta={"order": order.model_dump()})
                continue
            if fill:
                await portfolio.apply_fill(fill)
                last_fills.append(fill.model_dump())
                last_fills = last_fills[-5:]
                await self.ctx.bus.publish("execution", self.agent_id, f"FILLED {fill.symbol} {fill.side} qty={fill.qty} px={fill.price:.6f} fees={fill.fees:.2f}", meta={"fill": fill.model_dump()})
                executed += 1
        await self.log_state({"state":"executing","executed":executed,"last_fills":last_fills})
