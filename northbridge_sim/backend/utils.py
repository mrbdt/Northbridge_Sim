from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo
from typing import Any, Dict

def now_iso(tz: str = "UTC") -> str:
    return _dt.datetime.now(tz=ZoneInfo(tz)).isoformat()

def utcnow_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def deep_get(d: Dict[str, Any], path: str, default=None):
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur
