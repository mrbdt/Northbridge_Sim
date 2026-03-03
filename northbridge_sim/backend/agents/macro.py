from __future__ import annotations

from .base import BaseAgent, AgentConfig, AgentContext
from ..models import TradeIntent

class MacroAgent(BaseAgent):
    async def step(self) -> None:
        price_store = self.ctx.services["price_store"]
        prices = price_store.snapshot()

        def get(sym: str):
            for k, v in prices.items():
                if k.startswith(sym + "@"):
                    return float(v["last"])
            return None

        spy = get("SPY")
        btc = get("BTC-USDT")
        if spy is None or btc is None:
            await self.log_state({"state":"waiting_data"})
            return

        intent = TradeIntent(
            symbol="SPY",
            venue="EQUITIES",
            side="buy" if btc > 0 else "sell",
            qty=3.0,
            order_type="market",
            time_horizon_minutes=240,
            confidence=0.55,
            thesis="MVP macro tilt: BTC as a rough risk-on proxy vs equities.",
            risk_notes="Replace with real macro logic later.",
            tags=["macro","mvp"]
        )
        await self.ctx.bus.publish("trade_ideas", self.agent_id, f"TRADE_INTENT {intent.symbol} {intent.side} qty={intent.qty} conf={intent.confidence:.2f}", meta={"intent": intent.model_dump()})
        await self.log_state({"state":"proposing","idea":"SPY tilt","intent":intent.model_dump()})
