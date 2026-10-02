"""Nexus trade fills keyed by cycle (algo.nexus_trades)."""

from __future__ import annotations

import re
from datetime import date

import pandas as pd
from clickhouse_connect.driver import Client

_BRN_CAL = re.compile(
    r"^ICE BRN (?P<start>[A-Za-z]{3}\d{2})-(?P<end>[A-Za-z]{3}\d{2}) Calendar$"
)


def parse_brn_calendar(instrument_key: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """``ICE BRN Mar27-Apr27 Calendar`` → (start_month, end_month)."""
    m = _BRN_CAL.match(str(instrument_key).strip())
    if not m:
        return None
    start = pd.to_datetime("01" + m.group("start"), format="%d%b%y").normalize()
    end = pd.to_datetime("01" + m.group("end"), format="%d%b%y").normalize()
    return start, end


def pack_to_consecutive_lots(start: pd.Timestamp, end: pd.Timestamp, lots: float) -> pd.Series:
    """Multi-month pack → consecutive 1m spread lots.

    ``N`` lots of ``start→end`` become ``N`` on each consecutive front
    (e.g. Mar–Jun → Mar/Apr, Apr/May, May/Jun each get ``N``). Not divided
    by the number of months.
    """
    months = pd.date_range(start.normalize(), end.normalize(), freq="MS")
    if len(months) < 2:
        return pd.Series(dtype=float)
    fronts = months[:-1]
    return pd.Series(float(lots), index=fronts)


def n_1m_legs(instrument_key: str) -> int:
    """How many consecutive 1m spreads a calendar instrument spans."""
    parsed = parse_brn_calendar(instrument_key)
    if parsed is None:
        return 0
    start, end = parsed
    return max(len(pd.date_range(start.normalize(), end.normalize(), freq="MS")) - 1, 0)


def load_trades_for_cycle(
    client: Client,
    book: str,
    cycle_id: str,
    asof_date: date | None = None,
) -> pd.DataFrame:
    """Fills for one book / cycle from ``algo.nexus_trades``.

    ``text_c`` is comma-separated; cycle_id is the 3rd field.
    ``text_tt`` is the book name.
    """
    date_filter = ""
    if asof_date is not None:
        date_filter = f"AND toDate(transaction_timestamp) = '{asof_date}'"

    return client.query_df(
        f"""
        SELECT
            transaction_timestamp,
            instrument_key,
            side,
            quantity,
            price,
            text_tt,
            text_c,
            splitByChar(',', text_c)[3] AS cycle_id
        FROM algo.nexus_trades
        WHERE text_tt = '{book}'
          AND splitByChar(',', text_c)[3] = '{cycle_id}'
          {date_filter}
        ORDER BY transaction_timestamp
        """
    )


def executed_lots_series(trades: pd.DataFrame) -> pd.Series:
    """Signed fill lots by consecutive front month (packs expanded).

    Buy → +qty, Sell → −qty. Non-calendar instruments are skipped.
    """
    if trades.empty:
        return pd.Series(dtype=float)

    acc = pd.Series(dtype=float)
    for _, row in trades.iterrows():
        parsed = parse_brn_calendar(row["instrument_key"])
        if parsed is None:
            continue
        start, end = parsed
        side = str(row["side"]).strip().lower()
        qty = float(row["quantity"])
        signed = qty if side == "buy" else -qty if side == "sell" else 0.0
        if signed == 0.0:
            continue
        piece = pack_to_consecutive_lots(start, end, signed)
        acc = acc.add(piece, fill_value=0.0)
    if acc.empty:
        return acc
    acc.index = pd.DatetimeIndex(acc.index).normalize()
    return acc.sort_index()


def iter_fill_legs(trades: pd.DataFrame):
    """Yield ``(fill_ts, tenor_front, signed_lots)`` with packs expanded.

    Buy → +lots, Sell → −lots. Skips non-calendar instruments.
    """
    for fill_ts, tenor, lots, _price, _ikey in iter_fill_legs_priced(trades):
        yield fill_ts, tenor, lots


def iter_fill_legs_priced(trades: pd.DataFrame):
    """Yield ``(fill_ts, tenor_front, signed_lots, price_per_1m, instrument_key)``.

    Pack prices are sums of consecutive 1m spreads, so each leg is
    compared to ``price / n_legs``. Buy → +lots, Sell → −lots.
    ``instrument_key`` is the raw nexus_trades contract (may be a pack).
    """
    if trades.empty:
        return
    for row in trades.itertuples(index=False):
        ikey = str(getattr(row, "instrument_key"))
        parsed = parse_brn_calendar(ikey)
        if parsed is None:
            continue
        start, end = parsed
        side = str(getattr(row, "side")).strip().lower()
        qty = float(getattr(row, "quantity"))
        signed = qty if side == "buy" else -qty if side == "sell" else 0.0
        if signed == 0.0:
            continue
        legs = pack_to_consecutive_lots(start, end, signed)
        n = len(legs)
        if n == 0:
            continue
        price_per_1m = float(getattr(row, "price")) / n
        fill_ts = pd.Timestamp(getattr(row, "transaction_timestamp"))
        for tenor, lots in legs.items():
            yield (
                fill_ts,
                pd.Timestamp(tenor).normalize(),
                float(lots),
                price_per_1m,
                ikey,
            )

def load_executed_lots_for_cycle(
    client: Client,
    book: str,
    cycle_id: str,
    asof_date: date | None = None,
) -> pd.Series:
    """Convenience: book/cycle → signed consecutive-month executed lots."""
    trades = load_trades_for_cycle(client, book, cycle_id, asof_date)
    return executed_lots_series(trades)


def load_trades_for_book_day(
    client: Client,
    book: str,
    asof_date: date,
) -> pd.DataFrame:
    """All fills for one book / day from ``algo.nexus_trades``."""
    return client.query_df(
        f"""
        SELECT
            transaction_timestamp,
            instrument_key,
            side,
            quantity,
            price,
            text_tt,
            text_c,
            splitByChar(',', text_c)[3] AS cycle_id
        FROM algo.nexus_trades
        WHERE text_tt = '{book}'
          AND toDate(transaction_timestamp) = '{asof_date}'
        ORDER BY transaction_timestamp
        """
    )


def executed_lots_by_cycle(trades: pd.DataFrame) -> dict[str, pd.Series]:
    """Group day trades by cycle_id → expanded consecutive-month lots."""
    if trades.empty:
        return {}
    out: dict[str, pd.Series] = {}
    for cid, grp in trades.groupby("cycle_id"):
        out[str(cid)] = executed_lots_series(grp)
    return out
