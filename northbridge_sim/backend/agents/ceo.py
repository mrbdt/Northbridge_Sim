from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
import datetime as dt

from .base import BaseAgent
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
        self._last_decision_analysis: str = ""

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
            resp = await self.ctx.llm.chat(
                model=model,
                messages=[
                    {"role": "system", "content": "You are the CEO/CIO of a multi-strategy trading boutique. Be decisive and risk-aware. Output ONLY JSON."},
                    {"role": "user", "content": prompt},
                ],
                format=CEO_SELECT_SCHEMA,
            )
            content = (resp.get("message", {}) or {}).get("content", "{}")
            try:
                data = json.loads(content)
            except Exception:
                data = {"analysis": "", "selected": []}

            analysis = str(data.get("analysis") or "")
            self._last_decision_analysis = analysis

            # Full LLM trace (not truncated) for transparency
            await self.ctx.bus.publish("llm_trace", self.agent_id, "CEO_SELECT_TRADES", meta={
                "prompt": prompt,
                "raw_output": content,
                "analysis": analysis,
                "ideas": top,
            })

            for raw in (data.get("selected") or [])[:2]:
                try:
                    intent = TradeIntent(**raw)
                except Exception:
                    continue
                await self.ctx.bus.publish("risk", self.agent_id, "NEW_INTENT", meta={"intent": intent.model_dump(), "ceo_analysis": analysis})

        await self._maybe_update_rolling_report(snap)
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

    async def _maybe_update_rolling_report(self, snap) -> None:
        # Update a rolling CEO report throughout the day (not just once).
        tz = self.ctx.settings.firm.get("timezone", "Europe/London")
        now = dt.datetime.now(ZoneInfo(tz))
        today = now.date().isoformat()

        update_minutes = int(self.ctx.settings.firm.get("ceo_report_update_minutes", 30))
        if update_minutes <= 0:
            return

        if self._last_report_date != today:
            self._last_report_date = today
            self._report_version = 0
            self._last_report_ts = None

        if self._last_report_ts is not None:
            delta = now - self._last_report_ts
            if delta.total_seconds() < update_minutes * 60:
                return

        self._report_version += 1
        self._last_report_ts = now

        model = self._resolve_model(self.cfg.model)
        # Include recent executions (last ~20)
        rows = await self.ctx.db.fetchall(
            "SELECT ts, sender, message, meta_json FROM messages WHERE channel='execution' ORDER BY id DESC LIMIT 30"
        )
        # keep it concise
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

        prompt = (
            "Write a rolling CEO report (<=300 words).\n"
            "Be concrete and actionable.\n"
            f"Date={today} Version={self._report_version}.\n"
            f"NAV={snap.nav:.2f}, gross={snap.gross_exposure:.2f}, net={snap.net_exposure:.2f}, lev={snap.leverage:.2f}, dd={snap.drawdown:.2%}.\n"
            f"Positions={json.dumps(snap.positions)}\n"
            f"Risk posture={json.dumps(self.risk_posture)}\n"
            f"Recent executions:\n{exec_block}\n"
            "Include: key positions, notable changes since prior report, current risk posture, and next focus areas."
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
        meta = {"date": today, "version": self._report_version}
        await self.ctx.db.execute(
            "INSERT INTO ceo_reports(ts, report_text, meta_json) VALUES(?,?,?)",
            (ts, text, json.dumps(meta)),
        )
        await self.ctx.bus.publish("ceo", self.agent_id, f"DAILY_REPORT_UPDATE v{self._report_version}\n{text}", meta=meta)
