"""Split reconciled event-walk gross into six sleeves.

Construction rule
-----------------
Replay the same event walk as ``reconcile_timeline_vs_library``. Every day-side
dollar (interval MTM, entry BV, cash, EOD align) is assigned exactly once.
Overnight is the recon overnight amount. ``validation_error`` is reported, never
plugged.

Assignment
----------
- overnight: recon overnight
- timing: inbound transfer event value ΔS(c−p) (= day entry + cash)
         + interval MTM on pending transfer lots until intention
- mark_to_fill: fill event value ΔS(c−p) (all fills)
- unexecuted.base2_bv: outbound entry BV ΔSc (cash 0 on Hedger)
- unexecuted.eod_align: recon EOD align
- interval MTM inside a cycle segment (non-timing lots):
      split with the section-3 lot maps into pca_held_mr / fill_mr /
      unexecuted_market_return; residual walk MTM − (pca + fill_mr) stays in
      unexecuted_market_return so inventory dollars still match the walk
- interval MTM outside cycle segments (non-timing lots): unexecuted_market_return
"""

from __future__ import annotations

from collections import defaultdict

from dataclasses import dataclass, field
from datetime import date

import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG, AppConfig
from .intentions import cycles_for_day, load_intentions_for_day
from .library import load_library_eod_pnl
from .market_cache import _naive as _curve_naive, load_curve_asof_many
from .overnight import _library_eod_ts
from .position_timeline import (
    _book_value,
    _event_cash,
    _mtm,
    _split_window,
    build_position_timeline,
    load_bod_spreads,
    map_to_series,
    position_asof,
    reconcile_timeline_vs_library,
)
from .spreads import position_map_to_series
from .transfers import naive_utc, normalize_cycle_id

SLEEVE_COLS = (
    "overnight",
    "timing_mr",
    "pca_held_mr",
    "fill_mr",
    "mark_to_fill",
    "unexecuted_mr",
)


@dataclass
class SleeveTotals:
    overnight: float = 0.0
    timing_mr: float = 0.0
    timing_event_value: float = 0.0
    timing_xfer_to_intention: float = 0.0
    pca_held_mr: float = 0.0
    fill_mr: float = 0.0
    mark_to_fill: float = 0.0
    unexecuted_mr: float = 0.0
    unexecuted_market_return: float = 0.0
    unexecuted_base2_bv: float = 0.0
    unexecuted_eod_align: float = 0.0
    ours: float = 0.0
    event_walk_gross: float = 0.0
    library_gross: float = float("nan")
    validation_error: float = 0.0
    notes: list[str] = field(default_factory=list)

    def finalize(self) -> dict:
        self.timing_mr = round(
            self.timing_event_value + self.timing_xfer_to_intention, 2
        )
        self.unexecuted_mr = round(
            self.unexecuted_market_return
            + self.unexecuted_base2_bv
            + self.unexecuted_eod_align,
            2,
        )
        for k in (
            "overnight",
            "timing_event_value",
            "timing_xfer_to_intention",
            "pca_held_mr",
            "fill_mr",
            "mark_to_fill",
            "unexecuted_market_return",
            "unexecuted_base2_bv",
            "unexecuted_eod_align",
        ):
            setattr(self, k, round(float(getattr(self, k)), 2))
        self.ours = round(sum(float(getattr(self, k)) for k in SLEEVE_COLS), 2)
        self.validation_error = round(self.ours - self.event_walk_gross, 2)
        return {
            **{k: float(getattr(self, k)) for k in SLEEVE_COLS},
            "timing_event_value": self.timing_event_value,
            "timing_xfer_to_intention": self.timing_xfer_to_intention,
            "unexecuted_market_return": self.unexecuted_market_return,
            "unexecuted_base2_bv": self.unexecuted_base2_bv,
            "unexecuted_eod_align": self.unexecuted_eod_align,
            "ours": self.ours,
            "event_walk_gross": self.event_walk_gross,
            "library_gross": self.library_gross,
            "validation_error": self.validation_error,
            "notes": list(self.notes),
        }


def _series(s: pd.Series | None) -> pd.Series:
    if s is None or len(s) == 0:
        return pd.Series(dtype=float)
    out = s.copy()
    out.index = pd.DatetimeIndex(out.index).normalize()
    return out.sort_index()


def attribute_day_event_walk_sleeves(
    client: Client,
    asof_date: date,
    book: str,
    assigned: pd.DataFrame | None = None,
    cfg: AppConfig | None = None,
) -> tuple[pd.DataFrame, dict]:
    from .sleeve_attribution import assign_transfers_to_cycles, compute_timing_mr

    c = cfg or CONFIG
    size = c.contract.size
    out = SleeveTotals()

    tl = build_position_timeline(client, asof_date, book, c)
    recon = reconcile_timeline_vs_library(
        client, asof_date, book, timeline=tl, cfg=c
    )
    out.overnight = float(recon.loc["overnight", "ours"])
    out.event_walk_gross = float(recon.loc["gross", "ours"])
    lib = load_library_eod_pnl(client, asof_date, book)
    out.library_gross = round(float(lib["gross"]), 2) if lib else float("nan")

    t_first = recon.attrs.get("t_first_transfer")
    t_ov_start = recon.attrs.get("overnight_start")
    lib_eod = recon.attrs.get("lib_eod") or _library_eod_ts(client, asof_date, book)
    out.unexecuted_eod_align = float(recon.attrs.get("eod_align") or 0.0)

    if assigned is None or assigned.empty:
        assigned = assign_transfers_to_cycles(client, book, asof_date, asof_date, c)
        assigned = compute_timing_mr(client, assigned, c)

    asg_by_id: dict = {}
    if assigned is not None and not assigned.empty and "id" in assigned.columns:
        for row in assigned.drop_duplicates("id").itertuples(index=False):
            asg_by_id[int(row.id)] = row

    intents = load_intentions_for_day(client, asof_date, book, c.intentions)
    cycles = (
        cycles_for_day(intents, asof_date) if intents is not None else pd.DataFrame()
    )
    cycle_rows: list[dict] = []
    if cycles is not None and not cycles.empty:
        for r in cycles.itertuples(index=False):
            cid = normalize_cycle_id(r.cycle_id)
            if cid is None:
                continue
            cycle_rows.append(
                {
                    "cycle_id": cid,
                    "t0": naive_utc(r.timestamp),
                    "t1_fallback": naive_utc(r.next_cycle_start),
                    "pca": position_map_to_series(
                        getattr(r, "position_held_pca", None) or {}
                    ),
                }
            )
    cycle_rows.sort(key=lambda x: x["t0"])

    for i, crow in enumerate(cycle_rows):
        t1 = crow["t1_fallback"]
        if i + 1 < len(cycle_rows):
            nxt = cycle_rows[i + 1]["cycle_id"]
            for row in tl.itertuples(index=False):
                if row.reason != "transfer" or bool(row.outbound):
                    continue
                rid = row.id
                if rid is not None and int(rid) in asg_by_id:
                    a = asg_by_id[int(rid)]
                    if normalize_cycle_id(getattr(a, "cycle_id_assigned", None)) == nxt:
                        t1 = naive_utc(row.timestamp)
                        break
        crow["t1"] = t1

    # Segments: cut at position-changing transfers AND foreign-cycle fills
    segments: list[dict] = []
    for crow in cycle_rows:
        t0, t1 = crow["t0"], crow["t1"]
        if t1 is None or t1 <= t0:
            out.notes.append(f"skip cycle {crow['cycle_id']}: bad window")
            continue
        cuts = {t0, t1}
        if not tl.empty:
            for row in tl.itertuples(index=False):
                ts = naive_utc(row.timestamp)
                if not (t0 < ts < t1):
                    continue
                if row.reason == "transfer":
                    cuts.add(ts)
                elif row.reason == "fill":
                    cid = normalize_cycle_id(getattr(row, "cycle_id", None))
                    if cid is not None and cid != crow["cycle_id"]:
                        cuts.add(ts)
        cuts_l = sorted(cuts)
        for j in range(len(cuts_l) - 1):
            segments.append(
                {
                    "cycle_id": crow["cycle_id"],
                    "t0": cuts_l[j],
                    "t1": cuts_l[j + 1],
                    "pca": crow["pca"] if j == 0 else pd.Series(dtype=float),
                    "walk_mtm": 0.0,
                }
            )

    times: list[pd.Timestamp] = [
        t for t in (t_ov_start, t_first, lib_eod) if t is not None
    ]
    if not tl.empty:
        times.extend(naive_utc(t) for t in tl["timestamp"])
    for seg in segments:
        times.extend([seg["t0"], seg["t1"]])
    for a in asg_by_id.values():
        if pd.notna(getattr(a, "cycle_ts", pd.NaT)):
            times.append(naive_utc(a.cycle_ts))
    curves = load_curve_asof_many(client, times, c.curves) if times else None

    def curve(ts: pd.Timestamp | None) -> pd.Series | None:
        if curves is None or ts is None:
            return None
        return curves.asof(_curve_naive(ts))

    # cycle_id -> sleeve accumulators (cycle-attributable only; no overnight/base2/gaps)
    by_cycle: dict[str, dict] = {}
    for crow in cycle_rows:
        by_cycle[crow["cycle_id"]] = {
            "date": asof_date,
            "cycle_id": crow["cycle_id"],
            "t0": crow["t0"],
            "t1": crow["t1"],
            "timing_mr": 0.0,
            "pca_held_mr": 0.0,
            "fill_mr": 0.0,
            "mark_to_fill": 0.0,
            "unexecuted_mr": 0.0,
        }

    def _cyc(cid: str | None) -> dict | None:
        if cid is None:
            return None
        return by_cycle.get(cid)

    # pending timing lots: (lots, clear_ts, cycle_id)
    pending_timing: list[tuple[pd.Series, pd.Timestamp, str | None]] = []

    def pending_at(ts: pd.Timestamp) -> pd.Series:
        acc = pd.Series(dtype=float)
        for lots, clear_ts, _cid in pending_timing:
            if ts < clear_ts:
                acc = acc.add(lots, fill_value=0.0)
        return acc[acc.abs() > 1e-12] if len(acc) else acc

    def find_segment(ts_mid: pd.Timestamp) -> dict | None:
        for seg in segments:
            if seg["t0"] <= ts_mid < seg["t1"] or (
                ts_mid == seg["t1"] and seg["t0"] < seg["t1"]
            ):
                # use [t0, t1)
                if seg["t0"] <= ts_mid < seg["t1"]:
                    return seg
        return None

    def add_interval_mtm(qty: pd.Series, a: pd.Timestamp, b: pd.Timestamp) -> None:
        """Assign day interval MTM on qty over [a,b], clipped into segment/gap pieces."""
        if b <= a or curves is None:
            return
        # Split at segment boundaries so each piece is fully in or out of one segment
        bounds = {a, b}
        for seg in segments:
            if a < seg["t0"] < b:
                bounds.add(seg["t0"])
            if a < seg["t1"] < b:
                bounds.add(seg["t1"])
        marks = sorted(bounds)
        for i in range(len(marks) - 1):
            pa, pb = marks[i], marks[i + 1]
            if pb <= pa:
                continue
            mid = pa + (pb - pa) / 2
            q_tim = pending_at(pa)
            q_rest = qty.sub(q_tim, fill_value=0.0)
            q_rest = q_rest[q_rest.abs() > 1e-12]
            rest_pnl = _mtm(q_rest, curve(pa), curve(pb), size)
            # attribute timing MTM per pending package → its cycle
            for lots, clear_ts, cid in pending_timing:
                if pa >= clear_ts:
                    continue
                tim_pnl = _mtm(lots, curve(pa), curve(pb), size)
                out.timing_xfer_to_intention += tim_pnl
                bucket = _cyc(cid)
                if bucket is not None:
                    bucket["timing_mr"] += tim_pnl
            seg = find_segment(mid)
            if seg is None:
                out.unexecuted_market_return += rest_pnl
            else:
                seg["walk_mtm"] += rest_pnl

    bod_ts, bod = load_bod_spreads(client, asof_date, book, c)
    t_mark = t_ov_start

    if not tl.empty:
        for row in tl.itertuples(index=False):
            ts = naive_utc(row.timestamp)
            before = map_to_series(row.position_before)
            after = map_to_series(row.position_after)
            delta = after.sub(before, fill_value=0.0)
            outbound = bool(getattr(row, "outbound", False))
            price = row.price if isinstance(getattr(row, "price", None), dict) else {}

            if t_mark is not None and curves is not None and ts > t_mark:
                _ov, day = _split_window(t_mark, ts, t_first)
                if day is not None:
                    add_interval_mtm(before, day[0], day[1])

            entry = _book_value(delta, curve(ts), size) if curves is not None else 0.0
            cash = _event_cash(delta, price, size, outbound=outbound)
            entry_is_day = not (t_first is not None and ts < t_first)
            day_entry = entry if entry_is_day else 0.0
            event_day = day_entry + cash

            if row.reason == "transfer" and outbound:
                out.unexecuted_base2_bv += day_entry
            elif row.reason == "transfer" and not outbound:
                out.timing_event_value += event_day
                clear_ts = None
                cid = None
                rid = getattr(row, "id", None)
                if rid is not None and int(rid) in asg_by_id:
                    a = asg_by_id[int(rid)]
                    if getattr(a, "assigned_from", None) != "hedger_to_base2":
                        cid = normalize_cycle_id(getattr(a, "cycle_id_assigned", None))
                        if pd.notna(getattr(a, "cycle_ts", pd.NaT)):
                            clear_ts = naive_utc(a.cycle_ts)
                if cid is None:
                    cid = normalize_cycle_id(getattr(row, "cycle_id", None))
                if clear_ts is None and cid is not None:
                    for crow in cycle_rows:
                        if crow["cycle_id"] == cid:
                            clear_ts = crow["t0"]
                            break
                bucket = _cyc(cid)
                if bucket is not None:
                    bucket["timing_mr"] += event_day
                if clear_ts is not None and clear_ts > ts and delta.abs().sum() > 1e-12:
                    pending_timing.append((_series(delta), clear_ts, cid))
            elif row.reason == "fill":
                out.mark_to_fill += event_day
                cid = normalize_cycle_id(getattr(row, "cycle_id", None))
                bucket = _cyc(cid)
                if bucket is not None:
                    bucket["mark_to_fill"] += event_day

            t_mark = ts

        if t_mark is not None and lib_eod is not None and lib_eod > t_mark:
            q_end = map_to_series(tl.iloc[-1]["position_after"])
            _ov, day = _split_window(t_mark, lib_eod, t_first)
            if day is not None:
                add_interval_mtm(q_end, day[0], day[1])
    elif t_ov_start is not None and lib_eod is not None:
        _ov, day = _split_window(t_ov_start, lib_eod, t_first)
        if day is not None:
            add_interval_mtm(bod, day[0], day[1])

    # Split each segment's walk inventory MTM into pca / fill_mr / unexec
    for seg in segments:
        q0 = map_to_series(position_asof(tl, seg["t0"]))
        q_pca = _series(seg["pca"])
        fills: list[tuple[pd.Series, pd.Series | None]] = []
        if not tl.empty:
            for row in tl.itertuples(index=False):
                if row.reason != "fill":
                    continue
                ts = naive_utc(row.timestamp)
                if not (seg["t0"] < ts <= seg["t1"]):
                    continue
                cid = normalize_cycle_id(getattr(row, "cycle_id", None))
                if cid is not None and cid != seg["cycle_id"]:
                    continue
                d = map_to_series(row.position_after).sub(
                    map_to_series(row.position_before), fill_value=0.0
                )
                fills.append((_series(d), curve(ts)))

        c0 = curve(seg["t0"])
        c1 = curve(seg["t1"])
        fill_sum = pd.Series(dtype=float)
        fmr = 0.0
        for d, c_fill in fills:
            fill_sum = fill_sum.add(d, fill_value=0.0)
            fmr += _mtm(-d, c0, c_fill, size)
        q_u = q0.sub(q_pca, fill_value=0.0).add(fill_sum, fill_value=0.0)
        q_u = q_u[q_u.abs() > 1e-12]
        pca_pnl = _mtm(q_pca, c0, c1, size)
        # Force inventory identity to the walk: residual → unexecuted
        walk = float(seg["walk_mtm"])
        unex_pnl = walk - pca_pnl - fmr
        out.pca_held_mr += pca_pnl
        out.fill_mr += fmr
        out.unexecuted_market_return += unex_pnl
        bucket = _cyc(seg["cycle_id"])
        if bucket is not None:
            bucket["pca_held_mr"] += pca_pnl
            bucket["fill_mr"] += fmr
            bucket["unexecuted_mr"] += unex_pnl

    totals = out.finalize()
    cycle_rows_out = []
    for cid, bucket in by_cycle.items():
        row = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in bucket.items()}
        row["cycle_pnl"] = round(
            row["timing_mr"]
            + row["pca_held_mr"]
            + row["fill_mr"]
            + row["mark_to_fill"]
            + row["unexecuted_mr"],
            2,
        )
        cycle_rows_out.append(row)
    cycle_df = (
        pd.DataFrame(cycle_rows_out).sort_values(["date", "t0"]).reset_index(drop=True)
        if cycle_rows_out
        else pd.DataFrame(
            columns=[
                "date",
                "cycle_id",
                "t0",
                "t1",
                "timing_mr",
                "pca_held_mr",
                "fill_mr",
                "mark_to_fill",
                "unexecuted_mr",
                "cycle_pnl",
            ]
        )
    )
    totals["by_cycle"] = cycle_df

    tl_out = tl.copy()
    for col in SLEEVE_COLS:
        tl_out[col] = 0.0
    if not tl_out.empty:
        tl_out["overnight"] = totals["overnight"]
    tl_out.attrs["sleeve_totals"] = totals
    tl_out.attrs["by_cycle"] = cycle_df
    return tl_out, totals


def attribute_range_event_walk_sleeves(
    client: Client,
    book: str,
    start: date,
    end: date | None = None,
    cfg: AppConfig | None = None,
) -> pd.DataFrame:
    from .sleeve_attribution import assign_transfers_to_cycles, compute_timing_mr

    try:
        # Plain tqdm (not tqdm.auto) — avoids IProgress / ipywidgets warning in notebooks.
        from tqdm import tqdm
    except ImportError:  # pragma: no cover
        def tqdm(x, **_kwargs):
            return x

    end = end or date.today()
    assigned = assign_transfers_to_cycles(client, book, start, end, cfg)
    assigned = compute_timing_mr(client, assigned, cfg)
    rows = []
    extra = [
        "timing_event_value",
        "timing_xfer_to_intention",
        "unexecuted_market_return",
        "unexecuted_base2_bv",
        "unexecuted_eod_align",
        "ours",
        "event_walk_gross",
        "library_gross",
        "validation_error",
    ]
    days = pd.date_range(start, end, freq="D").date.tolist()
    for d in tqdm(days, desc=f"{book} sleeves", unit="day"):
        lib = load_library_eod_pnl(client, d, book)
        if lib is None:
            continue
        intents = load_intentions_for_day(client, d, book)
        if intents is None or intents.empty:
            continue
        _tl, totals = attribute_day_event_walk_sleeves(
            client, d, book, assigned=assigned, cfg=cfg
        )
        rows.append({"date": d, **{k: totals[k] for k in list(SLEEVE_COLS) + extra}})
    return pd.DataFrame(rows)



def attribute_range_by_cycle(
    client: Client,
    book: str,
    start: date,
    end: date | None = None,
    cfg: AppConfig | None = None,
) -> pd.DataFrame:
    """Per-cycle sleeve PnL for every library day in ``[start, end]``.

    Cycle rows include timing / pca / fill_mr / mark_to_fill / unexecuted
    attributable to that cycle. Day-only pieces (overnight, Base2 BV, inter-cycle
    gaps, EOD align) are omitted — so cycle_pnl sums will not equal day gross.
    """
    from .sleeve_attribution import assign_transfers_to_cycles, compute_timing_mr

    try:
        from tqdm import tqdm
    except ImportError:  # pragma: no cover
        def tqdm(x, **_kwargs):
            return x

    end = end or date.today()
    assigned = assign_transfers_to_cycles(client, book, start, end, cfg)
    assigned = compute_timing_mr(client, assigned, cfg)
    frames: list[pd.DataFrame] = []
    days = pd.date_range(start, end, freq="D").date.tolist()
    for d in tqdm(days, desc=f"{book} cycles", unit="day"):
        lib = load_library_eod_pnl(client, d, book)
        if lib is None:
            continue
        intents = load_intentions_for_day(client, d, book)
        if intents is None or intents.empty:
            continue
        _tl, totals = attribute_day_event_walk_sleeves(
            client, d, book, assigned=assigned, cfg=cfg
        )
        cdf = totals.get("by_cycle")
        if cdf is not None and not cdf.empty:
            frames.append(cdf)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


__all__ = [
    "SLEEVE_COLS",
    "attribute_day_event_walk_sleeves",
    "attribute_range_event_walk_sleeves",
    "attribute_range_by_cycle",
]
