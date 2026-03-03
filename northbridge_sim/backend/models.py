from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]

class TradeIntent(BaseModel):
    symbol: str
    venue: str
    side: Side
    qty: float = Field(..., description="Positive quantity; side indicates buy/sell.")
    order_type: OrderType = "market"
    limit_price: Optional[float] = None
    time_horizon_minutes: int = 60
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str
    risk_notes: str = ""
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    tags: List[str] = Field(default_factory=list)

class RiskDecision(BaseModel):
    status: Literal["ok", "block", "resize"]
    reason: str
    resized_qty: Optional[float] = None

class Order(BaseModel):
    order_id: str
    ts: str
    agent_id: str
    symbol: str
    venue: str
    side: Side
    qty: float
    order_type: OrderType
    limit_price: Optional[float] = None
    status: Literal["new","filled","rejected","cancelled"] = "new"
    meta: Dict[str, Any] = Field(default_factory=dict)

class Fill(BaseModel):
    fill_id: str
    ts: str
    order_id: str
    symbol: str
    venue: str
    side: Side
    qty: float
    price: float
    fees: float
    meta: Dict[str, Any] = Field(default_factory=dict)

class PortfolioSnapshot(BaseModel):
    ts: str
    nav: float
    cash: Dict[str, float]
    positions: List[Dict[str, Any]]
    last_prices: Dict[str, Any]
    gross_exposure: float
    net_exposure: float
    leverage: float
    drawdown: float
