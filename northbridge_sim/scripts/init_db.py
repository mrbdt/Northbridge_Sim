import asyncio
import os
from pathlib import Path

import aiosqlite

SCHEMA_SQL = '''
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
'''

async def main():
    db_path = os.environ.get("NB_SQLITE_PATH", "data/firm.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA_SQL)
        await db.commit()
    print(f"Initialized SQLite schema at {db_path}")

if __name__ == "__main__":
    asyncio.run(main())
