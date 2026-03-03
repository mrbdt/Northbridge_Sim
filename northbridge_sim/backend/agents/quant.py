from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .base import BaseAgent
from ..models import TradeIntent
from ..services.indicators import pct_return, realized_vol
from ..utils import clamp


class QuantAgent(BaseAgent):
    """
    Quant PM:
      - Cross-sectional risk-adjusted momentum across the current universe
      - Proposes up to 2 trades (best long + best short) each heartbeat
    """

    async def step(self) -> None:
        universe = self.ctx.services["universe"]
        price_store = self.ctx.services["price_store"]
        portfolio = self.ctx.services["portfolio"]

        ins = await universe.list()
        if not ins:
            await self.log_state({"state": "idle", "reason": "empty universe"})
            return

        snap = await portfolio.snapshot()

        scored: List[Tuple[float, Dict[str, Any], float, float, float]] = []
        for inst in ins:
            sym = str(inst.get("symbol") or "").upper()
            if sym in ("USDT", "USD"):
                continue
            pref = inst.get("meta", {}).get("preferred_venue")
            hist = price_store.history_for_symbol(sym, preferred_venue=pref, max_points=400)
            prices = [p for _, p in hist]
            if len(prices) < 30:
                continue

            # short & medium momentum + realized vol
            mom_30 = pct_return(prices[-30:])
            mom_120 = pct_return(prices[-120:]) if len(prices) >= 120 else pct_return(prices)
            vol = realized_vol(prices[-120:]) if len(prices) >= 120 else realized_vol(prices)

            # composite score: risk-adjusted momentum
            score = (0.7 * mom_30 + 0.3 * mom_120) / (vol + 1e-6)
            scored.append((score, inst, mom_30, mom_120, vol))

        if not scored:
            await self.log_state({"state": "waiting_data", "tracked": len(ins)})
            return

        best_long = max(scored, key=lambda x: x[0])
        best_short = min(scored, key=lambda x: x[0])

        intents: List[TradeIntent] = []
        for score, inst, mom_30, mom_120, vol in [best_long, best_short]:
            sym = inst["symbol"]
            venue = inst.get("meta", {}).get("preferred_venue") or ("BINANCE" if "-" in sym else "EQUITIES")
            side = "buy" if score > 0 else "sell"
            confidence = clamp(min(0.9, max(0.2, abs(score) / 4.0)), 0.2, 0.9)

            # size: ~1.5% NAV notional per idea (CRO will clamp)
            last_px = price_store.get(f"{sym.upper()}@{venue.upper()}")
            px = last_px.last if last_px else None
            multiplier = float(inst.get("multiplier") or 1.0)
            target_notional = snap.nav * 0.015
            qty = 1.0 if not px or px <= 0 else max(0.001, target_notional / (px * multiplier))

            thesis = (
                f"Cross-sectional risk-adjusted momentum on {sym} ({venue}).\n"
                f"score={score:.3f}, mom30={mom_30:.3%}, mom120={mom_120:.3%}, vol={vol:.4f}.\n"
                f"Sizing ~1.5% NAV notional; CRO enforces limits."
            )
            intents.append(TradeIntent(
                symbol=sym,
                venue=venue,
                side=side,
                qty=float(qty),
                order_type="market",
                time_horizon_minutes=180,
                confidence=float(confidence),
                thesis=thesis,
                risk_notes="Momentum can mean-revert; watch vol spikes. Hard risk limits enforced by CRO.",
                tags=["quant", "momentum", "cross_section"],
            ))

        # Avoid duplicates if best_long == best_short
        dedup: Dict[str, TradeIntent] = {}
        for it in intents:
            dedup[it.symbol + "@" + it.venue + "@" + it.side] = it
        final = list(dedup.values())[:2]

        for intent in final:
            await self.ctx.bus.publish("trade_ideas", self.agent_id, "TRADE_INTENT", meta={"intent": intent.model_dump()})

        # Log full thinking (top 10 by abs score)
        ranked = sorted(scored, key=lambda x: abs(x[0]), reverse=True)[:10]
        thinking_lines = []
        for s, inst, m30, m120, v in ranked:
            thinking_lines.append(f"{inst['symbol']} score={s:.3f} mom30={m30:.2%} mom120={m120:.2%} vol={v:.4f}")
        thinking = "\n".join(thinking_lines)

        await self.log_state({
            "state": "proposing",
            "ideas_sent": len(final),
            "tracked": len(scored),
            "top_ranked": thinking_lines,
            "thinking": thinking,
        })
