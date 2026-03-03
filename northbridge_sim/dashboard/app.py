import json
import os
from typing import Any, Dict

import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

BACKEND = os.environ.get("NB_BACKEND_URL", "http://localhost:8000")

st.set_page_config(page_title="Northbridge Sim", layout="wide")
st.title("Northbridge Multi‑Strategy (Sim)")

st_autorefresh(interval=2000, key="refresh")

def get(path: str):
    r = requests.get(f"{BACKEND}{path}", timeout=5)
    r.raise_for_status()
    return r.json()

def post(path: str, payload: Dict[str, Any]):
    r = requests.post(f"{BACKEND}{path}", json=payload, timeout=10)
    r.raise_for_status()
    return r.json()

try:
    port = get("/api/portfolio")
except Exception as e:
    st.error(f"Backend not reachable: {e}")
    st.stop()

col1, col2, col3 = st.columns(3)
col1.metric("NAV (AUM)", f"{port['nav']:.2f}")
col2.metric("Gross Exposure", f"{port['gross_exposure']:.2f}")
col3.metric("Leverage", f"{port['leverage']:.2f}")

st.subheader("Positions")
st.dataframe(port["positions"], use_container_width=True)

st.subheader("Latest Prices")
rows = [{"instrument": k, **v} for k, v in port["last_prices"].items()]
st.dataframe(rows, use_container_width=True, height=250)

st.subheader("Agents")
agents = get("/api/agents")
agent_rows = []
for a in agents:
    rs = a.get("runtime_state") or {}
    state = (rs.get("state") or {})
    agent_rows.append({
        "id": a.get("id"),
        "role": a.get("role"),
        "status": a.get("status"),
        "model": a.get("model"),
        "heartbeat": (a.get("schedule") or {}).get("heartbeat_seconds"),
        "runtime_ts": rs.get("ts"),
        "runtime_state": json.dumps(state)[:250],
    })
st.dataframe(agent_rows, use_container_width=True, height=300)


st.subheader("Agent Inspector (live)")

agent_ids = [a.get("id") for a in agents if a.get("id")]
default_idx = 0
if "ceo" in agent_ids:
    default_idx = agent_ids.index("ceo")

selected_agent_id = st.selectbox("Inspect agent", agent_ids, index=default_idx)

selected_agent = next((a for a in agents if a.get("id") == selected_agent_id), None)
if selected_agent:
    st.markdown(
        f"**{selected_agent.get('name','')}** — {selected_agent.get('title','')}  \
"
        f"Role: `{selected_agent.get('role')}` | Status: `{selected_agent.get('status')}` | Model: `{selected_agent.get('model')}`"
    )

    rs = selected_agent.get("runtime_state") or {}
    st.write("Current runtime state (from `agent_state`):")
    st.json(rs.get("state") or {})

    try:
        agent_msgs = get(f"/api/agent/{selected_agent_id}/messages?limit=200")
    except Exception as e:
        st.warning(f"Could not fetch agent messages: {e}")
        agent_msgs = []

    st.write("Recent activity / thoughts (messages sent by this agent):")
    show_meta = st.checkbox("Show meta expanders", value=True, key=f"show_meta_{selected_agent_id}")
    max_show = st.slider("Messages to show", min_value=10, max_value=200, value=80, step=10, key=f"msg_count_{selected_agent_id}")

    for m in agent_msgs[-max_show:]:
        st.write(f"[{m['ts']}] ({m['channel']}) {m['message']}")
        if show_meta and m.get("meta"):
            with st.expander("meta", expanded=False):
                st.json(m["meta"])


st.subheader("Channels")
channels = ["trade_ideas","risk","execution","ops","ceo","ceo_inbox","llm_trace"]
chan = st.selectbox("Select channel", channels, index=3)
msgs = get(f"/api/channel/{chan}?limit=200")
for m in msgs[-50:]:
    st.write(f"[{m['ts']}] **{m['sender']}**: {m['message']}")
    if m.get("meta"):
        with st.expander("meta", expanded=False):
            st.json(m["meta"])

st.subheader("CEO Console")
with st.form("ceo_form", clear_on_submit=True):
    directive = st.text_area("Directive to CEO", height=80)
    submitted = st.form_submit_button("Send")
    if submitted and directive.strip():
        post("/api/ceo/directive", {"text": directive.strip()})
        st.success("Sent.")

st.subheader("Admin: retire/hire agents (optional)")
colA, colB = st.columns(2)

with colA:
    retire_id = st.text_input("Agent ID", value="quant")
    new_status = st.selectbox("Status", ["active","retired"], index=1)
    if st.button("Set status"):
        post(f"/api/admin/agent/{retire_id}/status", {"status": new_status})
        st.success("Updated agent status.")

with colB:
    st.write("Hire agent by providing JSON:")
    example = {
        "id": "new_agent",
        "name": "Nova",
        "title": "Experimental PM",
        "role": "quant",
        "status": "active",
        "model": "tiny",
        "schedule": {"heartbeat_seconds": 120},
        "permissions": {"can_trade": False}
    }
    agent_json = st.text_area("Agent JSON", value=json.dumps(example, indent=2), height=180)
    if st.button("Hire"):
        try:
            agent = json.loads(agent_json)
            post("/api/admin/agent/hire", {"agent": agent})
            st.success("Hired (written to agents.yaml).")
        except Exception as e:
            st.error(f"Failed: {e}")
