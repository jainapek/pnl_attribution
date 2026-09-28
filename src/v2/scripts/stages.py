"""Build transfer_pred / pca / routing peels in spread space.

Intention maps are already consecutive monthly spreads
(``2026-09-01`` → Sep/Oct lots) — no outright→spread conversion.
"""

from __future__ import annotations

import pandas as pd

from .spreads import as_row, densify_spreads, position_map_to_series

_MAP_FIELDS = (
    "position_before",
    "position_received",
    "position_held_prediction",
    "position_held_pca",
    "position_triggered",
)


def build_stages_from_row(row: pd.Series) -> dict[str, dict[str, pd.DataFrame]]:
    """One intention row → spread-space ``{stage: {in, held, out}}``."""
    maps = {
        name: position_map_to_series(row.get(name) or {}) for name in _MAP_FIELDS
    }

    all_idx = pd.DatetimeIndex([])
    for s in maps.values():
        if not s.empty:
            all_idx = all_idx.union(s.index)

    if len(all_idx) == 0:
        empty = as_row(pd.Series(dtype=float))
        zero = {"in": empty, "held": empty, "out": empty}
        return {"transfer_pred": zero, "pca": zero, "routing": zero}

    months = pd.date_range(all_idx.min(), all_idx.max(), freq="MS")
    spreads = {k: densify_spreads(v, months) for k, v in maps.items()}

    pos_to_be_hedged = as_row(spreads["position_before"]) + as_row(
        spreads["position_received"]
    )
    transfer_pred = {
        "in": pos_to_be_hedged,
        "held": as_row(spreads["position_held_prediction"]),
        "out": pos_to_be_hedged - as_row(spreads["position_held_prediction"]),
    }
    pca_in = transfer_pred["out"]
    pca = {
        "in": pca_in,
        "held": as_row(spreads["position_held_pca"]),
        "out": pca_in - as_row(spreads["position_held_pca"]),
    }
    routing = {
        "in": pca["out"],
        "out": as_row(spreads["position_triggered"]),
        "held": (pca["out"] - as_row(spreads["position_triggered"])).fillna(0),
    }
    return {"transfer_pred": transfer_pred, "pca": pca, "routing": routing}
