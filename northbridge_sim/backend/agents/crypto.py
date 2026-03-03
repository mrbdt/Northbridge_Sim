from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import BaseAgent
from ..models import TradeIntent
from ..services.indicators import pct_return, realized_vol
from ..utils import clamp


class CryptoAgent(BaseAgent):
    """
    Crypto PM (rule-based MVP):

    - Trades ETH-USDT using ETH/BTC ratio momentum as a simple relative-value proxy.
    - Falls back to BTC momentum if ETH data missing.
    - Incorporates latest crypto web signals into the rationale.
    """

    async def step(self) -> None:
        price_store = self.ctx.services["price_store"]
        portfolio = self.ctx.services["portfolio"]
        signals_store = self.ctx.services.get("signals_store")

        snap = await portfolio.snapshot()

        btc_hist = price_store.history_for_symbol("BTC-USDT", preferred_venue="BINANCE", max_points=400)
        eth_hist = price_store.history_for_symbol("ETH-USDT", preferred_venue="BINANCE", max_points=400)
        btc_prices = [p for _, p in btc_hist]
        eth_prices = [p for _, p in eth_hist]

        if len(btc_prices) < 30:
            await self.log_state({"state": "waiting_data", "btc_points": len(btc_prices), "eth_points": len(eth_prices)})
            return

        mom_btc = pct_return(btc_prices[-120:]) if len(btc_prices) >= 120 else pct_return(btc_prices)
        vol_btc = realized_vol(btc_prices[-240:]) if len(btc_prices) >= 240 else realized_vol(btc_prices)

        if len(eth_prices) >= 30:
            # ratio series
            ratio = [e / b for e, b in zip(eth_prices[-240:], btc_prices[-240:]) if b > 0]
            mom_ratio = pct_return(ratio[-120:]) if len(ratio) >= 120 else pct_return(ratio)
            vol_ratio = realized_vol(ratio[-240:]) if len(ratio) >= 240 else realized_vol(ratio)
            score = mom_ratio / (vol_ratio + 1e-6)

            side = "buy" if score > 0 else "sell"
            confidence = clamp(min(0.85, max(0.25, abs(score) / 4.0)), 0.25, 0.85)

            sym = "ETH-USDT"
            venue = "BINANCE"
            last_px = price_store.get(f"{sym}@{venue}")
            px = last_px.last if last_px else None
            target_notional = snap.nav * 0.02
            qty = 0.01 if not px or px <= 0 else max(0.001, target_notional / px)

            sigs: List[Dict[str, Any]] = []
            if signals_store:
                try:
                    sigs = await signals_store.recent(limit=6, category="crypto")
                except Exception:
                    sigs = []
            sig_titles = [s.get("title") for s in sigs[-3:] if s.get("title")]

            thesis_lines = [
                f"ETH/BTC RV: ratio_mom={mom_ratio:.2%}, ratio_vol~{vol_ratio:.4f}, score={score:.3f} → {side.upper()} ETH.",
                f"BTC context: mom~{mom_btc:.2%}, vol~{vol_btc:.4f}.",
            ]
            if sig_titles:
                thesis_lines.append("Recent crypto headlines:")
                thesis_lines.extend([f"- {t}" for t in sig_titles])

            thesis = "\n".join(thesis_lines)

            intent = TradeIntent(
                symbol=sym,
                venue=venue,
                side=side,
                qty=float(qty),
                order_type="market",
                time_horizon_minutes=120,
                confidence=float(confidence),
                thesis=thesis,
                risk_notes="Crypto can gap; use smaller sizing. CRO limits apply.",
                tags=["crypto", "rv", "ethbtc"],
            )
            await self.ctx.bus.publish("trade_ideas", self.agent_id, "TRADE_INTENT", meta={
                "intent": intent.model_dump(),
                "score": score,
                "mom_ratio": mom_ratio,
                "vol_ratio": vol_ratio,
                "mom_btc": mom_btc,
                "vol_btc": vol_btc,
            })
            await self.log_state({
                "state": "proposing",
                "symbol": sym,
                "side": side,
                "score": score,
                "mom_ratio": mom_ratio,
                "vol_ratio": vol_ratio,
                "mom_btc": mom_btc,
                "vol_btc": vol_btc,
                "confidence": confidence,
                "qty": qty,
                "thinking": thesis,
            })
            return

        # Fallback: trade BTC momentum if ETH missing
        score = mom_btc / (vol_btc + 1e-6)
        side = "buy" if score > 0 else "sell"
        confidence = clamp(min(0.8, max(0.2, abs(score) / 5.0)), 0.2, 0.8)
        sym = "BTC-USDT"
        venue = "BINANCE"
        last_px = price_store.get(f"{sym}@{venue}")
        px = last_px.last if last_px else None
        target_notional = snap.nav * 0.02
        qty = 0.01 if not px or px <= 0 else max(0.001, target_notional / px)

        thesis = f"BTC momentum fallback: mom={mom_btc:.2%}, vol~{vol_btc:.4f}, score={score:.3f}."
        intent = TradeIntent(
            symbol=sym,
            venue=venue,
            side=side,
            qty=float(qty),
            order_type="market",
            time_horizon_minutes=120,
            confidence=float(confidence),
            thesis=thesis,
            risk_notes="Fallback momentum. CRO limits apply.",
            tags=["crypto", "momentum"],
        )
        await self.ctx.bus.publish("trade_ideas", self.agent_id, "TRADE_INTENT", meta={"intent": intent.model_dump(), "score": score})
        await self.log_state({"state": "proposing", "symbol": sym, "score": score, "thinking": thesis})
