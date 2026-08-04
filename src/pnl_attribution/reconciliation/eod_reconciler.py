"""EOD reconciler."""

from __future__ import annotations

import pandas as pd

from pnl_attribution.models import EODReconciliationResult, ReconciliationRequest


class EODReconciler:
    """Reconcile end-of-day inventory."""

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

    def build(self, request: ReconciliationRequest) -> EODReconciliationResult:
        """Build the EOD reconciliation."""

        positions = self._position_repository.fetch(request.date, request.book_ids)
        trades = self._trade_repository.fetch(request.date)
        transfers = self._transfer_repository.fetch(request.date)

        months = self._position_transformer.build_month_grid(positions)
        actual_outrights = self._position_transformer.build_latest_outright_inventory(positions, months)
        actual_eod = self._position_transformer.outright_series_to_spreads(actual_outrights, months)

        first_transfer_ts = self._first_transfer_ts(positions, transfers)
        seed_outrights = self._position_transformer.build_seed_outright_inventory(
            positions,
            months,
            first_transfer_ts,
        )
        seed_inventory = self._position_transformer.outright_series_to_spreads(seed_outrights, months)

        transfer_eod = self._event_transformer.collapse_eod(
            self._event_transformer.build_transfer_deltas(transfers)
        )
        trade_eod = self._event_transformer.collapse_eod(
            self._event_transformer.build_trade_deltas(trades)
        )

        comparison = self._build_comparison(actual_eod, seed_inventory, transfer_eod, trade_eod)
        return EODReconciliationResult(
            request=request,
            months=months,
            first_transfer_ts=first_transfer_ts,
            comparison=comparison,
        )

    def _first_transfer_ts(self, positions: pd.DataFrame, transfers: pd.DataFrame) -> pd.Timestamp:
        """Pick the seed timestamp."""

        if not transfers.empty:
            return transfers["timestamp"].min()
        return positions["timestamp"].min()

    def _build_comparison(
        self,
        actual_eod: pd.Series,
        seed_inventory: pd.Series,
        transfer_eod: pd.Series,
        trade_eod: pd.Series,
    ) -> pd.DataFrame:
        """Build the comparison frame."""

        all_spreads = sorted(
            set(actual_eod.index)
            | set(seed_inventory.index)
            | set(transfer_eod.index)
            | set(trade_eod.index)
        )
        comparison = pd.DataFrame(
            {
                "seed_inventory": seed_inventory.reindex(all_spreads).fillna(0),
                "transfer_delta": transfer_eod.reindex(all_spreads).fillna(0),
                "trade_delta": trade_eod.reindex(all_spreads).fillna(0),
                "actual_eod": actual_eod.reindex(all_spreads).fillna(0),
            }
        )
        comparison["calc_eod"] = (
            comparison["seed_inventory"]
            + comparison["transfer_delta"]
            + comparison["trade_delta"]
        )
        comparison["residual"] = comparison["calc_eod"] - comparison["actual_eod"]
        comparison["abs_residual"] = comparison["residual"].abs()
        return comparison
