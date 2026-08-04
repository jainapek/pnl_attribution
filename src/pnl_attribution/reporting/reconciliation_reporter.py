"""Reconciliation reporting helpers."""

from __future__ import annotations

import pandas as pd

from pnl_attribution.models import EODReconciliationResult


class ReconciliationReporter:
    """Format reconciliation outputs."""

    def top_residuals(self, result: EODReconciliationResult, limit: int = 20) -> pd.DataFrame:
        """Return the worst EOD residuals."""

        comparison = result.comparison.sort_values(
            ["abs_residual", "actual_eod"],
            ascending=[False, False],
        )
        return comparison.head(limit)
