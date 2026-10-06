"""Curve asof helpers for sleeve MR (chunked ClickHouse pulls)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG, CurvesConfig


def _naive(ts: pd.Timestamp) -> pd.Timestamp:
    """Normalise to tz-naive UTC wall time for ClickHouse ``DateTime64(..., 'UTC')``."""
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


def _ch_dt64_utc(ts: pd.Timestamp) -> str:
    """SQL literal: UTC-naive timestamp as DateTime64(3, 'UTC')."""
    t = _naive(ts)
    return f"toDateTime64('{t}', 3, 'UTC')"


def _curve_map_to_spreads(curve_map) -> pd.Series:
    outrights = pd.Series(curve_map, dtype=float)
    outrights.index = pd.to_datetime(outrights.index).normalize()
    outrights = outrights.sort_index()
    if len(outrights) <= 1:
        return pd.Series(dtype=float)
    spreads = outrights.diff(-1).iloc[:-1]
    spreads.index = pd.DatetimeIndex(spreads.index).normalize()
    return spreads


@dataclass
class CurveAsofCache:
    """Spread curves keyed by requested asof timestamps."""

    spreads_by_asof: dict[pd.Timestamp, pd.Series]
    timeline_ts: np.ndarray | None = None
    timeline_spreads: list[pd.Series] | None = None

    def asof(self, t: pd.Timestamp) -> pd.Series | None:
        key = _naive(t)
        hit = self.spreads_by_asof.get(key)
        if hit is not None:
            return hit
        if self.timeline_ts is None or self.timeline_spreads is None:
            return None
        if len(self.timeline_ts) == 0:
            return None
        i = int(np.searchsorted(self.timeline_ts, np.datetime64(key), side="right") - 1)
        if i < 0:
            return None
        return self.timeline_spreads[i]

    def move(self, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
        """``spread_later − spread_start`` (m2m gain on a long)."""
        s0 = self.asof(start)
        s1 = self.asof(end)
        if s0 is None or s1 is None:
            return pd.Series(dtype=float)
        move = (s1 - s0).dropna()
        move.index = pd.DatetimeIndex(move.index).normalize()
        return move


def load_curve_asof_many(
    client: Client,
    timestamps: list[pd.Timestamp],
    cfg: CurvesConfig | None = None,
    *,
    chunk_size: int = 400,
) -> CurveAsofCache:
    """ASOF join for requested timestamps (chunked so the SQL stays small)."""
    c = cfg or CONFIG.curves
    times = sorted({_naive(t) for t in timestamps})
    if not times:
        return CurveAsofCache({})

    out: dict[pd.Timestamp, pd.Series] = {}
    for i in range(0, len(times), chunk_size):
        chunk = times[i : i + chunk_size]
        arr = ", ".join(_ch_dt64_utc(t) for t in chunk)
        raw = client.query_df(
            f"""
            WITH times AS (
                SELECT arrayJoin([{arr}]) AS t, toUInt8(1) AS k
            )
            SELECT
                t.t AS as_of,
                c.curve
            FROM times AS t
            ASOF LEFT JOIN (
                SELECT timestamp, curve, toUInt8(1) AS k
                FROM {c.table}
                WHERE product = '{c.product}'
                  AND timestamp >= {_ch_dt64_utc(chunk[0])} - INTERVAL 6 HOUR
                  AND timestamp <= {_ch_dt64_utc(chunk[-1])}
            ) AS c ON t.k = c.k AND t.t >= c.timestamp
            """
        )
        for row in raw.itertuples(index=False):
            if row.curve is None:
                continue
            out[_naive(row.as_of)] = _curve_map_to_spreads(row.curve)
    return CurveAsofCache(out)


__all__ = [
    "CurveAsofCache",
    "_ch_dt64_utc",
    "_naive",
    "load_curve_asof_many",
]
