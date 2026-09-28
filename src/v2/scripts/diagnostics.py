"""Diagnostics on intention position maps (already spread-keyed)."""

from __future__ import annotations

from datetime import date

import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG
from .intentions import cycles_for_day, load_intentions_for_day
from .spreads import position_map_to_series

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
