import json
import os
import time
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

BACKEND_DEFAULT = os.environ.get("NB_BACKEND_URL", "http://localhost:8000")

PAGES = [
    "Home",
    "Agent Detail",
    "Channels",
    "CEO Chat",
    "Internal Messaging Platform",
]


def _load_css() -> None:
    css_file = os.path.join(os.path.dirname(__file__), "bloomberg.css")
    try:
        with open(css_file, "r", encoding="utf-8") as f:
            css = f.read()
        st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)
    except Exception:
        # Don’t hard-fail if CSS missing
        pass


def _http() -> requests.Session:
    # Persist one session across reruns (keep-alive helps a lot)
    if "_http_session" not in st.session_state:
        st.session_state["_http_session"] = requests.Session()
    return st.session_state["_http_session"]


def api_get(base: str, path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 10):
    r = _http().get(f"{base}{path}", params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def api_post(base: str, path: str, payload: Dict[str, Any], timeout: int = 30):
    r = _http().post(f"{base}{path}", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _cache_key(base: str, path: str, params: Optional[Dict[str, Any]]) -> str:
    return f"{base}|{path}|{json.dumps(params or {}, sort_keys=True)}"


def cached_get(
    base: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    ttl: float = 2.0,
    timeout: int = 10,
) -> Tuple[Optional[Any], Optional[Exception]]:
    """
    Small per-session TTL cache that:
      - avoids hammering the backend every rerun
      - returns last-good data if the latest call times out/fails
    """
    cache: Dict[str, Dict[str, Any]] = st.session_state.setdefault("_api_cache", {})
    key = _cache_key(base, path, params)
    now = time.time()

    entry = cache.get(key)
    if entry and (now - entry["ts"]) < ttl:
        return entry["data"], None

    try:
        data = api_get(base, path, params=params, timeout=timeout)
        cache[key] = {"ts": now, "data": data}
        return data, None
    except Exception as e:
        # If we have stale data, return it + the error (so UI still renders)
        if entry:
            return entry["data"], e
        return None, e


def invalidate_cache(base: str, path: str, params: Optional[Dict[str, Any]] = None) -> None:
    cache: Dict[str, Dict[str, Any]] = st.session_state.setdefault("_api_cache", {})
    cache.pop(_cache_key(base, path, params), None)


st.set_page_config(page_title="Northbridge // Sim", layout="wide")
_load_css()

# Sidebar
st.sidebar.markdown("### NORTHBRIDGE // SIM")
backend = st.sidebar.text_input("Backend URL", value=BACKEND_DEFAULT)

auto = st.sidebar.checkbox("Auto refresh", value=True)
interval_ms = int(
    st.sidebar.slider("Refresh interval (ms)", min_value=1000, max_value=10000, value=5000, step=500)
)
api_timeout = int(st.sidebar.slider("API timeout (seconds)", min_value=3, max_value=60, value=15, step=1))

if auto:
    st_autorefresh(interval=interval_ms, key="refresh")

st.markdown("## NORTHBRIDGE ⟂ MULTI‑STRATEGY ⟂ SIMULATED TERMINAL")

# Health check (fast)
health, err = cached_get(backend, "/api/health", ttl=2, timeout=min(5, api_timeout))
if err or not health:
    st.error(f"Backend not reachable at {backend}: {err}")
    st.stop()

# Navigation (only renders ONE page worth of API calls per rerun)
page = st.radio("", PAGES, horizontal=True, label_visibility="collapsed")

# ---------------- HOME ----------------
if page == "Home":
    port, port_err = cached_get(backend, "/api/portfolio", ttl=2, timeout=api_timeout)
    universe, uni_err = cached_get(backend, "/api/universe", ttl=30, timeout=api_timeout)
    last, last_err = cached_get(backend, "/api/market/last", ttl=2, timeout=api_timeout)
    agents, agents_err = cached_get(backend, "/api/agents", ttl=5, timeout=api_timeout)

    # Soft warnings, don’t crash the whole UI
    if port_err:
        st.warning(f"Portfolio fetch issue (showing last good if available): {port_err}")
    if uni_err:
        st.warning(f"Universe fetch issue (showing last good if available): {uni_err}")
    if last_err:
        st.warning(f"Prices fetch issue (showing last good if available): {last_err}")
    if agents_err:
        st.warning(f"Agents fetch issue (showing last good if available): {agents_err}")

    if not port:
        st.error("No portfolio data available yet.")
        st.stop()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("NAV (AUM)", f"{port['nav']:.2f}")
    c2.metric("Gross", f"{port['gross_exposure']:.2f}")
    c3.metric("Net", f"{port['net_exposure']:.2f}")
    c4.metric("Leverage", f"{port['leverage']:.2f}")
    c5.metric("Drawdown", f"{port['drawdown']:.2%}")

    st.markdown("### Portfolio Positions")
    st.dataframe(port.get("positions", []), use_container_width=True, height=260)

    st.markdown("### Current Prices (Universe)")
    rows: List[Dict[str, Any]] = []
    universe = universe or []
    last = last or {}
    for ins in universe:
        sym = (ins.get("symbol") or "").strip()
        meta = ins.get("meta") or {}
        pref = (meta.get("preferred_venue") or "").upper()
        key = f"{sym.upper()}@{pref}" if pref else None

        px = None
        key_used = None
        if key and key in last:
            px = last[key]
            key_used = key
        else:
            for k, v in last.items():
                if k.startswith(sym.upper() + "@"):
                    px = v
                    key_used = k
                    break

        rows.append(
            {
                "symbol": sym,
                "asset_class": ins.get("asset_class"),
                "provider": meta.get("provider"),
                "preferred_venue": meta.get("preferred_venue"),
                "key": key_used,
                "last": (px or {}).get("last") if px else None,
                "ts": (px or {}).get("ts") if px else None,
            }
        )
    st.dataframe(rows, use_container_width=True, height=320)

    st.markdown("### Agents Overview")
    agent_rows = []
    for a in (agents or []):
        rs = a.get("runtime_state") or {}
        state = (rs.get("state") or {})
        thinking = state.get("thinking") or state.get("last_decision_analysis") or ""
        agent_rows.append(
            {
                "id": a.get("id"),
                "role": a.get("role"),
                "status": a.get("status"),
                "model": a.get("model"),
                "hb": (a.get("schedule") or {}).get("heartbeat_seconds"),
                "ts": rs.get("ts"),
                "state": state.get("state") if isinstance(state, dict) else None,
                "thinking_snippet": str(thinking)[:240],
            }
        )
    st.dataframe(agent_rows, use_container_width=True, height=320)

    st.markdown("### Signals (Web Intel)")
    sigs, sig_err = cached_get(backend, "/api/signals", params={"limit": 20}, ttl=20, timeout=api_timeout)
    if sig_err:
        st.info(f"No signals yet (or slow backend): {sig_err}")
    else:
        for s in (sigs or [])[-10:]:
            st.write(f"[{s['ts']}] **{s.get('category','')}** — {s.get('title','')}")
            if s.get("link"):
                st.caption(s["link"])
            if s.get("summary"):
                with st.expander("summary / meta", expanded=False):
                    st.write(s["summary"])
                    st.json(s.get("meta") or {})

# ---------------- AGENT DETAIL ----------------
elif page == "Agent Detail":
    agents, agents_err = cached_get(backend, "/api/agents", ttl=5, timeout=api_timeout)
    if agents_err:
        st.warning(f"Agents list issue: {agents_err}")

    agent_ids = [a["id"] for a in (agents or []) if a.get("id")]
    if not agent_ids:
        st.info("No agents available yet.")
        st.stop()

    sel = st.selectbox("Select agent", agent_ids, index=0)

    st.markdown(f"### Agent: `{sel}`")

    st_state, st_err = cached_get(backend, f"/api/agent/{sel}/state", ttl=2, timeout=api_timeout)
    if st_err:
        st.error(f"Failed to load agent state: {st_err}")
    else:
        st.markdown("#### Runtime State (full)")
        st.json(st_state)

        thinking = (st_state.get("state") or {}).get("thinking") or (st_state.get("state") or {}).get(
            "last_decision_analysis"
        )
        if thinking:
            st.markdown("#### Thinking / Rationale (full)")
            st.code(str(thinking), language="text")

    cols = st.columns(2)

    with cols[0]:
        st.markdown("#### Messages sent by agent")
        msgs, m_err = cached_get(
            backend, f"/api/agent/{sel}/messages", params={"limit": 200}, ttl=2, timeout=api_timeout
        )
        if m_err:
            st.info(f"No messages (or slow backend): {m_err}")
        else:
            for m in (msgs or [])[-60:]:
                st.write(f"[{m['ts']}] **{m['channel']}** — {m['message']}")
                if m.get("meta"):
                    with st.expander("meta", expanded=False):
                        st.json(m["meta"])

    with cols[1]:
        st.markdown("#### LLM Traces (filtered)")
        traces, t_err = cached_get(backend, "/api/channel/llm_trace", params={"limit": 200}, ttl=2, timeout=api_timeout)
        if t_err:
            st.info(f"No llm_trace channel (or slow backend): {t_err}")
        else:
            filtered = [t for t in (traces or []) if t.get("sender") == sel]
            if not filtered:
                st.caption("No llm_trace messages for this agent yet.")
            for t in filtered[-25:]:
                st.write(f"[{t['ts']}] **{t['sender']}** — {t['message']}")
                if t.get("meta"):
                    with st.expander("trace meta (prompt / raw)", expanded=False):
                        st.json(t["meta"])

# ---------------- CHANNELS ----------------
elif page == "Channels":
    st.markdown("### Channels")

    base_channels = ["trade_ideas", "risk", "execution", "ops", "ceo", "signals", "llm_trace", "ceo_inbox"]
    rooms, _ = cached_get(backend, "/api/chat/rooms", ttl=10, timeout=api_timeout)
    room_ids = [r["room_id"] for r in (rooms or []) if r.get("room_id")]

    channels = base_channels + sorted(room_ids)
    chan = st.selectbox("Select channel / room", channels, index=channels.index("ops") if "ops" in channels else 0)
    limit = st.slider("Message limit", min_value=50, max_value=500, value=200, step=50)

    if chan.startswith("dm:") or chan.startswith("room:"):
        msgs, err = cached_get(backend, f"/api/chat/room/{chan}/tail", params={"limit": limit}, ttl=2, timeout=api_timeout)
    else:
        msgs, err = cached_get(backend, f"/api/channel/{chan}", params={"limit": limit}, ttl=2, timeout=api_timeout)

    if err:
        st.error(f"Failed to load: {err}")
        st.stop()

    for m in (msgs or [])[-120:]:
        st.write(f"[{m['ts']}] **{m['sender']}**: {m['message']}")
        if m.get("meta"):
            with st.expander("meta", expanded=False):
                st.json(m["meta"])

# ---------------- CEO CHAT ----------------
elif page == "CEO Chat":
    st.markdown("### CEO Chat")
    st.caption("Chat is stored in DM room `dm:ceo:user`. Directives go to `ceo_inbox`.")

    convo, err = cached_get(backend, "/api/chat/room/dm:ceo:user/tail", params={"limit": 200}, ttl=2, timeout=api_timeout)
    if err:
        st.warning(f"Could not load CEO thread: {err}")
        convo = convo or []

    for m in (convo or [])[-80:]:
        st.write(f"[{m['ts']}] **{m.get('sender')}**: {m['message']}")

    with st.form("ceo_chat_form", clear_on_submit=True):
        txt = st.text_area(
            "Message",
            height=90,
            placeholder="Ask the CEO for an update, change risk posture, or ask about current positioning…",
        )
        submitted = st.form_submit_button("Send to CEO")
        if submitted and txt.strip():
            try:
                api_post(backend, "/api/ceo/chat", {"text": txt.strip()}, timeout=max(30, api_timeout))
                invalidate_cache(backend, "/api/chat/room/dm:ceo:user/tail", {"limit": 200})
                st.success("Sent (thread will refresh).")
            except Exception as e:
                st.error(f"Failed to chat: {e}")

    st.markdown("---")
    st.markdown("### CEO Directive (risk posture)")
    with st.form("ceo_directive_form", clear_on_submit=True):
        directive = st.text_area("Directive text", height=80, placeholder="e.g., 'Lower risk today; cap leverage 1.0.'")
        sent = st.form_submit_button("Send directive")
        if sent and directive.strip():
            try:
                api_post(backend, "/api/ceo/directive", {"text": directive.strip()}, timeout=max(20, api_timeout))
                st.success("Directive sent.")
            except Exception as e:
                st.error(f"Failed: {e}")

    st.markdown("---")
    st.markdown("### Universe Management (as CEO)")
    with st.form("universe_add_form", clear_on_submit=True):
        sym = st.text_input("Symbol", placeholder="Examples: MSFT, GC=F, EURUSD=X, BTC-USDT, ^GSPC")
        asset_class = st.selectbox(
            "Asset class", ["", "equity", "crypto", "commodity", "fx", "rates", "index", "unknown"], index=0
        )
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
                api_post(backend, "/api/universe/add", payload, timeout=max(20, api_timeout))
                invalidate_cache(backend, "/api/universe", None)
                st.success(f"Added {sym.strip()} (prices show when a feed supports it).")
            except Exception as e:
                st.error(f"Failed to add: {e}")

    st.markdown("---")
    st.markdown("### CEO Rolling Reports")
    latest, _ = cached_get(backend, "/api/ceo/reports/latest", ttl=10, timeout=api_timeout)
    if latest:
        st.markdown(f"**Latest report** ({latest['ts']})")
        st.code(latest["text"], language="text")

    sel_date = st.date_input("View reports for date", value=date.today())
    reports, rep_err = cached_get(
        backend, "/api/ceo/reports", params={"date": sel_date.isoformat(), "limit": 200}, ttl=20, timeout=api_timeout
    )
    if rep_err:
        st.info(f"Reports unavailable (or slow backend): {rep_err}")
    else:
        if reports:
            for r in (reports or [])[-10:]:
                st.markdown(f"**{r['ts']}** (v{(r.get('meta') or {}).get('version')})")
                with st.expander("report text", expanded=False):
                    st.code(r["text"], language="text")
        else:
            st.caption("No reports for this date yet.")

# ---------------- INTERNAL MESSAGING ----------------
elif page == "Internal Messaging Platform":
    st.markdown("### Internal Messaging Platform")
    st.caption("DMs exist between every agent pair + room:all. CEO can create group chats & manage membership.")

    rooms, err = cached_get(backend, "/api/chat/rooms", ttl=10, timeout=api_timeout)
    if err:
        st.error(f"Chat rooms unavailable: {err}")
        st.stop()

    room_map = {r["room_id"]: r for r in (rooms or []) if r.get("room_id")}
    room_ids = sorted(room_map.keys())
    if not room_ids:
        st.info("No rooms found.")
        st.stop()

    colL, colR = st.columns([0.33, 0.67], gap="large")

    with colL:
        st.markdown("#### Rooms")
        sel_room = st.selectbox("Select room", room_ids, index=room_ids.index("room:all") if "room:all" in room_ids else 0)
        room = room_map.get(sel_room) or {}
        st.write(f"**{room.get('name','')}** (`{sel_room}`)")
        st.caption(f"kind={room.get('kind')} created_by={room.get('created_by')}")

        members, _ = cached_get(backend, f"/api/chat/room/{sel_room}/members", ttl=10, timeout=api_timeout)
        members = members or []
        st.markdown("**Members**")
        st.write(", ".join(members) if members else "—")

        st.markdown("---")
        st.markdown("#### CEO: Create group chat")
        with st.form("create_room_form", clear_on_submit=True):
            name = st.text_input("Room name", placeholder="e.g., 'Macro War Room'")
            agents, _ = cached_get(backend, "/api/agents", ttl=10, timeout=api_timeout)
            agent_ids = sorted({a["id"] for a in (agents or []) if a.get("id")})
            picks = st.multiselect("Members", options=agent_ids, default=[a for a in ["ceo", "cro"] if a in agent_ids])
            create = st.form_submit_button("Create")

            if create and name.strip():
                try:
                    out = api_post(
                        backend,
                        "/api/chat/room/create",
                        {"name": name.strip(), "members": picks, "actor": "ceo", "meta": {}},
                        timeout=max(20, api_timeout),
                    )
                    invalidate_cache(backend, "/api/chat/rooms", None)
                    st.success(f"Created {out.get('room_id')}")
                except Exception as e:
                    st.error(f"Failed: {e}")

        st.markdown("---")
        st.markdown("#### CEO: Manage membership")
        if room.get("kind") in ("group", "system"):
            with st.form("membership_form", clear_on_submit=True):
                agents, _ = cached_get(backend, "/api/agents", ttl=10, timeout=api_timeout)
                agent_ids = sorted({a["id"] for a in (agents or []) if a.get("id")})
                add_member = st.selectbox("Add member", options=[""] + agent_ids, index=0)
                remove_member = st.selectbox("Remove member", options=[""] + (members or []), index=0)
                do_apply = st.form_submit_button("Apply changes")

                if do_apply:
                    try:
                        if add_member:
                            api_post(backend, f"/api/chat/room/{sel_room}/add", {"member_id": add_member, "actor": "ceo"})
                        if remove_member:
                            api_post(
                                backend, f"/api/chat/room/{sel_room}/remove", {"member_id": remove_member, "actor": "ceo"}
                            )
                        invalidate_cache(backend, f"/api/chat/room/{sel_room}/members", None)
                        st.success("Updated membership.")
                    except Exception as e:
                        st.error(f"Failed: {e}")
        else:
            st.caption("Membership controls disabled for DM rooms.")

    with colR:
        st.markdown("#### Room Messages")
        msgs, msg_err = cached_get(backend, f"/api/chat/room/{sel_room}/tail", params={"limit": 300}, ttl=2, timeout=api_timeout)
        if msg_err:
            st.error(f"Failed to load room messages: {msg_err}")
            msgs = msgs or []

        for m in (msgs or [])[-140:]:
            st.write(f"[{m['ts']}] **{m['sender']}**: {m['message']}")
            if m.get("meta"):
                with st.expander("meta", expanded=False):
                    st.json(m["meta"])

        st.markdown("---")
        st.markdown("#### Send message")
        agents, _ = cached_get(backend, "/api/agents", ttl=10, timeout=api_timeout)
        agent_ids = sorted({a["id"] for a in (agents or []) if a.get("id")} | {"user"})
        if "ceo" in agent_ids:
            default_sender = "ceo"
        else:
            default_sender = agent_ids[0] if agent_ids else "user"

        with st.form("send_room_form", clear_on_submit=True):
            sender = st.selectbox("Sender", options=agent_ids, index=agent_ids.index(default_sender) if default_sender in agent_ids else 0)
            msg = st.text_area("Message text", height=90)
            send = st.form_submit_button("Send")
            if send and msg.strip():
                try:
                    api_post(backend, f"/api/chat/room/{sel_room}/send", {"sender": sender, "message": msg.strip(), "meta": {}})
                    invalidate_cache(backend, f"/api/chat/room/{sel_room}/tail", {"limit": 300})
                    st.success("Sent.")
                except Exception as e:
                    st.error(f"Failed: {e}")

st.sidebar.markdown("---")
st.sidebar.caption("Tip: Before restarting backend, uncheck 'Auto refresh' so Streamlit stops hammering / holding connections.")