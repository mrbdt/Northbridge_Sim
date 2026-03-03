import asyncio
import os
from pathlib import Path

from init_db import init_db


async def main() -> None:
    """
    Deletes the SQLite DB file (and WAL/SHM sidecars) and recreates it by calling init_db().

    Uses env var NB_SQLITE_PATH if set; otherwise defaults to data/firm.db.
    """
    db_path = os.environ.get("NB_SQLITE_PATH", "data/firm.db")
    p = Path(db_path)

    # Delete DB + WAL/SHM if present
    for suffix in ("", "-wal", "-shm"):
        fp = Path(str(p) + suffix)
        if fp.exists():
            fp.unlink()
            print(f"Deleted {fp}")

    # Recreate schema + seed rows
    await init_db(str(p))


if __name__ == "__main__":
    asyncio.run(main())