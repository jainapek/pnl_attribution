"""Library EOD PnL from ``algo.nexus_pnl_attribution``."""

from __future__ import annotations

from datetime import date

import pandas as pd
from clickhouse_connect.driver import Client


def load_library_eod_pnl(
    client: Client,
    asof_date: date,
    book: str,
) -> dict[str, float] | None:
    """Last ``tenor='total'`` snapshot for one book / day.

    ``gross = overnight + m2m_pnl + trade_pnl``.
    """
    raw = client.query_df(
        f"""
        SELECT overnight, m2m_pnl, trade_pnl, gross
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
    row = raw.iloc[0]
    return {
        "overnight": float(row["overnight"]),
        "m2m_pnl": float(row["m2m_pnl"]),
        "trade_pnl": float(row["trade_pnl"]),
        "gross": float(row["gross"]),
    }


__all__ = ["load_library_eod_pnl"]
