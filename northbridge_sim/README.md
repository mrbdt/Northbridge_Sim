# Northbridge Multi‑Strategy (Sim)

Local multi-agent trading firm simulator (MVP):

- 10 agents (CEO, CRO, Quant, Macro, Event, Crypto, Vol, Execution, Infra, Ops)
- Ollama + Qwen3.5 (model per agent via `configs/agents.yaml`)
- Real-time crypto via `cryptofeed`
- Delayed equities via Alpaca polling (or Yahoo fallback)
- BrokerSim with fees + slippage
- SQLite ledger + Redis pub/sub + Parquet ticks
- Streamlit dashboard

## Quickstart
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/init_db.py
uvicorn backend.main:app --host 0.0.0.0 --port 8000
streamlit run dashboard/app.py
```

## Equities data
For Alpaca polling:
```bash
export ALPACA_API_KEY="..."
export ALPACA_SECRET_KEY="..."
```

If you want to avoid Alpaca keys for MVP, set in `configs/firm.yaml`:
```yaml
data:
  equities:
    provider: "yahoo_poll"
```

## Hire / retire agents
Edit `configs/agents.yaml`:
- set `status: retired` to take agent offline
- add a new agent block to hire

Or use Streamlit Admin buttons (writes to the YAML file via backend endpoints).


## Alpaca free tier rate limit (429) automatic fallback
If `provider: alpaca_poll` is enabled and Alpaca returns **429 Too Many Requests**, the system automatically switches to **Yahoo polling** for the rest of the session and posts a message in the `ops` channel.

***

notes.

#to kill the backend, do:

ps aux | grep -E "uvicorn|backend\.main:app|backend\.main" | grep -v grep

the you get:

bdts             aaaaaaa   0.0  0.0 435291568  13808 s000  S+   11:14p.m.   0:14.01 /opt/homebrew/Cellar/python@3.12/3.12.12_2/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python /Users/bdts/Northbridge_Sim/northbridge_sim/.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

upon which you do:

kill -9 44434 } copying over aaaaaaaa from above.