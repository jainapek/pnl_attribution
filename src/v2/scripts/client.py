"""ClickHouse client factory."""

from __future__ import annotations

import os

from clickhouse_connect import get_client
from clickhouse_connect.driver import Client
from dotenv import load_dotenv

from .config import CONFIG, ClickHouseConfig


def get_ch_client(cfg: ClickHouseConfig | None = None) -> Client:
    """Build a ClickHouse client from env overrides + config defaults."""
    load_dotenv()
    c = cfg or CONFIG.clickhouse
    return get_client(
        host=os.getenv("CLICKHOUSE_HOST", c.host),
        port=int(os.getenv("CLICKHOUSE_PORT", str(c.port))),
        username=os.getenv("CLICKHOUSE_USERNAME", ""),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DATABASE", c.database),
    )
