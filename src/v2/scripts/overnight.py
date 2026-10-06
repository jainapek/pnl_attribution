"""BOD position helpers (shared with the running ledger)."""

from __future__ import annotations

from datetime import date

import pandas as pd
from clickhouse_connect.driver import Client

from .config import AppConfig
from .market_cache import _ch_dt64_utc
from .transfers import naive_utc

BRENT_PRODUCT_ID = 2


def _is_real_ts(ts) -> bool:
    if ts is None or pd.isna(ts):
        return False
    return pd.Timestamp(ts).year >= 1990


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


def _first_curve_on_london_date(
    client: Client, d: date, cfg: AppConfig
) -> pd.Timestamp | None:
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
    return naive_utc(raw.iloc[0]["t"])


def _last_position_before(
    client: Client, book_id: int, before: pd.Timestamp
) -> tuple[pd.Timestamp, pd.Series] | None:
    """Last ``algo.position`` snapshot strictly before ``before`` (UTC-naive)."""
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
    ts = naive_utc(raw.iloc[0]["timestamp"])
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
    return naive_utc(raw.iloc[0]["timestamp"])


__all__ = [
    "BRENT_PRODUCT_ID",
    "outrights_map_to_spreads",
    "_first_curve_on_london_date",
    "_last_position_before",
    "_library_eod_ts",
]
