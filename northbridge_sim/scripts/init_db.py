import asyncio
import os
from pathlib import Path

import aiosqlite

DEFAULT_DB = os.environ.get("NB_SQLITE_PATH", "data/firm.db")

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS instruments(
  symbol TEXT PRIMARY KEY,
  asset_class TEXT,
  ccy TEXT,
  multiplier REAL,
  meta_json TEXT
);

CREATE TABLE IF NOT EXISTS universe_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  actor TEXT,
  action TEXT,
  symbol TEXT,
  meta_json TEXT
);

CREATE TABLE IF NOT EXISTS positions(
  symbol TEXT PRIMARY KEY,
  qty REAL,
  avg_px REAL,
  side TEXT,
  meta_json TEXT
);

CREATE TABLE IF NOT EXISTS cash(
  ccy TEXT PRIMARY KEY,
  balance REAL
);

CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  channel TEXT,
  sender TEXT,
  message TEXT,
  meta_json TEXT
);

-- CRITICAL indexes for tails/filters as messages grow
CREATE INDEX IF NOT EXISTS idx_messages_channel_id ON messages(channel, id);
CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages(sender, id);

CREATE TABLE IF NOT EXISTS agent_state(
  agent_id TEXT PRIMARY KEY,
  ts TEXT,
  state_json TEXT
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  nav REAL,
  gross REAL,
  net REAL,
  leverage REAL,
  drawdown REAL,
  positions_json TEXT
);

CREATE TABLE IF NOT EXISTS signals_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  category TEXT,
  title TEXT,
  link TEXT,
  summary TEXT,
  meta_json TEXT
);

CREATE TABLE IF NOT EXISTS chat_rooms(
  room_id TEXT PRIMARY KEY,
  kind TEXT,
  name TEXT,
  created_by TEXT,
  created_ts TEXT,
  meta_json TEXT
);

CREATE TABLE IF NOT EXISTS chat_members(
  room_id TEXT,
  member_id TEXT,
  added_by TEXT,
  added_ts TEXT,
  meta_json TEXT,
  PRIMARY KEY (room_id, member_id)
);

CREATE TABLE IF NOT EXISTS ceo_reports(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  report_text TEXT,
  meta_json TEXT
);
"""


async def init_db(db_path: str = DEFAULT_DB) -> None:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(str(p)) as db:
        await db.executescript(SCHEMA_SQL)
        # Seed USD cash if missing (aiosqlite compatibility: no execute_fetchone)
        cur = await db.execute("SELECT balance FROM cash WHERE ccy='USD'")
        row = await cur.fetchone()
        await cur.close()

        if row is None:
            await db.execute("INSERT INTO cash(ccy, balance) VALUES('USD', 1000000.0)")
            await db.commit()

    print(f"Initialized DB at {p.resolve()}")


if __name__ == "__main__":
    asyncio.run(init_db())