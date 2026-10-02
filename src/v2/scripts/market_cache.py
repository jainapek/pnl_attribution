"""Day-scoped market data caches (curves + spread quotes).

Prefetch once per book/day so cycle attribution avoids N ClickHouse round-trips.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

import numpy as np
import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG, AppConfig, CurvesConfig, QuotesConfig


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


def _parse_1m_strips(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    parts = df["strip_name"].astype(str).str.split("/", expand=True)
    out = df.copy()
    out["start_tenor"] = pd.to_datetime("01" + parts[0], format="%d%b%y").dt.normalize()
    out["end_tenor"] = pd.to_datetime("01" + parts[1], format="%d%b%y").dt.normalize()
    return out


def _quotes_sql_filters(q: QuotesConfig) -> str:
    return f"""
      hub_alias = '{q.hub_alias}'
      AND security_sub_type_name = '{q.security_sub_type}'
      AND isNotNull(bid_price_1)
      AND isNotNull(ask_price_1)
      AND (bid_quantity_1 + bid_implied_quantity_1) > 0
      AND (ask_quantity_1 + ask_implied_quantity_1) > 0
      AND length(splitByChar('/', strip_name)) = 2
      AND toFloat64(ask_price_1) > toFloat64(bid_price_1)
      AND dateDiff(
            'month',
            toStartOfMonth(parseDateTimeBestEffort(concat('1 ', splitByChar('/', strip_name)[1]))),
            toStartOfMonth(parseDateTimeBestEffort(concat('1 ', splitByChar('/', strip_name)[2])))
          ) = 1
    """


@dataclass
class QuoteMinuteCache:
    """1m calendar SPR minute bars for fast asof half-spread lookup."""

    lookback: pd.Timedelta
    # (minutes, bid, ask, spread, quote_ts) numpy columns per tenor key
    by_key: dict[
        tuple[pd.Timestamp, pd.Timestamp],
        tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    ]
    source: str

    @classmethod
    def from_bars(
        cls,
        bars: pd.DataFrame,
        *,
        lookback: pd.Timedelta,
        source: str,
    ) -> QuoteMinuteCache:
        by_key: dict[
            tuple[pd.Timestamp, pd.Timestamp],
            tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
        ] = {}
        if bars.empty:
            return cls(lookback=lookback, by_key=by_key, source=source)

        bars = _parse_1m_strips(bars)
        bars["minute"] = pd.to_datetime(bars["minute"])
        bars["spread"] = bars["ask"] - bars["bid"]
        bars = bars.sort_values("minute")
        for (s, e), g in bars.groupby(["start_tenor", "end_tenor"], sort=False):
            by_key[(pd.Timestamp(s), pd.Timestamp(e))] = (
                g["minute"].to_numpy(dtype="datetime64[ns]"),
                g["bid"].to_numpy(dtype=float),
                g["ask"].to_numpy(dtype=float),
                g["spread"].to_numpy(dtype=float),
                pd.to_datetime(g["quote_ts"]).to_numpy(dtype="datetime64[ns]"),
            )
        return cls(lookback=lookback, by_key=by_key, source=source)

    def asof(self, as_of: pd.Timestamp) -> pd.DataFrame:
        as_of = _naive(as_of)
        lo = as_of - self.lookback
        as_of_np = np.datetime64(as_of)
        lo_np = np.datetime64(lo)
        idx: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        bids: list[float] = []
        asks: list[float] = []
        spreads: list[float] = []
        qts: list[np.datetime64] = []
        for key, (mins, bid, ask, spread, quote_ts) in self.by_key.items():
            i = int(np.searchsorted(mins, as_of_np, side="right") - 1)
            if i < 0 or mins[i] < lo_np:
                continue
            idx.append(key)
            bids.append(float(bid[i]))
            asks.append(float(ask[i]))
            spreads.append(float(spread[i]))
            qts.append(quote_ts[i])
        if not idx:
            return pd.DataFrame(columns=["bid", "ask", "mid", "quote_ts", "spread"])
        out = pd.DataFrame(
            {
                "bid": bids,
                "ask": asks,
                "mid": (np.asarray(bids) + np.asarray(asks)) / 2.0,
                "quote_ts": pd.to_datetime(qts),
                "spread": spreads,
            },
            index=pd.MultiIndex.from_tuples(idx, names=["start_tenor", "end_tenor"]),
        )
        return out.sort_index()


@dataclass
class CurveAsofCache:
    """Spread curves with exact asof hits and/or a day timeline for arbitrary asof."""

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


@dataclass
class DayMarketCache:
    curves: CurveAsofCache
    quotes: QuoteMinuteCache


def load_curve_asof_many(
    client: Client,
    timestamps: list[pd.Timestamp],
    cfg: CurvesConfig | None = None,
) -> CurveAsofCache:
    """One ASOF join for all requested timestamps."""
    c = cfg or CONFIG.curves
    times = sorted({_naive(t) for t in timestamps})
    if not times:
        return CurveAsofCache({})

    arr = ", ".join(_ch_dt64_utc(t) for t in times)
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
              AND timestamp >= {_ch_dt64_utc(times[0])} - INTERVAL 6 HOUR
              AND timestamp <= {_ch_dt64_utc(times[-1])}
        ) AS c ON t.k = c.k AND t.t >= c.timestamp
        """
    )
    out: dict[pd.Timestamp, pd.Series] = {}
    for row in raw.itertuples(index=False):
        if row.curve is None:
            continue
        out[_naive(row.as_of)] = _curve_map_to_spreads(row.curve)
    return CurveAsofCache(out)


def load_curve_timeline(
    client: Client,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cfg: CurvesConfig | None = None,
) -> CurveAsofCache:
    """All curve snapshots in ``[start − 6h, end]`` for arbitrary asof lookup."""
    c = cfg or CONFIG.curves
    start = _naive(start)
    end = _naive(end)
    raw = client.query_df(
        f"""
        SELECT timestamp, curve
        FROM {c.table}
        WHERE product = '{c.product}'
          AND timestamp >= {_ch_dt64_utc(start)} - INTERVAL 6 HOUR
          AND timestamp <= {_ch_dt64_utc(end)}
        ORDER BY timestamp
        """
    )
    if raw.empty:
        return CurveAsofCache({})
    ts_list: list[pd.Timestamp] = []
    spreads: list[pd.Series] = []
    for row in raw.itertuples(index=False):
        if row.curve is None:
            continue
        ts_list.append(_naive(row.timestamp))
        spreads.append(_curve_map_to_spreads(row.curve))
    if not ts_list:
        return CurveAsofCache({})
    return CurveAsofCache(
        spreads_by_asof={},
        timeline_ts=np.asarray(ts_list, dtype="datetime64[ns]"),
        timeline_spreads=spreads,
    )


def load_quote_minute_bars(
    client: Client,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    table: str,
    price_scale: float,
    cfg: QuotesConfig | None = None,
) -> pd.DataFrame:
    """1m SPR L1 aggregated to one bar per strip per minute."""
    q = cfg or CONFIG.quotes
    return client.query_df(
        f"""
        SELECT
            toStartOfMinute(venue_time) AS minute,
            strip_name,
            argMax(toFloat64(bid_price_1) / {price_scale}, venue_time) AS bid,
            argMax(toFloat64(ask_price_1) / {price_scale}, venue_time) AS ask,
            max(venue_time) AS quote_ts
        FROM {table}
        WHERE venue_time > '{start}'
          AND venue_time <= '{end}'
          AND {_quotes_sql_filters(q)}
        GROUP BY minute, strip_name
        """
    )


def load_quote_cache_for_day(
    client: Client,
    asof_date: date,
    cfg: QuotesConfig | None = None,
) -> QuoteMinuteCache:
    """Prefetch primary (else fallback) 1m minute bars covering the day + lookback."""
    q = cfg or CONFIG.quotes
    lookback = pd.Timedelta(minutes=q.lookback_minutes)
    start = pd.Timestamp(datetime.combine(asof_date, time.min)) - lookback
    end = pd.Timestamp(datetime.combine(asof_date, time(23, 59, 59)))

    bars = load_quote_minute_bars(
        client,
        start,
        end,
        table=q.primary_table,
        price_scale=q.primary_price_scale,
        cfg=q,
    )
    source = q.primary_table
    if bars.empty:
        bars = load_quote_minute_bars(
            client,
            start,
            end,
            table=q.fallback_table,
            price_scale=q.fallback_price_scale,
            cfg=q,
        )
        source = q.fallback_table
    return QuoteMinuteCache.from_bars(bars, lookback=lookback, source=source)


def load_day_market_cache(
    client: Client,
    asof_date: date,
    cycle_times: list[pd.Timestamp],
    cfg: AppConfig | None = None,
    *,
    extra_asof: list[pd.Timestamp] | None = None,
) -> DayMarketCache:
    """Curves at cycle bounds (+ optional fill times) and quote bars for the day."""
    c = cfg or CONFIG
    times = list(cycle_times)
    if extra_asof:
        times.extend(extra_asof)
    curves = load_curve_asof_many(client, times, c.curves)
    quotes = load_quote_cache_for_day(client, asof_date, c.quotes)
    return DayMarketCache(curves=curves, quotes=quotes)
