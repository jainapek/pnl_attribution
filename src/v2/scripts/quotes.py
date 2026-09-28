"""Top-of-book spread bid/ask from mdc / mdcoll."""

from __future__ import annotations

import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG, QuotesConfig


def load_spread_bid_ask_as_of(
    client: Client,
    as_of: pd.Timestamp,
    *,
    table: str | None = None,
    price_scale: float | None = None,
    lookback: pd.Timedelta | None = None,
    cfg: QuotesConfig | None = None,
) -> pd.DataFrame:
    """Last 1m calendar L1 bid/ask at or before ``as_of``.

    Index: ``(start_tenor, end_tenor)``.
    Columns: ``bid``, ``ask``, ``mid``, ``quote_ts``, ``spread``.
    """
    q = cfg or CONFIG.quotes
    table = table or q.primary_table
    price_scale = q.primary_price_scale if price_scale is None else price_scale
    lookback = lookback or pd.Timedelta(minutes=q.lookback_minutes)
    window_start = as_of - lookback

    raw = client.query_df(
        f"""
        SELECT
            strip_name,
            argMax(toFloat64(bid_price_1) / {price_scale}, venue_time) AS bid,
            argMax(toFloat64(ask_price_1) / {price_scale}, venue_time) AS ask,
            max(venue_time) AS quote_ts
        FROM {table}
        WHERE venue_time > '{window_start}'
          AND venue_time <= '{as_of}'
          AND hub_alias = '{q.hub_alias}'
          AND security_sub_type_name = '{q.security_sub_type}'
          AND isNotNull(bid_price_1)
          AND isNotNull(ask_price_1)
          AND (bid_quantity_1 + bid_implied_quantity_1) > 0
          AND (ask_quantity_1 + ask_implied_quantity_1) > 0
          AND length(splitByChar('/', strip_name)) = 2
          AND toFloat64(ask_price_1) > toFloat64(bid_price_1)
        GROUP BY strip_name
        """
    )
    if raw.empty:
        return raw

    parts = raw["strip_name"].astype(str).str.split("/", expand=True)
    raw = raw.copy()
    raw["start_tenor"] = pd.to_datetime("01" + parts[0], format="%d%b%y")
    raw["end_tenor"] = pd.to_datetime("01" + parts[1], format="%d%b%y")
    month_diff = (
        (raw["end_tenor"].dt.year - raw["start_tenor"].dt.year) * 12
        + (raw["end_tenor"].dt.month - raw["start_tenor"].dt.month)
    )
    raw = raw.loc[month_diff == 1]
    if raw.empty:
        return pd.DataFrame()

    last = raw
    last["mid"] = (last["bid"] + last["ask"]) / 2.0
    last["spread"] = last["ask"] - last["bid"]
    last["start_tenor"] = pd.to_datetime(last["start_tenor"]).dt.normalize()
    last["end_tenor"] = pd.to_datetime(last["end_tenor"]).dt.normalize()
    last["quote_ts"] = pd.to_datetime(last["quote_ts"])
    return last.set_index(["start_tenor", "end_tenor"]).sort_index()[
        ["bid", "ask", "mid", "quote_ts", "spread"]
    ]


def load_spread_bid_ask_with_fallback(
    client: Client,
    as_of: pd.Timestamp,
    cfg: QuotesConfig | None = None,
) -> tuple[pd.DataFrame, str]:
    """Try mdc first, then mdcoll. Returns ``(quotes, source_table)``."""
    q = cfg or CONFIG.quotes
    quotes = load_spread_bid_ask_as_of(
        client,
        as_of,
        table=q.primary_table,
        price_scale=q.primary_price_scale,
        cfg=q,
    )
    source = q.primary_table
    if quotes.empty:
        quotes = load_spread_bid_ask_as_of(
            client,
            as_of,
            table=q.fallback_table,
            price_scale=q.fallback_price_scale,
            cfg=q,
        )
        source = q.fallback_table
    return quotes, source
