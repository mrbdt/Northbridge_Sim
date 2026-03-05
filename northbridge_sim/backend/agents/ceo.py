from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
import datetime as dt

from .base import BaseAgent
from ..llm import LLMServiceError, LLMTimeoutError
from ..models import TradeIntent
from ..utils import utcnow_iso


TRADE_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "venue": {"type": "string"},
        "side": {"type": "string", "enum": ["buy", "sell"]},
        "qty": {"type": "number"},
        "order_type": {"type": "string", "enum": ["market", "limit"]},
        "limit_price": {"type": ["number", "null"]},
        "time_horizon_minutes": {"type": "integer"},
        "confidence": {"type": "number"},
        "thesis": {"type": "string"},
        "risk_notes": {"type": "string"},
        "stop_loss": {"type": ["number", "null"]},
        "take_profit": {"type": ["number", "null"]},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["symbol", "venue", "side", "qty", "order_type", "time_horizon_minutes", "confidence", "thesis"],
}

CEO_SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},
        "selected": {"type": "array", "items": TRADE_INTENT_SCHEMA},
    },
    "required": ["analysis", "selected"],
}


class CEOAgent(BaseAgent):
    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self._ideas_q = ctx.bus.subscribe("trade_ideas")
        self._directive_q = ctx.bus.subscribe("ceo_inbox")
        self._pending_intents: List[Dict[str, Any]] = []
        self.risk_posture = {"max_risk": "normal", "notes": "default"}
        self._last_report_ts: Optional[dt.datetime] = None
        self._report_version: int = 0
        self._last_report_date: Optional[str] = None
        self._last_hourly_key: Optional[str] = None
        self._last_midnight_date: Optional[str] = None
        self._last_decision_analysis: str = ""
        self._last_cash_deploy_ts: Optional[dt.datetime] = None

    async def step(self) -> None:
        await self._drain_directives()
        await self._drain_trade_ideas()

        portfolio = self.ctx.services["portfolio"]
        snap = await portfolio.snapshot()

        if self._pending_intents:
            # batch the idea list so we stay within context budgets
            top = self._pending_intents[:12]
            self._pending_intents = self._pending_intents[12:]

            prompt = self._build_prompt(snap, top)
            model = self._resolve_model(self.cfg.model)
            content = "{}"
            llm_error: Optional[str] = None
            try:
                resp = await self.ctx.llm.chat(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You are the CEO/CIO of a multi-strategy trading boutique. Be decisive and risk-aware. Output ONLY JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    format=CEO_SELECT_SCHEMA,
                )
                content = (resp.get("message", {}) or {}).get("content", "{}")
            except (LLMTimeoutError, LLMServiceError) as e:
                llm_error = str(e)
                await self.ctx.bus.publish(
                    "ops",
                    self.agent_id,
                    f"CEO trade-selection LLM unavailable; using deterministic fallback: {e}",
                    meta={"type": "ceo_trade_selection_fallback"},
                )

            try:
                data = json.loads(content)
            except Exception:
                data = {"analysis": "", "selected": []}
            if llm_error:
                data = {
                    "analysis": f"LLM unavailable ({llm_error}); fallback selected by confidence.",
                    "selected": [],
                }

            analysis = str(data.get("analysis") or "")
            self._last_decision_analysis = analysis

            # Full LLM trace (not truncated) for transparency
            await self.ctx.bus.publish("llm_trace", self.agent_id, "CEO_SELECT_TRADES", meta={
                "prompt": prompt,
                "raw_output": content,
                "analysis": analysis,
                "ideas": top,
            })

            selected_intents = self._normalize_selected_intents(data, top)
            selected_intents = self._coerce_to_tradeable_venues(selected_intents, snap)
            for intent in selected_intents:
                await self.ctx.bus.publish("risk", self.agent_id, "NEW_INTENT", meta={"intent": intent.model_dump(), "ceo_analysis": analysis})

        await self._maybe_deploy_cash(snap)
        await self._maybe_send_scheduled_reports(snap)
        await self.log_state({
            "state": "running",
            "pending_ideas": len(self._pending_intents),
            "risk_posture": self.risk_posture,
            "last_decision_analysis": self._last_decision_analysis,
            "last_report_date": self._last_report_date,
            "report_version": self._report_version,
        })

    async def _drain_trade_ideas(self) -> None:
        drained = 0
        while True:
            try:
                msg = self._ideas_q.get_nowait()
            except Exception:
                break
            drained += 1
            meta = msg.get("meta", {})
            intent = meta.get("intent")
            if intent:
                self._pending_intents.append(intent)
        if drained:
            await self.ctx.bus.publish("ceo", self.agent_id, f"Ingested {drained} new trade ideas.")

    async def _drain_directives(self) -> None:
        while True:
            try:
                msg = self._directive_q.get_nowait()
            except Exception:
                break
            text = msg.get("message", "")
            self.risk_posture["notes"] = text
            await self.ctx.bus.publish("ceo", self.agent_id, f"Updated risk posture: {text}")

    def _build_prompt(self, snap, ideas: List[Dict[str, Any]]) -> str:
        return (
            f"Risk posture: {json.dumps(self.risk_posture)}\n\n"
            f"Portfolio snapshot: NAV={snap.nav:.2f}, gross={snap.gross_exposure:.2f}, net={snap.net_exposure:.2f}, lev={snap.leverage:.2f}, dd={snap.drawdown:.2%}\n"
            f"Positions: {json.dumps(snap.positions)}\n\n"
            "Task: Select up to 2 best opportunities from the following trade intents.\n"
            "Explain your decision in the JSON field 'analysis', then output the selected TradeIntent objects in 'selected'.\n\n"
            f"IDEAS: {json.dumps(ideas)}"
        )

    def _resolve_model(self, key_or_name: str) -> str:
        models = self.ctx.settings.llm.get("models", {})
        return models.get(key_or_name, key_or_name)

    def _normalize_selected_intents(self, llm_data: Dict[str, Any], proposed_ideas: List[Dict[str, Any]]) -> List[TradeIntent]:
        out: List[TradeIntent] = []
        for raw in (llm_data.get("selected") or [])[:2]:
            try:
                out.append(TradeIntent(**raw))
            except Exception:
                continue

        if out:
            return out

        # Deterministic fallback so trading still proceeds if LLM output is empty/invalid.
        ranked = sorted(
            [i for i in proposed_ideas if isinstance(i, dict)],
            key=lambda i: float(i.get("confidence") or 0.0),
            reverse=True,
        )
        for raw in ranked[:2]:
            try:
                out.append(TradeIntent(**raw))
            except Exception:
                continue
        return out

    def _priced_universe(self, snap) -> List[Tuple[str, str, float]]:
        out: List[Tuple[str, str, float]] = []
        by_symbol: Dict[str, Dict[str, Any]] = {p.get("symbol"): p for p in (snap.positions or [])}
        for k, v in (snap.last_prices or {}).items():
            if "@" not in k:
                continue
            sym, venue = k.split("@", 1)
            try:
                px = float((v or {}).get("last"))
            except Exception:
                continue
            if px <= 0:
                continue
            out.append((sym, venue, px))

        # prefer instruments already in universe / positions with sane prices
        out.sort(key=lambda t: abs(float((by_symbol.get(t[0]) or {}).get("qty") or 0.0)), reverse=True)
        return out

    def _coerce_to_tradeable_venues(self, intents: List[TradeIntent], snap) -> List[TradeIntent]:
        available_venues: Dict[str, List[str]] = {}
        for k in (snap.last_prices or {}).keys():
            if "@" not in k:
                continue
            sym, ven = k.split("@", 1)
            available_venues.setdefault(sym.upper(), []).append(ven.upper())

        out: List[TradeIntent] = []
        for it in intents:
            sym = it.symbol.upper()
            venues = available_venues.get(sym, [])
            if not venues:
                continue
            if it.venue.upper() not in venues:
                it.venue = venues[0]
            out.append(it)
        return out

    async def _maybe_deploy_cash(self, snap) -> None:
        # Founder objective: actively minimize idle cash while respecting risk/execution controls.
        base_ccy = self.ctx.settings.firm.get("base_ccy", "USD")
        cash = float((snap.cash or {}).get(base_ccy, 0.0))
        nav = float(snap.nav or 0.0)
        if nav <= 0:
            return

        target_cash_pct = float(self.ctx.settings.firm.get("target_cash_pct", 0.05))
        deploy_step_pct = float(self.ctx.settings.firm.get("cash_deploy_step_pct", 0.20))
        min_ticket = float(self.ctx.settings.firm.get("min_cash_deploy_ticket", 2500.0))
        cooldown_seconds = int(self.ctx.settings.firm.get("cash_deploy_cooldown_seconds", 120))

        excess_cash = cash - (nav * target_cash_pct)
        if excess_cash <= min_ticket:
            return

        now = dt.datetime.now(dt.timezone.utc)
        if self._last_cash_deploy_ts is not None:
            if (now - self._last_cash_deploy_ts).total_seconds() < cooldown_seconds:
                return

        priced = self._priced_universe(snap)
        if not priced:
            return

        deploy_budget = max(min_ticket, excess_cash * deploy_step_pct)
        deploy_budget = min(deploy_budget, excess_cash)

        # Split deployment across up to 2 priced instruments for diversification.
        picks = priced[:2]
        per_leg = deploy_budget / max(1, len(picks))
        sent = 0
        for sym, venue, px in picks:
            qty = per_leg / px
            if qty <= 0:
                continue
            intent = TradeIntent(
                symbol=sym,
                venue=venue,
                side="buy",
                qty=float(qty),
                order_type="market",
                time_horizon_minutes=240,
                confidence=0.55,
                thesis=(
                    f"Cash deployment policy: reduce idle {base_ccy} from {cash:.2f} "
                    f"toward target {target_cash_pct:.1%} of NAV while staying within CRO limits."
                ),
                risk_notes="Systematic treasury deployment; CRO can resize or block.",
                tags=["ceo", "treasury", "cash_deployment"],
            )
            await self.ctx.bus.publish("risk", self.agent_id, "NEW_INTENT", meta={"intent": intent.model_dump(), "policy": "cash_deployment"})
            sent += 1

        if sent:
            self._last_cash_deploy_ts = now
            await self.ctx.bus.publish("ceo", self.agent_id, f"Cash deployment submitted: {sent} intents for ~{deploy_budget:.2f} {base_ccy}.")

    async def _maybe_send_scheduled_reports(self, snap) -> None:
        tz = self.ctx.settings.firm.get("timezone", "Europe/London")
        now = dt.datetime.now(ZoneInfo(tz))
        today = now.date().isoformat()

        run_hourly = now.minute == 0
        hourly_key = now.strftime("%Y-%m-%d %H")

        # Daily report at 00:00 London time (runs once per date).
        run_midnight = now.hour == 0 and now.minute == 0 and self._last_midnight_date != today

        if not run_hourly and not run_midnight:
            return
        if run_hourly and self._last_hourly_key == hourly_key and not run_midnight:
            return

        self._report_version += 1
        self._last_report_ts = now
        self._last_report_date = today
        self._last_hourly_key = hourly_key
        if run_midnight:
            self._last_midnight_date = today

        model = self._resolve_model(self.cfg.model)
        rows = await self.ctx.db.fetchall(
            "SELECT ts, sender, message, meta_json FROM messages WHERE channel='execution' ORDER BY id DESC LIMIT 30"
        )
        exec_lines: List[str] = []
        for r in reversed(rows):
            msg = str(r.get("message") or "")
            if msg == "FILLED":
                meta = json.loads(r.get("meta_json") or "{}")
                fill = meta.get("fill", {})
                sym = fill.get("symbol")
                side = fill.get("side")
                qty = fill.get("qty")
                price = fill.get("price")
                fees = fill.get("fees")
                exec_lines.append(f"- {sym} {side} {qty} @ {price} (fees {fees})")
        exec_block = "\n".join(exec_lines[-10:]) if exec_lines else "No fills yet."

        cadence = "daily_midnight" if run_midnight else "hourly"
        prompt = (
            "Write a CEO report for the founder in <=300 words.\n"
            "Be concrete and actionable.\n"
            f"Cadence={cadence}. Date={today}. Version={self._report_version}.\n"
            f"NAV={snap.nav:.2f}, gross={snap.gross_exposure:.2f}, net={snap.net_exposure:.2f}, lev={snap.leverage:.2f}, dd={snap.drawdown:.2%}.\n"
            f"Positions={json.dumps(snap.positions)}\n"
            f"Risk posture={json.dumps(self.risk_posture)}\n"
            f"Recent executions:\n{exec_block}\n"
            "Include: key positions, notable changes, current risks, and next focus areas."
        )
        resp = await self.ctx.llm.chat(
            model=model,
            messages=[
                {"role": "system", "content": "You are the CEO/CIO. Be concise, specific, and decision-oriented."},
                {"role": "user", "content": prompt},
            ],
        )
        text = (resp.get("message", {}) or {}).get("content", "").strip()
        ts = utcnow_iso()
        meta = {"date": today, "version": self._report_version, "cadence": cadence, "timezone": tz}
        await self.ctx.db.execute(
            "INSERT INTO ceo_reports(ts, report_text, meta_json) VALUES(?,?,?)",
            (ts, text, json.dumps(meta)),
        )
        await self.ctx.bus.publish("ceo", self.agent_id, f"CEO_REPORT[{cadence}] v{self._report_version}\n{text}", meta=meta)
        await self.ctx.bus.publish("dm:ceo:user", "ceo", text, meta={**meta, "type": "scheduled_report"})
