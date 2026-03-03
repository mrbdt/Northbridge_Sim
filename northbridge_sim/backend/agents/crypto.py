from __future__ import annotations

from .base import BaseAgent, AgentConfig, AgentContext
from ..models import TradeIntent

class CryptoAgent(BaseAgent):
    async def step(self) -> None:
        prices = self.ctx.services["price_store"].snapshot()

        def get(sym: str):
            for k, v in prices.items():
                if k.startswith(sym + "@"):
                    return float(v["last"]), v["venue"]
            return None, None

        btc, vbtc = get("BTC-USDT")
        eth, veth = get("ETH-USDT")
        if btc is None or eth is None:
            await self.log_state({"state":"waiting_data"})
            return

        ratio = eth / btc if btc else 0
        side = "buy" if ratio < 0.06 else "sell"

        intent = TradeIntent(
            symbol="ETH-USDT",
            venue=veth or "BINANCE",
            side=side,
            qty=0.2,
            order_type="market",
            time_horizon_minutes=90,
            confidence=0.6,
            thesis=f"MVP crypto RV: ETH/BTC={ratio:.4f}.",
            risk_notes="Replace with funding/basis later.",
            tags=["crypto","mvp"]
        )
        await self.ctx.bus.publish("trade_ideas", self.agent_id, f"TRADE_INTENT {intent.symbol} {intent.side} qty={intent.qty} conf={intent.confidence:.2f}", meta={"intent": intent.model_dump()})
        await self.log_state({"state":"proposing","ratio":ratio,"intent":intent.model_dump()})
