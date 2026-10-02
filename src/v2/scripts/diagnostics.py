"""Diagnostics on intention position maps (already spread-keyed)."""

from __future__ import annotations

from datetime import date

import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG
from .intentions import cycles_for_day, load_intentions_for_day
from .spreads import position_map_to_series
from .transfers import (
    load_transfers_for_book_day,
    to_utc,
    transfer_lots_series,
)

# Maps written on each intention row (peel stages + bookends)
POSITION_FIELDS = (
    "position_before",
    "position_received",
    "position_held_prediction",
    "position_held_pca",
    "position_triggered",
    "position_held_other",
    "position_held_preposition",
    "position_held_crossing_buffer",
)


def map_lot_sum(pos_map) -> float:
    """Sum of spread lots across front-month keys."""
    s = position_map_to_series(pos_map or {})
    if s.empty:
        return 0.0
    return float(s.sum())


def intention_row_lot_sums(row: pd.Series) -> dict[str, float]:
    """Per-field tenor-lot sum for one intention row."""
    return {field: map_lot_sum(row.get(field)) for field in POSITION_FIELDS}


def check_intention_lot_sums(
    client: Client,
    asof_date: date,
    book: str,
    *,
    atol: float = 1e-9,
) -> pd.DataFrame:
    """For each cycle starter row: sum of lots in every position map.

    Maps are already M/M+1 spreads (key = front month); lot-sum is a
    diagnostic residual, not an outright-flatness check.
    """
    intentions = load_intentions_for_day(client, asof_date, book, CONFIG.intentions)
    if intentions.empty:
        return pd.DataFrame(columns=list(POSITION_FIELDS) + ["all_flat"])

    cycles = cycles_for_day(intentions, asof_date)
    rows = []
    for _, row in cycles.iterrows():
        sums = intention_row_lot_sums(row)
        sums["cycle_id"] = row["cycle_id"]
        sums["timestamp"] = row["timestamp"]
        sums["all_flat"] = all(abs(sums[f]) <= atol for f in POSITION_FIELDS)
        rows.append(sums)

    out = pd.DataFrame(rows).set_index("cycle_id")
    cols = ["timestamp", *POSITION_FIELDS, "all_flat"]
    return out[cols]


def intention_positions_by_cycle(
    client: Client,
    asof_date: date,
    book: str,
) -> pd.DataFrame:
    """Raw position maps (tenor → lots) per cycle starter row.

    Index = ``cycle_id``. Columns = ``timestamp`` + each ``POSITION_FIELDS`` map.
    """
    intentions = load_intentions_for_day(client, asof_date, book, CONFIG.intentions)
    if intentions.empty:
        return pd.DataFrame(columns=["timestamp", *POSITION_FIELDS])

    cycles = cycles_for_day(intentions, asof_date)
    cols = ["cycle_id", "timestamp", *POSITION_FIELDS]
    out = cycles[cols].copy()
    return out.set_index("cycle_id")


def _absdiff(a: pd.Series, b: pd.Series) -> float:
    idx = a.index.union(b.index)
    if len(idx) == 0:
        return 0.0
    return float(
        (a.reindex(idx, fill_value=0.0) - b.reindex(idx, fill_value=0.0)).abs().sum()
    )


def _received_series(row: pd.Series) -> pd.Series:
    s = position_map_to_series(row.get("position_received") or {})
    if s.empty:
        return s
    s.index = pd.DatetimeIndex(s.index).normalize()
    return s.sort_index()


def _coalesce_cycle_groups(
    cycles: pd.DataFrame, *, coalesce_ms: float
) -> list[list[int]]:
    """Group cycles whose starts fall within ``coalesce_ms`` of the group head."""
    if cycles.empty:
        return []
    groups: list[list[int]] = []
    cur = [int(cycles.index[0])]
    head_ts = to_utc(cycles.loc[cur[0], "timestamp"])
    for i in cycles.index[1:]:
        ts = to_utc(cycles.loc[i, "timestamp"])
        if (ts - head_ts).total_seconds() * 1000.0 <= coalesce_ms:
            cur.append(int(i))
        else:
            groups.append(cur)
            cur = [int(i)]
            head_ts = ts
    groups.append(cur)
    return groups


def check_position_received_vs_transfers(
    client: Client,
    asof_date: date,
    book: str,
    *,
    coalesce_ms: float = 1000.0,
    exclude_eod_roll: bool = True,
    atol: float = 1e-9,
) -> dict[str, pd.DataFrame | dict]:
    """Prove ``Σ position_received`` ≡ pack-expanded ``nexus_transfers``.

    Conventions (must match intentions):
    - spread space (front-month keys), not outrights
    - multi-month packs → N lots on each consecutive 1m front
    - book sign: desk buy → −, desk sell → +
    - transfers filtered by ``source_book``; optional drop London hour ≥ 21

    Returns dict with:
    - ``day``: one-row day totals + abs_diff
    - ``tenor``: day-level received vs transfers by front month
    - ``cycles``: per coalesced cycle-group window check
      (``(prev_end, group_end]`` transfers vs Σ received in group)
    """
    intentions = load_intentions_for_day(client, asof_date, book, CONFIG.intentions)
    cycles = cycles_for_day(intentions, asof_date).reset_index(drop=True)
    transfers = load_transfers_for_book_day(
        client, book, asof_date, exclude_eod_roll=exclude_eod_roll
    )

    recv_day = pd.Series(dtype=float)
    for _, row in cycles.iterrows():
        recv_day = recv_day.add(_received_series(row), fill_value=0.0)
    xfer_day = transfer_lots_series(transfers)

    tenor_idx = recv_day.index.union(xfer_day.index)
    tenor = pd.DataFrame(
        {
            "received": recv_day.reindex(tenor_idx, fill_value=0.0),
            "transfers": xfer_day.reindex(tenor_idx, fill_value=0.0),
        }
    )
    tenor["delta"] = tenor["received"] - tenor["transfers"]
    day_abs_diff = float(tenor["delta"].abs().sum()) if not tenor.empty else 0.0

    day = pd.DataFrame(
        [
            {
                "date": asof_date,
                "book": book,
                "n_cycles": int(len(cycles)),
                "n_transfers": int(len(transfers)),
                "abs_received": float(recv_day.abs().sum()),
                "abs_transfers": float(xfer_day.abs().sum()),
                "signed_received": float(recv_day.sum()),
                "signed_transfers": float(xfer_day.sum()),
                "abs_diff": day_abs_diff,
                "ok": day_abs_diff <= atol,
            }
        ]
    )

    xf = transfers.copy()
    if not xf.empty:
        xf["ts_utc"] = xf["timestamp"].map(to_utc)

    groups = _coalesce_cycle_groups(cycles, coalesce_ms=coalesce_ms)
    cycle_rows: list[dict] = []
    prev_end: pd.Timestamp | None = None
    for g in groups:
        t_end = max(to_utc(cycles.loc[i, "timestamp"]) for i in g)
        if xf.empty:
            gap = xf
            xfer_s = pd.Series(dtype=float)
        elif prev_end is None:
            gap = xf.loc[xf["ts_utc"] <= t_end]
            xfer_s = transfer_lots_series(gap)
        else:
            gap = xf.loc[(xf["ts_utc"] > prev_end) & (xf["ts_utc"] <= t_end)]
            xfer_s = transfer_lots_series(gap)

        recv_s = pd.Series(dtype=float)
        for i in g:
            recv_s = recv_s.add(_received_series(cycles.loc[i]), fill_value=0.0)

        abs_diff = _absdiff(recv_s, xfer_s)
        cycle_rows.append(
            {
                "date": asof_date,
                "book": book,
                "group_ts": cycles.loc[g[0], "timestamp"],
                "n_cycles": len(g),
                "cycle_ids": ",".join(str(cycles.loc[i, "cycle_id"]) for i in g),
                "n_transfers": int(len(gap)) if gap is not None else 0,
                "abs_received": float(recv_s.abs().sum()),
                "abs_transfers": float(xfer_s.abs().sum()),
                "abs_diff": abs_diff,
                "ok": abs_diff <= atol,
            }
        )
        prev_end = t_end

    cycles_df = pd.DataFrame(cycle_rows)
    return {"day": day, "tenor": tenor, "cycles": cycles_df}


def check_position_received_vs_transfers_range(
    client: Client,
    book: str,
    start: date,
    end: date | None = None,
    *,
    coalesce_ms: float = 1000.0,
    exclude_eod_roll: bool = True,
    atol: float = 1e-9,
) -> pd.DataFrame:
    """Day-level ``position_received`` vs transfers summary over a date range."""
    if end is None:
        end = start
    rows: list[dict] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out = check_position_received_vs_transfers(
                client,
                d,
                book,
                coalesce_ms=coalesce_ms,
                exclude_eod_roll=exclude_eod_roll,
                atol=atol,
            )
            day = out["day"]
            if not day.empty and (
                int(day.iloc[0]["n_cycles"]) > 0 or int(day.iloc[0]["n_transfers"]) > 0
            ):
                rows.append(day.iloc[0].to_dict())
        d = date.fromordinal(d.toordinal() + 1)
    return pd.DataFrame(rows)
