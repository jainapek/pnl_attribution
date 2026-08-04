"""Transfer repository."""

from __future__ import annotations


class TransferRepository:
    """Fetch Nexus transfers."""

    def __init__(self, client) -> None:
        """Store client."""

        self._client = client

    def fetch(self, date: str):
        """Fetch raw transfers."""

        query = f"""
SELECT *
FROM algo.nexus_transfers
WHERE toDate(timestamp) = '{date}'
ORDER BY timestamp ASC
"""
        return self._client.query_df(query)
