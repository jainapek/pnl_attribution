"""Event transforms."""

from __future__ import annotations

import numpy as np
import pandas as pd

from pnl_attribution.transforms.tenor_converter import TenorConverter


class EventTransformer:
    """Transform trades and transfers."""

    def __init__(self, tenor_converter: TenorConverter) -> None:
        """Store dependencies."""

        self._tenor_converter = tenor_converter

    def build_transfer_deltas(self, transfers_df: pd.DataFrame) -> pd.DataFrame:
        """Build transfer spread deltas."""

        if transfers_df.empty:
            return pd.DataFrame()
        transfers = transfers_df.copy()
        transfers["signed_qty"] = np.where(
            transfers["side"].str.lower() == "sell",
            transfers["quantity"].astype(float),
            -1.0 * transfers["quantity"].astype(float),
        )
        return self._build_spread_delta_frame(
            transfers,
            time_column="timestamp",
            start_column="start_tenor",
            end_column="end_tenor",
        )

    def build_trade_deltas(self, trades_df: pd.DataFrame) -> pd.DataFrame:
        """Build trade spread deltas."""

        if trades_df.empty:
            return pd.DataFrame()
        trades = trades_df.copy()
        trades["contract"] = trades["instrument_key"].apply(self._tenor_converter.extract_trade_contract)
        trades["contract"] = trades["contract"].apply(self._tenor_converter.trade_contract_to_range)
        trades[["start_tenor", "end_tenor"]] = trades["contract"].str.split("/", expand=True)
        trades["signed_qty"] = np.where(
            trades["side"].str.lower() == "buy",
            trades["quantity"].astype(float),
            -1.0 * trades["quantity"].astype(float),
        )
        return self._build_spread_delta_frame(
            trades,
            time_column="transaction_timestamp",
            start_column="start_tenor",
            end_column="end_tenor",
        )

    def collapse_eod(self, deltas: pd.DataFrame) -> pd.Series:
        """Collapse deltas into one series."""

        if deltas.empty:
            return pd.Series(dtype=float)
        return deltas.sum(axis=0)

    def build_aligned_cumsum(self, deltas: pd.DataFrame, timestamps: pd.Index, start_at: pd.Timestamp) -> pd.DataFrame:
        """Build aligned cumulative deltas."""

        if deltas.empty:
            return pd.DataFrame(index=timestamps[timestamps >= start_at])
        aligned = deltas.reindex(timestamps).fillna(0)
        return aligned.loc[aligned.index >= start_at].cumsum()

    def _build_spread_delta_frame(
        self,
        frame: pd.DataFrame,
        time_column: str,
        start_column: str,
        end_column: str,
    ) -> pd.DataFrame:
        """Build a timestamped spread delta frame."""

        spread_rows = []
        for _, row in frame.iterrows():
            spread_delta = self._tenor_converter.expand_range_to_monthly_spreads(
                row[start_column],
                row[end_column],
                row["signed_qty"],
            )
            if spread_delta.empty:
                continue
            spread_rows.append({time_column: row[time_column], **spread_delta.to_dict()})
        if not spread_rows:
            return pd.DataFrame()
        deltas = pd.DataFrame(spread_rows).fillna(0)
        return deltas.groupby(time_column).sum().sort_index()
