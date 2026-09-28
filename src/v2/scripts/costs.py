"""Exec cost and market-risk primitives."""

from __future__ import annotations

import pandas as pd

from .config import CONFIG, AppConfig
from .spreads import series_from_row


def half_spread_on_tenors(
    bid_ask: pd.DataFrame,
    tenors: pd.DatetimeIndex,
) -> pd.Series:
    """Half bid/ask for consecutive-month spreads keyed by front month."""
    vals: dict[pd.Timestamp, float] = {}
    for t in tenors:
        start = pd.Timestamp(t).normalize()
        end = (start + pd.DateOffset(months=1)).normalize()
        key = (start, end)
        if not bid_ask.empty and key in bid_ask.index:
            vals[start] = float(bid_ask.loc[key, "spread"]) / 2.0
        else:
            vals[start] = float("nan")
    return pd.Series(vals)


def abs_lots(pos_df: pd.DataFrame) -> float:
    """Σ |lots| across tenors."""
    return float(series_from_row(pos_df).abs().sum())


def market_risk(
    pos_df: pd.DataFrame,
    curve_move: pd.Series,
    cfg: AppConfig | None = None,
) -> float:
    """Signed MTM on held: Σ pos × size × Δspread (+ = gain)."""
    c = cfg or CONFIG
    pos = series_from_row(pos_df).reindex(curve_move.index).fillna(0.0)
    move = curve_move.reindex(pos.index).fillna(0.0)
    return round(float((pos * c.contract.size * move).sum()), 2)


def exec_cost(
    pos_df: pd.DataFrame,
    bid_ask: pd.DataFrame,
    cfg: AppConfig | None = None,
) -> dict[str, float]:
    """Half-spread + exchange cost on abs lots."""
    c = cfg or CONFIG
    pos = series_from_row(pos_df)
    half = half_spread_on_tenors(bid_ask, pos.index).reindex(pos.index)
    half_spread_cost = float(
        (pos.abs() * c.contract.size * half.fillna(0.0)).sum()
    )
    exchange_cost = float(
        (pos.abs() * c.contract.size * c.contract.clearing_rate).sum()
    )
    return {
        "half_spread_cost": round(half_spread_cost, 2),
        "exchange_cost": round(exchange_cost, 2),
        "exec_cost": round(half_spread_cost + exchange_cost, 2),
        "abs_lots": float(pos.abs().sum()),
    }
