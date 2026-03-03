from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import yaml

@dataclass(frozen=True)
class Settings:
    firm: Dict[str, Any]
    llm: Dict[str, Any]
    data: Dict[str, Any]
    risk: Dict[str, Any]
    execution: Dict[str, Any]
    storage: Dict[str, Any]
    redis: Dict[str, Any]

def load_yaml(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Missing config file: {p}")
    return yaml.safe_load(p.read_text())

def load_settings(firm_path: str = "configs/firm.yaml") -> Settings:
    raw = load_yaml(firm_path)
    return Settings(
        firm=raw.get("firm", {}),
        llm=raw.get("llm", {}),
        data=raw.get("data", {}),
        risk=raw.get("risk", {}),
        execution=raw.get("execution", {}),
        storage=raw.get("storage", {}),
        redis=raw.get("redis", {}),
    )

def load_agents(path: str = "configs/agents.yaml") -> List[Dict[str, Any]]:
    raw = load_yaml(path)
    agents = raw.get("agents", [])
    if not isinstance(agents, list):
        raise ValueError("agents.yaml must contain a top-level list 'agents'")
    return agents

def save_agents(agents: List[Dict[str, Any]], path: str = "configs/agents.yaml") -> None:
    p = Path(path)
    p.write_text(yaml.safe_dump({"agents": agents}, sort_keys=False))
