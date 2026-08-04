import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from pnl_attribution.transforms.position_transformer import PositionTransformer
from pnl_attribution.transforms.tenor_converter import TenorConverter


class PositionTransformerTest(unittest.TestCase):
    """Test position transforms."""

    def setUp(self) -> None:
        """Build the transformer."""

        self.transformer = PositionTransformer(TenorConverter())

    def test_build_month_grid_uses_dense_calendar(self) -> None:
        """Densify sparse months."""

        positions = pd.DataFrame(
            {
                "timestamp": [pd.Timestamp("2026-01-01")],
                "book_id": [1],
                "positions": [{"2028-12-01": 1.0, "2029-02-01": -1.0}],
            }
        )
        months = self.transformer.build_month_grid(positions)
        self.assertEqual(months, ["2028-12-01", "2029-01-01", "2029-02-01"])

    def test_build_intraday_outright_inventory_forward_fills_per_book(self) -> None:
        """Forward fill book snapshots."""

        positions = pd.DataFrame(
            {
                "timestamp": [
                    pd.Timestamp("2026-07-28 07:36:57.634"),
                    pd.Timestamp("2026-07-28 07:36:57.653"),
                ],
                "book_id": [129, 337],
                "positions": [
                    {"2026-10-01": 2.0, "2026-11-01": -2.0},
                    {"2026-10-01": 3.0, "2026-11-01": -3.0},
                ],
            }
        )
        months = ["2026-10-01", "2026-11-01"]
        timestamps = pd.Index(sorted(positions["timestamp"]), name="timestamp")

        inventory = self.transformer.build_intraday_outright_inventory(positions, months, timestamps)
        self.assertEqual(inventory.loc[pd.Timestamp("2026-07-28 07:36:57.653"), "2026-10-01"], 5.0)


if __name__ == "__main__":
    unittest.main()
