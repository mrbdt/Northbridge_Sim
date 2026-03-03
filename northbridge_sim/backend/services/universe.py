from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ..db import Database
from ..utils import utcnow_iso


def classify_symbol(symbol: str) -> Tuple[str, str, str]:
    """
    Best-effort classification:
      - crypto pairs like BTC-USDT -> (crypto, USDT, crypto)
      - futures like GC=F -> (commodity, USD, yahoo)
      - FX like EURUSD=X -> (fx, USD, yahoo)
      - indices like ^GSPC -> (index, USD, yahoo)
      - otherwise equity -> (equity, USD, alpaca)
    Returns: (asset_class, ccy, provider)
    """
    s = symbol.strip().upper()
    if "-" in s and (s.endswith("USDT") or s.endswith("USD")):
        # e.g., BTC-USDT, ETH-USD
        parts = s.split("-")
        ccy = parts[-1] if parts else "USD"
        return "crypto", ccy, "crypto"
    if s.endswith("=F"):
        return "commodity", "USD", "yahoo"
    if s.endswith("=X"):
        return "fx", "USD", "yahoo"
    if s.startswith("^"):
        return "index", "USD", "yahoo"
    return "equity", "USD", "alpaca"



def default_preferred_venue(provider: str, asset_class: str) -> str:
    p = (provider or "").lower()
    if p == "crypto":
        return "BINANCE"
    if p == "alpaca":
        return "EQUITIES"
    if p == "yahoo":
        return "YAHOO"
    # fallback
    return "BINANCE" if asset_class == "crypto" else "EQUITIES" if asset_class in ("equity", "stock") else "YAHOO"


class UniverseService:
    def __init__(self, db: Database):
        self.db = db

    async def bootstrap_from_yaml(self, path: str = "configs/universe.yaml") -> None:
        p = Path(path)
        if not p.exists():
            return
        raw = yaml.safe_load(p.read_text()) or {}
        instruments = raw.get("instruments", []) or []
        for ins in instruments:
            sym = str(ins.get("symbol")).strip()
            if not sym:
                continue
            asset_class = ins.get("asset_class") or classify_symbol(sym)[0]
            ccy = ins.get("ccy") or classify_symbol(sym)[1]
            mult = float(ins.get("multiplier", 1.0))
            meta = ins.get("meta") or {}
            # preferred venue / provider stored in meta
            _, _, provider = classify_symbol(sym)
            meta.setdefault("provider", provider)
            meta.setdefault("preferred_venue", default_preferred_venue(meta.get("provider"), asset_class))
            await self.upsert(sym, asset_class=asset_class, ccy=ccy, multiplier=mult, meta=meta)

    async def upsert(self, symbol: str, asset_class: str, ccy: str, multiplier: float = 1.0, meta: Optional[Dict[str, Any]] = None) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO instruments(symbol, asset_class, ccy, multiplier, meta_json)
                 VALUES(?,?,?,?,?)""",
            (symbol, asset_class, ccy, float(multiplier), json.dumps(meta or {})),
        )

    async def list(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall("SELECT symbol, asset_class, ccy, multiplier, meta_json FROM instruments ORDER BY symbol")
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "symbol": r["symbol"],
                "asset_class": r["asset_class"],
                "ccy": r["ccy"],
                "multiplier": float(r["multiplier"]),
                "meta": json.loads(r.get("meta_json") or "{}"),
            })
        return out

    async def add(self, symbol: str, asset_class: Optional[str] = None, ccy: Optional[str] = None, multiplier: float = 1.0, meta: Optional[Dict[str, Any]] = None, actor: str = "ceo") -> Dict[str, Any]:
        sym = symbol.strip().upper()
        ac, default_ccy, provider = classify_symbol(sym)
        asset_class = (asset_class or ac).strip().lower()
        ccy = (ccy or default_ccy).strip().upper()
        meta = meta or {}
        meta.setdefault("provider", provider)
        meta.setdefault("preferred_venue", default_preferred_venue(meta.get("provider"), asset_class))

        await self.upsert(sym, asset_class=asset_class, ccy=ccy, multiplier=multiplier, meta=meta)

        await self.db.execute(
            "INSERT INTO universe_events(ts, actor, action, symbol, meta_json) VALUES(?,?,?,?,?)",
            (utcnow_iso(), actor, "add", sym, json.dumps(meta)),
        )
        return {"symbol": sym, "asset_class": asset_class, "ccy": ccy, "multiplier": float(multiplier), "meta": meta}

    async def get_symbols(self, provider: Optional[str] = None, asset_class: Optional[str] = None) -> List[str]:
        instruments = await self.list()
        out: List[str] = []
        for ins in instruments:
            m = ins.get("meta", {})
            if provider and (m.get("provider") != provider):
                continue
            if asset_class and (ins.get("asset_class") != asset_class):
                continue
            out.append(ins["symbol"])
        return out
