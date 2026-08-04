"""Position repository."""

from __future__ import annotations


class PositionRepository:
    """Fetch position snapshots."""

    def __init__(self, client) -> None:
        """Store client."""

        self._client = client

    def fetch(self, date: str, book_ids: tuple[int, ...]):
        """Fetch raw positions."""

        book_id_list = ",".join(map(str, book_ids))
        query = f"""
SELECT
  timestamp,
  book_id,
  product_id,
  positions
FROM algo.position
WHERE book_id IN ({book_id_list})
  AND toDate(timestamp) = '{date}'
ORDER BY timestamp ASC
"""
        return self._client.query_df(query)
