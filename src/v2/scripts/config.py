"""Load attribution config from config.toml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tomllib

_CONFIG_PATH = Path(__file__).with_name("config.toml")


@dataclass(frozen=True)
class ContractConfig:
    size: int


@dataclass(frozen=True)
class CurvesConfig:
    product: str
    table: str


@dataclass(frozen=True)
class IntentionsConfig:
    table: str


@dataclass(frozen=True)
class ClickHouseConfig:
    host: str
    port: int
    database: str


@dataclass(frozen=True)
class AppConfig:
    contract: ContractConfig
    books: dict[str, int]
    curves: CurvesConfig
    intentions: IntentionsConfig
    clickhouse: ClickHouseConfig

    @property
    def book_names(self) -> list[str]:
        return list(self.books.keys())


def load_config(path: Path | None = None) -> AppConfig:
    cfg_path = path or _CONFIG_PATH
    with cfg_path.open("rb") as f:
        raw = tomllib.load(f)

    return AppConfig(
        contract=ContractConfig(size=int(raw["contract"]["size"])),
        books={str(k): int(v) for k, v in raw["books"].items()},
        curves=CurvesConfig(
            product=raw["curves"]["product"],
            table=raw["curves"]["table"],
        ),
        intentions=IntentionsConfig(table=raw["intentions"]["table"]),
        clickhouse=ClickHouseConfig(
            host=raw["clickhouse"]["host"],
            port=int(raw["clickhouse"]["port"]),
            database=raw["clickhouse"]["database"],
        ),
    )


CONFIG = load_config()
