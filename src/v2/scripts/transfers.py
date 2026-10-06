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


def naive_utc(ts) -> pd.Timestamp:
    """Aware → UTC then drop tz; naive treated as UTC wall time."""
    return to_utc(ts).tz_localize(None)


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


def normalize_cycle_id(value) -> str | None:
    """UUID / string → comparable id, or ``None`` if the column is unset."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    if not s or s.lower() in ("none", "nan", "nat", "null", "<na>"):
        return None
    return s.lower()


def _timestamp_window_mask(
    stamps: pd.Series,
    cycle_ts: pd.Timestamp,
    prev_ts: pd.Timestamp | None,
) -> pd.Series:
    if prev_ts is None:
        return stamps <= cycle_ts
    return (stamps > prev_ts) & (stamps <= cycle_ts)


def transfers_for_cycles(
    transfers: pd.DataFrame,
    cycle_ids,
    cycle_ts: pd.Timestamp,
    prev_ts: pd.Timestamp | None = None,
    *,
    timestamp_col: str = "timestamp",
) -> pd.DataFrame:
    """Transfers for one cycle or a coalesced group.

    Rows with a non-null ``cycle_id`` match those ids. Untagged rows use
    ``timestamp ∈ (prev_ts, cycle_ts]`` (``(−∞, cycle_ts]`` if ``prev_ts``
    is None). Tagged rows are never also assigned by time.
    """
    if transfers is None or transfers.empty:
        return transfers if transfers is not None else pd.DataFrame()

    wanted = {normalize_cycle_id(c) for c in cycle_ids}
    wanted.discard(None)

    if "cycle_id" not in transfers.columns:
        tagged_mask = pd.Series(False, index=transfers.index)
    else:
        tagged_mask = transfers["cycle_id"].map(normalize_cycle_id).notna()

    tagged = transfers.loc[tagged_mask]
    untagged = transfers.loc[~tagged_mask]

    if tagged.empty or not wanted:
        by_id = tagged.iloc[0:0]
    else:
        by_id = tagged.loc[
            tagged["cycle_id"].map(normalize_cycle_id).isin(wanted)
        ]

    if untagged.empty:
        by_ts = untagged
    else:
        by_ts = untagged.loc[
            _timestamp_window_mask(untagged[timestamp_col], cycle_ts, prev_ts)
        ]

    out = pd.concat([by_id, by_ts], axis=0)
    if out.empty:
        return out
    if timestamp_col in out.columns:
        return out.sort_values(timestamp_col)
    return out


def transfers_for_cycle(
    transfers: pd.DataFrame,
    cycle_id,
    cycle_ts: pd.Timestamp,
    prev_ts: pd.Timestamp | None = None,
    *,
    timestamp_col: str = "timestamp",
) -> pd.DataFrame:
    """Transfers belonging to one cycle. See ``transfers_for_cycles``."""
    return transfers_for_cycles(
        transfers,
        [cycle_id],
        cycle_ts,
        prev_ts,
        timestamp_col=timestamp_col,
    )


def assign_transfers_to_cycles(
    cycles: pd.DataFrame,
    transfers: pd.DataFrame,
) -> tuple[pd.DataFrame, list[pd.DataFrame]]:
    """Assign transfers, then start each cycle at the earlier of intention vs first transfer.

    Assignment still uses intention timestamps (``cycle_id`` when set, else
    ``(prev_intention, this_intention]``). Cycle start becomes

        min(intention_ts, earliest assigned transfer)

    so transfer→reporting lag is inside the cycle window instead of
    ``transfer_timing_mr``. ``next_cycle_start`` tessellates to the next
    cycle's effective start (last cycle keeps its EOD end).
    """
    if cycles is None or cycles.empty:
        return cycles if cycles is not None else pd.DataFrame(), []

    gaps: list[pd.DataFrame] = []
    intention_ts: list[pd.Timestamp] = []
    prev_int: pd.Timestamp | None = None
    for _, row in cycles.iterrows():
        t_int = naive_utc(row["timestamp"])
        intention_ts.append(t_int)
        gaps.append(transfers_for_cycle(transfers, row["cycle_id"], t_int, prev_int))
        prev_int = t_int

    starts: list[pd.Timestamp] = []
    for t_int, gap in zip(intention_ts, gaps):
        if gap is None or gap.empty:
            starts.append(t_int)
        else:
            starts.append(min(t_int, min(naive_utc(t) for t in gap["timestamp"])))

    last_end = naive_utc(cycles.iloc[-1]["next_cycle_start"])
    ends = starts[1:] + [last_end]
    out = cycles.copy()
    out["intention_timestamp"] = intention_ts
    out["timestamp"] = starts
    out["next_cycle_start"] = ends
    return out, gaps


def load_transfers_for_book_day(
    client: Client,
    book: str,
    asof_date: date,
    *,
    exclude_eod_roll: bool = True,
    include_outbound: bool = False,
) -> pd.DataFrame:
    """Raw ``algo.nexus_transfers`` for one book / London calendar day.

    ``source_book`` is the Nexus book that **receives** the risk. Desk deals
    are duplicated across books (e.g. Hedger Spreads + Spreadgr Base 2).

    When ``include_outbound`` is True, also loads rows this desk **sent** to
    another book (``desk = book`` and ``source_book != book``) — e.g. the
    21:00 Hedger Spread Roller onto Spreadgr Base 2, which never appears as
    a Hedger ``source_book`` row. Those rows get ``outbound=True``; lot sign
    must be flipped vs the receiver.

    When ``exclude_eod_roll`` is True, drops inbound ``toHour(timestamp) >= 21``
    (UTC wall — legacy). Outbound rolls are not dropped.
    """
    hour_filter = "AND toHour(timestamp) < 21" if exclude_eod_roll else ""
    if include_outbound:
        book_filter = f"""
          AND (
            (source_book = '{book}' {hour_filter})
            OR (desk = '{book}' AND source_book != '{book}')
          )
        """
    else:
        book_filter = f"AND source_book = '{book}' {hour_filter}"
    raw = client.query_df(
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
            edge_applied,
            cycle_id
        FROM algo.nexus_transfers
        WHERE toDate(timestamp) = '{asof_date}'
          {book_filter}
        ORDER BY timestamp
        """
    )
    if raw is None or raw.empty:
        return pd.DataFrame(
            columns=[
                "timestamp",
                "id",
                "desk",
                "trader",
                "start_tenor",
                "end_tenor",
                "price",
                "quantity",
                "side",
                "source_book",
                "mark",
                "edge_applied",
                "cycle_id",
                "outbound",
            ]
        )
    out = raw.copy()
    out["outbound"] = (out["desk"].astype(str) == book) & (
        out["source_book"].astype(str) != book
    )
    return out


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
