"""Algo curve snapshots (outrights + derived consecutive spreads)."""

from __future__ import annotations

import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG, CurvesConfig


def load_curve_as_of(
    client: Client,
    as_of: pd.Timestamp | None,
    cfg: CurvesConfig | None = None,
) -> tuple[pd.Timestamp | None, pd.Series | None, pd.Series | None]:
    """Last curve snapshot at or before ``as_of``.

    Returns ``(curve_timestamp, outrights, spreads)`` where
    ``spreads[m] = outright[m] - outright[m+1]``.
    """
    if as_of is None:
        return None, None, None

    c = cfg or CONFIG.curves
    row = client.query_df(
        f"""
        SELECT timestamp, curve
        FROM {c.table}
        WHERE product = '{c.product}'
          AND timestamp <= '{as_of}'
        ORDER BY timestamp DESC
        LIMIT 1
        """
    )
    if row.empty:
        return None, None, None

    ts = pd.Timestamp(row["timestamp"].iloc[0])
    outrights = pd.Series(row["curve"].iloc[0], dtype=float)
    outrights.index = pd.to_datetime(outrights.index)
    outrights = outrights.sort_index()
    spreads = outrights.diff(-1).iloc[:-1]
    return ts, outrights, spreads


def curve_move(
    client: Client,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cfg: CurvesConfig | None = None,
) -> pd.Series:
    """Spread-curve move from ``start`` to ``end`` (end − start)."""
    _, _, s0 = load_curve_as_of(client, start, cfg)
    _, _, s1 = load_curve_as_of(client, end, cfg)
    if s0 is None or s1 is None:
        return pd.Series(dtype=float)
    move = (s1 - s0).dropna()
    move.index = pd.DatetimeIndex(move.index).normalize()
    return move
