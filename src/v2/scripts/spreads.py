"""Intention / stage positions in consecutive monthly spread space.

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


# Back-compat alias (maps were previously treated as outrights)
outright_map_to_series = position_map_to_series


def densify_spreads(s: pd.Series, months: pd.DatetimeIndex) -> pd.Series:
    """Reindex onto a dense month grid; missing tenors → 0."""
    if months.empty:
        return pd.Series(dtype=float)
    return s.reindex(months, fill_value=0.0)


def as_row(s: pd.Series) -> pd.DataFrame:
    """Single-row DataFrame with tenors as columns (notebook-friendly)."""
    if s.empty:
        return pd.DataFrame(index=[0])
    return pd.DataFrame([s.values], columns=s.index, index=[0])


def series_from_row(df: pd.DataFrame) -> pd.Series:
    """Inverse of ``as_row``."""
    if df.empty:
        return pd.Series(dtype=float)
    s = df.iloc[0].astype(float)
    s.index = pd.to_datetime(s.index).normalize()
    return s.sort_index()
