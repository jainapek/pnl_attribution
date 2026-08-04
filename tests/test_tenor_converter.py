import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from pnl_attribution.transforms.tenor_converter import TenorConverter


class TenorConverterTest(unittest.TestCase):
    """Test tenor conversions."""

    def setUp(self) -> None:
        """Build the converter."""

        self.converter = TenorConverter()

    def test_trade_contract_to_range(self) -> None:
        """Convert TT contracts."""

        self.assertEqual(
            self.converter.trade_contract_to_range("Oct26-Nov26"),
            "2026-10-01/2026-11-01",
        )

    def test_expand_range_to_monthly_spreads(self) -> None:
        """Expand multi-month ranges."""

        spreads = self.converter.expand_range_to_monthly_spreads("2026-10-01", "2027-01-01", 3)
        self.assertEqual(
            spreads.to_dict(),
            {
                "2026-10-01/2026-11-01": 3.0,
                "2026-11-01/2026-12-01": 3.0,
                "2026-12-01/2027-01-01": 3.0,
            },
        )

    def test_build_month_grid_fills_gaps(self) -> None:
        """Fill sparse months."""

        months = self.converter.build_month_grid(["2028-12-01", "2029-02-01", "2029-04-01"])
        self.assertEqual(months, ["2028-12-01", "2029-01-01", "2029-02-01", "2029-03-01", "2029-04-01"])

    def test_outright_to_consecutive_spreads(self) -> None:
        """Convert outrights into spreads."""

        months = ["2026-10-01", "2026-11-01", "2026-12-01"]
        outrights = pd.Series([2.0, -5.0, 3.0], index=months)
        spreads = self.converter.outright_to_consecutive_spreads(outrights, months)
        self.assertEqual(
            spreads.to_dict(),
            {
                "2026-10-01/2026-11-01": 2.0,
                "2026-11-01/2026-12-01": -3.0,
            },
        )


if __name__ == "__main__":
    unittest.main()
