import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

BACKEND_DEFAULT = os.environ.get("NB_BACKEND_URL", "http://localhost:8000")

PAGES = ["Home", "Agent Detail", "CEO Chat", "Internal Messaging"]


# ----------------------- Styling -----------------------
def _load_css() -> None:
    css_file = os.path.join(os.path.dirname(__file__), "bloomberg.css")
    try:
        with open(css_file, "r", encoding="utf-8") as f:
            st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)
    except Exception:
        pass


# ----------------------- HTTP + Cache -----------------------
def _http() -> requests.Session:
    if "_http_session" not in st.session_state:
        s = requests.Session()
        st.session_state["_http_session"] = s
    return st.session_state["_http_session"]


def _cache() -> Dict[str, Dict[str, Any]]:
    return st.session_state.setdefault("_api_cache", {})


def _cache_key(base: str, path: str, params: Optional[Dict[str, Any]]) -> str:
    return f"{base.rstrip('/')}{path}|{json.dumps(params or {}, sort_keys=True)}"


def api_get(base: str, path: str, params: Optional[Dict[str, Any]] = None, timeout: float = 3.0):
    r = _http().get(f"{base.rstrip('/')}{path}", params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def api_post(base: str, path: str, payload: Dict[str, Any], timeout: float = 30.0):
    r = _http().post(f"{base.rstrip('/')}{path}", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def cached_get(
    base: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    ttl: float = 2.0,
    timeout: float = 3.0,
) -> Tuple[Optional[Any], Optional[Exception]]:
    """
    Session TTL cache with stale-fallback:
      - fast for Streamlit reruns
      - never blocks the whole UI if backend is briefly slow
    """
    cache = _cache()
    k = _cache_key(base, path, params)
    now = time.monotonic()

    ent = cache.get(k)
    if ent and (now - ent["ts"]) < ttl:
        return ent["data"], None

    try:
        data = api_get(base, path, params=params, timeout=timeout)
        cache[k] = {"ts": now, "data": data}
        return data, None
    except Exception as e:
        if ent:
            return ent["data"], e
        return None, e


def clear_cache() -> None:
    _cache().clear()


# ----------------------- Helpers -----------------------
def _price_for_symbol(sym: str, preferred_venue: Optional[str], last: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    sym_u = (sym or "").upper().strip()
    pref = (preferred_venue or "").upper().strip()

    # Exact match first
    if pref:
        k = f"{sym_u}@{pref}"
        if k in last:
            px = last[k]
            return px.get("last"), px.get("ts")

    # Fallback: any venue
    for k, v in last.items():
        if k.startswith(sym_u + "@"):
            return v.get("last"), v.get("ts")

    return None, None


# ----------------------- App -----------------------
st.set_page_config(page_title="Northbridge // Sim", layout="wide")
_load_css()

# Sidebar controls (keep minimal — performance first)
st.sidebar.markdown("### NORTHBRIDGE // SIM")
backend = st.sidebar.text_input("Backend URL", value=BACKEND_DEFAULT)

colA, colB = st.sidebar.columns(2)
with colA:
    if st.button("Refresh UI"):
        clear_cache()
        st.rerun()
with colB:
    auto = st.toggle("Auto refresh", value=False)

interval_ms = int(st.sidebar.slider("Refresh interval (ms)", 1000, 10000, 4000, 500))
api_timeout = float(st.sidebar.slider("API timeout (s)", 1.0, 20.0, 3.0, 0.5))

if auto:
    st_autorefresh(interval=interval_ms, key="nb_refresh")

# Fast health check
health, h_err = cached_get(backend, "/api/health", ttl=2, timeout=min(1.5, api_timeout))
if h_err or not health:
    st.error(f"Backend not reachable at {backend}: {h_err}")
    st.stop()

st.markdown("## NORTHBRIDGE ⟂ MULTI‑STRATEGY ⟂ SIMULATED TERMINAL")

# Top nav (only the selected page runs)
page = st.radio("Page", PAGES, horizontal=True, index=0, label_visibility="collapsed")


# ----------------------- HOME -----------------------
if page == "Home":
    # One-call endpoint (preferred). Fallback to older endpoints if missing.
    home, home_err = cached_get(backend, "/api/dashboard/home", ttl=2, timeout=api_timeout)

    if home_err or not home:
        # fallback (older backend)
        port, port_err = cached_get(backend, "/api/portfolio", ttl=2, timeout=api_timeout)
        uni, uni_err = cached_get(backend, "/api/universe", ttl=30, timeout=api_timeout)
        last, last_err = cached_get(backend, "/api/market/last", ttl=2, timeout=api_timeout)

        if port_err:
            st.warning(f"Portfolio fetch issue: {port_err}")
        if uni_err:
            st.warning(f"Universe fetch issue: {uni_err}")
        if last_err:
            st.warning(f"Prices fetch issue: {last_err}")

        home = {"portfolio": port or {}, "universe": uni or [], "last_prices": last or {}}

    port = home.get("portfolio") or {}
    universe = home.get("universe") or []
    last = home.get("last_prices") or {}

    if not port:
        st.info("Portfolio not ready yet.")
        st.stop()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("AUM (NAV)", f"{float(port.get('nav', 0.0)):.2f}")
    c2.metric("Gross", f"{float(port.get('gross_exposure', 0.0)):.2f}")
    c3.metric("Net", f"{float(port.get('net_exposure', 0.0)):.2f}")
    c4.metric("Leverage", f"{float(port.get('leverage', 0.0)):.2f}")

    st.markdown("### Investments")
    st.dataframe(port.get("positions", []), use_container_width=True, height=320)

    st.markdown("### Universe (Tracked Assets)")
    rows: List[Dict[str, Any]] = []
    for ins in universe:
        meta = ins.get("meta") or {}
        last_px, last_ts = _price_for_symbol(ins.get("symbol", ""), meta.get("preferred_venue"), last)
        rows.append(
            {
                "symbol": ins.get("symbol"),
                "asset_class": ins.get("asset_class"),
                "provider": meta.get("provider"),
                "preferred_venue": meta.get("preferred_venue"),
                "multiplier": ins.get("multiplier"),
                "last": last_px,
                "ts": last_ts,
            }
        )
    st.dataframe(rows, use_container_width=True, height=420)


# ----------------------- AGENT DETAIL -----------------------
elif page == "Agent Detail":
    agents, a_err = cached_get(backend, "/api/agents", ttl=5, timeout=api_timeout)
    if a_err:
        st.warning(f"Agents list issue: {a_err}")

    agents = agents or []
    agent_ids = [a.get("id") for a in agents if a.get("id")]
    if not agent_ids:
        st.info("No agents available yet.")
        st.stop()

    sel = st.selectbox("Select agent", agent_ids, index=0)

    # Agent metadata row
    meta = next((a for a in agents if a.get("id") == sel), {}) or {}
    top = st.columns([0.25, 0.25, 0.25, 0.25])
    top[0].metric("Agent", sel)
    top[1].metric("Role", str(meta.get("role", "")))
    top[2].metric("Status", str(meta.get("status", "")))
    top[3].metric("Model", str(meta.get("model", "")))

    # State (lightweight fetch)
    st_state, s_err = cached_get(backend, f"/api/agent/{sel}/state", ttl=1.0, timeout=api_timeout)
    if s_err:
        st.error(f"Failed to load agent state: {s_err}")
        st.stop()

    state_obj = st_state or {}
    inner = (state_obj.get("state") or {}) if isinstance(state_obj, dict) else {}
    thinking = inner.get("thinking") or inner.get("last_decision_analysis") or ""

    st.markdown("### Thinking / Rationale")
    if thinking:
        st.code(str(thinking), language="text")
    else:
        st.caption("No thinking logged yet for this agent.")

    with st.expander("Full runtime state (JSON)", expanded=False):
        st.json(state_obj)

    # Messages sent by agent
    st.markdown("### Agent Messages (what it said / did)")
    msgs, m_err = cached_get(backend, f"/api/agent/{sel}/messages", params={"limit": 200}, ttl=1.5, timeout=api_timeout)
    if m_err:
        st.warning(f"Messages unavailable: {m_err}")
        msgs = msgs or []
    msgs = msgs or []

    if msgs:
        # show as table first (fast)
        table = [
            {"ts": m.get("ts"), "channel": m.get("channel"), "message": (m.get("message") or "")[:200], "sender": m.get("sender")}
            for m in msgs[-200:]
        ]
        st.dataframe(table, use_container_width=True, height=260)

        # optional deep inspect
        with st.expander("Inspect a message (full text + meta)", expanded=False):
            options = list(range(len(msgs)))
            idx = st.selectbox(
                "Pick message",
                options,
                format_func=lambda i: f"{msgs[i].get('ts')} | {msgs[i].get('channel')} | {(msgs[i].get('message') or '')[:80]}",
            )
            st.code(str(msgs[idx].get("message") or ""), language="text")
            meta_obj = msgs[idx].get("meta") or {}
            if meta_obj:
                st.json(meta_obj)
    else:
        st.caption("No messages from this agent yet.")

    # LLM trace (prefer new endpoint; fallback to channel filter)
    st.markdown("### LLM Traces (what it prompted / received)")
    traces, t_err = cached_get(backend, f"/api/agent/{sel}/llm_trace", params={"limit": 80}, ttl=2.0, timeout=api_timeout)
    if t_err or traces is None:
        # fallback (older backend): fetch channel then filter (can be slower)
        traces, t_err2 = cached_get(backend, "/api/channel/llm_trace", params={"limit": 250}, ttl=2.0, timeout=api_timeout)
        traces = [t for t in (traces or []) if t.get("sender") == sel]
        if t_err and t_err2:
            st.caption(f"No llm traces available: {t_err2}")

    traces = traces or []
    if traces:
        table2 = [{"ts": t.get("ts"), "message": (t.get("message") or "")[:200], "sender": t.get("sender")} for t in traces[-80:]]
        st.dataframe(table2, use_container_width=True, height=240)

        with st.expander("Inspect a trace (prompt / raw)", expanded=False):
            options = list(range(len(traces)))
            idx = st.selectbox(
                "Pick trace",
                options,
                format_func=lambda i: f"{traces[i].get('ts')} | {(traces[i].get('message') or '')[:80]}",
            )
            st.code(str(traces[idx].get("message") or ""), language="text")
            meta_obj = traces[idx].get("meta") or {}
            if meta_obj:
                st.json(meta_obj)
    else:
        st.caption("No llm_trace entries for this agent yet.")


# ----------------------- CEO CHAT -----------------------
elif page == "CEO Chat":
    st.markdown("### CEO Chat")
    st.caption("You ↔ CEO chat is stored in room `dm:ceo:user`.")

    room_id = "dm:ceo:user"
    convo, c_err = cached_get(backend, f"/api/chat/room/{room_id}/tail", params={"limit": 200}, ttl=1.5, timeout=api_timeout)
    if c_err:
        st.warning(f"Could not load CEO thread (showing stale if any): {c_err}")
    convo = convo or []

    # Render chat messages
    for m in convo[-120:]:
        sender = m.get("sender")
        role = "assistant" if sender == "ceo" else "user"
        with st.chat_message(role):
            st.markdown(m.get("message") or "")
            st.caption(f"{m.get('ts')} · {sender}")

    prompt = st.chat_input("Message the CEO…")
    if prompt and prompt.strip():
        # This endpoint waits for the CEO LLM reply and returns it. It can take a few seconds.
        try:
            api_post(backend, "/api/ceo/chat", {"text": prompt.strip()}, timeout=max(30.0, api_timeout))
            clear_cache()  # ensures new convo appears
            st.rerun()
        except Exception as e:
            st.error(f"Failed to send to CEO: {e}")


# ----------------------- INTERNAL MESSAGING (READ ONLY) -----------------------
elif page == "Internal Messaging":
    st.markdown("### Internal Messaging Platform (Read‑Only)")
    st.caption("This is the primary agent-to-agent communication system. You can read, but not write.")

    rooms, r_err = cached_get(backend, "/api/chat/rooms", ttl=30, timeout=api_timeout)
    if r_err:
        st.error(f"Chat rooms unavailable: {r_err}")
        st.stop()

    rooms = rooms or []
    room_ids = [r.get("room_id") for r in rooms if r.get("room_id")]
    if not room_ids:
        st.info("No rooms found.")
        st.stop()

    # Room selector
    default = "room:all" if "room:all" in room_ids else room_ids[0]
    sel_room = st.selectbox("Room", room_ids, index=room_ids.index(default))

    limit = st.slider("Messages to display", 50, 500, 200, 50)

    msgs, m_err = cached_get(backend, f"/api/chat/room/{sel_room}/tail", params={"limit": limit}, ttl=1.5, timeout=api_timeout)
    if m_err:
        st.warning(f"Room messages slow/unavailable (showing stale if any): {m_err}")
    msgs = msgs or []

    # Fast table render
    table = [{"ts": m.get("ts"), "sender": m.get("sender"), "message": (m.get("message") or "")[:300]} for m in msgs[-limit:]]
    st.dataframe(table, use_container_width=True, height=520)

    with st.expander("Inspect a message (full text + meta)", expanded=False):
        if msgs:
            options = list(range(len(msgs)))
            idx = st.selectbox(
                "Pick message",
                options,
                format_func=lambda i: f"{msgs[i].get('ts')} | {msgs[i].get('sender')} | {(msgs[i].get('message') or '')[:80]}",
            )
            st.code(str(msgs[idx].get("message") or ""), language="text")
            meta_obj = msgs[idx].get("meta") or {}
            if meta_obj:
                st.json(meta_obj)
        else:
            st.caption("No messages in this room yet.")