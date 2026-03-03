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

*** *** *** 

Operations Runbook (Setup, Teardown, Updates, DB resets)

This project runs as two long-lived processes:

Backend (FastAPI/Uvicorn) on http://localhost:8000

Dashboard (Streamlit) on http://localhost:8501

And uses local services:

Redis (recommended): redis://localhost:6379

Ollama (required): http://localhost:11434

Terminal layout (recommended)

Use separate VSCode terminals so you always know what’s running where:

Terminal 1 — Backend

Terminal 2 — Streamlit

Terminal 3 — Admin/maintenance (DB resets, pip installs, git pulls)

You can run commands in any terminal, but this layout prevents confusion.

Virtual environment (venv): do I exit and re-enter?

If your prompt shows (.venv), you’re already in the venv → do not deactivate. Just run commands.

If you don’t see (.venv), activate it:

source .venv/bin/activate

Best practice: use the venv Python explicitly so you never hit system Python:

./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python scripts/init_db.py
First-time setup (fresh clone, fresh DB)
1) Get code + create venv

From project root:

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip setuptools wheel
python3 -m pip install -r requirements.txt
2) Start Redis (if you’re using it)
brew services start redis
3) Ensure Ollama is running
curl -s http://localhost:11434 | head
4) Initialize a fresh DB (creates data/firm.db)
python3 scripts/init_db.py
5) Start backend (Terminal 1)

Prefer no reload while debugging:

uvicorn backend.main:app --host 0.0.0.0 --port 8000
6) Start dashboard (Terminal 2)
streamlit run dashboard/app.py
Normal startup (reuse existing DB)

If data/firm.db already exists, you do not need to reinitialize it.

Start backend
source .venv/bin/activate
uvicorn backend.main:app --host 0.0.0.0 --port 8000
Start Streamlit
source .venv/bin/activate
streamlit run dashboard/app.py
Tear down / stop everything cleanly

Always stop Streamlit first, then backend.

Stop Streamlit (Terminal 2)

Press:

Ctrl + C

Stop backend (Terminal 1)

Press:

Ctrl + C

If backend doesn’t exit cleanly (common with --reload or background tasks), use the “kill” commands below.

Kill backend / Streamlit if they get stuck
Find & kill anything on port 8000 (backend)
lsof -nP -iTCP:8000 -sTCP:LISTEN
kill <PID>
# if it refuses:
kill -9 <PID>
Find & kill anything on port 8501 (streamlit)
lsof -nP -iTCP:8501 -sTCP:LISTEN
kill <PID>
# if it refuses:
kill -9 <PID>
Quick “kill uvicorn backend.main:app” (useful for --reload)
pkill -f "uvicorn backend.main:app"
Verify ports are free
lsof -nP -iTCP:8000 -sTCP:LISTEN
lsof -nP -iTCP:8501 -sTCP:LISTEN

(should return nothing)

Reinitialize DB (fresh DB) — recommended if things get sluggish

If DB tables (especially messages) have grown huge, resetting can immediately improve performance.

Option A: Reset DB with reset script (recommended)

If you have scripts/reset_db.py:

# Terminal 3 (admin)
source .venv/bin/activate

# Stop backend + streamlit first!
pkill -f "uvicorn backend.main:app" || true

python3 scripts/reset_db.py

Then restart backend + Streamlit normally.

Option B: Manual reset (if reset_db.py doesn’t exist)
# Stop backend + streamlit first!
pkill -f "uvicorn backend.main:app" || true

rm -f data/firm.db data/firm.db-wal data/firm.db-shm
python3 scripts/init_db.py
Updating the codebase (git pull / new features)
1) Pull latest code (Terminal 3)
git pull
2) If dependencies changed

Always re-run requirements install after pulling (safe even if unchanged):

source .venv/bin/activate
python3 -m pip install -r requirements.txt
3) Restart backend + Streamlit

Stop streamlit → stop backend → start backend → start streamlit.

When should I use --reload?

--reload is convenient during development but can create extra processes and occasional shutdown weirdness.

Use during active coding:

uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

Use without reload for stability/performance:

uvicorn backend.main:app --host 0.0.0.0 --port 8000
Quick health checks (are things alive?)
Backend health:
curl -s http://localhost:8000/api/health
Streamlit:

Open http://localhost:8501

Ollama:
curl -s http://localhost:11434 | head
Performance tips (simple)

Turn off Streamlit Auto refresh while restarting services.

If UI feels slow, first try:

reset DB (messages can balloon quickly)

restart backend without --reload

Install watchdog (better Streamlit dev reload performance):

python3 -m pip install watchdog
What to do if you’re “already in the venv”

If your prompt shows (.venv):

stay in it and run commands normally.
No need to exit/enter between tasks.

Extra: Where to run which commands

Backend start/stop: Terminal 1

Streamlit start/stop: Terminal 2

DB init/reset, pip install, git pull: Terminal 3

(You can do everything in one terminal, but you’ll lose track quickly.)