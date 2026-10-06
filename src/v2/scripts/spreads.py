"""Intention / book positions in consecutive monthly spread space.

Maps are already spreads: key ``2026-09-01`` = Sep/Oct contract lots.
"""

from __future__ import annotations

import pandas as pd


def position_map_to_series(pos_map) -> pd.Series:
    """Intention map ``{front_month: lots}`` → sorted DatetimeIndex series."""
    if not pos_map:
        return pd.Series(dtype=float)
    s = pd.Series(pos_map, dtype=float)
    s.index = pd.to_datetime(s.index).normalize()
    return s.sort_index()


__all__ = ["position_map_to_series"]
