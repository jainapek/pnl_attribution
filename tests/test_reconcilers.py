import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from pnl_attribution.models import ReconciliationRequest
from pnl_attribution.reconciliation.eod_reconciler import EODReconciler
from pnl_attribution.reconciliation.intraday_reconciler import IntradayReconciler
from pnl_attribution.reporting.reconciliation_reporter import ReconciliationReporter
from pnl_attribution.transforms.event_transformer import EventTransformer
from pnl_attribution.transforms.position_transformer import PositionTransformer
from pnl_attribution.transforms.tenor_converter import TenorConverter


class FakePositionRepository:
    """Return synthetic positions."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame

    def fetch(self, date: str, book_ids: tuple[int, ...]) -> pd.DataFrame:
        return self._frame.copy()


class FakeTradeRepository:
    """Return synthetic trades."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame

    def fetch(self, date: str) -> pd.DataFrame:
        return self._frame.copy()


class FakeTransferRepository:
    """Return synthetic transfers."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame

    def fetch(self, date: str) -> pd.DataFrame:
        return self._frame.copy()


class ReconcilerTest(unittest.TestCase):
    """Test reconcilers."""

    def setUp(self) -> None:
        """Build shared fixtures."""

        timestamps = [
            pd.Timestamp("2026-07-28 07:00:00", tz="Europe/London"),
            pd.Timestamp("2026-07-28 08:00:00", tz="Europe/London"),
            pd.Timestamp("2026-07-28 09:00:00", tz="Europe/London"),
        ]
        self.positions = pd.DataFrame(
            {
                "timestamp": [timestamps[0], timestamps[1], timestamps[2], timestamps[0]],
                "book_id": [1, 1, 1, 2],
                "product_id": [1, 1, 1, 1],
                "positions": [
                    {"2026-10-01": 10.0, "2026-11-01": -10.0},
                    {"2026-10-01": 12.0, "2026-11-01": -12.0},
                    {"2026-10-01": 9.0, "2026-11-01": -9.0},
                    {"2026-10-01": 1.0, "2026-11-01": -1.0},
                ],
            }
        )
        self.transfers = pd.DataFrame(
            {
                "timestamp": [timestamps[1]],
                "start_tenor": [pd.Timestamp("2026-10-01")],
                "end_tenor": [pd.Timestamp("2026-11-01")],
                "quantity": [1],
                "side": ["Sell"],
            }
        )
        self.trades = pd.DataFrame(
            {
                "transaction_timestamp": [timestamps[2]],
                "instrument_key": ["ICE BRN Oct26-Nov26 Calendar"],
                "quantity": [3],
                "side": ["Sell"],
            }
        )

        tenor_converter = TenorConverter()
        position_transformer = PositionTransformer(tenor_converter)
        event_transformer = EventTransformer(tenor_converter)
        self.eod_reconciler = EODReconciler(
            FakePositionRepository(self.positions),
            FakeTradeRepository(self.trades),
            FakeTransferRepository(self.transfers),
            position_transformer,
            event_transformer,
        )
        self.intraday_reconciler = IntradayReconciler(
            FakePositionRepository(self.positions),
            FakeTradeRepository(self.trades),
            FakeTransferRepository(self.transfers),
            position_transformer,
            event_transformer,
        )
        self.reporter = ReconciliationReporter()
        self.request = ReconciliationRequest(date="2026-07-28", book_ids=(1, 2))

    def test_eod_reconciler_builds_expected_residual(self) -> None:
        """Reconcile synthetic EOD inventory."""

        result = self.eod_reconciler.build(self.request)
        row = result.comparison.loc["2026-10-01/2026-11-01"]
        self.assertEqual(row["seed_inventory"], 13.0)
        self.assertEqual(row["transfer_delta"], 1.0)
        self.assertEqual(row["trade_delta"], -3.0)
        self.assertEqual(row["actual_eod"], 10.0)
        self.assertEqual(row["calc_eod"], 11.0)
        self.assertEqual(row["residual"], 1.0)

    def test_intraday_reconciler_ends_at_eod_residual(self) -> None:
        """Match the EOD residual at the close."""

        intraday = self.intraday_reconciler.build(
            self.request,
            tenor="2026-10-01/2026-11-01",
            resample_rule=None,
        )
        self.assertEqual(intraday.inventory_timeseries.iloc[-1]["residual"], 1.0)

    def test_intraday_result_exposes_inventory_timeseries(self) -> None:
        """Expose a named intraday view."""

        intraday = self.intraday_reconciler.build(
            self.request,
            tenor="2026-10-01/2026-11-01",
            resample_rule=None,
        )
        self.assertEqual(
            list(intraday.inventory_timeseries.columns),
            [
                "actual_inventory",
                "calc_inventory",
                "transfer_cumsum",
                "trade_cumsum",
                "residual",
            ],
        )

    def test_reporter_returns_top_residuals(self) -> None:
        """Return sorted EOD residuals."""

        result = self.eod_reconciler.build(self.request)
        residuals = self.reporter.top_residuals(result, limit=1)
        self.assertEqual(list(residuals.index), ["2026-10-01/2026-11-01"])
        self.assertEqual(residuals.iloc[0]["abs_residual"], 1.0)


if __name__ == "__main__":
    unittest.main()
