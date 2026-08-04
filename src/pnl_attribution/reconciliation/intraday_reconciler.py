"""Intraday reconciler."""

from __future__ import annotations

import pandas as pd

from pnl_attribution.models import IntradayReconciliationResult, ReconciliationRequest


class IntradayReconciler:
    """Reconcile intraday inventory."""

    def __init__(
        self,
        position_repository,
        trade_repository,
        transfer_repository,
        position_transformer,
        event_transformer,
    ) -> None:
        """Store dependencies."""

        self._position_repository = position_repository
        self._trade_repository = trade_repository
        self._transfer_repository = transfer_repository
        self._position_transformer = position_transformer
        self._event_transformer = event_transformer

    def build(
        self,
        request: ReconciliationRequest,
        tenor: str,
        resample_rule: str | None = None,
    ) -> IntradayReconciliationResult:
        """Build the intraday reconciliation."""

        positions = self._position_repository.fetch(request.date, request.book_ids)
        trades = self._trade_repository.fetch(request.date)
        transfers = self._transfer_repository.fetch(request.date)

        months = self._position_transformer.build_month_grid(positions)
        all_event_timestamps = self._build_event_timestamps(positions, trades, transfers)
        first_transfer_ts = self._first_transfer_ts(positions, transfers)

        actual_outrights = self._position_transformer.build_intraday_outright_inventory(
            positions,
            months,
            all_event_timestamps,
        )
        actual_spreads = self._position_transformer.outright_frame_to_spreads(actual_outrights, months)
        actual_spreads = actual_spreads.loc[actual_spreads.index >= first_transfer_ts]

        seed_outrights = self._position_transformer.build_seed_outright_inventory(
            positions,
            months,
            first_transfer_ts,
        )
        seed_inventory = self._position_transformer.outright_series_to_spreads(seed_outrights, months)

        transfer_cumsum = self._event_transformer.build_aligned_cumsum(
            self._event_transformer.build_transfer_deltas(transfers),
            all_event_timestamps,
            first_transfer_ts,
        )
        trade_cumsum = self._event_transformer.build_aligned_cumsum(
            self._event_transformer.build_trade_deltas(trades),
            all_event_timestamps,
            first_transfer_ts,
        )

        calc_spreads = transfer_cumsum.add(trade_cumsum, fill_value=0).add(
            seed_inventory.reindex(actual_spreads.columns).fillna(0),
            axis="columns",
        )
        actual_spreads = actual_spreads.reindex(calc_spreads.index, method="ffill").reindex(
            columns=calc_spreads.columns,
            fill_value=0,
        )

        timeseries = pd.DataFrame(
            {
                "actual_inventory": actual_spreads.reindex(columns=[tenor], fill_value=0)[tenor],
                "calc_inventory": calc_spreads.reindex(columns=[tenor], fill_value=0)[tenor],
                "transfer_cumsum": transfer_cumsum.reindex(columns=[tenor], fill_value=0)[tenor],
                "trade_cumsum": trade_cumsum.reindex(columns=[tenor], fill_value=0)[tenor],
            }
        )
        timeseries["residual"] = timeseries["calc_inventory"] - timeseries["actual_inventory"]
        if resample_rule:
            timeseries = timeseries.resample(resample_rule).last().ffill()

        return IntradayReconciliationResult(
            request=request,
            tenor=tenor,
            resample_rule=resample_rule,
            first_transfer_ts=first_transfer_ts,
            timeseries=timeseries,
        )

    def _build_event_timestamps(
        self,
        positions: pd.DataFrame,
        trades: pd.DataFrame,
        transfers: pd.DataFrame,
    ) -> pd.Index:
        """Build the intraday timestamp index."""

        timestamps = set(positions["timestamp"])
        if not trades.empty:
            timestamps |= set(trades["transaction_timestamp"])
        if not transfers.empty:
            timestamps |= set(transfers["timestamp"])
        return pd.Index(sorted(timestamps), name="timestamp")

    def _first_transfer_ts(self, positions: pd.DataFrame, transfers: pd.DataFrame) -> pd.Timestamp:
        """Pick the seed timestamp."""

        if not transfers.empty:
            return transfers["timestamp"].min()
        return positions["timestamp"].min()
