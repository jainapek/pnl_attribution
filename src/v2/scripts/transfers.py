"""Nexus transfers → consecutive monthly spread lots (same space as intentions)."""

from __future__ import annotations

from datetime import date

import pandas as pd
from clickhouse_connect.driver import Client

from .trades import pack_to_consecutive_lots


def to_utc(ts) -> pd.Timestamp:
    """Normalise timestamps: naive → UTC, aware → UTC."""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def book_signed_qty(side: str, quantity: float) -> float:
    """Desk side → book inventory sign.

    Desk buy → book short (−); desk sell → book long (+).
    Matches ``position_received`` on intention rows.
    """
    s = str(side).strip().lower()
    q = float(quantity)
    if s == "buy":
        return -q
    if s == "sell":
        return q
    return 0.0


def expand_transfer_row(row) -> pd.Series:
    """One ``nexus_transfers`` row → signed consecutive 1m spread lots.

    Packs (``start_tenor``→``end_tenor`` spanning >1 month) put the same
    signed quantity on each consecutive front, identical to trade packs.
    """
    start = pd.Timestamp(row["start_tenor"]).normalize()
    end = pd.Timestamp(row["end_tenor"]).normalize()
    signed = book_signed_qty(row["side"], row["quantity"])
    if signed == 0.0:
        return pd.Series(dtype=float)
    return pack_to_consecutive_lots(start, end, signed)


def iter_transfer_legs_priced(transfers: pd.DataFrame):
    """Yield ``(xfer_ts, tenor_front, signed_lots, price_per_1m)``.

    Pack prices are sums of consecutive 1m spreads → each leg uses
    ``price / n_legs``. Book sign: desk buy → −lots, desk sell → +lots.
    """
    if transfers is None or len(transfers) == 0:
        return
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
        price_per_1m = float(getattr(row, "price")) / n
        xfer_ts = pd.Timestamp(getattr(row, "timestamp"))
        for tenor, lots in legs.items():
            yield xfer_ts, pd.Timestamp(tenor).normalize(), float(lots), price_per_1m


def load_transfers_for_book_day(
    client: Client,
    book: str,
    asof_date: date,
    *,
    exclude_eod_roll: bool = True,
) -> pd.DataFrame:
    """Raw ``algo.nexus_transfers`` for one receiving book / London calendar day.

    ``source_book`` is the Nexus book that receives the risk. Desk deals are
    duplicated across books (e.g. Hedger Spreads + Spreadgr Base 2).

    When ``exclude_eod_roll`` is True, drops ``toHour(timestamp) >= 21``
    (London) — the end-of-day internal roll, not desk flow.
    """
    hour_filter = "AND toHour(timestamp) < 21" if exclude_eod_roll else ""
    return client.query_df(
        f"""
        SELECT
            timestamp,
            id,
            desk,
            trader,
            start_tenor,
            end_tenor,
            price,
            quantity,
            side,
            source_book,
            mark,
            edge_applied
        FROM algo.nexus_transfers
        WHERE toDate(timestamp) = '{asof_date}'
          AND source_book = '{book}'
          {hour_filter}
        ORDER BY timestamp
        """
    )


def transfer_lots_series(transfers: pd.DataFrame) -> pd.Series:
    """Sum of pack-expanded, book-signed lots by front month."""
    if transfers.empty:
        return pd.Series(dtype=float)
    acc = pd.Series(dtype=float)
    for _, row in transfers.iterrows():
        acc = acc.add(expand_transfer_row(row), fill_value=0.0)
    if acc.empty:
        return acc
    acc.index = pd.DatetimeIndex(acc.index).normalize()
    return acc.sort_index()
