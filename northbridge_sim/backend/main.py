from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .config import load_settings, load_yaml, load_agents
from .db import Database
from .redis_client import RedisClient
from .bus import MessageBus
from .llm import OllamaLLM, LLMTimeoutError, LLMServiceError
from .services.price_store import PriceStore
from .services.parquet_writer import ParquetTickWriter
from .services.market_data_crypto import CryptoDataHub
from .services.market_data_equities import AutoEquitiesPoller, YahooPoller
from .services.market_data_yahoo import YahooUniversePoller
from .services.portfolio import PortfolioService
from .services.risk import RiskLimits, RiskService
from .services.broker import FeeModel, BrokerSim
from .services.universe import UniverseService
from .services.chat import ChatService
from .services.signals_store import SignalsStore
from .services.web import WebClient
from .orchestrator import AgentSupervisor
from .utils import utcnow_iso
from .agents.base import AgentContext


MIGRATION_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS instruments (
  symbol TEXT PRIMARY KEY,
  asset_class TEXT NOT NULL,
  ccy TEXT NOT NULL,
  multiplier REAL NOT NULL DEFAULT 1.0,
  meta_json TEXT
);

CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  venue TEXT NOT NULL,
  side TEXT NOT NULL,
  qty REAL NOT NULL,
  order_type TEXT NOT NULL,
  limit_price REAL,
  status TEXT NOT NULL,
  meta_json TEXT
);

CREATE TABLE IF NOT EXISTS fills (
  fill_id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  order_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  venue TEXT NOT NULL,
  side TEXT NOT NULL,
  qty REAL NOT NULL,
  price REAL NOT NULL,
  fees REAL NOT NULL,
  meta_json TEXT
);

CREATE TABLE IF NOT EXISTS positions (
  symbol TEXT PRIMARY KEY,
  qty REAL NOT NULL,
  avg_price REAL NOT NULL,
  realized_pnl REAL NOT NULL,
  meta_json TEXT
);

CREATE TABLE IF NOT EXISTS cash (
  ccy TEXT PRIMARY KEY,
  balance REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS nav (
  ts TEXT PRIMARY KEY,
  nav REAL NOT NULL,
  gross_exposure REAL NOT NULL,
  net_exposure REAL NOT NULL,
  leverage REAL NOT NULL,
  drawdown REAL NOT NULL,
  meta_json TEXT
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  channel TEXT NOT NULL,
  sender TEXT NOT NULL,
  message TEXT NOT NULL,
  meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_channel_id ON messages(channel, id);
CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages(sender, id);

CREATE TABLE IF NOT EXISTS agent_state (
  agent_id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  state_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ceo_directives (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  directive_text TEXT NOT NULL,
  meta_json TEXT
);

CREATE TABLE IF NOT EXISTS ceo_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  report_text TEXT NOT NULL,
  meta_json TEXT
);

CREATE TABLE IF NOT EXISTS chat_rooms (
  room_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  created_by TEXT,
  created_ts TEXT,
  meta_json TEXT
);

CREATE TABLE IF NOT EXISTS chat_members (
  room_id TEXT NOT NULL,
  member_id TEXT NOT NULL,
  added_by TEXT,
  added_ts TEXT,
  meta_json TEXT,
  PRIMARY KEY(room_id, member_id)
);

CREATE TABLE IF NOT EXISTS signals_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  category TEXT NOT NULL,
  source TEXT,
  title TEXT,
  link TEXT,
  summary TEXT,
  meta_json TEXT
);

CREATE TABLE IF NOT EXISTS universe_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  symbol TEXT,
  meta_json TEXT
);
"""


async def migrate_positions_schema(db: Database) -> None:
    """Backfill legacy positions schema (avg_px/side) into avg_price/realized_pnl."""
    cols = await db.fetchall("PRAGMA table_info(positions)")
    if not cols:
        return

    names = {str(c.get("name") or "") for c in cols}
    # Already on current schema.
    if "avg_price" in names and "realized_pnl" in names:
        return

    # Legacy schema detected; rebuild table while preserving qty + average entry.
    await db.executescript(
        """
        ALTER TABLE positions RENAME TO positions_legacy;

        CREATE TABLE IF NOT EXISTS positions (
          symbol TEXT PRIMARY KEY,
          qty REAL NOT NULL,
          avg_price REAL NOT NULL,
          realized_pnl REAL NOT NULL,
          meta_json TEXT
        );

        INSERT OR REPLACE INTO positions(symbol, qty, avg_price, realized_pnl, meta_json)
        SELECT
          symbol,
          COALESCE(qty, 0.0),
          COALESCE(avg_px, 0.0),
          0.0,
          COALESCE(meta_json, '{}')
        FROM positions_legacy;

        DROP TABLE positions_legacy;
        """
    )



class DirectiveIn(BaseModel):
    text: str


class StatusIn(BaseModel):
    status: str  # active | retired


class HireIn(BaseModel):
    agent: Dict[str, Any]


class UniverseAddIn(BaseModel):
    symbol: str
    asset_class: Optional[str] = None
    ccy: Optional[str] = None
    multiplier: float = 1.0
    provider: Optional[str] = None  # alpaca|yahoo|crypto (stored in meta)
    preferred_venue: Optional[str] = None  # EQUITIES|YAHOO|BINANCE
    meta: Dict[str, Any] = {}


class CEOChatIn(BaseModel):
    text: str


class ChatCreateIn(BaseModel):
    name: str
    members: list[str]
    actor: str = "ceo"
    meta: Dict[str, Any] = {}


class ChatMemberIn(BaseModel):
    member_id: str
    actor: str = "ceo"


class ChatSendIn(BaseModel):
    sender: str
    message: str
    meta: Dict[str, Any] = {}


def build_app() -> FastAPI:
    settings = load_settings("configs/firm.yaml")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # DB + schema (auto-migrate)
        db = Database(settings.storage.get("sqlite_path", "data/firm.db"))
        await db.connect()
        await db.executescript(MIGRATION_SQL)
        await migrate_positions_schema(db)

        # Redis + bus
        rc = RedisClient(settings.redis.get("url", "redis://localhost:6379/0"))
        await rc.connect()

        bus_prefix = settings.redis.get("channels", {}).get("bus_prefix", "nb:chan:")
        bus = MessageBus(db=db, redis=rc, redis_prefix=bus_prefix)
        await bus.start()

        # LLM client
        llm = OllamaLLM(
            base_url=settings.llm.get("ollama_base_url", "http://localhost:11434"),
            max_concurrent=int(settings.llm.get("max_concurrent_generations", 2)),
            timeout_seconds=int(settings.llm.get("request_timeout_seconds", 120)),
            default_keep_alive=settings.llm.get("default_keep_alive", "30m"),
            default_options=settings.llm.get("default_options", {}),
        )

        # Services
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
        await portfolio.hydrate_risk_state()

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
            "YAHOO": 2.0,
        }
        broker = BrokerSim(db=db, price_store=price_store, fee_model=fee_model, base_slippage_bps=base_slippage_bps)

        # Universe + chat + signals
        universe = UniverseService(db=db)
        await universe.bootstrap_from_yaml("configs/universe.yaml")

        chat = ChatService(db=db, bus=bus)
        agent_ids_all = [a.get("id") for a in load_agents("configs/agents.yaml") if a.get("id")]
        await chat.bootstrap(agent_ids_all)

        signals_store = SignalsStore(db=db)
        web = WebClient(timeout_seconds=15)
        signals_cfg = load_yaml("configs/signals.yaml") or {}

        services: Dict[str, Any] = {
            "db": db,
            "redis": rc,
            "bus": bus,
            "llm": llm,
            "price_store": price_store,
            "tick_writer": tick_writer,
            "portfolio": portfolio,
            "risk": risk,
            "broker": broker,
            "universe": universe,
            "chat": chat,
            "signals_store": signals_store,
            "web": web,
            "signals_config": signals_cfg,
        }

        # Data hubs
        crypto_hub = None
        equities_poller = None
        yahoo_universe_poller = None

        if settings.data.get("crypto", {}).get("enabled", True):
            crypto_syms = list(dict.fromkeys((settings.data["crypto"].get("symbols", []) or []) + (await universe.get_symbols(provider="crypto"))))
            crypto_hub = CryptoDataHub(
                venues=settings.data["crypto"].get("venues", ["BINANCE"]),
                symbols=crypto_syms or ["BTC-USDT"],
                price_store=price_store,
                tick_writer=tick_writer,
            )
            await crypto_hub.start()
            for err in getattr(crypto_hub, "startup_errors", []) or []:
                await bus.publish("ops", "crypto", err, meta={"component": "crypto_hub"})

        if settings.data.get("equities", {}).get("enabled", True):
            provider = settings.data["equities"].get("provider", "alpaca_poll")
            interval = int(settings.data["equities"].get("poll_interval_seconds", 10))
            syms_cfg = settings.data["equities"].get("symbols", ["SPY"])
            syms_uni = await universe.get_symbols(provider="alpaca")
            syms = list(dict.fromkeys([s.strip().upper() for s in (syms_cfg or []) + (syms_uni or []) if s]))

            async def _equities_event(msg: str):
                await bus.publish("ops", "equities", msg)

            if provider == "alpaca_poll":
                equities_poller = AutoEquitiesPoller(
                    syms, interval, price_store, tick_writer, venue="EQUITIES", on_event=_equities_event
                )
            else:
                equities_poller = YahooPoller(syms, interval, price_store, tick_writer, venue="EQUITIES")

            await equities_poller.start()

        if settings.data.get("yahoo_universe", {}).get("enabled", True):
            interval = int(settings.data.get("yahoo_universe", {}).get("poll_interval_seconds", 15))
            yahoo_universe_poller = YahooUniversePoller(universe=universe, poll_interval_seconds=interval, price_store=price_store, tick_writer=tick_writer, venue="YAHOO")
            await yahoo_universe_poller.start()

        # Agents
        agent_ctx = AgentContext(settings=settings, db=db, bus=bus, llm=llm, services=services)
        supervisor = AgentSupervisor("configs/agents.yaml", agent_ctx)
        await supervisor.start()

        # Persist NAV to Redis for the dashboard
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

        # Save state
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
        app.state.universe = universe
        app.state.chat = chat
        app.state.signals_store = signals_store
        app.state.web = web
        app.state.supervisor = supervisor
        app.state.nav_task = nav_task
        app.state.crypto_hub = crypto_hub
        app.state.equities_poller = equities_poller
        app.state.yahoo_universe_poller = yahoo_universe_poller

        await bus.publish("ops", "system", "Northbridge Sim backend started.")
        try:
            yield
        finally:
            nav_task.cancel()
            await supervisor.stop()
            if equities_poller:
                await equities_poller.stop()
            if yahoo_universe_poller:
                await yahoo_universe_poller.stop()
            if crypto_hub:
                await crypto_hub.stop()
            await tick_writer.stop()
            await llm.aclose()
            await web.aclose()
            await rc.close()
            await db.close()

    app = FastAPI(title="Northbridge Sim", lifespan=lifespan)

    # ----------------- Basics -----------------
    @app.get("/api/health")
    async def health():
        return {"ok": True, "firm": settings.firm.get("name"), "base_ccy": settings.firm.get("base_ccy")}

    @app.get("/api/portfolio")
    async def get_portfolio(cached: bool = True):
        if cached:
            snap_key = app.state.settings.redis.get("channels", {}).get(
                "portfolio_snapshot", "nb:portfolio:snapshot"
            )
            try:
                raw = await app.state.redis.r.get(snap_key)
                if raw:
                    return json.loads(raw)
            except Exception:
                pass

        snap = await app.state.portfolio.snapshot()
        return json.loads(snap.model_dump_json())
    
    @app.get("/api/dashboard/home")
    async def dashboard_home():
        port = await get_portfolio(cached=True)
        uni = await app.state.universe.list()
        last = app.state.price_store.snapshot()
        return {"portfolio": port, "universe": uni, "last_prices": last}

    @app.get("/api/market/last")
    async def last_prices():
        return app.state.price_store.snapshot()

    # ----------------- Agents -----------------
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
            return {"agent_id": agent_id, "ts": None, "state": {"state": "starting"}}
        return {"agent_id": row["agent_id"], "ts": row["ts"], "state": json.loads(row["state_json"] or "{}")}

    @app.get("/api/agent/{agent_id}/llm_trace")
    async def get_agent_llm_trace(agent_id: str, limit: int = 80):
        rows = await app.state.db.fetchall(
            "SELECT id, ts, channel, sender, message, meta_json FROM messages "
            "WHERE channel='llm_trace' AND sender=? ORDER BY id DESC LIMIT ?",
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
                "meta": json.loads(r.get("meta_json") or "{}"),
            })
        return out

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
                "meta": json.loads(r.get("meta_json") or "{}"),
            })
        return out

    # ----------------- Channels -----------------
    @app.get("/api/channel/{channel}")
    async def tail_channel(channel: str, limit: int = 200):
        return await app.state.bus.tail(channel, limit=limit)

    # ----------------- CEO: directives + chat + reports -----------------
    @app.post("/api/ceo/directive")
    async def ceo_directive(inp: DirectiveIn):
        await app.state.db.execute(
            "INSERT INTO ceo_directives(ts, directive_text, meta_json) VALUES(?,?,?)",
            (utcnow_iso(), inp.text, "{}"),
        )
        await app.state.bus.publish("ceo_inbox", "user", inp.text)
        return {"ok": True}

    @app.post("/api/ceo/chat")
    async def ceo_chat(inp: CEOChatIn):
        # Store user message in DM room
        room_id = "dm:ceo:user"  # canonical sort puts ceo before user
        await app.state.bus.publish(room_id, "user", inp.text, meta={"type": "user_chat"})

        # Build a short chat context from the last N messages
        history = await app.state.bus.tail(room_id, limit=30)
        msgs = [{"role": "system", "content": "You are the CEO/CIO of a multi-strategy trading boutique. Be direct, risk-aware, and actionable."}]
        for m in history:
            role = "assistant" if m["sender"] == "ceo" else "user"
            msgs.append({"role": role, "content": m["message"]})

        # Add current portfolio snapshot
        snap = await app.state.portfolio.snapshot()
        msgs.append({"role": "user", "content": f"(Context) Portfolio: NAV={snap.nav:.2f}, lev={snap.leverage:.2f}, dd={snap.drawdown:.2%}, positions={json.dumps(snap.positions)}"})

        model = settings.llm.get("models", {}).get("big", settings.llm.get("models", {}).get("worker", ""))
        try:
            resp = await app.state.llm.chat(model=model or "worker", messages=msgs)
            reply = (resp.get("message", {}) or {}).get("content", "").strip()
            if not reply:
                reply = "I couldn't produce a complete response just now. Please retry."
            await app.state.bus.publish(room_id, "ceo", reply, meta={"type": "ceo_chat"})
            # LLM trace
            await app.state.bus.publish("llm_trace", "ceo", "CEO_CHAT", meta={"messages": msgs, "raw_output": reply})
            return {"ok": True, "reply": reply}
        except LLMTimeoutError as e:
            fallback = "I’m still processing but the model timed out. Please retry in a few moments; I’ll keep this thread context."
            await app.state.bus.publish(room_id, "ceo", fallback, meta={"type": "ceo_chat", "error": "timeout"})
            await app.state.bus.publish("ops", "ceo", f"CEO chat timeout: {e}")
            await app.state.bus.publish("llm_trace", "ceo", "CEO_CHAT_TIMEOUT", meta={"messages": msgs, "error": str(e)})
            return {"ok": True, "reply": fallback, "warning": "llm_timeout"}
        except LLMServiceError as e:
            fallback = "I hit an upstream LLM service issue. Please retry shortly."
            await app.state.bus.publish(room_id, "ceo", fallback, meta={"type": "ceo_chat", "error": "llm_service"})
            await app.state.bus.publish("ops", "ceo", f"CEO chat llm service error: {e}")
            await app.state.bus.publish("llm_trace", "ceo", "CEO_CHAT_ERROR", meta={"messages": msgs, "error": str(e)})
            return {"ok": True, "reply": fallback, "warning": "llm_service"}

    @app.get("/api/ceo/reports")
    async def ceo_reports(date: Optional[str] = None, limit: int = 50):
        if date:
            rows = await app.state.db.fetchall(
                "SELECT ts, report_text, meta_json FROM ceo_reports WHERE substr(ts,1,10)=? ORDER BY ts DESC LIMIT ?",
                (date, limit),
            )
        else:
            rows = await app.state.db.fetchall(
                "SELECT ts, report_text, meta_json FROM ceo_reports ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
        out = []
        for r in reversed(rows):
            out.append({"ts": r["ts"], "text": r["report_text"], "meta": json.loads(r.get("meta_json") or "{}")})
        return out

    @app.get("/api/ceo/reports/latest")
    async def ceo_reports_latest():
        row = await app.state.db.fetchone("SELECT ts, report_text, meta_json FROM ceo_reports ORDER BY ts DESC LIMIT 1")
        if not row:
            return None
        return {"ts": row["ts"], "text": row["report_text"], "meta": json.loads(row.get("meta_json") or "{}")}

    # ----------------- Universe -----------------
    @app.get("/api/universe")
    async def universe_list():
        return await app.state.universe.list()

    @app.post("/api/universe/add")
    async def universe_add(inp: UniverseAddIn):
        meta = dict(inp.meta or {})
        if inp.provider:
            meta["provider"] = inp.provider
        if inp.preferred_venue:
            meta["preferred_venue"] = inp.preferred_venue
        item = await app.state.universe.add(
            symbol=inp.symbol,
            asset_class=inp.asset_class,
            ccy=inp.ccy,
            multiplier=inp.multiplier,
            meta=meta,
            actor="ceo",
        )

        # If it's an alpaca equity, add to the equities poller immediately (best effort)
        try:
            provider = item.get("meta", {}).get("provider")
            if provider == "alpaca" and app.state.equities_poller and hasattr(app.state.equities_poller, "add_symbols"):
                app.state.equities_poller.add_symbols([item["symbol"]])
        except Exception:
            pass

        # For crypto provider: we store it but realtime subscription requires restart (cryptofeed limitation)
        if item.get("meta", {}).get("provider") == "crypto":
            await app.state.bus.publish("ops", "universe", f"Added {item['symbol']} (crypto). Note: crypto feed subscriptions require backend restart to take effect.")

        await app.state.bus.publish("ops", "universe", f"Universe added: {item['symbol']}", meta=item)
        return {"ok": True, "instrument": item}

    @app.get("/api/universe/events")
    async def universe_events(limit: int = 100):
        rows = await app.state.db.fetchall("SELECT ts, actor, action, symbol, meta_json FROM universe_events ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in reversed(rows):
            out.append({"ts": r["ts"], "actor": r["actor"], "action": r["action"], "symbol": r.get("symbol"), "meta": json.loads(r.get("meta_json") or "{}")})
        return out

    # ----------------- Signals -----------------
    @app.get("/api/signals")
    async def signals(limit: int = 50, category: Optional[str] = None):
        return await app.state.signals_store.recent(limit=limit, category=category)

    # ----------------- Chat -----------------
    @app.get("/api/chat/rooms")
    async def chat_rooms(member_id: Optional[str] = None):
        return await app.state.chat.list_rooms(member_id=member_id)

    @app.get("/api/chat/room/{room_id}/members")
    async def chat_room_members(room_id: str):
        return await app.state.chat.members(room_id)

    @app.post("/api/chat/room/create")
    async def chat_room_create(inp: ChatCreateIn):
        if inp.actor != "ceo":
            raise HTTPException(403, "Only CEO can create group chats in this simulation.")
        rid = await app.state.chat.create_group_room(inp.name, inp.members, created_by=inp.actor, meta=inp.meta)
        return {"ok": True, "room_id": rid}

    @app.post("/api/chat/room/{room_id}/add")
    async def chat_room_add(room_id: str, inp: ChatMemberIn):
        if inp.actor != "ceo":
            raise HTTPException(403, "Only CEO can add/remove members in this simulation.")
        await app.state.chat.add_member(room_id, inp.member_id, actor=inp.actor)
        return {"ok": True}

    @app.post("/api/chat/room/{room_id}/remove")
    async def chat_room_remove(room_id: str, inp: ChatMemberIn):
        if inp.actor != "ceo":
            raise HTTPException(403, "Only CEO can add/remove members in this simulation.")
        await app.state.chat.remove_member(room_id, inp.member_id, actor=inp.actor)
        return {"ok": True}

    @app.post("/api/chat/room/{room_id}/send")
    async def chat_room_send(room_id: str, inp: ChatSendIn):
        await app.state.chat.send(room_id, sender=inp.sender, message=inp.message, meta=inp.meta)
        return {"ok": True}

    @app.get("/api/chat/room/{room_id}/tail")
    async def chat_room_tail(room_id: str, limit: int = 200):
        return await app.state.chat.tail(room_id, limit=limit)

    # ----------------- Admin -----------------
    @app.post("/api/admin/agent/{agent_id}/status")
    async def set_agent_status(agent_id: str, inp: StatusIn):
        if inp.status not in ("active", "retired"):
            raise HTTPException(400, "status must be active|retired")
        await app.state.supervisor.set_status(agent_id, inp.status)
        return {"ok": True, "agent_id": agent_id, "status": inp.status}

    @app.post("/api/admin/agent/hire")
    async def hire_agent(inp: HireIn):
        await app.state.supervisor.hire(inp.agent)
        return {"ok": True}

    return app


app = build_app()
