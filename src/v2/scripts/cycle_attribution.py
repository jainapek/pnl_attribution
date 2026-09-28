"""Attribute a single cycle to the summary bridge components."""

from __future__ import annotations

import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG, AppConfig
from .costs import abs_lots, exec_cost, market_risk
from .curves import curve_move
from .market_cache import DayMarketCache
from .quotes import load_spread_bid_ask_with_fallback
from .stages import build_stages_from_row


def traded_abs_lots(stages: dict[str, dict[str, pd.DataFrame]]) -> float:
    """Abs lots traded this cycle: |triggered| (= routing out), else |to-be-hedged|."""
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

    Sign convention
        exec costs ≥ 0 (half-spread + exchange on |lots|)
        held_market_risk = Σ held × size × Δspread   (PnL; + = MTM gain)

    value_added_by_strategy
        = in_exec_cost
        − (out_exec_cost − held_market_risk − held_exec_save)

    i.e. in_exec − out_exec + held_market_risk + held_exec_save.
    Held MR is subtracted from the residual cost (a gain offsets cost);
    held_exec_save is the avoided half-spread + exchange on |held|.
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
                    in_x - (out_x - mr - held_x), 2
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
    """One cycle → (``(stage, component)`` $ series, traded abs lots).

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
    lots = traded_abs_lots(stages)

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

    c/bbl = $ / (abs_lots × contract_size) × 100.
    Day / team totals should pass **summed** abs_lots so c/bbl is volume-weighted.
    """
    c = cfg or CONFIG
    size = c.contract.size
    bbls = abs_lots_s.astype(float) * size
    denom = bbls.where(bbls > 0)
    cbbl = dollar_df.div(denom, axis=0) * 100.0

    table = pd.concat({"$": dollar_df, "c/bbl": cbbl}, axis=1)
    table.columns.names = ["unit", "stage", "component"]

    meta = pd.DataFrame({"abs_lots": abs_lots_s.astype(float)})
    meta.columns = pd.MultiIndex.from_tuples(
        [("meta", "abs_lots", "")],
        names=["unit", "stage", "component"],
    )
    return pd.concat([meta, table], axis=1)
