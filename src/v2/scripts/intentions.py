"""Intentions loaders."""

from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG, IntentionsConfig


def load_intentions_for_day(
    client: Client,
    asof_date: date,
    book: str,
    cfg: IntentionsConfig | None = None,
) -> pd.DataFrame:
    """All intention rows for one book / calendar day, ordered by timestamp."""
    table = (cfg or CONFIG.intentions).table
    return client.query_df(
        f"""
        SELECT *
        FROM {table}
        WHERE toDate(timestamp) = '{asof_date}'
          AND book = '{book}'
        ORDER BY timestamp
        """
    )


def cycles_for_day(intentions: pd.DataFrame, asof_date: date) -> pd.DataFrame:
    """First intention per ``cycle_id``, with ``next_cycle_start`` filled."""
    if intentions.empty:
        return intentions

    cycles = (
        intentions.sort_values("timestamp")
        .drop_duplicates("cycle_id", keep="first")
        .reset_index(drop=True)
    )
    cycles["next_cycle_start"] = cycles["timestamp"].shift(-1)
    last = cycles.index[-1]
    if pd.isna(cycles.loc[last, "next_cycle_start"]):
        cycles.loc[last, "next_cycle_start"] = pd.Timestamp(
            datetime.combine(asof_date, time(23, 59, 59))
        )
    return cycles
