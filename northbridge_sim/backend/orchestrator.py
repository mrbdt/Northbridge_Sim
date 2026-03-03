from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import load_agents, save_agents
from .agents.base import AgentConfig, AgentContext, BaseAgent
from .agents.ceo import CEOAgent
from .agents.cro import CROAgent
from .agents.quant import QuantAgent
from .agents.macro import MacroAgent
from .agents.event import EventAgent
from .agents.crypto import CryptoAgent
from .agents.vol import VolAgent
from .agents.execution import ExecutionAgent
from .agents.infra import InfraAgent
from .agents.ops import OpsAgent
from .agents.signals import SignalsAgent
from .agents.commodities import CommoditiesAgent
from .agents.fx import FXAgent

AGENT_CLASS_BY_ROLE = {
    "portfolio_lead": CEOAgent,
    "risk": CROAgent,
    "quant": QuantAgent,
    "macro": MacroAgent,
    "event": EventAgent,
    "crypto": CryptoAgent,
    "vol": VolAgent,
    "execution": ExecutionAgent,
    "infra": InfraAgent,
    "ops": OpsAgent,
    "signals": SignalsAgent,
    "commodities": CommoditiesAgent,
    "fx": FXAgent,
}

class AgentSupervisor:
    """
    Starts/stops agents based on configs/agents.yaml.
    Supports hot reload by diffing active agents.
    """
    def __init__(self, agents_path: str, ctx: AgentContext):
        self.agents_path = agents_path
        self.ctx = ctx
        self._agents: Dict[str, BaseAgent] = {}
        self._stop = asyncio.Event()
        self._watch_task: Optional[asyncio.Task] = None
        self._mtime: float = 0.0

    async def start(self) -> None:
        await self._apply_config()
        self._watch_task = asyncio.create_task(self._watch_loop(), name="agent_config_watcher")

    async def stop(self) -> None:
        self._stop.set()
        if self._watch_task:
            await self._watch_task
        for a in list(self._agents.values()):
            await a.stop()
        self._agents.clear()

    async def _watch_loop(self) -> None:
        path = Path(self.agents_path)
        while not self._stop.is_set():
            try:
                mtime = path.stat().st_mtime
                if mtime != self._mtime:
                    await self._apply_config()
                    self._mtime = mtime
            except Exception:
                pass
            await asyncio.sleep(2)

    async def _apply_config(self) -> None:
        cfgs = load_agents(self.agents_path)
        # keep internal messaging rooms in sync with agent roster
        chat = self.ctx.services.get("chat")
        if chat:
            try:
                await chat.bootstrap([c.get("id") for c in cfgs if c.get("id")])
            except Exception:
                pass
        desired_active = {c["id"]: c for c in cfgs if c.get("status","active") == "active"}

        # stop agents not desired
        for aid in list(self._agents.keys()):
            if aid not in desired_active:
                await self.ctx.bus.publish("ops", "supervisor", f"Retiring agent {aid}")
                await self._agents[aid].stop()
                del self._agents[aid]

        # start new agents
        for aid, raw in desired_active.items():
            if aid in self._agents:
                continue
            role = raw.get("role")
            cls = AGENT_CLASS_BY_ROLE.get(role)
            if not cls:
                await self.ctx.bus.publish("ops", "supervisor", f"Unknown role '{role}' for agent {aid}; skipping.")
                continue
            hb = int(raw.get("schedule", {}).get("heartbeat_seconds", 60))
            drt = raw.get("schedule", {}).get("daily_report_time")
            agent_cfg = AgentConfig(
                id=raw["id"],
                name=raw.get("name", raw["id"]),
                title=raw.get("title", ""),
                role=role,
                status=raw.get("status", "active"),
                model=raw.get("model", "worker"),
                heartbeat_seconds=hb,
                daily_report_time=drt,
                permissions=raw.get("permissions", {}),
            )
            agent = cls(agent_cfg, self.ctx)
            self._agents[aid] = agent
            await self.ctx.bus.publish("ops", "supervisor", f"Hiring agent {aid} ({role})")
            await agent.start()

    async def set_status(self, agent_id: str, status: str) -> None:
        cfgs = load_agents(self.agents_path)
        # keep internal messaging rooms in sync with agent roster
        chat = self.ctx.services.get("chat")
        if chat:
            try:
                await chat.bootstrap([c.get("id") for c in cfgs if c.get("id")])
            except Exception:
                pass
        found = False
        for a in cfgs:
            if a.get("id") == agent_id:
                a["status"] = status
                found = True
        if not found:
            raise KeyError(f"Unknown agent id {agent_id}")
        save_agents(cfgs, self.agents_path)

    async def hire(self, agent_block: Dict[str, Any]) -> None:
        cfgs = load_agents(self.agents_path)
        # keep internal messaging rooms in sync with agent roster
        chat = self.ctx.services.get("chat")
        if chat:
            try:
                await chat.bootstrap([c.get("id") for c in cfgs if c.get("id")])
            except Exception:
                pass
        if any(a.get("id") == agent_block.get("id") for a in cfgs):
            raise ValueError("Agent id already exists")
        cfgs.append(agent_block)
        save_agents(cfgs, self.agents_path)

    def list_agents(self) -> List[Dict[str, Any]]:
        return load_agents(self.agents_path)
