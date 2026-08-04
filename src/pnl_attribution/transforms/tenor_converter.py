"""Tenor conversion helpers."""

from __future__ import annotations

import pandas as pd


class TenorConverter:
    """Convert tenors into consecutive monthly spreads."""

    MONTH_MAPPING = {
        "Jan": "01",
        "Feb": "02",
        "Mar": "03",
        "Apr": "04",
        "May": "05",
        "Jun": "06",
        "Jul": "07",
        "Aug": "08",
        "Sep": "09",
        "Oct": "10",
        "Nov": "11",
        "Dec": "12",
    }

    def extract_trade_contract(self, instrument_key: str) -> str:
        """Extract the tenor token."""

        parts = instrument_key.split()
        if len(parts) < 3:
            raise ValueError(f"Unexpected instrument_key: {instrument_key}")
        return parts[2]

    def trade_contract_to_range(self, contract: str) -> str:
        """Convert a TT contract string."""

        start, end = contract.split("-")
        start_date = f"20{start[-2:]}-{self.MONTH_MAPPING[start[:3]]}-01"
        end_date = f"20{end[-2:]}-{self.MONTH_MAPPING[end[:3]]}-01"
        return f"{start_date}/{end_date}"

    def build_month_grid(self, months: list[str]) -> list[str]:
        """Build a dense month grid."""

        if not months:
            return []
        return pd.date_range(months[0], months[-1], freq="MS").strftime("%Y-%m-%d").tolist()

    def consecutive_spread_labels(self, months: list[str]) -> list[str]:
        """Build consecutive spread labels."""

        return [f"{start}/{end}" for start, end in zip(months[:-1], months[1:])]

    def outright_to_consecutive_spreads(self, outrights: pd.Series, months: list[str]) -> pd.Series:
        """Convert outrights into spreads."""

        values = outrights.reindex(months).fillna(0)
        spreads = values.cumsum().iloc[:-1]
        spreads.index = self.consecutive_spread_labels(months)
        return spreads

    def frame_to_consecutive_spreads(self, outrights: pd.DataFrame, months: list[str]) -> pd.DataFrame:
        """Convert an outright frame into spreads."""

        values = outrights.reindex(columns=months).fillna(0)
        spreads = values.cumsum(axis=1).iloc[:, :-1]
        spreads.columns = self.consecutive_spread_labels(months)
        return spreads

    def expand_range_to_monthly_spreads(
        self,
        start_tenor,
        end_tenor,
        quantity: float,
    ) -> pd.Series:
        """Expand a tenor range into spreads."""

        months = pd.date_range(start_tenor, end_tenor, freq="MS").strftime("%Y-%m-%d").tolist()
        if len(months) < 2:
            return pd.Series(dtype=float)
        return pd.Series(float(quantity), index=self.consecutive_spread_labels(months))
