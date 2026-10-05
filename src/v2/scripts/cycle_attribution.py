"""Attribute a single cycle to the summary bridge components."""

from __future__ import annotations

import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG, AppConfig
from .costs import (
    abs_lots,
    exec_cost,
    executed_fill_market_risk,
    mark_to_exec,
    market_risk,
    transfer_timing_mr,
    transfer_vs_mark,
)
from .curves import curve_move
from .market_cache import DayMarketCache
from .quotes import load_spread_bid_ask_with_fallback
from .spreads import position_map_to_series
from .stages import attach_executed, build_stages_from_row
from .trades import executed_lots_series


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
    *,
    executed_mr: float = 0.0,
    mark_to_exec_pnl: float = 0.0,
    transfer_vs_mark_pnl: float = 0.0,
    transfer_timing_mr_pnl: float = 0.0,
) -> pd.DataFrame:
    """Per-stage summary; index ordered with TOTAL first.

    Sign convention (cashflow / PnL, + = money in / gain)
        exec_cost (in / out / held_exec_save), same rule:
            buy  (pos>0): −ask × pos × size
            sell (pos<0): −bid × pos × size  (= +bid × |pos| × size)
            clearing:     −clearing × |pos| × size
        held_market_risk = Σ held × size × (spread_later − spread_start)
            (+ = gain on a long; library m2m sign)
        executed_market_risk (executed stage only)
            = Σ (−fill_lots) × size × (spread_at_fill − spread_start)
              (inventory while waiting is opposite the closing fill)
        mark_to_exec (executed stage only)
            = Σ fill_lots × size × (mark_at_fill − price/n)
        transfer_vs_mark (transfer_pred stage only)
            = Σ xfer_lots × size × (transfer.mark − price)/n
              (Nexus booking mark, not algo.curves; ≈ −edge)
        transfer_timing_mr (transfer_pred stage only)
            = Σ xfer_lots × size × (mark_asof(cycle_start) − mark_asof(xfer_ts))

    value_added_by_strategy
        = in_exec_cost − out_exec_cost + held_market_risk
          + executed_market_risk + mark_to_exec + transfer_vs_mark
          + transfer_timing_mr − held_exec_save
    """
    c = cfg or CONFIG
    tvm = float(transfer_vs_mark_pnl)
    ttm = float(transfer_timing_mr_pnl)
    rows = []
    for name in c.peel_stages:
        book = stages[name]
        in_x = exec_cost(book["in"], bid_ask, c)["exec_cost"]
        out_x = exec_cost(book["out"], bid_ask, c)["exec_cost"]
        held_x = exec_cost(book["held"], bid_ask, c)["exec_cost"]
        mr = market_risk(book["held"], curve_move_s, c)
        # receive-vs-mark / receive-timing live on the first peel
        tvm_here = tvm if name == "transfer_pred" else 0.0
        ttm_here = ttm if name == "transfer_pred" else 0.0
        rows.append(
            {
                "stage": name,
                "in_exec_cost": in_x,
                "out_exec_cost": out_x,
                "held_market_risk": mr,
                "executed_market_risk": 0.0,
                "mark_to_exec": 0.0,
                "transfer_vs_mark": tvm_here,
                "transfer_timing_mr": ttm_here,
                "held_exec_save": held_x,
                "value_added_by_strategy": round(
                    in_x - out_x + mr + tvm_here + ttm_here - held_x, 2
                ),
            }
        )

    if "executed" in stages:
        unexec = stages["executed"]["unexecuted"]
        mr_unexec = market_risk(unexec, curve_move_s, c)
        mr_exec = float(executed_mr)
        m2e = float(mark_to_exec_pnl)
        rows.append(
            {
                "stage": "executed",
                "in_exec_cost": 0.0,
                "out_exec_cost": 0.0,
                "held_market_risk": mr_unexec,
                "executed_market_risk": mr_exec,
                "mark_to_exec": m2e,
                "transfer_vs_mark": 0.0,
                "transfer_timing_mr": 0.0,
                "held_exec_save": 0.0,
                "value_added_by_strategy": round(mr_unexec + mr_exec + m2e, 2),
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
    fills: pd.Series | None = None,
    fill_trades: pd.DataFrame | None = None,
    cycle_transfers: pd.DataFrame | None = None,
) -> tuple[pd.Series, float]:
    """One cycle → (``(stage, component)`` $ series, Σ|position_received| lots).

    Pass ``market`` (day cache) to skip per-cycle ClickHouse curve/quote queries.
    Pass ``fills`` / ``fill_trades`` for the gap ``(timestamp, next_cycle_start]``:
    aggregated fills build ``unexecuted``; raw ``fill_trades`` mark
    ``executed_market_risk`` / ``mark_to_exec``.
    Pass ``cycle_transfers`` already assigned to this cycle. ``timestamp`` /
    ``next_cycle_start`` on ``row`` should be the effective window
    (start = min(intention, earliest transfer)).
    """
    c = cfg or CONFIG
    start = pd.Timestamp(row["timestamp"])
    end = pd.Timestamp(row["next_cycle_start"])

    if fill_trades is not None and fills is None:
        fills = executed_lots_series(fill_trades)
    if fills is None:
        fills = pd.Series(dtype=float)

    fill_df = fill_trades if fill_trades is not None else pd.DataFrame()
    xfer_df = cycle_transfers if cycle_transfers is not None else pd.DataFrame()
    if market is not None:
        move = market.curves.move(start, end)
        bid_ask = market.quotes.asof(start)
        exec_mr = executed_fill_market_risk(fill_df, start, market.curves, c)
        m2e = mark_to_exec(fill_df, market.curves, c)
        tvm = transfer_vs_mark(xfer_df, market.curves, c)
        ttm = transfer_timing_mr(xfer_df, start, market.curves, c)
    else:
        move = curve_move(client, start, end, c.curves)
        bid_ask, _src = load_spread_bid_ask_with_fallback(client, start, c.quotes)
        # without a day cache, event-time marks need per-event curve loads — skip
        exec_mr = 0.0
        m2e = 0.0
        tvm = 0.0
        ttm = 0.0

    stages = attach_executed(build_stages_from_row(row), fills)
    summary = stage_summary(
        stages,
        move,
        bid_ask,
        c,
        executed_mr=exec_mr,
        mark_to_exec_pnl=m2e,
        transfer_vs_mark_pnl=tvm,
        transfer_timing_mr_pnl=ttm,
    )
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
