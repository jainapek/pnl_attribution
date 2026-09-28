"""Load attribution config from config.toml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tomllib

_CONFIG_PATH = Path(__file__).with_name("config.toml")


@dataclass(frozen=True)
class ContractConfig:
    size: int
    clearing_rate: float


@dataclass(frozen=True)
class QuotesConfig:
    primary_table: str
    primary_price_scale: float
    fallback_table: str
    fallback_price_scale: float
    lookback_minutes: int
    hub_alias: str
    security_sub_type: str


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
    stage_order: tuple[str, ...]
    peel_stages: tuple[str, ...]
    summary_components: tuple[str, ...]
    curves: CurvesConfig
    quotes: QuotesConfig
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
        contract=ContractConfig(
            size=int(raw["contract"]["size"]),
            clearing_rate=float(raw["contract"]["clearing_rate"]),
        ),
        books={str(k): int(v) for k, v in raw["books"].items()},
        stage_order=tuple(raw["stages"]["order"]),
        peel_stages=tuple(raw["stages"]["peels"]),
        summary_components=tuple(raw["summary_components"]["order"]),
        curves=CurvesConfig(
            product=raw["curves"]["product"],
            table=raw["curves"]["table"],
        ),
        quotes=QuotesConfig(
            primary_table=raw["quotes"]["primary_table"],
            primary_price_scale=float(raw["quotes"]["primary_price_scale"]),
            fallback_table=raw["quotes"]["fallback_table"],
            fallback_price_scale=float(raw["quotes"]["fallback_price_scale"]),
            lookback_minutes=int(raw["quotes"]["lookback_minutes"]),
            hub_alias=raw["quotes"]["hub_alias"],
            security_sub_type=raw["quotes"]["security_sub_type"],
        ),
        intentions=IntentionsConfig(table=raw["intentions"]["table"]),
        clickhouse=ClickHouseConfig(
            host=raw["clickhouse"]["host"],
            port=int(raw["clickhouse"]["port"]),
            database=raw["clickhouse"]["database"],
        ),
    )


# Module-level singleton for convenience
CONFIG = load_config()
