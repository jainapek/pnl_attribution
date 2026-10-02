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
    """Signed MTM vs cycle-start mark: Σ pos × size × (later − start).

    ``curve_move`` is ``spread_later − spread_start`` (or fill − start).
    Positive = gain on a long (+lots), same sign as library m2m.
    """
    c = cfg or CONFIG
    pos = series_from_row(pos_df).reindex(curve_move.index).fillna(0.0)
    move = curve_move.reindex(pos.index).fillna(0.0)
    return round(float((pos * c.contract.size * move).sum()), 2)


def executed_fill_market_risk_by_instrument(
    trades: pd.DataFrame,
    cycle_start: pd.Timestamp,
    curve_asof,
    cfg: AppConfig | None = None,
) -> pd.Series:
    """Same $ as ``executed_fill_market_risk``, keyed by ``instrument_key``.

    Packs stay under the raw nexus_trades contract string; 1m legs are
    expanded only for marking, then summed back onto that instrument.
    """
    from .trades import iter_fill_legs_priced

    c = cfg or CONFIG
    if trades is None or len(trades) == 0:
        return pd.Series(dtype=float)
    s0 = curve_asof.asof(cycle_start)
    if s0 is None or s0.empty:
        return pd.Series(dtype=float)
    s0 = s0.copy()
    s0.index = pd.DatetimeIndex(s0.index).normalize()

    acc: dict[str, float] = {}
    for fill_ts, tenor, lots, _price, ikey in iter_fill_legs_priced(trades):
        s1 = curve_asof.asof(fill_ts)
        if s1 is None or s1.empty:
            continue
        s1 = s1.copy()
        s1.index = pd.DatetimeIndex(s1.index).normalize()
        v0 = float(s0.reindex([tenor]).fillna(0.0).iloc[0])
        v1 = float(s1.reindex([tenor]).fillna(0.0).iloc[0])
        acc[ikey] = acc.get(ikey, 0.0) + (-lots) * c.contract.size * (v1 - v0)
    if not acc:
        return pd.Series(dtype=float)
    out = pd.Series(acc, dtype=float).sort_values(key=lambda s: s.abs(), ascending=False)
    return out.round(2)


def executed_fill_market_risk(
    trades: pd.DataFrame,
    cycle_start: pd.Timestamp,
    curve_asof,
    cfg: AppConfig | None = None,
) -> float:
    """MTM on risk held while waiting for fills: cycle-start → fill.

    Fills *close* book risk, so inventory during the wait has the **opposite**
    sign to the fill (long risk waiting to sell, etc.):

        Σ (−fill_lots) × size × (mark_fill − mark_start)

    Buy fill → +lots → inventory −lots while waiting. Positive = gain on that
    residual risk (library m2m sign).
    """
    by_inst = executed_fill_market_risk_by_instrument(
        trades, cycle_start, curve_asof, cfg
    )
    if by_inst.empty:
        return 0.0
    return round(float(by_inst.sum()), 2)

def mark_to_exec(
    trades: pd.DataFrame,
    curve_asof,
    cfg: AppConfig | None = None,
) -> float:
    """Fill price vs curve mark at fill: Σ lots × size × (mark − price/n).

    Pack prices are sums of 1m spreads, so each consecutive leg is marked
    against ``price / n_legs``. Buy → +lots: positive = bought below mark
    (good exec). Same PnL sign as library m2m (+ = gain).
    """
    from .trades import iter_fill_legs_priced

    c = cfg or CONFIG
    if trades is None or len(trades) == 0:
        return 0.0

    total = 0.0
    for fill_ts, tenor, lots, price_1m, _ikey in iter_fill_legs_priced(trades):
        s1 = curve_asof.asof(fill_ts)
        if s1 is None or s1.empty:
            continue
        s1 = s1.copy()
        s1.index = pd.DatetimeIndex(s1.index).normalize()
        mark = float(s1.reindex([tenor]).fillna(0.0).iloc[0])
        total += lots * c.contract.size * (mark - price_1m)
    return round(total, 2)


def transfer_vs_mark(
    transfers: pd.DataFrame,
    curve_asof=None,
    cfg: AppConfig | None = None,
) -> float:
    """Transfer deal price vs Nexus transfer mark: Σ lots × size × (mark − price)/n.

    Transfers are booked at the row ``mark`` (curve used at booking), so this
    is essentially −edge and should be small. Pack prices/marks are sums of
    1m legs → per-leg ``/ n``. ``curve_asof`` is unused (kept for call-site
    symmetry with ``mark_to_exec``); pure-mark / skew vs ``algo.curves`` is
    a separate split later.
    """
    from .trades import pack_to_consecutive_lots
    from .transfers import book_signed_qty

    c = cfg or CONFIG
    if transfers is None or len(transfers) == 0:
        return 0.0

    total = 0.0
    for row in transfers.itertuples(index=False):
        start = pd.Timestamp(getattr(row, "start_tenor")).normalize()
        end = pd.Timestamp(getattr(row, "end_tenor")).normalize()
        signed = book_signed_qty(getattr(row, "side"), getattr(row, "quantity"))
        if signed == 0.0:
            continue
        legs = pack_to_consecutive_lots(start, end, signed)
        n = len(legs)
        if n == 0:
            continue
        price_1m = float(getattr(row, "price")) / n
        mark_1m = float(getattr(row, "mark")) / n
        for _tenor, lots in legs.items():
            total += float(lots) * c.contract.size * (mark_1m - price_1m)
    return round(total, 2)


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
