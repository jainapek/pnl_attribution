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
    load_curve_asof_many,
    load_day_market_cache,
    load_quote_cache_for_day,
)


def _empty_book_frame(cfg: AppConfig) -> pd.DataFrame:
    dollar_cols = pd.MultiIndex.from_product(
        [cfg.stage_order, cfg.summary_components],
        names=["stage", "component"],
    )
    empty = pd.DataFrame(columns=dollar_cols)
    return with_cents_per_bbl(empty, pd.Series(dtype=float), cfg)


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
    then attributes in-process.

    Index: ``cycle_id`` (+ ``DAY TOTAL``).
    Columns: MultiIndex ``(unit, stage, component)`` with ``unit`` in
    ``meta`` (abs_lots), ``$``, ``c/bbl``.
    """
    c = cfg or CONFIG
    intentions = load_intentions_for_day(client, asof_date, book, c.intentions)
    if intentions.empty:
        return _empty_book_frame(c)

    cycles = cycles_for_day(intentions, asof_date)
    cycle_times = [
        pd.Timestamp(t)
        for t in pd.concat([cycles["timestamp"], cycles["next_cycle_start"]])
    ]
    if market is None:
        print(f"  {book}: prefetching market data ({len(cycles)} cycles)…")
        market = load_day_market_cache(client, asof_date, cycle_times, c)
    else:
        print(f"  {book}: prefetching curves ({len(cycles)} cycles)…")
        market = DayMarketCache(
            curves=load_curve_asof_many(client, cycle_times, c.curves),
            quotes=market.quotes,
        )

    rows: list[pd.Series] = []
    lots: list[float] = []
    ids: list = []
    for i, (_, row) in enumerate(cycles.iterrows(), start=1):
        s, abs_traded = attribute_cycle(client, row, c, market=market)
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
    shared = DayMarketCache(
        curves=load_curve_asof_many(client, [], c.curves),
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
