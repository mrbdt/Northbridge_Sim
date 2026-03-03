from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .base import BaseAgent
from ..models import TradeIntent
from ..services.indicators import log_returns, realized_vol
from ..utils import clamp


class EventAgent(BaseAgent):
    """
    Event-driven PM (rule-based MVP):

    Detects outsized short-horizon moves (return z-scores) and proposes
    a short-term mean reversion trade (fade the move).
    """

    async def step(self) -> None:
        universe = self.ctx.services["universe"]
        price_store = self.ctx.services["price_store"]
        portfolio = self.ctx.services["portfolio"]
        signals_store = self.ctx.services.get("signals_store")

        snap = await portfolio.snapshot()
        ins = await universe.list()
        if not ins:
            await self.log_state({"state": "idle", "reason": "empty universe"})
            return

        best: Optional[Tuple[float, Dict[str, Any], float, float, float]] = None
        # best = (abs_z, inst, z, r_now, vol)
        for inst in ins:
            sym = str(inst.get("symbol") or "").upper()
            # focus mostly on liquid equities, but allow others
            pref = inst.get("meta", {}).get("preferred_venue")
            hist = price_store.history_for_symbol(sym, preferred_venue=pref, max_points=300)
            prices = [p for _, p in hist]
            if len(prices) < 60:
                continue
            # compute log returns
            r = log_returns(prices)
            if r.size < 30:
                continue
            r_now = float(r[-1])
            mu = float(np.mean(r[-60:]))
            sig = float(np.std(r[-60:], ddof=1)) if r.size >= 60 else float(np.std(r, ddof=1))
            z = 0.0 if sig <= 1e-12 else (r_now - mu) / sig
            abs_z = abs(z)
            vol = realized_vol(prices[-120:]) if len(prices) >= 120 else realized_vol(prices)
            if best is None or abs_z > best[0]:
                best = (abs_z, inst, float(z), float(r_now), float(vol))

        if not best:
            await self.log_state({"state": "waiting_data", "tracked": len(ins)})
            return

        abs_z, inst, z, r_now, vol = best
        sym = inst["symbol"]
        venue = inst.get("meta", {}).get("preferred_venue") or ("EQUITIES" if sym.isalpha() else "YAHOO")

        # mean reversion: fade the move
        side = "sell" if z > 0 else "buy"
        confidence = clamp(min(0.85, max(0.25, abs_z / 4.0)), 0.25, 0.85)

        last_px = price_store.get(f"{sym.upper()}@{venue.upper()}")
        px = last_px.last if last_px else None
        multiplier = float(inst.get("multiplier") or 1.0)
        target_notional = snap.nav * 0.01  # smaller, event style
        qty = 1.0 if not px or px <= 0 else max(0.001, target_notional / (px * multiplier))

        sigs: List[Dict[str, Any]] = []
        if signals_store:
            try:
                sigs = await signals_store.recent(limit=6, category="equities")
            except Exception:
                sigs = []
        sig_titles = [s.get("title") for s in sigs[-3:] if s.get("title")]

        thesis_lines = [
            f"Event detection on {sym}: last return z={z:.2f} (abs={abs_z:.2f}), r_now={r_now:.4f}, vol~{vol:.4f}.",
            f"Mean reversion trade: {side.upper()} {sym} (~1% NAV notional).",
        ]
        if sig_titles:
            thesis_lines.append("Recent equities headlines:")
            thesis_lines.extend([f"- {t}" for t in sig_titles])

        thesis = "\n".join(thesis_lines)

        intent = TradeIntent(
            symbol=sym,
            venue=venue,
            side=side,
            qty=float(qty),
            order_type="market",
            time_horizon_minutes=60,
            confidence=float(confidence),
            thesis=thesis,
            risk_notes="Event mean reversion can fail in trending regimes; CRO limits apply.",
            tags=["event", "mean_reversion"],
        )
        await self.ctx.bus.publish("trade_ideas", self.agent_id, "TRADE_INTENT", meta={
            "intent": intent.model_dump(),
            "z": z,
            "abs_z": abs_z,
            "r_now": r_now,
            "vol": vol,
        })
        await self.log_state({
            "state": "proposing",
            "symbol": sym,
            "venue": venue,
            "z": z,
            "abs_z": abs_z,
            "r_now": r_now,
            "vol": vol,
            "confidence": confidence,
            "qty": qty,
            "thinking": thesis,
        })
