"""Day-level attribution across cycles and books."""

from __future__ import annotations

from datetime import date

import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG, AppConfig
from .cycle_attribution import attribute_cycle, with_cents_per_bbl
from .intentions import cycles_for_day, load_intentions_for_day
from .market_cache import (
    DayMarketCache,
    load_day_market_cache,
    load_quote_cache_for_day,
)
from .trades import executed_lots_series, load_trades_for_book_day
from .transfers import assign_transfers_to_cycles, load_transfers_for_book_day
from .costs import executed_fill_market_risk_by_instrument


def _empty_book_frame(cfg: AppConfig) -> pd.DataFrame:
    dollar_cols = pd.MultiIndex.from_product(
        [cfg.stage_order, cfg.summary_components],
        names=["stage", "component"],
    )
    empty = pd.DataFrame(columns=dollar_cols)
    return with_cents_per_bbl(empty, pd.Series(dtype=float), cfg)


def _to_utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _naive_utc(ts) -> pd.Timestamp:
    """Aware → UTC then drop tz; naive treated as UTC wall time."""
    return _to_utc(ts).tz_localize(None)


def attribute_book_day(
    client: Client,
    asof_date: date,
    book: str,
    cfg: AppConfig | None = None,
    *,
    progress_every: int = 50,
    market: DayMarketCache | None = None,
) -> pd.DataFrame:
    """All cycles for one book / day.

    Prefetches curves + quote minute-bars once (unless ``market`` is passed),
    Cycle start is ``min(intention timestamp, earliest assigned transfer)``
    so transfer→reporting lag sits in held MR, not a separate timing bucket.
    Cycle ends tessellate. Also loads the day's nexus trades and transfers
    once so each cycle gets:
    - fills in ``(timestamp, next_cycle_start]`` → unexecuted / executed MR /
      mark_to_exec
    - transfers tagged with ``cycle_id`` → that cycle; untagged rows in
      ``(prev_intention, this_intention]`` → transfer_vs_mark /
      transfer_timing_mr (timing is ~0 on the first transfer)

    Index: ``cycle_id`` (+ ``DAY TOTAL``).
    Columns: MultiIndex ``(unit, stage, component)`` with ``unit`` in
    ``meta`` (abs_lots), ``$``, ``c/bbl``.
    """
    c = cfg or CONFIG
    intentions = load_intentions_for_day(client, asof_date, book, c.intentions)
    if intentions.empty:
        return _empty_book_frame(c)

    cycles = cycles_for_day(intentions, asof_date)

    trades = load_trades_for_book_day(client, book, asof_date)
    fill_times: list[pd.Timestamp] = []
    if not trades.empty:
        trades = trades.copy()
        trades["transaction_timestamp"] = [
            _naive_utc(t) for t in trades["transaction_timestamp"]
        ]
        fill_times = [pd.Timestamp(t) for t in trades["transaction_timestamp"]]

    transfers = load_transfers_for_book_day(client, book, asof_date)
    xfer_times: list[pd.Timestamp] = []
    if not transfers.empty:
        transfers = transfers.copy()
        transfers["timestamp"] = [_naive_utc(t) for t in transfers["timestamp"]]
        xfer_times = [pd.Timestamp(t) for t in transfers["timestamp"]]

    cycles, xfer_gaps = assign_transfers_to_cycles(cycles, transfers)
    cycle_times = [
        pd.Timestamp(t)
        for t in pd.concat([cycles["timestamp"], cycles["next_cycle_start"]])
    ]

    extra_asof = fill_times + xfer_times
    if market is None:
        print(f"  {book}: prefetching market data ({len(cycles)} cycles)…")
        market = load_day_market_cache(
            client, asof_date, cycle_times, c, extra_asof=extra_asof
        )
    else:
        print(f"  {book}: prefetching curves ({len(cycles)} cycles)…")
        from .market_cache import load_curve_asof_many

        market = DayMarketCache(
            curves=load_curve_asof_many(
                client, cycle_times + extra_asof, c.curves
            ),
            quotes=market.quotes,
        )

    rows: list[pd.Series] = []
    lots: list[float] = []
    ids: list = []
    for i, ((_, row), xfer_gap) in enumerate(
        zip(cycles.iterrows(), xfer_gaps), start=1
    ):
        t0 = _naive_utc(row["timestamp"])
        t1 = _naive_utc(row["next_cycle_start"])
        if trades.empty:
            gap = trades
            fills = pd.Series(dtype=float)
        else:
            gap = trades[
                (trades["transaction_timestamp"] > t0)
                & (trades["transaction_timestamp"] <= t1)
            ]
            fills = executed_lots_series(gap)

        s, abs_traded = attribute_cycle(
            client,
            row,
            c,
            market=market,
            fills=fills,
            fill_trades=gap,
            cycle_transfers=xfer_gap,
        )
        rows.append(s)
        lots.append(abs_traded)
        ids.append(row["cycle_id"])
        if progress_every and i % progress_every == 0:
            print(f"  {book}: {i}/{len(cycles)} cycles")

    dollars = pd.DataFrame(rows, index=ids)
    dollars.columns = pd.MultiIndex.from_tuples(
        dollars.columns, names=["stage", "component"]
    )
    dollars.index.name = "cycle_id"
    dollars = dollars.reindex(
        columns=pd.MultiIndex.from_product(
            [c.stage_order, c.summary_components],
            names=["stage", "component"],
        )
    )
    abs_lots_s = pd.Series(lots, index=ids, name="abs_lots", dtype=float)
    dollars.loc["DAY TOTAL"] = dollars.sum(numeric_only=True)
    abs_lots_s.loc["DAY TOTAL"] = float(abs_lots_s.sum())
    return with_cents_per_bbl(dollars, abs_lots_s, c)


def executed_mr_by_instrument_book_day(
    client: Client,
    asof_date: date,
    book: str,
    cfg: AppConfig | None = None,
    *,
    market: DayMarketCache | None = None,
) -> pd.DataFrame:
    """Day ``executed_mr`` split by ``nexus_trades.instrument_key``.

    Same cycle windows and sign as day attribution. Packs stay under the
    raw multi-month contract string. Returns columns
    ``executed_mr``, ``n_fills``, ``abs_lots``, ``c/bbl`` sorted by |executed_mr|.
    Index is contracts only (no TOTAL row).
    """
    c = cfg or CONFIG
    intentions = load_intentions_for_day(client, asof_date, book, c.intentions)
    if intentions.empty:
        return pd.DataFrame(columns=["executed_mr", "n_fills", "abs_lots"])

    cycles = cycles_for_day(intentions, asof_date)
    transfers = load_transfers_for_book_day(client, book, asof_date)
    if not transfers.empty:
        transfers = transfers.copy()
        transfers["timestamp"] = [_naive_utc(t) for t in transfers["timestamp"]]
    cycles, _xfer_gaps = assign_transfers_to_cycles(cycles, transfers)
    cycle_times = [
        pd.Timestamp(t)
        for t in pd.concat([cycles["timestamp"], cycles["next_cycle_start"]])
    ]

    trades = load_trades_for_book_day(client, book, asof_date)
    fill_times: list[pd.Timestamp] = []
    if not trades.empty:
        trades = trades.copy()
        trades["transaction_timestamp"] = [
            _naive_utc(t) for t in trades["transaction_timestamp"]
        ]
        fill_times = [pd.Timestamp(t) for t in trades["transaction_timestamp"]]

    if market is None:
        market = load_day_market_cache(
            client, asof_date, cycle_times, c, extra_asof=fill_times
        )
    elif fill_times:
        from .market_cache import load_curve_asof_many

        market = DayMarketCache(
            curves=load_curve_asof_many(
                client, cycle_times + fill_times, c.curves
            ),
            quotes=market.quotes,
        )

    pnl_acc: dict[str, float] = {}
    fill_acc: dict[str, int] = {}
    lots_acc: dict[str, float] = {}

    for _, row in cycles.iterrows():
        t0 = _naive_utc(row["timestamp"])
        t1 = _naive_utc(row["next_cycle_start"])
        if trades.empty:
            continue
        gap = trades[
            (trades["transaction_timestamp"] > t0)
            & (trades["transaction_timestamp"] <= t1)
        ]
        if gap.empty:
            continue
        by_inst = executed_fill_market_risk_by_instrument(
            gap, t0, market.curves, c
        )
        for ikey, pnl in by_inst.items():
            pnl_acc[ikey] = pnl_acc.get(ikey, 0.0) + float(pnl)
        for ikey, grp in gap.groupby("instrument_key"):
            key = str(ikey)
            fill_acc[key] = fill_acc.get(key, 0) + int(len(grp))
            lots_acc[key] = lots_acc.get(key, 0.0) + float(
                grp["quantity"].astype(float).abs().sum()
            )

    if not pnl_acc:
        return pd.DataFrame(columns=["executed_mr", "n_fills", "abs_lots"])

    out = pd.DataFrame(
        {
            "executed_mr": pd.Series(pnl_acc, dtype=float),
            "n_fills": pd.Series(fill_acc, dtype=float),
            "abs_lots": pd.Series(lots_acc, dtype=float),
        }
    )
    out.index.name = "instrument_key"
    out = out.fillna(0.0)
    out["n_fills"] = out["n_fills"].astype(int)
    out = out.sort_values("executed_mr", key=lambda s: s.abs(), ascending=False)
    # slippage sign: +c/bbl = cost, −c/bbl = gain (same as day tables)
    bbls = out["abs_lots"].astype(float) * c.contract.size
    out["c/bbl"] = (-out["executed_mr"] / bbls.where(bbls > 0) * 100.0).round(4)
    return out.round({"executed_mr": 2, "abs_lots": 2})


def attribute_all_books_day(
    client: Client,
    asof_date: date,
    books: list[str] | None = None,
    cfg: AppConfig | None = None,
    *,
    progress_every: int = 50,
) -> pd.DataFrame:
    """Same table for every book.

    Index: MultiIndex ``(book, cycle_id)`` with per-book ``DAY TOTAL``
    and a final ``(TEAM, TOTAL)`` row summing book day totals
    (c/bbl recomputed from summed $ / summed abs_lots).
    """
    c = cfg or CONFIG
    books = books or c.book_names
    frames: list[pd.DataFrame] = []

    print(f"prefetching quote bars for {asof_date}…")
    shared_quotes = load_quote_cache_for_day(client, asof_date, c.quotes)
    from .market_cache import CurveAsofCache

    shared = DayMarketCache(
        curves=CurveAsofCache({}),
        quotes=shared_quotes,
    )

    for book in books:
        print(f"book={book}")
        one = attribute_book_day(
            client,
            asof_date,
            book,
            c,
            progress_every=progress_every,
            market=shared,
        )
        if one.empty or len(one) == 0:
            print(f"  (no cycles)")
            continue
        one = one.copy()
        one.index = pd.MultiIndex.from_product(
            [[book], one.index], names=["book", "cycle_id"]
        )
        frames.append(one)

    if not frames:
        empty = _empty_book_frame(c)
        empty.index = pd.MultiIndex.from_tuples([], names=["book", "cycle_id"])
        return empty

    out = pd.concat(frames)

    # TEAM TOTAL from book DAY TOTAL rows: sum $ and abs_lots, recompute c/bbl
    day_totals = out.xs("DAY TOTAL", level="cycle_id", drop_level=False)
    abs_col = ("meta", "abs_lots", "")
    dollar_cols = [
        col for col in out.columns if col[0] == "$"
    ]
    team_abs = float(day_totals[abs_col].sum())
    team_dollars = day_totals[dollar_cols].sum(numeric_only=True)
    # strip unit level → (stage, component)
    team_dollars.index = pd.MultiIndex.from_tuples(
        [(s, comp) for (_u, s, comp) in team_dollars.index],
        names=["stage", "component"],
    )
    team = with_cents_per_bbl(
        pd.DataFrame([team_dollars], index=pd.Index([("TEAM", "TOTAL")])),
        pd.Series([team_abs], index=pd.Index([("TEAM", "TOTAL")])),
        c,
    )
    team.index = pd.MultiIndex.from_tuples(
        [("TEAM", "TOTAL")], names=["book", "cycle_id"]
    )
    return pd.concat([out, team])


def load_library_eod_pnl(
    client: Client,
    asof_date: date,
    book: str,
) -> dict[str, float] | None:
    """EOD library PnL for one book / day.

    Last ``tenor='total'`` snapshot on ``asof_date`` from
    ``algo.nexus_pnl_attribution``.

        gross = overnight + m2m_pnl + trade_pnl
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
    out = {
        "overnight": float(row["overnight"]),
        "m2m_pnl": float(row["m2m_pnl"]),
        "trade_pnl": float(row["trade_pnl"]),
        "gross": float(row["gross"]),
    }
    if any(pd.isna(v) for v in out.values()):
        return None
    return out


def load_library_m2m_trade(
    client: Client,
    asof_date: date,
    book: str,
) -> float | None:
    """EOD library ``m2m_pnl + trade_pnl`` (no overnight)."""
    lib = load_library_eod_pnl(client, asof_date, book)
    if lib is None:
        return None
    return lib["m2m_pnl"] + lib["trade_pnl"]
