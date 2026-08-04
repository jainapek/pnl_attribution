"""Trade repository."""

from __future__ import annotations


class TradeRepository:
    """Fetch Nexus trades."""

    def __init__(self, client) -> None:
        """Store client."""

        self._client = client

    def fetch(self, date: str):
        """Fetch raw trades."""

        query = f"""
SELECT *
FROM algo.nexus_trades
WHERE toDate(transaction_timestamp) = '{date}'
ORDER BY transaction_timestamp ASC
"""
        return self._client.query_df(query)
