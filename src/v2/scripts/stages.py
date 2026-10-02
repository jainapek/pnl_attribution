"""Build transfer_pred / pca / routing peels in spread space.

Intention maps are already consecutive monthly spreads
(``2026-09-01`` → Sep/Oct lots) — no outright→spread conversion.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG
from .intentions import cycles_for_day, load_intentions_for_day
from .spreads import as_row, densify_spreads, position_map_to_series, series_from_row

_MAP_FIELDS = (
    "position_before",
    "position_received",
    "position_held_prediction",
    "position_held_pca",
    "position_triggered",
)

_STAGE_ORDER = ("transfer_pred", "pca", "routing", "executed")
_LEG_ORDER = ("in", "held", "out")
_EXECUTED_LEG_ORDER = ("in", "executed", "unexecuted")


def _legs_for(stage: str) -> tuple[str, ...]:
    return _EXECUTED_LEG_ORDER if stage == "executed" else _LEG_ORDER


def _stage_index() -> pd.MultiIndex:
    pairs = [(s, leg) for s in _STAGE_ORDER for leg in _legs_for(s)]
    return pd.MultiIndex.from_tuples(pairs, names=["stage", "leg"])


def build_stages_from_row(row: pd.Series) -> dict[str, dict[str, pd.DataFrame]]:
    """One intention row → spread-space ``{stage: {in, held, out}}``."""
    maps = {
        name: position_map_to_series(row.get(name) or {}) for name in _MAP_FIELDS
    }

    all_idx = pd.DatetimeIndex([])
    for s in maps.values():
        if not s.empty:
            all_idx = all_idx.union(s.index)

    if len(all_idx) == 0:
        empty = as_row(pd.Series(dtype=float))
        zero = {"in": empty, "held": empty, "out": empty}
        executed_zero = {
            "in": empty,
            "executed": empty,
            "unexecuted": empty,
        }
        return {
            "transfer_pred": zero,
            "pca": zero,
            "routing": zero,
            "executed": executed_zero,
        }

    months = pd.date_range(all_idx.min(), all_idx.max(), freq="MS")
    spreads = {k: densify_spreads(v, months) for k, v in maps.items()}

    pos_to_be_hedged = as_row(spreads["position_before"]) + as_row(
        spreads["position_received"]
    )
    transfer_pred = {
        "in": pos_to_be_hedged,
        "held": as_row(spreads["position_held_prediction"]),
        "out": pos_to_be_hedged - as_row(spreads["position_held_prediction"]),
    }
    pca_in = transfer_pred["out"]
    pca = {
        "in": pca_in,
        "held": as_row(spreads["position_held_pca"]),
        "out": pca_in - as_row(spreads["position_held_pca"]),
    }
    routing = {
        "in": pca["out"],
        "out": as_row(spreads["position_triggered"]),
        "held": (pca["out"] - as_row(spreads["position_triggered"])).fillna(0),
    }
    # placeholder until fills are attached via ``attach_executed``
    executed = {
        "in": routing["out"].copy(),
        "executed": as_row(pd.Series(0.0, index=months)),
        "unexecuted": routing["out"].copy(),  # in + 0; fills oppose trigger sign
    }
    return {
        "transfer_pred": transfer_pred,
        "pca": pca,
        "routing": routing,
        "executed": executed,
    }


def attach_executed(
    stages: dict[str, dict[str, pd.DataFrame]],
    executed: pd.Series,
) -> dict[str, dict[str, pd.DataFrame]]:
    """Set executed stage: in = routing.out, executed = fills, unexecuted = in + fills.

    Fills (Buy+/Sell−) oppose ``position_triggered`` sign, so fully filled
    tenors net to 0 via ``in + executed``.
    """
    routing_out = series_from_row(stages["routing"]["out"])
    fills = executed.copy() if executed is not None else pd.Series(dtype=float)
    if not fills.empty:
        fills.index = pd.DatetimeIndex(fills.index).normalize()
        fills = fills.sort_index()

    all_idx = routing_out.index.union(fills.index)
    if len(all_idx) == 0:
        empty = as_row(pd.Series(dtype=float))
        stages = {
            **stages,
            "executed": {
                "in": empty,
                "executed": empty,
                "unexecuted": empty,
            },
        }
        return stages

    months = pd.date_range(all_idx.min(), all_idx.max(), freq="MS")
    rin = densify_spreads(routing_out, months)
    fills_d = densify_spreads(fills, months)
    unexecuted = rin + fills_d
    stages = {
        **stages,
        "executed": {
            "in": as_row(rin),
            "executed": as_row(fills_d),
            "unexecuted": as_row(unexecuted),
        },
    }
    return stages


def stages_to_frame(
    stages: dict[str, dict[str, pd.DataFrame]],
    *,
    drop_zeros: bool = True,
) -> pd.DataFrame:
    """One cycle's stages → tidy frame: columns stage, leg, tenor, lots."""
    rows: list[dict] = []
    for stage in _STAGE_ORDER:
        legs = stages.get(stage) or {}
        for leg in _legs_for(stage):
            s = series_from_row(legs.get(leg, pd.DataFrame()))
            for tenor, lots in s.items():
                val = float(lots)
                if drop_zeros and abs(val) <= 1e-12:
                    continue
                rows.append(
                    {
                        "stage": stage,
                        "leg": leg,
                        "tenor": pd.Timestamp(tenor).normalize(),
                        "lots": val,
                    }
                )
    return pd.DataFrame(rows, columns=["stage", "leg", "tenor", "lots"])


def stages_to_wide(
    stages: dict[str, dict[str, pd.DataFrame]],
    *,
    drop_zeros: bool = False,
) -> pd.DataFrame:
    """One cycle → MultiIndex rows ``(stage, leg)``, columns = tenors."""
    long = stages_to_frame(stages, drop_zeros=drop_zeros)
    idx = _stage_index()
    if long.empty:
        return pd.DataFrame(index=idx)
    wide = long.pivot_table(
        index=["stage", "leg"], columns="tenor", values="lots", aggfunc="sum"
    ).reindex(idx)
    return wide.fillna(0.0)


def cycle_stage_positions(
    client: Client,
    asof_date: date,
    book: str,
    *,
    drop_zeros: bool = True,
) -> pd.DataFrame:
    """All cycles for one book/day: in / held / out lots by stage and tenor.

    Columns: ``cycle_id``, ``timestamp``, ``stage``, ``leg``, ``tenor``, ``lots``.
    """
    intentions = load_intentions_for_day(client, asof_date, book, CONFIG.intentions)
    if intentions.empty:
        return pd.DataFrame(
            columns=["cycle_id", "timestamp", "stage", "leg", "tenor", "lots"]
        )

    cycles = cycles_for_day(intentions, asof_date)
    frames: list[pd.DataFrame] = []
    for _, row in cycles.iterrows():
        stages = build_stages_from_row(row)
        one = stages_to_frame(stages, drop_zeros=drop_zeros)
        if one.empty:
            continue
        one = one.copy()
        one.insert(0, "timestamp", row["timestamp"])
        one.insert(0, "cycle_id", row["cycle_id"])
        frames.append(one)

    if not frames:
        return pd.DataFrame(
            columns=["cycle_id", "timestamp", "stage", "leg", "tenor", "lots"]
        )
    return pd.concat(frames, ignore_index=True)


def book_remaining(stages: dict[str, dict[str, pd.DataFrame]]) -> pd.Series:
    """Book lots that should roll into the next cycle's ``position_before``.

    ``Σ held(transfer_pred, pca, routing) + executed.unexecuted``
    where ``unexecuted = triggered + fills`` (fills oppose trigger sign).
    """
    rem = pd.Series(dtype=float)
    for stage, leg in (
        ("transfer_pred", "held"),
        ("pca", "held"),
        ("routing", "held"),
        ("executed", "unexecuted"),
    ):
        rem = rem.add(series_from_row(stages[stage][leg]), fill_value=0.0)
    if rem.empty:
        return rem
    rem.index = pd.DatetimeIndex(rem.index).normalize()
    return rem.sort_index()


def check_risk_roll_continuity(
    client: Client,
    book: str,
    start: date,
    end: date | None = None,
    *,
    include_overnight: bool = False,
) -> pd.DataFrame:
    """Validate ``Σ held + unexecuted`` (cycle i) == ``position_before`` (cycle i+1).

    Fills used for ``unexecuted`` are trades in ``(t_i, t_{i+1}]`` (wall-clock),
    because ``position_before`` updates with fills as they land — not with all
    fills eventually tagged to ``cycle_id``.

    By default skips overnight pairs (last cycle of day D → first of D+1):
    overnight transfers can move the book outside this identity. Set
    ``include_overnight=True`` to keep them (they will usually mismatch).
    """
    from .trades import executed_lots_series

    if end is None:
        end = start

    def _absdiff(a: pd.Series, b: pd.Series) -> float:
        idx = a.index.union(b.index)
        return float(
            (a.reindex(idx, fill_value=0.0) - b.reindex(idx, fill_value=0.0))
            .abs()
            .sum()
        )

    def _to_utc(ts) -> pd.Timestamp:
        t = pd.Timestamp(ts)
        if t.tzinfo is None:
            return t.tz_localize("UTC")
        return t.tz_convert("UTC")

    # all cycles in range, ordered
    cycle_rows: list[pd.Series] = []
    d = start
    while d <= end:
        intentions = load_intentions_for_day(client, d, book, CONFIG.intentions)
        for _, row in cycles_for_day(intentions, d).iterrows():
            r = row.copy()
            r["_date"] = d
            cycle_rows.append(r)
        d = date.fromordinal(d.toordinal() + 1)

    if len(cycle_rows) < 2:
        return pd.DataFrame()

    trades = client.query_df(
        f"""
        SELECT
            transaction_timestamp,
            instrument_key,
            side,
            quantity,
            splitByChar(',', text_c)[3] AS cycle_id
        FROM algo.nexus_trades
        WHERE text_tt = '{book}'
          AND toDate(transaction_timestamp) >= '{start}'
          AND toDate(transaction_timestamp) <= '{end}'
        ORDER BY transaction_timestamp
        """
    )
    if not trades.empty:
        trades = trades.copy()
        trades["transaction_timestamp"] = pd.to_datetime(
            trades["transaction_timestamp"], utc=True
        )
        trades["cycle_id"] = trades["cycle_id"].astype(str)

    rows: list[dict] = []
    for i in range(len(cycle_rows) - 1):
        cur = cycle_rows[i]
        nxt = cycle_rows[i + 1]
        overnight = cur["_date"] != nxt["_date"]
        if overnight and not include_overnight:
            continue

        t0 = _to_utc(cur["timestamp"])
        t1 = _to_utc(nxt["timestamp"])

        if trades.empty:
            gap = trades
        else:
            gap = trades[
                (trades["transaction_timestamp"] > t0)
                & (trades["transaction_timestamp"] <= t1)
            ]
        fills = executed_lots_series(gap)
        stages = attach_executed(build_stages_from_row(cur), fills)
        remaining = book_remaining(stages)

        before = position_map_to_series(nxt.get("position_before") or {})
        if not before.empty:
            before.index = pd.DatetimeIndex(before.index).normalize()

        rows.append(
            {
                "date": nxt["_date"],
                "prev_date": cur["_date"],
                "prev_cycle_id": str(cur["cycle_id"]),
                "cycle_id": str(nxt["cycle_id"]),
                "prev_ts": cur["timestamp"],
                "timestamp": nxt["timestamp"],
                "overnight": overnight,
                "abs_diff": _absdiff(remaining, before),
                "abs_remaining_prev": float(remaining.abs().sum()),
                "abs_before": float(before.abs().sum()),
                "n_fills_gap": int(len(gap)),
            }
        )

    return pd.DataFrame(rows)
