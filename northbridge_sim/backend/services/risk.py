from __future__ import annotations

from dataclasses import dataclass
from ..models import RiskDecision, TradeIntent, PortfolioSnapshot

@dataclass
class RiskLimits:
    max_gross_leverage: float
    max_net_leverage: float
    max_position_pct_nav: float
    max_daily_loss_pct: float
    max_drawdown_pct: float

class RiskService:
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def check_intent(self, snap: PortfolioSnapshot, intent: TradeIntent) -> RiskDecision:
        nav = snap.nav if snap.nav != 0 else 1.0

        px = None
        for k, v in snap.last_prices.items():
            if k.startswith(intent.symbol + "@"):
                px = float(v["last"])
                break
        if px is None:
            return RiskDecision(status="block", reason=f"No price for {intent.symbol}.")

        notional = intent.qty * px
        pct = abs(notional) / nav
        if pct > self.limits.max_position_pct_nav:
            resized_qty = intent.qty * (self.limits.max_position_pct_nav / pct)
            return RiskDecision(status="resize", reason=f"Position cap: {pct:.2%} > {self.limits.max_position_pct_nav:.2%}.", resized_qty=resized_qty)

        if snap.drawdown > self.limits.max_drawdown_pct:
            return RiskDecision(status="block", reason=f"Drawdown {snap.drawdown:.2%} exceeds max {self.limits.max_drawdown_pct:.2%}.")

        if snap.leverage > self.limits.max_gross_leverage:
            return RiskDecision(status="block", reason=f"Leverage {snap.leverage:.2f} exceeds max {self.limits.max_gross_leverage:.2f}.")

        return RiskDecision(status="ok", reason="OK")
