"""No-roll sleeve attribution on the running book (benchmark path).

Components (day total):
  overnight (once)
  + timing_mr
  + pca_held_mr
  + fill_mr
  + mark_to_fill
  + unexecuted_mr

Outbound Hedger → Spreadgr Base 2 is not a sleeve.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG, AppConfig
from .intentions import load_intentions_for_day
from .library import load_library_eod_pnl
from .market_cache import load_curve_asof_many
from .position_timeline import (
    build_position_timeline,
    stamp_cycle_component_mr_on_timeline,
    stamp_overnight_on_timeline,
    stamp_pca_held_on_timeline,
    stamp_timing_mr_on_timeline,
)
from .trades import pack_to_consecutive_lots
from .transfers import book_signed_qty, naive_utc, normalize_cycle_id

SLEEVE_COLS = (
    "overnight",
    "timing_mr",
    "pca_held_mr",
    "fill_mr",
    "mark_to_fill",
    "unexecuted_mr",
)


def assign_transfers_to_cycles(
    client: Client,
    book: str,
    start: date,
    end: date,
    cfg: AppConfig | None = None,
) -> pd.DataFrame:
    """Assign nexus transfers to cycles from ``start``…``end``.

    - Recorded ``cycle_id`` kept when set.
    - Untagged → first intention strictly after the transfer.
    - Hedger → Spreadgr Base 2 outbound: no cycle (``hedger_to_base2``).
    """
    c = cfg or CONFIG
    xfers = client.query_df(
        f"""
        SELECT
            timestamp,
            id,
            desk,
            source_book,
            trader,
            side,
            quantity,
            start_tenor,
            end_tenor,
            price,
            toString(cycle_id) AS cycle_id_recorded
        FROM algo.nexus_transfers
        WHERE toDate(timestamp) >= '{start}'
          AND toDate(timestamp) <= '{end}'
          AND (
            (source_book = '{book}' AND toHour(timestamp) < 21)
            OR (desk = '{book}' AND source_book != '{book}')
          )
        ORDER BY timestamp
        """
    )
    if xfers is None or xfers.empty:
        return pd.DataFrame()

    xfers = xfers.copy()
    xfers["timestamp"] = xfers["timestamp"].map(naive_utc)
    xfers["outbound"] = (xfers["desk"].astype(str) == book) & (
        xfers["source_book"].astype(str) != book
    )
    xfers["cycle_id_recorded"] = xfers["cycle_id_recorded"].map(normalize_cycle_id)

    cycles = client.query_df(
        f"""
        SELECT
            cycle_id AS cycle_id_assigned,
            min(timestamp) AS cycle_ts
        FROM {c.intentions.table}
        WHERE book = '{book}'
          AND toDate(timestamp) >= '{start}'
          AND toDate(timestamp) <= '{end}'
        GROUP BY cycle_id
        ORDER BY cycle_ts
        """
    )
    if cycles is None or cycles.empty:
        out = xfers.copy()
        out["cycle_id_assigned"] = pd.NA
        out["cycle_ts"] = pd.NaT
        out["assigned_from"] = "no_cycles"
        out["lag_ms"] = pd.NA
        return out

    cycles = cycles.copy()
    cycles["cycle_ts"] = cycles["cycle_ts"].map(naive_utc)
    cycle_ts_by_id = cycles.set_index("cycle_id_assigned")["cycle_ts"]
    cts = cycles["cycle_ts"].sort_values().to_numpy(dtype="datetime64[ns]")
    cids = cycles.sort_values("cycle_ts")["cycle_id_assigned"].tolist()

    skip_m = xfers["outbound"] & xfers["source_book"].astype(str).eq(
        "Spreadgr Base 2"
    )
    skip = xfers.loc[skip_m].copy()
    skip["cycle_id_assigned"] = pd.NA
    skip["cycle_ts"] = pd.NaT
    skip["assigned_from"] = "hedger_to_base2"

    keep = xfers.loc[~skip_m & xfers["cycle_id_recorded"].notna()].copy()
    keep["cycle_id_assigned"] = keep["cycle_id_recorded"]
    keep["cycle_ts"] = keep["cycle_id_assigned"].map(cycle_ts_by_id)
    keep["assigned_from"] = "recorded"

    need = xfers.loc[~skip_m & xfers["cycle_id_recorded"].isna()].copy()

    def _next_cycle(ts):
        t = np.datetime64(naive_utc(ts))
        i = int(np.searchsorted(cts, t, side="right"))
        if i >= len(cts):
            return pd.NA, pd.NaT
        return cids[i], pd.Timestamp(cts[i])

    asg = [_next_cycle(t) for t in need["timestamp"]]
    need["cycle_id_assigned"] = [a[0] for a in asg]
    need["cycle_ts"] = [a[1] for a in asg]
    need["assigned_from"] = "next_intention"

    assigned = pd.concat([keep, need, skip], ignore_index=True).sort_values(
        "timestamp"
    )
    assigned["lag_ms"] = (
        (assigned["cycle_ts"] - assigned["timestamp"]).dt.total_seconds() * 1000
    )
    return assigned.reset_index(drop=True)


def compute_timing_mr(
    client: Client,
    assigned: pd.DataFrame,
    cfg: AppConfig | None = None,
) -> pd.DataFrame:
    """Stamp ``timing_mr`` on assigned transfers (not hedger→base2)."""
    c = cfg or CONFIG
    size = c.contract.size
    out = assigned.copy()
    out["timing_mr"] = 0.0

    rows = out.loc[
        out["cycle_ts"].notna() & (out["assigned_from"] != "hedger_to_base2")
    ].copy()
    if rows.empty:
        return out

    need_cols = ["side", "quantity", "start_tenor", "end_tenor"]
    missing = [col for col in need_cols if col not in rows.columns]
    if missing:
        extra = client.query_df(
            f"""
            SELECT id, side, quantity, start_tenor, end_tenor
            FROM algo.nexus_transfers
            WHERE id IN ({','.join(str(int(i)) for i in rows['id'].unique())})
            """
        )
        rows = rows.drop(columns=[c for c in need_cols if c in rows.columns], errors="ignore")
        rows = rows.merge(extra, on="id", how="left")

    times = [naive_utc(t) for t in list(rows["timestamp"]) + list(rows["cycle_ts"])]
    curves = load_curve_asof_many(client, times, c.curves)

    pnl: list[float] = []
    work = rows[
        ["timestamp", "side", "quantity", "start_tenor", "end_tenor", "cycle_ts"]
    ]
    for ts, side, qty, st, en, cts in work.itertuples(index=False, name=None):
        signed = book_signed_qty(side, qty)
        if signed == 0.0:
            pnl.append(0.0)
            continue
        legs = pack_to_consecutive_lots(
            pd.Timestamp(st).normalize(),
            pd.Timestamp(en).normalize(),
            signed,
        )
        s0 = curves.asof(naive_utc(ts))
        s1 = curves.asof(naive_utc(cts))
        if s0 is None or s1 is None:
            pnl.append(float("nan"))
            continue
        total = 0.0
        for tenor, lots in legs.items():
            v0 = float(s0.reindex([tenor]).fillna(0.0).iloc[0])
            v1 = float(s1.reindex([tenor]).fillna(0.0).iloc[0])
            total += float(lots) * size * (v1 - v0)
        pnl.append(round(total, 2))

    rows = rows.copy()
    rows["timing_mr"] = pnl
    out = out.drop(columns=["timing_mr"], errors="ignore")
    out = out.merge(rows[["id", "timing_mr"]], on="id", how="left")
    out["timing_mr"] = pd.to_numeric(out["timing_mr"], errors="coerce").fillna(0.0)
    return out


def attribute_day_sleeves(
    client: Client,
    asof_date: date,
    book: str,
    assigned: pd.DataFrame,
    cfg: AppConfig | None = None,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """Build ledger + no-roll sleeves for one day.

    Returns ``(tl_mr, totals)`` where ``totals`` has sleeve columns + ``ours``.
    """
    c = cfg or CONFIG
    tl = build_position_timeline(client, asof_date, book, c)
    if tl.empty:
        zeros = {k: 0.0 for k in SLEEVE_COLS}
        zeros["ours"] = 0.0
        return tl, zeros

    tl_mr = stamp_overnight_on_timeline(client, tl, asof_date, book, c)
    tl_mr = stamp_timing_mr_on_timeline(tl_mr, assigned)
    intents = load_intentions_for_day(client, asof_date, book, c.intentions)
    tl_mr = stamp_pca_held_on_timeline(tl_mr, intents)
    tl_mr = stamp_cycle_component_mr_on_timeline(client, tl_mr, intents, c)

    totals = {
        "overnight": float(tl_mr["overnight"].iloc[0]),
        "timing_mr": float(tl_mr["timing_mr"].sum()),
        "pca_held_mr": float(tl_mr["pca_held_mr"].sum()),
        "fill_mr": float(tl_mr["fill_mr"].sum()),
        "mark_to_fill": float(tl_mr["mark_to_fill"].sum()),
        "unexecuted_mr": float(tl_mr["unexecuted_mr"].sum()),
    }
    totals["ours"] = round(sum(totals[k] for k in SLEEVE_COLS), 2)
    return tl_mr, totals


def attribute_range_vs_library(
    client: Client,
    book: str,
    start: date,
    end: date | None = None,
    cfg: AppConfig | None = None,
) -> pd.DataFrame:
    """No-roll sleeve totals vs library gross for each day with library EOD."""
    end = end or date.today()
    assigned = assign_transfers_to_cycles(client, book, start, end, cfg)
    assigned = compute_timing_mr(client, assigned, cfg)

    rows: list[dict] = []
    for d in pd.date_range(start, end, freq="D").date.tolist():
        lib = load_library_eod_pnl(client, d, book)
        if lib is None:
            continue
        intents = load_intentions_for_day(client, d, book)
        if intents is None or intents.empty:
            continue
        _tl, totals = attribute_day_sleeves(client, d, book, assigned, cfg)
        if _tl.empty:
            continue
        lib_g = round(float(lib["gross"]), 2)
        diff = round(totals["ours"] - lib_g, 2)
        rows.append(
            {
                "date": d,
                **{k: totals[k] for k in SLEEVE_COLS},
                "ours": totals["ours"],
                "library_gross": lib_g,
                "diff": diff,
            }
        )
    return pd.DataFrame(rows)


__all__ = [
    "SLEEVE_COLS",
    "assign_transfers_to_cycles",
    "compute_timing_mr",
    "attribute_day_sleeves",
    "attribute_range_vs_library",
]
