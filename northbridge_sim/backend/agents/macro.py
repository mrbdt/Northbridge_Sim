from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .base import BaseAgent
from ..models import TradeIntent
from ..services.indicators import pct_return, realized_vol
from ..utils import clamp


class MacroAgent(BaseAgent):
    """
    Discretionary macro PM (rule-based MVP):

    - Builds a simple risk-on / risk-off proxy from SPY vs TLT momentum.
    - Uses FX momentum (EURUSD, USDJPY) if present.
    - Incorporates the latest macro web signals (titles) into its rationale.
    """

    async def step(self) -> None:
        universe = self.ctx.services["universe"]
        price_store = self.ctx.services["price_store"]
        portfolio = self.ctx.services["portfolio"]
        signals_store = self.ctx.services.get("signals_store")

        snap = await portfolio.snapshot()

        # Helper to get recent prices quickly
        def tail_prices(sym: str, max_points: int = 240, preferred_venue: Optional[str] = None) -> List[float]:
            hist = price_store.history_for_symbol(sym, preferred_venue=preferred_venue, max_points=max_points)
            return [p for _, p in hist]

        spy = tail_prices("SPY", 240)
        tlt = tail_prices("TLT", 240)
        if len(spy) < 30 or len(tlt) < 30:
            await self.log_state({"state": "waiting_data", "have_spy": len(spy), "have_tlt": len(tlt)})
            return

        mom_spy = pct_return(spy[-60:])
        mom_tlt = pct_return(tlt[-60:])
        vol_spy = realized_vol(spy[-120:]) if len(spy) >= 120 else realized_vol(spy)

        risk_on_score = mom_spy - mom_tlt
        # Basic decision: if risk_on_score positive -> long SPY, else long TLT (defensive)
        if risk_on_score >= 0:
            sym = "SPY"
            venue = "EQUITIES"
            side = "buy"
        else:
            sym = "TLT"
            venue = "EQUITIES"
            side = "buy"

        # Confidence increases with magnitude of divergence and decreases with vol
        raw = abs(risk_on_score) / (vol_spy + 1e-6)
        confidence = clamp(min(0.85, max(0.25, raw / 6.0)), 0.25, 0.85)

        last_px = price_store.get(f"{sym}@{venue}")
        px = last_px.last if last_px else None
        target_notional = snap.nav * 0.02
        qty = 1.0 if not px or px <= 0 else max(0.001, target_notional / px)

        sigs: List[Dict[str, Any]] = []
        if signals_store:
            try:
                sigs = await signals_store.recent(limit=6, category="macro")
            except Exception:
                sigs = []
        sig_titles = [s.get("title") for s in sigs[-3:] if s.get("title")]

        thesis_lines = [
            f"Macro risk proxy: mom60 SPY={mom_spy:.2%}, TLT={mom_tlt:.2%} → risk_on_score={risk_on_score:.2%}.",
            f"Choosing {side.upper()} {sym} (~2% NAV notional). Realized vol(approx)={vol_spy:.4f}.",
        ]
        if sig_titles:
            thesis_lines.append("Recent macro headlines:")
            thesis_lines.extend([f"- {t}" for t in sig_titles])

        thesis = "\n".join(thesis_lines)

        intent = TradeIntent(
            symbol=sym,
            venue=venue,
            side=side,
            qty=float(qty),
            order_type="market",
            time_horizon_minutes=360,
            confidence=float(confidence),
            thesis=thesis,
            risk_notes="This is a coarse macro proxy; can whipsaw. CRO limits apply.",
            tags=["macro", "risk_on_off"],
        )
        await self.ctx.bus.publish("trade_ideas", self.agent_id, "TRADE_INTENT", meta={
            "intent": intent.model_dump(),
            "risk_on_score": risk_on_score,
            "mom_spy": mom_spy,
            "mom_tlt": mom_tlt,
            "vol_spy": vol_spy,
        })
        await self.log_state({
            "state": "proposing",
            "sym": sym,
            "side": side,
            "risk_on_score": risk_on_score,
            "mom_spy": mom_spy,
            "mom_tlt": mom_tlt,
            "vol_spy": vol_spy,
            "confidence": confidence,
            "qty": qty,
            "thinking": thesis,
        })
