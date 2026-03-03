from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..bus import MessageBus
from ..db import Database
from ..llm import OllamaLLM
from ..utils import utcnow_iso

@dataclass
class AgentConfig:
    id: str
    name: str
    title: str
    role: str
    status: str
    model: str
    heartbeat_seconds: int
    daily_report_time: Optional[str]
    permissions: Dict[str, Any]

class AgentContext:
    def __init__(self, settings, db: Database, bus: MessageBus, llm: OllamaLLM, services: Dict[str, Any]):
        self.settings = settings
        self.db = db
        self.bus = bus
        self.llm = llm
        self.services = services

class BaseAgent:
    def __init__(self, cfg: AgentConfig, ctx: AgentContext):
        self.cfg = cfg
        self.ctx = ctx
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    @property
    def agent_id(self) -> str:
        return self.cfg.id

    async def start(self) -> None:
        self._task = asyncio.create_task(self.run_loop(), name=f"agent:{self.agent_id}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def log_state(self, state: Dict[str, Any]) -> None:
        ts = utcnow_iso()
        await self.ctx.db.execute(
            "INSERT OR REPLACE INTO agent_state(agent_id, ts, state_json) VALUES(?,?,?)",
            (self.agent_id, ts, json.dumps(state)),
        )

    async def run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.step()
            except Exception as e:
                await self.ctx.bus.publish("ops", self.agent_id, f"Agent error: {e}", meta={"role": self.cfg.role})
            await asyncio.sleep(self.cfg.heartbeat_seconds)

    async def step(self) -> None:
        raise NotImplementedError
