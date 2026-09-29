"""Attribute a single cycle to the summary bridge components."""

from __future__ import annotations

import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG, AppConfig
from .costs import abs_lots, exec_cost, market_risk
from .curves import curve_move
from .market_cache import DayMarketCache
from .quotes import load_spread_bid_ask_with_fallback
from .spreads import position_map_to_series
from .stages import build_stages_from_row


def received_abs_lots(row: pd.Series) -> float:
    """Σ |position_received| lots this cycle (slippage volume)."""
    return float(position_map_to_series(row.get("position_received") or {}).abs().sum())


def traded_abs_lots(stages: dict[str, dict[str, pd.DataFrame]]) -> float:
    """Deprecated alias — prefer ``received_abs_lots`` for slippage volume."""
    routed = abs_lots(stages["routing"]["out"])
    if routed > 0:
        return routed
    return abs_lots(stages["transfer_pred"]["in"])


def stage_summary(
    stages: dict[str, dict[str, pd.DataFrame]],
    curve_move_s: pd.Series,
    bid_ask: pd.DataFrame,
    cfg: AppConfig | None = None,
) -> pd.DataFrame:
    """Per-stage summary; index ordered with TOTAL first.

    Sign convention (cashflow / PnL, + = money in / gain)
        exec_cost (in / out / held_exec_save), same rule:
            buy  (pos>0): −ask × pos × size
            sell (pos<0): −bid × pos × size  (= +bid × |pos| × size)
            clearing:     −clearing × |pos| × size
        held_market_risk = Σ held × size × Δspread   (+ = MTM gain)

    value_added_by_strategy
        = in_exec_cost − (out_exec_cost + held_market_risk + held_exec_save)
    """
    c = cfg or CONFIG
    rows = []
    for name in c.peel_stages:
        book = stages[name]
        in_x = exec_cost(book["in"], bid_ask, c)["exec_cost"]
        out_x = exec_cost(book["out"], bid_ask, c)["exec_cost"]
        held_x = exec_cost(book["held"], bid_ask, c)["exec_cost"]
        mr = market_risk(book["held"], curve_move_s, c)
        rows.append(
            {
                "stage": name,
                "in_exec_cost": in_x,
                "out_exec_cost": out_x,
                "held_market_risk": mr,
                "held_exec_save": held_x,
                "value_added_by_strategy": round(
                    in_x - (out_x + mr + held_x), 2
                ),
            }
        )
    out = pd.DataFrame(rows).set_index("stage")
    out.loc["TOTAL"] = out.sum(numeric_only=True)
    return out.reindex(list(c.stage_order))


def attribute_cycle(
    client: Client,
    row: pd.Series,
    cfg: AppConfig | None = None,
    *,
    market: DayMarketCache | None = None,
) -> tuple[pd.Series, float]:
    """One cycle → (``(stage, component)`` $ series, Σ|position_received| lots).

    Pass ``market`` (day cache) to skip per-cycle ClickHouse curve/quote queries.
    """
    c = cfg or CONFIG
    start = pd.Timestamp(row["timestamp"])
    end = pd.Timestamp(row["next_cycle_start"])

    if market is not None:
        move = market.curves.move(start, end)
        bid_ask = market.quotes.asof(start)
    else:
        move = curve_move(client, start, end, c.curves)
        bid_ask, _src = load_spread_bid_ask_with_fallback(client, start, c.quotes)

    stages = build_stages_from_row(row)
    summary = stage_summary(stages, move, bid_ask, c)
    lots = received_abs_lots(row)

    flat: dict[tuple[str, str], float] = {}
    for stage in c.stage_order:
        for comp in c.summary_components:
            flat[(stage, comp)] = float(summary.loc[stage, comp])
    return pd.Series(flat), lots


def with_cents_per_bbl(
    dollar_df: pd.DataFrame,
    abs_lots_s: pd.Series,
    cfg: AppConfig | None = None,
) -> pd.DataFrame:
    """Stack ``$`` and ``c/bbl`` under a ``unit`` column level; keep ``abs_lots``.

    ``abs_lots`` = Σ |position_received| (per cycle; day/team = sum of cycles).
    $ stays cashflow / PnL (+ = money in / gain).
    c/bbl uses slippage sign: ``−$ / (abs_lots × size) × 100``
    so +c/bbl = cost, −c/bbl = gain.
    """
    c = cfg or CONFIG
    size = c.contract.size
    bbls = abs_lots_s.astype(float) * size
    denom = bbls.where(bbls > 0)
    cbbl = -dollar_df.div(denom, axis=0) * 100.0

    table = pd.concat({"$": dollar_df, "c/bbl": cbbl}, axis=1)
    table.columns.names = ["unit", "stage", "component"]

    meta = pd.DataFrame({"abs_lots": abs_lots_s.astype(float)})
    meta.columns = pd.MultiIndex.from_tuples(
        [("meta", "abs_lots", "")],
        names=["unit", "stage", "component"],
    )
    return pd.concat([meta, table], axis=1)
