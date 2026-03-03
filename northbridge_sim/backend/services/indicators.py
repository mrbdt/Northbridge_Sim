from __future__ import annotations

from typing import Iterable, List, Optional, Tuple
import math

import numpy as np


def _as_prices(prices: Iterable[float]) -> np.ndarray:
    arr = np.array([float(p) for p in prices if p is not None], dtype=float)
    return arr


def pct_return(prices: Iterable[float]) -> float:
    arr = _as_prices(prices)
    if arr.size < 2:
        return 0.0
    return float(arr[-1] / arr[0] - 1.0)


def log_returns(prices: Iterable[float]) -> np.ndarray:
    arr = _as_prices(prices)
    if arr.size < 2:
        return np.array([], dtype=float)
    return np.diff(np.log(arr))


def realized_vol(prices: Iterable[float]) -> float:
    r = log_returns(prices)
    if r.size < 2:
        return 0.0
    return float(np.std(r, ddof=1))


def zscore(x: float, mu: float, sigma: float) -> float:
    if sigma <= 1e-12:
        return 0.0
    return float((x - mu) / sigma)


def simple_momentum_signal(prices: List[float], lookback: int = 30) -> Tuple[float, float]:
    """Returns (momentum, vol) using last `lookback` samples."""
    if len(prices) < 5:
        return 0.0, 0.0
    tail = prices[-lookback:] if len(prices) >= lookback else prices
    mom = pct_return(tail)
    vol = realized_vol(tail)
    return mom, vol
