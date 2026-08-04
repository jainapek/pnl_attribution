"""Position transforms."""

from __future__ import annotations

import pandas as pd

from pnl_attribution.transforms.tenor_converter import TenorConverter


class PositionTransformer:
    """Transform position snapshots."""

    def __init__(self, tenor_converter: TenorConverter) -> None:
        """Store dependencies."""

        self._tenor_converter = tenor_converter

    def build_month_grid(self, positions_df: pd.DataFrame) -> list[str]:
        """Build the dense month grid."""

        sparse_months = sorted(
            {month for month_map in positions_df["positions"] for month in month_map.keys()}
        )
        return self._tenor_converter.build_month_grid(sparse_months)

    def build_latest_outright_inventory(self, positions_df: pd.DataFrame, months: list[str]) -> pd.Series:
        """Build the latest outright inventory."""

        latest_rows = positions_df.groupby("book_id").tail(1).sort_values("book_id")
        return self._sum_outright_rows(latest_rows, months)

    def build_seed_outright_inventory(
        self,
        positions_df: pd.DataFrame,
        months: list[str],
        cutoff: pd.Timestamp,
    ) -> pd.Series:
        """Build the seed outright inventory."""

        seed_rows = positions_df[positions_df["timestamp"] <= cutoff]
        if seed_rows.empty:
            return pd.Series(0.0, index=months, dtype=float)
        seed_rows = seed_rows.groupby("book_id").tail(1).sort_values("book_id")
        return self._sum_outright_rows(seed_rows, months)

    def build_intraday_outright_inventory(
        self,
        positions_df: pd.DataFrame,
        months: list[str],
        timestamps: pd.Index,
    ) -> pd.DataFrame:
        """Build the intraday outright inventory."""

        book_frames = []
        for _, group in positions_df.groupby("book_id"):
            book_outrights = pd.DataFrame(group.positions.to_list(), index=group["timestamp"])
            book_outrights = book_outrights.reindex(columns=months).sort_index().groupby(level=0).last()
            book_outrights = book_outrights.reindex(timestamps).ffill().fillna(0)
            book_frames.append(book_outrights)
        if not book_frames:
            return pd.DataFrame(index=timestamps, columns=months).fillna(0)
        return pd.concat(book_frames).groupby(level=0).sum().sort_index()

    def outright_series_to_spreads(self, outrights: pd.Series, months: list[str]) -> pd.Series:
        """Convert an outright series into spreads."""

        return self._tenor_converter.outright_to_consecutive_spreads(outrights, months)

    def outright_frame_to_spreads(self, outrights: pd.DataFrame, months: list[str]) -> pd.DataFrame:
        """Convert an outright frame into spreads."""

        return self._tenor_converter.frame_to_consecutive_spreads(outrights, months)

    def _sum_outright_rows(self, rows: pd.DataFrame, months: list[str]) -> pd.Series:
        """Sum mapped outright rows."""

        outright_rows = []
        for _, row in rows.iterrows():
            outright_rows.append(pd.Series(row["positions"], dtype=float).reindex(months).fillna(0))
        if not outright_rows:
            return pd.Series(0.0, index=months, dtype=float)
        return pd.DataFrame(outright_rows).fillna(0).sum(axis=0)
