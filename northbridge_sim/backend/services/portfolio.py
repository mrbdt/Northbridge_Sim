from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List

from ..db import Database
from ..models import Fill, PortfolioSnapshot
from ..utils import utcnow_iso
from .price_store import PriceStore

@dataclass
class RiskState:
    peak_nav: float

class PortfolioService:
    def __init__(self, db: Database, price_store: PriceStore, base_ccy: str, initial_cash: float):
        self.db = db
        self.price_store = price_store
        self.base_ccy = base_ccy
        self.initial_cash = initial_cash
        self._risk_state = RiskState(peak_nav=initial_cash)

    async def init_if_empty(self) -> None:
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM cash WHERE ccy=?", (self.base_ccy,))
        if row and row["n"] == 0:
            await self.db.execute("INSERT INTO cash(ccy, balance) VALUES(?,?)", (self.base_ccy, self.initial_cash))



    async def hydrate_risk_state(self) -> None:
        """
        Restore peak NAV across restarts so drawdown checks remain realistic.

        Without this, restarting the backend resets peak_nav to initial_cash, which can
        cause persistent false drawdown breaches (and CRO blocking all trades) when
        running against an existing DB whose current NAV is below initial_cash.
        """
        row = await self.db.fetchone("SELECT MAX(nav) AS peak_nav FROM nav")
        peak_from_nav = float(row["peak_nav"]) if row and row.get("peak_nav") is not None else 0.0

        cash_row = await self.db.fetchone("SELECT balance FROM cash WHERE ccy=?", (self.base_ccy,))
        cash_now = float(cash_row["balance"]) if cash_row else 0.0

        self._risk_state.peak_nav = max(peak_from_nav, cash_now, 1.0)

    async def apply_fill(self, fill: Fill) -> None:
        side_mult = 1.0 if fill.side == "buy" else -1.0
        delta_qty = side_mult * fill.qty
        cash_delta = -(fill.price * fill.qty) * side_mult
        cash_delta -= fill.fees

        cash = await self.db.fetchone("SELECT balance FROM cash WHERE ccy=?", (self.base_ccy,))
        bal = float(cash["balance"]) if cash else 0.0
        bal += cash_delta
        await self.db.execute("INSERT OR REPLACE INTO cash(ccy, balance) VALUES(?,?)", (self.base_ccy, bal))

        pos = await self.db.fetchone("SELECT qty, avg_price, realized_pnl FROM positions WHERE symbol=?", (fill.symbol,))
        if not pos:
            new_qty = delta_qty
            avg_price = fill.price
            realized = 0.0
        else:
            qty0 = float(pos["qty"])
            avg0 = float(pos["avg_price"])
            realized = float(pos["realized_pnl"])
            new_qty = qty0 + delta_qty

            if qty0 != 0 and (qty0 * delta_qty) < 0:
                closed_qty = min(abs(qty0), abs(delta_qty))
                if qty0 > 0:
                    realized += (fill.price - avg0) * closed_qty
                else:
                    realized += (avg0 - fill.price) * closed_qty

            if new_qty == 0:
                avg_price = 0.0
            else:
                if qty0 == 0 or (qty0 * delta_qty) > 0:
                    avg_price = (qty0 * avg0 + delta_qty * fill.price) / new_qty
                else:
                    avg_price = avg0

        await self.db.execute(
            "INSERT OR REPLACE INTO positions(symbol, qty, avg_price, realized_pnl, meta_json) VALUES(?,?,?,?,?)",
            (fill.symbol, new_qty, avg_price, realized, json.dumps({})),
        )

    async def snapshot(self) -> PortfolioSnapshot:
        ts = utcnow_iso()
        cash_row = await self.db.fetchone("SELECT balance FROM cash WHERE ccy=?", (self.base_ccy,))
        cash = {self.base_ccy: float(cash_row["balance"]) if cash_row else 0.0}

        positions = await self.db.fetchall("SELECT symbol, qty, avg_price, realized_pnl FROM positions")
        last_prices = self.price_store.snapshot()

        gross = 0.0
        net = 0.0
        pos_out: List[Dict[str, Any]] = []
        for p in positions:
            sym = p["symbol"]
            qty = float(p["qty"])
            avg = float(p["avg_price"])
            rpnl = float(p["realized_pnl"])

            px = None
            for k, v in last_prices.items():
                if k.startswith(sym + "@"):
                    px = float(v["last"])
                    break
            px = px if px is not None else avg

            mkt = qty * px
            cost = qty * avg
            upnl = mkt - cost
            gross += abs(mkt)
            net += mkt
            pos_out.append({"symbol": sym, "qty": qty, "avg_price": avg, "last": px, "unrealized_pnl": upnl, "realized_pnl": rpnl})

        nav = cash[self.base_ccy] + sum((p["qty"] * p["last"]) for p in pos_out)
        leverage = (gross / nav) if nav != 0 else 0.0

        if nav > self._risk_state.peak_nav:
            self._risk_state.peak_nav = nav
        drawdown = (self._risk_state.peak_nav - nav) / self._risk_state.peak_nav if self._risk_state.peak_nav > 0 else 0.0

        return PortfolioSnapshot(
            ts=ts,
            nav=float(nav),
            cash=cash,
            positions=pos_out,
            last_prices=last_prices,
            gross_exposure=float(gross),
            net_exposure=float(net),
            leverage=float(leverage),
            drawdown=float(drawdown),
        )

    async def persist_nav(self, snap: PortfolioSnapshot) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO nav(ts, nav, gross_exposure, net_exposure, leverage, drawdown, meta_json) VALUES(?,?,?,?,?,?,?)",
            (snap.ts, snap.nav, snap.gross_exposure, snap.net_exposure, snap.leverage, snap.drawdown, json.dumps({})),
        )
