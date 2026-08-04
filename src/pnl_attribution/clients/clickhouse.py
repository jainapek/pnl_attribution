"""ClickHouse client factory."""

from __future__ import annotations

from clickhouse_connect import get_client

from pnl_attribution.config import AppConfig


class ClickHouseClientFactory:
    """Build ClickHouse clients."""

    def __init__(self, config: AppConfig) -> None:
        """Store config."""

        self._config = config

    def create(self):
        """Create a ClickHouse client."""

        return get_client(
            host=self._config.host,
            port=self._config.port,
            username=self._config.username,
            password=self._config.password,
            database=self._config.database,
        )
