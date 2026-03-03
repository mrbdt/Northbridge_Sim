from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .config import load_settings, load_yaml
from .db import Database
from .redis_client import RedisClient
from .bus import MessageBus
from .llm import OllamaLLM
from .services.price_store import PriceStore
from .services.parquet_writer import ParquetTickWriter
from .services.market_data_crypto import CryptoDataHub
from .services.market_data_equities import AutoEquitiesPoller, YahooPoller
from .services.portfolio import PortfolioService
from .services.risk import RiskLimits, RiskService
from .services.broker import FeeModel, BrokerSim
from .orchestrator import AgentSupervisor
from .utils import utcnow_iso
from .agents.base import AgentContext

class DirectiveIn(BaseModel):
    text: str

class StatusIn(BaseModel):
    status: str  # active | retired

class HireIn(BaseModel):
    agent: Dict[str, Any]

def build_app() -> FastAPI:
    settings = load_settings("configs/firm.yaml")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.storage.get("sqlite_path", "data/firm.db"))
        await db.connect()

        rc = RedisClient(settings.redis.get("url", "redis://localhost:6379/0"))
        await rc.connect()

        bus_prefix = settings.redis.get("channels", {}).get("bus_prefix", "nb:chan:")
        bus = MessageBus(db=db, redis=rc, redis_prefix=bus_prefix)

        llm = OllamaLLM(
            base_url=settings.llm.get("ollama_base_url", "http://localhost:11434"),
            max_concurrent=int(settings.llm.get("max_concurrent_generations", 2)),
            timeout_seconds=int(settings.llm.get("request_timeout_seconds", 120)),
            default_keep_alive=settings.llm.get("default_keep_alive", "30m"),
            default_options=settings.llm.get("default_options", {}),
        )

        last_price_hash = settings.redis.get("channels", {}).get("last_price_hash", "nb:last_price")
        price_store = PriceStore(redis=rc, redis_hash=last_price_hash)

        tick_writer = ParquetTickWriter(settings.storage.get("parquet_root", "data/parquet"))
        await tick_writer.start()

        portfolio = PortfolioService(
            db=db,
            price_store=price_store,
            base_ccy=settings.firm.get("base_ccy", "USD"),
            initial_cash=float(settings.firm.get("initial_cash", 1_000_000)),
        )
        await portfolio.init_if_empty()

        risk = RiskService(RiskLimits(
            max_gross_leverage=float(settings.risk.get("max_gross_leverage", 3.0)),
            max_net_leverage=float(settings.risk.get("max_net_leverage", 1.0)),
            max_position_pct_nav=float(settings.risk.get("max_position_pct_nav", 0.2)),
            max_daily_loss_pct=float(settings.risk.get("max_daily_loss_pct", 0.02)),
            max_drawdown_pct=float(settings.risk.get("max_drawdown_pct", 0.10)),
        ))

        fees_cfg = load_yaml("configs/fees.yaml")
        fee_model = FeeModel(fees_cfg)
        base_slippage_bps = {
            "BINANCE": float(settings.execution.get("slippage_model", {}).get("base_bps_crypto", 2.0)),
            "COINBASE": float(settings.execution.get("slippage_model", {}).get("base_bps_crypto", 2.0)),
            "EQUITIES": float(settings.execution.get("slippage_model", {}).get("base_bps_equities", 3.0)),
        }
        broker = BrokerSim(db=db, price_store=price_store, fee_model=fee_model, base_slippage_bps=base_slippage_bps)

        services = {
            "db": db,
            "redis": rc,
            "bus": bus,
            "llm": llm,
            "price_store": price_store,
            "tick_writer": tick_writer,
            "portfolio": portfolio,
            "risk": risk,
            "broker": broker,
        }

        # Data
        crypto_hub = None
        equities_poller = None

        if settings.data.get("crypto", {}).get("enabled", True):
            crypto_hub = CryptoDataHub(
                venues=settings.data["crypto"].get("venues", ["BINANCE"]),
                symbols=settings.data["crypto"].get("symbols", ["BTC-USDT"]),
                price_store=price_store,
                tick_writer=tick_writer,
            )
            await crypto_hub.start()

        if settings.data.get("equities", {}).get("enabled", True):
            provider = settings.data["equities"].get("provider", "alpaca_poll")
            interval = int(settings.data["equities"].get("poll_interval_seconds", 10))
            syms = settings.data["equities"].get("symbols", ["SPY"])

            async def _equities_event(msg: str):
                await bus.publish("ops", "equities", msg)

            if provider == "alpaca_poll":
                equities_poller = AutoEquitiesPoller(
                    syms, interval, price_store, tick_writer, venue="EQUITIES", on_event=_equities_event
                )
            else:
                equities_poller = YahooPoller(syms, interval, price_store, tick_writer, venue="EQUITIES")

            await equities_poller.start()


        # Agents
        agent_ctx = AgentContext(settings=settings, db=db, bus=bus, llm=llm, services=services)
        supervisor = AgentSupervisor("configs/agents.yaml", agent_ctx)
        await supervisor.start()

        async def nav_loop():
            snap_key = settings.redis.get("channels", {}).get("portfolio_snapshot", "nb:portfolio:snapshot")
            while True:
                snap = await portfolio.snapshot()
                await portfolio.persist_nav(snap)
                try:
                    await rc.r.set(snap_key, snap.model_dump_json())
                except Exception:
                    pass
                await asyncio.sleep(5)

        nav_task = asyncio.create_task(nav_loop(), name="nav_loop")

        app.state.settings = settings
        app.state.db = db
        app.state.redis = rc
        app.state.bus = bus
        app.state.llm = llm
        app.state.price_store = price_store
        app.state.tick_writer = tick_writer
        app.state.portfolio = portfolio
        app.state.risk = risk
        app.state.broker = broker
        app.state.supervisor = supervisor
        app.state.nav_task = nav_task

        await bus.publish("ops", "system", "Northbridge Sim backend started.")
        try:
            yield
        finally:
            nav_task.cancel()
            await supervisor.stop()
            if equities_poller:
                await equities_poller.stop()
            if crypto_hub:
                await crypto_hub.stop()
            await tick_writer.stop()
            await llm.aclose()
            await rc.close()
            await db.close()

    app = FastAPI(title="Northbridge Sim", lifespan=lifespan)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "firm": settings.firm.get("name"), "base_ccy": settings.firm.get("base_ccy")}

    @app.get("/api/portfolio")
    async def get_portfolio():
        snap = await app.state.portfolio.snapshot()
        return json.loads(snap.model_dump_json())

    @app.get("/api/agents")
    async def get_agents():
        agents_cfg = app.state.supervisor.list_agents()
        rows = await app.state.db.fetchall("SELECT agent_id, ts, state_json FROM agent_state")
        state_by_id = {r["agent_id"]: {"ts": r["ts"], "state": json.loads(r["state_json"])} for r in rows}
        for a in agents_cfg:
            a["runtime_state"] = state_by_id.get(a["id"])
        return agents_cfg


    @app.get("/api/agent/{agent_id}/state")
    async def get_agent_state(agent_id: str):
        row = await app.state.db.fetchone("SELECT agent_id, ts, state_json FROM agent_state WHERE agent_id=?", (agent_id,))
        if not row:
            raise HTTPException(404, "No state found for agent")
        return {"agent_id": row["agent_id"], "ts": row["ts"], "state": json.loads(row["state_json"])}

    @app.get("/api/agent/{agent_id}/messages")
    async def get_agent_messages(agent_id: str, limit: int = 200):
        rows = await app.state.db.fetchall(
            "SELECT id, ts, channel, sender, message, meta_json FROM messages WHERE sender=? ORDER BY id DESC LIMIT ?",
            (agent_id, limit),
        )
        out = []
        for r in reversed(rows):
            out.append({
                "id": r["id"],
                "ts": r["ts"],
                "channel": r["channel"],
                "sender": r["sender"],
                "message": r["message"],
                "meta": json.loads(r["meta_json"] or "{}"),
            })
        return out

    @app.get("/api/channel/{channel}")
    async def tail_channel(channel: str, limit: int = 200):
        return await app.state.bus.tail(channel, limit=limit)

    @app.post("/api/ceo/directive")
    async def ceo_directive(inp: DirectiveIn):
        await app.state.db.execute("INSERT INTO ceo_directives(ts, directive_text, meta_json) VALUES(?,?,?)", (utcnow_iso(), inp.text, "{}"))
        await app.state.bus.publish("ceo_inbox", "user", inp.text)
        return {"ok": True}

    @app.post("/api/admin/agent/{agent_id}/status")
    async def set_agent_status(agent_id: str, inp: StatusIn):
        if inp.status not in ("active","retired"):
            raise HTTPException(400, "status must be active|retired")
        await app.state.supervisor.set_status(agent_id, inp.status)
        return {"ok": True, "agent_id": agent_id, "status": inp.status}

    @app.post("/api/admin/agent/hire")
    async def hire_agent(inp: HireIn):
        await app.state.supervisor.hire(inp.agent)
        return {"ok": True}

    @app.get("/api/market/last")
    async def last_prices():
        return app.state.price_store.snapshot()

    return app

app = build_app()
