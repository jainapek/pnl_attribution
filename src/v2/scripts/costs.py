"""Exec cost and market-risk primitives."""

from __future__ import annotations

import pandas as pd

from .config import CONFIG, AppConfig
from .spreads import series_from_row


def _scalar(val) -> float:
    """Coerce a loc result that may be a 1-element Series/DataFrame to float."""
    if isinstance(val, pd.DataFrame):
        val = val.iloc[-1]
    if isinstance(val, pd.Series):
        val = val.iloc[-1] if len(val) > 1 else val.item()
    return float(val)


def l1_bid_ask_on_tenors(
    bid_ask: pd.DataFrame,
    tenors: pd.DatetimeIndex,
) -> tuple[pd.Series, pd.Series]:
    """L1 bid/ask for consecutive-month spreads keyed by front month."""
    bids: dict[pd.Timestamp, float] = {}
    asks: dict[pd.Timestamp, float] = {}
    for t in tenors:
        start = pd.Timestamp(t).normalize()
        end = (start + pd.DateOffset(months=1)).normalize()
        key = (start, end)
        if bid_ask.empty or key not in bid_ask.index:
            bids[start] = float("nan")
            asks[start] = float("nan")
            continue
        row = bid_ask.loc[key]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[-1]
        bids[start] = _scalar(row["bid"])
        asks[start] = _scalar(row["ask"])
    return pd.Series(bids), pd.Series(asks)


def half_spread_on_tenors(
    bid_ask: pd.DataFrame,
    tenors: pd.DatetimeIndex,
) -> pd.Series:
    """Half bid/ask (legacy helper)."""
    bid, ask = l1_bid_ask_on_tenors(bid_ask, tenors)
    return (ask - bid) / 2.0


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
    """Taker cashflow + exchange on a signed position.

    Per tenor (cross the spread), **cashflow** (+ = money in):
        pos > 0 (buy):  −ask × pos × size   (spend)
        pos < 0 (sell): −bid × pos × size   (= +bid × |pos| × size, receive)
    Clearing is always a cost:
        − clearing_rate × |pos| × size

    Missing L1 quotes contribute 0 for that tenor's touch leg.
    """
    c = cfg or CONFIG
    pos = series_from_row(pos_df)
    if pos.empty:
        return {
            "touch_cash": 0.0,
            "exchange_cost": 0.0,
            "exec_cost": 0.0,
            "abs_lots": 0.0,
        }

    bid, ask = l1_bid_ask_on_tenors(bid_ask, pos.index)
    bid = bid.reindex(pos.index)
    ask = ask.reindex(pos.index)

    touch = pd.Series(index=pos.index, dtype=float)
    buy = pos > 0
    sell = pos < 0
    touch.loc[buy] = ask.loc[buy]
    touch.loc[sell] = bid.loc[sell]

    # cashflow: −pos×touch×size  (buy→negative, sell→positive)
    touch_cash = float((-pos * c.contract.size * touch.fillna(0.0)).sum())
    exchange_cost = float(
        -(pos.abs() * c.contract.size * c.contract.clearing_rate).sum()
    )
    return {
        "touch_cash": round(touch_cash, 2),
        "exchange_cost": round(exchange_cost, 2),
        "exec_cost": round(touch_cash + exchange_cost, 2),
        "abs_lots": float(pos.abs().sum()),
    }
