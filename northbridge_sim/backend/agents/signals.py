from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import feedparser

from .base import BaseAgent
from ..utils import utcnow_iso
from ..services.signals_store import SignalsStore


CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "importance": {"type": "number"},
                    "headline": {"type": "string"},
                    "summary": {"type": "string"},
                    "link": {"type": "string"},
                    "tickers": {"type": "array", "items": {"type": "string"}},
                    "recipients": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["category", "importance", "headline", "summary", "link", "recipients"],
            },
        }
    },
    "required": ["items"],
}


class SignalsAgent(BaseAgent):
    """
    Continuously pulls a small set of RSS feeds and broadcasts high-signal items.

    Goal: give all agents low-latency awareness of the outside world without everyone
    individually scraping the web.
    """

    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self.signals_store: SignalsStore = ctx.services["signals_store"]
        self._seen: set[str] = set()
        self._last_fetch_ts: Optional[str] = None

    async def step(self) -> None:
        feeds_cfg = self.ctx.services.get("signals_config") or {}
        feeds = feeds_cfg.get("feeds", []) or []
        if not feeds:
            await self.log_state({"state": "idle", "reason": "no feeds configured"})
            return

        # Fetch RSS in a thread (feedparser is sync).
        raw_items: List[Dict[str, Any]] = []
        for f in feeds:
            url = f.get("url")
            if not url:
                continue
            parsed = await asyncio.to_thread(feedparser.parse, url)
            for e in (parsed.entries or [])[:10]:
                link = (getattr(e, "link", None) or getattr(e, "id", None) or "").strip()
                title = (getattr(e, "title", None) or "").strip()
                summary = (getattr(e, "summary", None) or getattr(e, "description", None) or "").strip()
                if not link or not title:
                    continue
                if link in self._seen:
                    continue
                self._seen.add(link)
                raw_items.append({
                    "category": f.get("category", "general"),
                    "source": f.get("name", "rss"),
                    "title": title,
                    "summary": summary[:800],
                    "link": link,
                })

        # Avoid unbounded growth
        if len(self._seen) > 10_000:
            self._seen = set(list(self._seen)[-5_000:])

        if not raw_items:
            await self.log_state({"state": "monitoring", "new_items": 0, "seen": len(self._seen)})
            return

        # Classify/summarise via tiny model (1 call per batch).
        model = self._resolve_model(self.cfg.model)
        prompt = (
            "You are an 'intel' agent for a multi-strategy trading firm.\n"
            "You will receive RSS headlines + short summaries.\n"
            "Return JSON with an array 'items'. For each item, classify the most relevant category "
            "(macro|equities|crypto|commodities|fx|rates|general), assign importance 0..1, "
            "write a 1-2 sentence trading-relevant summary, and choose recipients (agent ids) "
            "from: ceo,cro,quant,macro,event,crypto,vol,commodities,fx,infra,ops,signals.\n"
            "If unsure, send to ['ceo'].\n\n"
            f"RAW_ITEMS: {json.dumps(raw_items)[:12000]}"
        )
        resp = await self.ctx.llm.chat(
            model=model,
            messages=[{"role": "system", "content": "Output ONLY JSON."}, {"role": "user", "content": prompt}],
            format=CLASSIFY_SCHEMA,
        )
        content = (resp.get("message", {}) or {}).get("content", "{}")
        try:
            data = json.loads(content)
        except Exception:
            data = {"items": []}

        published = 0
        for it in (data.get("items") or [])[:25]:
            try:
                cat = str(it.get("category") or "general")
                headline = str(it.get("headline") or "")
                link = str(it.get("link") or "")
                summary = str(it.get("summary") or "")
                recipients = list(it.get("recipients") or ["ceo"])
                importance = float(it.get("importance") or 0.3)
                tickers = list(it.get("tickers") or [])
            except Exception:
                continue

            meta = {
                "category": cat,
                "importance": importance,
                "headline": headline,
                "link": link,
                "summary": summary,
                "tickers": tickers,
                "recipients": recipients,
                "raw_model": self.cfg.model,
            }
            # store
            await self.signals_store.add_item(category=cat, source="signals_agent", title=headline, link=link, summary=summary, meta=meta, ts=utcnow_iso())
            # publish to firmwide signals channel
            await self.ctx.bus.publish("signals", self.agent_id, headline, meta=meta)
            # DM recipients (best-effort: just publish to DM channels)
            for rid in recipients:
                if rid == self.agent_id:
                    continue
                # Use dm channel naming convention; doesn't require room creation to work
                dm = self.ctx.services.get("chat")
                if dm:
                    try:
                        room_id = dm_room(self.agent_id, rid)  # type: ignore[name-defined]
                    except Exception:
                        room_id = f"dm:{min(self.agent_id, rid)}:{max(self.agent_id, rid)}"
                    await self.ctx.bus.publish(room_id, self.agent_id, headline, meta=meta)
            published += 1

        # LLM trace (full, not truncated)
        await self.ctx.bus.publish("llm_trace", self.agent_id, "SIGNALS_CLASSIFY_BATCH", meta={
            "prompt": prompt,
            "raw_output": content,
            "raw_items": raw_items,
        })

        await self.log_state({
            "state": "published",
            "new_items": len(raw_items),
            "published": published,
            "seen": len(self._seen),
        })

    def _resolve_model(self, key_or_name: str) -> str:
        models = self.ctx.settings.llm.get("models", {})
        return models.get(key_or_name, key_or_name)


# Local import to avoid circular when ChatService not loaded in some tests
def dm_room(a: str, b: str) -> str:
    x, y = sorted([a, b])
    return f"dm:{x}:{y}"
