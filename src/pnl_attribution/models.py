"""Data models."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class ReconciliationRequest:
    """Reconciliation inputs."""

    date: str
    book_ids: tuple[int, ...]


@dataclass(frozen=True)
class EODReconciliationResult:
    """EOD reconciliation output."""

    request: ReconciliationRequest
    months: list[str]
    first_transfer_ts: pd.Timestamp
    comparison: pd.DataFrame


@dataclass(frozen=True)
class IntradayReconciliationResult:
    """Intraday reconciliation output."""

    request: ReconciliationRequest
    tenor: str
    resample_rule: str | None
    first_transfer_ts: pd.Timestamp
    timeseries: pd.DataFrame

    @property
    def inventory_timeseries(self) -> pd.DataFrame:
        """Return the intraday inventory view."""

        return self.timeseries[[
            "actual_inventory",
            "calc_inventory",
            "transfer_cumsum",
            "trade_cumsum",
            "residual",
        ]]
