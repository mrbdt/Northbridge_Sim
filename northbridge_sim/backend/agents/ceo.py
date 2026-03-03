from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
import datetime as dt

from .base import BaseAgent, AgentConfig, AgentContext
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


class CEOAgent(BaseAgent):
    def __init__(self, cfg: AgentConfig, ctx: AgentContext):
        super().__init__(cfg, ctx)
        self._ideas_q = ctx.bus.subscribe("trade_ideas")
        self._directive_q = ctx.bus.subscribe("ceo_inbox")

        self._pending_intents: List[Dict[str, Any]] = []
        self.risk_posture = {"max_risk": "normal", "notes": "default"}
        self._last_daily_report_date: Optional[str] = None

        # observability
        self._last_prompt: Optional[str] = None
        self._last_llm_raw: Optional[str] = None
        self._last_selected: List[Dict[str, Any]] = []
        self._last_directive: Optional[str] = None
        self._last_step_note: str = "init"

    def _snip(self, s: Optional[str], n: int = 2000) -> str:
        if not s:
            return ""
        if len(s) <= n:
            return s
        return s[:n] + f" ...<truncated {len(s) - n} chars>"

    async def step(self) -> None:
        await self._drain_directives()
        await self._drain_trade_ideas()

        portfolio = self.ctx.services["portfolio"]
        snap = await portfolio.snapshot()

        selected_intents: List[TradeIntent] = []
        if self._pending_intents:
            top = self._pending_intents[:10]
            self._pending_intents = self._pending_intents[10:]
            prompt = self._build_prompt(snap, top)
            self._last_prompt = prompt

            model = self._resolve_model(self.cfg.model)
            resp = await self.ctx.llm.chat(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are the CEO/CIO of a multi-strategy trading boutique. "
                            "Be decisive and risk-aware. Output ONLY JSON."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                format={
                    "type": "object",
                    "properties": {"selected": {"type": "array", "items": TRADE_INTENT_SCHEMA}},
                    "required": ["selected"],
                },
            )

            content = resp.get("message", {}).get("content", "{}")
            self._last_llm_raw = content

            # Trace for dashboard: prompt + raw output snippet
            await self.ctx.bus.publish(
                "llm_trace",
                self.agent_id,
                "CEO_SELECT",
                meta={
                    "model": model,
                    "prompt_snip": self._snip(prompt, 2500),
                    "llm_raw_snip": self._snip(content, 2500),
                    "n_ideas": len(top),
                    "nav": snap.nav,
                    "leverage": snap.leverage,
                    "drawdown": snap.drawdown,
                },
            )

            try:
                data = json.loads(content)
            except Exception:
                data = {"selected": []}

            for raw in (data.get("selected") or [])[:2]:
                try:
                    intent = TradeIntent(**raw)
                except Exception:
                    continue
                selected_intents.append(intent)

                summary = f"{intent.symbol} {intent.side} qty={intent.qty} conf={intent.confidence:.2f}"
                await self.ctx.bus.publish("risk", self.agent_id, f"NEW_INTENT {summary}", meta={"intent": intent.model_dump()})

            self._last_selected = [i.model_dump() for i in selected_intents]
            if selected_intents:
                self._last_step_note = f"Forwarded {len(selected_intents)} intent(s) to CRO"
                await self.ctx.bus.publish(
                    "ceo",
                    self.agent_id,
                    self._last_step_note,
                    meta={"selected": self._last_selected},
                )
            else:
                self._last_step_note = "No intents selected this step"

        else:
            self._last_step_note = "No pending ideas"

        await self._maybe_daily_report(snap)

        await self.log_state(
            {
                "state": "running",
                "pending_ideas": len(self._pending_intents),
                "risk_posture": self.risk_posture,
                "last_step_note": self._last_step_note,
                "last_directive": self._last_directive,
                "last_selected": self._last_selected,
                "last_prompt_snip": self._snip(self._last_prompt, 1200),
                "last_llm_raw_snip": self._snip(self._last_llm_raw, 1200),
            }
        )

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
            await self.ctx.bus.publish("ceo", self.agent_id, f"Ingested {drained} new trade idea(s).")

    async def _drain_directives(self) -> None:
        while True:
            try:
                msg = self._directive_q.get_nowait()
            except Exception:
                break
            text = (msg.get("message", "") or "").strip()
            if not text:
                continue
            self._last_directive = text
            self.risk_posture["notes"] = text
            await self.ctx.bus.publish("ceo", self.agent_id, f"Updated risk posture: {text}")

    def _build_prompt(self, snap, ideas: List[Dict[str, Any]]) -> str:
        return (
            f"Risk posture: {json.dumps(self.risk_posture)}\n\n"
            f"Portfolio: NAV={snap.nav:.2f}, lev={snap.leverage:.2f}, dd={snap.drawdown:.2%}\n"
            f"Positions: {snap.positions}\n\n"
            f"Select up to 2 best opportunities from the following trade intents and output clean JSON TradeIntent objects.\n"
            f"IDEAS: {json.dumps(ideas)[:8000]}"
        )

    def _resolve_model(self, key_or_name: str) -> str:
        models = self.ctx.settings.llm.get("models", {})
        return models.get(key_or_name, key_or_name)

    async def _maybe_daily_report(self, snap) -> None:
        tz = self.ctx.settings.firm.get("timezone", "Europe/London")
        report_time = self.cfg.daily_report_time
        if not report_time:
            return
        now = dt.datetime.now(ZoneInfo(tz))
        today = now.date().isoformat()

        hh, mm = report_time.split(":")
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if now < target:
            return
        if self._last_daily_report_date == today:
            return
        self._last_daily_report_date = today

        model = self._resolve_model(self.cfg.model)
        prompt = (
            "Write today's CEO daily update (<=250 words). "
            "Include NAV, leverage, drawdown, key positions, and risk posture.\n"
            f"NAV={snap.nav:.2f}, gross={snap.gross_exposure:.2f}, net={snap.net_exposure:.2f}, "
            f"lev={snap.leverage:.2f}, dd={snap.drawdown:.2%}.\n"
            f"Positions={snap.positions}\n"
            f"Risk posture={json.dumps(self.risk_posture)}\n"
        )
        resp = await self.ctx.llm.chat(
            model=model,
            messages=[
                {"role": "system", "content": "You are the CEO/CIO. Be concrete. No fluff."},
                {"role": "user", "content": prompt},
            ],
        )
        text = (resp.get("message", {}) or {}).get("content", "").strip()
        ts = utcnow_iso()
        await self.ctx.db.execute("INSERT INTO ceo_reports(ts, report_text, meta_json) VALUES(?,?,?)", (ts, text, "{}"))

        await self.ctx.bus.publish(
            "llm_trace",
            self.agent_id,
            "CEO_DAILY_REPORT",
            meta={
                "model": model,
                "prompt_snip": self._snip(prompt, 2500),
                "llm_raw_snip": self._snip(text, 2500),
            },
        )
        await self.ctx.bus.publish("ceo", self.agent_id, f"DAILY_REPORT\n{text}")
