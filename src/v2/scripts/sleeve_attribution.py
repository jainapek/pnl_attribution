"""Transfer assignment + day/range sleeve drivers.

Day sleeves are the event-walk split in ``event_walk_sleeves``:
overnight + timing + pca_held_mr + fill_mr + mark_to_fill + unexecuted
(with Base2 outbound entry BV inside unexecuted).
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG, AppConfig
from .market_cache import load_curve_asof_many
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
) -> tuple[pd.DataFrame, dict]:
    """Split event-walk gross into six sleeves (exact allocation).

    See ``event_walk_sleeves.attribute_day_event_walk_sleeves``.
    """
    from .event_walk_sleeves import attribute_day_event_walk_sleeves

    return attribute_day_event_walk_sleeves(
        client, asof_date, book, assigned=assigned, cfg=cfg
    )


def attribute_range_vs_library(
    client: Client,
    book: str,
    start: date,
    end: date | None = None,
    cfg: AppConfig | None = None,
) -> pd.DataFrame:
    """Sleeve totals vs event-walk / library gross for each day with library EOD."""
    from .event_walk_sleeves import attribute_range_event_walk_sleeves

    return attribute_range_event_walk_sleeves(client, book, start, end, cfg)


def attribute_range_by_cycle(
    client: Client,
    book: str,
    start: date,
    end: date | None = None,
    cfg: AppConfig | None = None,
) -> pd.DataFrame:
    """Per-cycle sleeve PnL across days. See ``event_walk_sleeves``."""
    from .event_walk_sleeves import attribute_range_by_cycle as _impl

    return _impl(client, book, start, end, cfg)


__all__ = [
    "SLEEVE_COLS",
    "assign_transfers_to_cycles",
    "compute_timing_mr",
    "attribute_day_sleeves",
    "attribute_range_vs_library",
    "attribute_range_by_cycle",
]
