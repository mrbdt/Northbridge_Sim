import json
import os
from datetime import date
from typing import Any, Dict, List, Optional

import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

BACKEND_DEFAULT = os.environ.get("NB_BACKEND_URL", "http://localhost:8000")


def _load_css():
    css_file = os.path.join(os.path.dirname(__file__), "bloomberg.css")
    try:
        with open(css_file, "r", encoding="utf-8") as f:
            st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)
    except Exception:
        pass


def api_get(base: str, path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 10):
    r = requests.get(f"{base}{path}", params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def api_post(base: str, path: str, payload: Dict[str, Any], timeout: int = 30):
    r = requests.post(f"{base}{path}", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


st.set_page_config(page_title="Northbridge // Sim", layout="wide")
_load_css()

# Sidebar controls (helps with restarts; stop auto-refresh before restarting backend)
st.sidebar.markdown("### NORTHBRIDGE // SIM")
backend = st.sidebar.text_input("Backend URL", value=BACKEND_DEFAULT)
auto = st.sidebar.checkbox("Auto refresh", value=True)
interval_ms = int(st.sidebar.slider("Refresh interval (ms)", min_value=1000, max_value=10000, value=2500, step=500))
if auto:
    st_autorefresh(interval=interval_ms, key="refresh")

st.markdown("## NORTHBRIDGE  ⟂  MULTI‑STRATEGY  ⟂  SIMULATED TERMINAL")

# Try a quick health check
try:
    health = api_get(backend, "/api/health", timeout=5)
except Exception as e:
    st.error(f"Backend not reachable at {backend}: {e}")
    st.stop()

tabs = st.tabs(["Home", "Agent Detail", "Channels", "CEO Chat", "Internal Messaging Platform"])

# ---------------- Home ----------------
with tabs[0]:
    port = api_get(backend, "/api/portfolio")
    universe = api_get(backend, "/api/universe")
    last = api_get(backend, "/api/market/last")
    agents = api_get(backend, "/api/agents")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("NAV (AUM)", f"{port['nav']:.2f}")
    c2.metric("Gross", f"{port['gross_exposure']:.2f}")
    c3.metric("Net", f"{port['net_exposure']:.2f}")
    c4.metric("Leverage", f"{port['leverage']:.2f}")
    c5.metric("Drawdown", f"{port['drawdown']:.2%}")

    st.markdown("### Portfolio Positions")
    st.dataframe(port["positions"], use_container_width=True, height=260)

    st.markdown("### Current Prices (Universe)")
    rows = []
    for ins in universe:
        sym = ins["symbol"]
        meta = ins.get("meta") or {}
        pref = (meta.get("preferred_venue") or "").upper()
        key = f"{sym.upper()}@{pref}" if pref else None

        px = None
        if key and key in last:
            px = last[key]
            key_used = key
        else:
            # fallback: any venue
            key_used = None
            for k, v in last.items():
                if k.startswith(sym.upper() + "@"):
                    px = v
                    key_used = k
                    break

        rows.append({
            "symbol": sym,
            "asset_class": ins.get("asset_class"),
            "provider": meta.get("provider"),
            "preferred_venue": meta.get("preferred_venue"),
            "key": key_used,
            "last": (px or {}).get("last") if px else None,
            "ts": (px or {}).get("ts") if px else None,
        })
    st.dataframe(rows, use_container_width=True, height=300)

    st.markdown("### Agents Overview")
    agent_rows = []
    for a in agents:
        rs = a.get("runtime_state") or {}
        state = (rs.get("state") or {})
        thinking = state.get("thinking") or state.get("last_decision_analysis") or ""
        agent_rows.append({
            "id": a.get("id"),
            "role": a.get("role"),
            "status": a.get("status"),
            "model": a.get("model"),
            "hb": (a.get("schedule") or {}).get("heartbeat_seconds"),
            "ts": rs.get("ts"),
            "state": state.get("state") if isinstance(state, dict) else None,
            "thinking_snippet": str(thinking)[:200],
        })
    st.dataframe(agent_rows, use_container_width=True, height=320)

    st.markdown("### Signals (Web Intel)")
    try:
        sigs = api_get(backend, "/api/signals", params={"limit": 20})
        for s in sigs[-10:]:
            st.write(f"[{s['ts']}] **{s.get('category','')}** — {s.get('title','')}")
            if s.get("link"):
                st.caption(s["link"])
            if s.get("summary"):
                with st.expander("summary / meta", expanded=False):
                    st.write(s["summary"])
                    st.json(s.get("meta") or {})
    except Exception as e:
        st.info(f"No signals yet (or backend not configured): {e}")

# ---------------- Agent Detail ----------------
with tabs[1]:
    agents = api_get(backend, "/api/agents")
    agent_ids = [a["id"] for a in agents]
    sel = st.selectbox("Select agent", agent_ids, index=0 if agent_ids else None)
    if sel:
        st.markdown(f"### Agent: `{sel}`")

        # State (full, not truncated)
        try:
            st_state = api_get(backend, f"/api/agent/{sel}/state")
            st.markdown("#### Runtime State (full)")
            st.json(st_state)
            thinking = (st_state.get("state") or {}).get("thinking") or (st_state.get("state") or {}).get("last_decision_analysis")
            if thinking:
                st.markdown("#### Thinking / Rationale (full)")
                st.code(str(thinking), language="text")
        except Exception as e:
            st.error(f"Failed to load agent state: {e}")

        cols = st.columns(2)
        with cols[0]:
            st.markdown("#### Messages sent by agent")
            try:
                msgs = api_get(backend, f"/api/agent/{sel}/messages", params={"limit": 200})
                for m in msgs[-50:]:
                    st.write(f"[{m['ts']}] **{m['channel']}** — {m['message']}")
                    if m.get("meta"):
                        with st.expander("meta", expanded=False):
                            st.json(m["meta"])
            except Exception as e:
                st.info(f"No messages: {e}")

        with cols[1]:
            st.markdown("#### LLM Traces (filtered)")
            try:
                traces = api_get(backend, "/api/channel/llm_trace", params={"limit": 200})
                traces = [t for t in traces if t.get("sender") == sel]
                if not traces:
                    st.caption("No llm_trace messages for this agent yet.")
                for t in traces[-20:]:
                    st.write(f"[{t['ts']}] **{t['sender']}** — {t['message']}")
                    if t.get("meta"):
                        with st.expander("trace meta (prompt / raw)", expanded=False):
                            st.json(t["meta"])
            except Exception as e:
                st.info(f"No llm_trace channel (or backend not ready): {e}")

# ---------------- Channels ----------------
with tabs[2]:
    st.markdown("### Channels")
    base_channels = ["trade_ideas", "risk", "execution", "ops", "ceo", "signals", "llm_trace"]
    try:
        rooms = api_get(backend, "/api/chat/rooms")
        room_ids = [r["room_id"] for r in rooms]
    except Exception:
        room_ids = []
    channels = base_channels + sorted(room_ids)

    chan = st.selectbox("Select channel / room", channels, index=channels.index("ops") if "ops" in channels else 0)
    limit = st.slider("Message limit", min_value=50, max_value=500, value=200, step=50)

    # Use chat endpoint for rooms for consistent ordering, else use channel endpoint
    try:
        if chan.startswith("dm:") or chan.startswith("room:"):
            msgs = api_get(backend, f"/api/chat/room/{chan}/tail", params={"limit": limit})
        else:
            msgs = api_get(backend, f"/api/channel/{chan}", params={"limit": limit})
    except Exception as e:
        st.error(f"Failed to load channel: {e}")
        msgs = []

    for m in msgs[-100:]:
        st.write(f"[{m['ts']}] **{m['sender']}**: {m['message']}")
        if m.get("meta"):
            with st.expander("meta", expanded=False):
                st.json(m["meta"])

# ---------------- CEO Chat ----------------
with tabs[3]:
    st.markdown("### CEO Chat")
    st.caption("Chat is stored in DM room `dm:ceo:user`. Directives still go to `ceo_inbox`.")

    # Conversation history
    try:
        convo = api_get(backend, "/api/chat/room/dm:ceo:user/tail", params={"limit": 200})
    except Exception:
        convo = []
    for m in convo[-60:]:
        who = m.get("sender")
        st.write(f"[{m['ts']}] **{who}**: {m['message']}")

    with st.form("ceo_chat_form", clear_on_submit=True):
        txt = st.text_area("Message", height=90, placeholder="Ask the CEO for an update, change risk posture, or ask about current positioning…")
        submitted = st.form_submit_button("Send to CEO")
        if submitted and txt.strip():
            try:
                out = api_post(backend, "/api/ceo/chat", {"text": txt.strip()})
                st.success("CEO replied (see thread above).")
            except Exception as e:
                st.error(f"Failed to chat: {e}")

    st.markdown("---")
    st.markdown("### CEO Directive (risk posture)")
    with st.form("ceo_directive_form", clear_on_submit=True):
        directive = st.text_area("Directive text", height=80, placeholder="e.g., 'Lower risk tolerance today; cap leverage at 1.0; avoid crypto shorts.'")
        sent = st.form_submit_button("Send directive")
        if sent and directive.strip():
            try:
                api_post(backend, "/api/ceo/directive", {"text": directive.strip()})
                st.success("Directive sent.")
            except Exception as e:
                st.error(f"Failed: {e}")

    st.markdown("---")
    st.markdown("### Universe Management (as CEO)")
    with st.form("universe_add_form", clear_on_submit=True):
        sym = st.text_input("Symbol", placeholder="Examples: MSFT, GC=F, EURUSD=X, BTC-USDT, ^GSPC")
        asset_class = st.selectbox("Asset class", ["", "equity", "crypto", "commodity", "fx", "rates", "index", "unknown"], index=0)
        provider = st.selectbox("Provider", ["", "alpaca", "yahoo", "crypto"], index=0)
        venue = st.selectbox("Preferred venue", ["", "EQUITIES", "YAHOO", "BINANCE"], index=0)
        mult = st.number_input("Multiplier", value=1.0, step=0.5)
        add = st.form_submit_button("Add instrument")
        if add and sym.strip():
            payload = {
                "symbol": sym.strip(),
                "asset_class": asset_class or None,
                "multiplier": float(mult),
                "provider": provider or None,
                "preferred_venue": venue or None,
                "meta": {},
            }
            try:
                api_post(backend, "/api/universe/add", payload)
                st.success(f"Added {sym}. (Prices will appear when the relevant data feed supports it.)")
            except Exception as e:
                st.error(f"Failed to add: {e}")

    st.markdown("---")
    st.markdown("### CEO Rolling Reports")
    try:
        latest = api_get(backend, "/api/ceo/reports/latest")
        if latest:
            st.markdown(f"**Latest report** ({latest['ts']})")
            st.code(latest["text"], language="text")
    except Exception:
        pass

    sel_date = st.date_input("View reports for date", value=date.today())
    try:
        reports = api_get(backend, "/api/ceo/reports", params={"date": sel_date.isoformat(), "limit": 200})
        if reports:
            for r in reports[-10:]:
                st.markdown(f"**{r['ts']}**  (v{(r.get('meta') or {}).get('version')})")
                with st.expander("report text", expanded=False):
                    st.code(r["text"], language="text")
        else:
            st.caption("No reports for this date yet.")
    except Exception as e:
        st.info(f"Reports unavailable: {e}")

# ---------------- Internal Messaging Platform ----------------
with tabs[4]:
    st.markdown("### Internal Messaging Platform")
    st.caption("DMs exist between every agent pair + room:all. CEO can create additional group chats and manage membership.")

    try:
        rooms = api_get(backend, "/api/chat/rooms")
    except Exception as e:
        st.error(f"Chat rooms unavailable: {e}")
        rooms = []

    room_map = {r["room_id"]: r for r in rooms}
    room_ids = sorted(room_map.keys())
    if not room_ids:
        st.stop()

    colL, colR = st.columns([0.33, 0.67], gap="large")

    with colL:
        st.markdown("#### Rooms")
        sel_room = st.selectbox("Select room", room_ids, index=room_ids.index("room:all") if "room:all" in room_ids else 0)
        room = room_map.get(sel_room) or {}
        st.write(f"**{room.get('name','')}**  (`{sel_room}`)")
        st.caption(f"kind={room.get('kind')} created_by={room.get('created_by')}")
        try:
            members = api_get(backend, f"/api/chat/room/{sel_room}/members")
            st.markdown("**Members**")
            st.write(", ".join(members) if members else "—")
        except Exception:
            members = []

        st.markdown("---")
        st.markdown("#### CEO: Create group chat")
        with st.form("create_room_form", clear_on_submit=True):
            name = st.text_input("Room name", placeholder="e.g., 'Macro War Room'")
            # Instead of trying to fetch all members aggressively, just use agent list
            try:
                agents = api_get(backend, "/api/agents")
                agent_ids = sorted({a["id"] for a in agents})
            except Exception:
                agent_ids = []
            picks = st.multiselect("Members", options=agent_ids, default=[a for a in ["ceo", "cro"] if a in agent_ids])
            create = st.form_submit_button("Create")
            if create and name.strip():
                try:
                    out = api_post(backend, "/api/chat/room/create", {"name": name.strip(), "members": picks, "actor": "ceo", "meta": {}})
                    st.success(f"Created {out.get('room_id')}")
                except Exception as e:
                    st.error(f"Failed: {e}")

        st.markdown("---")
        st.markdown("#### CEO: Manage membership")
        if room.get("kind") in ("group", "system"):
            with st.form("membership_form", clear_on_submit=True):
                try:
                    agents = api_get(backend, "/api/agents")
                    agent_ids = sorted({a["id"] for a in agents})
                except Exception:
                    agent_ids = []
                add_member = st.selectbox("Add member", options=[""] + agent_ids, index=0)
                remove_member = st.selectbox("Remove member", options=[""] + (members or []), index=0)
                do_add = st.form_submit_button("Apply changes")
                if do_add:
                    try:
                        if add_member:
                            api_post(backend, f"/api/chat/room/{sel_room}/add", {"member_id": add_member, "actor": "ceo"})
                        if remove_member:
                            api_post(backend, f"/api/chat/room/{sel_room}/remove", {"member_id": remove_member, "actor": "ceo"})
                        st.success("Updated membership.")
                    except Exception as e:
                        st.error(f"Failed: {e}")
        else:
            st.caption("Membership controls disabled for DM rooms.")

    with colR:
        st.markdown("#### Room Messages")
        try:
            msgs = api_get(backend, f"/api/chat/room/{sel_room}/tail", params={"limit": 300})
        except Exception:
            msgs = []
        for m in msgs[-120:]:
            st.write(f"[{m['ts']}] **{m['sender']}**: {m['message']}")
            if m.get("meta"):
                with st.expander("meta", expanded=False):
                    st.json(m["meta"])

        st.markdown("---")
        st.markdown("#### Send message")
        try:
            agents = api_get(backend, "/api/agents")
            agent_ids = sorted({a["id"] for a in agents} | {"user"})
        except Exception:
            agent_ids = ["user", "ceo"]
        with st.form("send_room_form", clear_on_submit=True):
            sender = st.selectbox("Sender", options=agent_ids, index=agent_ids.index("ceo") if "ceo" in agent_ids else 0)
            msg = st.text_area("Message text", height=90)
            send = st.form_submit_button("Send")
            if send and msg.strip():
                try:
                    api_post(backend, f"/api/chat/room/{sel_room}/send", {"sender": sender, "message": msg.strip(), "meta": {}})
                    st.success("Sent.")
                except Exception as e:
                    st.error(f"Failed: {e}")

st.sidebar.markdown("---")
st.sidebar.caption("Tip: Before restarting the backend, uncheck 'Auto refresh' to stop Streamlit from holding connections open.")
