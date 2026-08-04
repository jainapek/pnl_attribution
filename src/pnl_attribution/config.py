"""Application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class AppConfig:
    """Application settings."""

    host: str = "10.20.0.243"
    port: int = 8123
    username: str = ""
    password: str = ""
    database: str = "algo"
    default_book_ids: tuple[int, ...] = (337, 129, 340, 342, 339, 318, 343, 309, 341, 332)

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Build config from environment."""

        load_dotenv()
        return cls(
            host=os.getenv("CLICKHOUSE_HOST", cls.host),
            port=int(os.getenv("CLICKHOUSE_PORT", str(cls.port))),
            username=os.getenv("CLICKHOUSE_USERNAME", cls.username),
            password=os.getenv("CLICKHOUSE_PASSWORD", cls.password),
            database=os.getenv("CLICKHOUSE_DATABASE", cls.database),
        )
