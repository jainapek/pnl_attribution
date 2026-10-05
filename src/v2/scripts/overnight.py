"""Inventory mark outside the cycle window (calculated overnight).

Not a plug. Three pieces, all leftover lots × curve move × size:

    close → BOD          yesterday EOD qty, frozen (library overnight)
    BOD → first cycle    first-cycle ``position_before``
    last-cycle end → library EOD snapshot
      (our last cycle already marks to ``next_cycle_start`` ≈ 23:59 UTC;
       this is only the clock gap vs the library's last 10s bar)
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG, AppConfig
from .day_attribution import _naive_utc
from .intentions import cycles_for_day, load_intentions_for_day
from .market_cache import load_curve_asof_many
from .spreads import position_map_to_series, series_from_row
from .stages import attach_executed, build_stages_from_row
from .trades import executed_lots_series, load_trades_for_book_day
from .transfers import assign_transfers_to_cycles, load_transfers_for_book_day

BRENT_PRODUCT_ID = 2


def outrights_map_to_spreads(pos_map) -> pd.Series:
    """``algo.position`` month outrights → consecutive 1m spread lots (cumsum)."""
    if not pos_map:
        return pd.Series(dtype=float)
    s = pd.Series({pd.Timestamp(k): float(v) for k, v in pos_map.items()})
    s.index = pd.DatetimeIndex(s.index).normalize()
    s = s.sort_index()
    if s.empty:
        return s
    months = pd.date_range(s.index.min(), s.index.max(), freq="MS")
    s = s.reindex(months, fill_value=0.0)
    spreads = s.cumsum()
    if len(spreads) > 1:
        spreads = spreads.iloc[:-1]
    return spreads


def _is_real_ts(ts) -> bool:
    """ClickHouse ``max(timestamp)`` on no rows is epoch 1970."""
    if ts is None or pd.isna(ts):
        return False
    return pd.Timestamp(ts).year >= 1990


def _spread_pnl(qty: pd.Series, s0: pd.Series | None, s1: pd.Series | None, size: int) -> float:
    if qty is None or len(qty) == 0 or s0 is None or s1 is None or s0.empty or s1.empty:
        return 0.0
    s0 = s0.copy()
    s1 = s1.copy()
    s0.index = pd.DatetimeIndex(s0.index).normalize()
    s1.index = pd.DatetimeIndex(s1.index).normalize()
    qty = qty.copy()
    qty.index = pd.DatetimeIndex(qty.index).normalize()
    idx = s0.index.intersection(s1.index)
    if len(idx) == 0:
        return 0.0
    q = qty.reindex(idx).fillna(0.0)
    m0 = s0.reindex(idx)
    m1 = s1.reindex(idx)
    ok = m0.notna() & m1.notna()
    if not ok.any():
        return 0.0
    return round(float((q[ok] * size * (m1[ok] - m0[ok])).sum()), 2)


def _last_curve_on_london_date(client: Client, d: date, cfg: AppConfig) -> pd.Timestamp | None:
    raw = client.query_df(
        f"""
        SELECT max(timestamp) AS t, count() AS n
        FROM {cfg.curves.table}
        WHERE product = '{cfg.curves.product}'
          AND toDate(toTimeZone(timestamp, 'Europe/London')) = toDate('{d}')
        """
    )
    if raw.empty or int(raw.iloc[0]["n"]) == 0 or not _is_real_ts(raw.iloc[0]["t"]):
        return None
    return _naive_utc(raw.iloc[0]["t"])


def _first_curve_on_london_date(client: Client, d: date, cfg: AppConfig) -> pd.Timestamp | None:
    raw = client.query_df(
        f"""
        SELECT min(timestamp) AS t, count() AS n
        FROM {cfg.curves.table}
        WHERE product = '{cfg.curves.product}'
          AND toDate(toTimeZone(timestamp, 'Europe/London')) = toDate('{d}')
        """
    )
    if raw.empty or int(raw.iloc[0]["n"]) == 0 or not _is_real_ts(raw.iloc[0]["t"]):
        return None
    return _naive_utc(raw.iloc[0]["t"])


def _last_curve_before(
    client: Client, before: pd.Timestamp, cfg: AppConfig
) -> pd.Timestamp | None:
    """Last published curve strictly before ``before`` (skips weekends with no curve)."""
    from .market_cache import _ch_dt64_utc

    raw = client.query_df(
        f"""
        SELECT max(timestamp) AS t, count() AS n
        FROM {cfg.curves.table}
        WHERE product = '{cfg.curves.product}'
          AND timestamp < {_ch_dt64_utc(before)}
        """
    )
    if raw.empty or int(raw.iloc[0]["n"]) == 0 or not _is_real_ts(raw.iloc[0]["t"]):
        return None
    return _naive_utc(raw.iloc[0]["t"])


def _last_position_before(
    client: Client, book_id: int, before: pd.Timestamp
) -> tuple[pd.Timestamp, pd.Series] | None:
    """Last ``algo.position`` snapshot strictly before ``before`` (UTC-naive)."""
    from .market_cache import _ch_dt64_utc

    raw = client.query_df(
        f"""
        SELECT timestamp, positions
        FROM algo.position
        WHERE book_id = {book_id}
          AND product_id = {BRENT_PRODUCT_ID}
          AND timestamp < {_ch_dt64_utc(before)}
        ORDER BY timestamp DESC
        LIMIT 1
        """
    )
    if raw.empty:
        return None
    ts = _naive_utc(raw.iloc[0]["timestamp"])
    return ts, outrights_map_to_spreads(raw.iloc[0]["positions"])


def _library_eod_ts(client: Client, asof_date: date, book: str) -> pd.Timestamp | None:
    raw = client.query_df(
        f"""
        SELECT timestamp
        FROM algo.nexus_pnl_attribution
        WHERE book_name = '{book}'
          AND tenor = 'total'
          AND toDate(timestamp) = toDate('{asof_date}')
        ORDER BY timestamp DESC
        LIMIT 1
        """
    )
    if raw is None or raw.empty:
        return None
    return _naive_utc(raw.iloc[0]["timestamp"])


def _last_cycle_leftover(
    client: Client,
    asof_date: date,
    book: str,
    last_row: pd.Series,
    t_last: pd.Timestamp,
    t_end: pd.Timestamp,
) -> pd.Series:
    trades = load_trades_for_book_day(client, book, asof_date)
    if trades is None or trades.empty:
        fills = pd.Series(dtype=float)
    else:
        trades = trades.copy()
        trades["transaction_timestamp"] = [
            _naive_utc(t) for t in trades["transaction_timestamp"]
        ]
        gap = trades[
            (trades["transaction_timestamp"] > t_last)
            & (trades["transaction_timestamp"] <= t_end)
        ]
        fills = executed_lots_series(gap)
    stages = attach_executed(build_stages_from_row(last_row), fills)
    leftover = (
        series_from_row(stages["transfer_pred"]["held"])
        .add(series_from_row(stages["pca"]["held"]), fill_value=0.0)
        .add(series_from_row(stages["routing"]["held"]), fill_value=0.0)
        .add(series_from_row(stages["executed"]["unexecuted"]), fill_value=0.0)
    )
    leftover = leftover[leftover.abs() > 1e-12]
    return leftover


def compute_outside_cycle_mr(
    client: Client,
    asof_date: date,
    book: str,
    cfg: AppConfig | None = None,
) -> dict[str, float]:
    """Calculated overnight: inventory MR with no cycle running.

    Returns ``close_to_bod``, ``bod_to_first_cycle``, ``last_end_to_lib_eod``,
    ``overnight`` (sum). Zeros if a piece cannot be marked.
    """
    c = cfg or CONFIG
    size = c.contract.size
    book_id = c.books.get(book)
    empty = {
        "close_to_bod": 0.0,
        "bod_to_first_cycle": 0.0,
        "last_end_to_lib_eod": 0.0,
        "overnight": 0.0,
    }
    if book_id is None:
        return empty

    bod_ts = _first_curve_on_london_date(client, asof_date, c)
    prev_curve_ts = _last_curve_before(client, bod_ts, c) if bod_ts is not None else None
    prev_pos = (
        _last_position_before(client, book_id, bod_ts) if bod_ts is not None else None
    )
    prev_qty = prev_pos[1] if prev_pos is not None else pd.Series(dtype=float)
    prev_eod_ts = prev_curve_ts

    cycles = cycles_for_day(
        load_intentions_for_day(client, asof_date, book, c.intentions), asof_date
    )
    transfers = load_transfers_for_book_day(client, book, asof_date)
    if not transfers.empty:
        transfers = transfers.copy()
        transfers["timestamp"] = [_naive_utc(t) for t in transfers["timestamp"]]
    if not cycles.empty:
        cycles, _ = assign_transfers_to_cycles(cycles, transfers)
    t0 = _naive_utc(cycles.iloc[0]["timestamp"]) if not cycles.empty else None
    t_last = _naive_utc(cycles.iloc[-1]["timestamp"]) if not cycles.empty else None
    t_end = _naive_utc(cycles.iloc[-1]["next_cycle_start"]) if not cycles.empty else None
    p0 = (
        position_map_to_series(cycles.iloc[0].get("position_before") or {})
        if not cycles.empty
        else pd.Series(dtype=float)
    )
    leftover = (
        _last_cycle_leftover(client, asof_date, book, cycles.iloc[-1], t_last, t_end)
        if not cycles.empty
        else pd.Series(dtype=float)
    )
    lib_eod = _library_eod_ts(client, asof_date, book)
    if lib_eod is None:
        lib_eod = _last_curve_on_london_date(client, asof_date, c)

    times = [t for t in (prev_eod_ts, bod_ts, t0, t_end, lib_eod) if t is not None]
    if not times:
        return empty
    curves = load_curve_asof_many(client, times, c.curves)

    close_to_bod = 0.0
    if prev_eod_ts is not None and bod_ts is not None:
        close_to_bod = _spread_pnl(
            prev_qty, curves.asof(prev_eod_ts), curves.asof(bod_ts), size
        )

    bod_to_first = 0.0
    if bod_ts is not None and t0 is not None:
        bod_to_first = _spread_pnl(p0, curves.asof(bod_ts), curves.asof(t0), size)

    last_to_lib = 0.0
    if t_end is not None and lib_eod is not None:
        last_to_lib = _spread_pnl(
            leftover, curves.asof(t_end), curves.asof(lib_eod), size
        )

    overnight = round(close_to_bod + bod_to_first + last_to_lib, 2)
    return {
        "close_to_bod": close_to_bod,
        "bod_to_first_cycle": bod_to_first,
        "last_end_to_lib_eod": last_to_lib,
        "overnight": overnight,
    }


__all__ = [
    "compute_outside_cycle_mr",
    "outrights_map_to_spreads",
]
