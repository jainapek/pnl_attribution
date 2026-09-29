"""Compute day-total value_added c/bbl by strategy for a date range."""

from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from scripts import CONFIG, get_ch_client
from scripts.day_attribution import attribute_all_books_day


def main() -> None:
    client = get_ch_client()
    start = date(2026, 9, 17)
    end = date(2026, 9, 29)
    out_path = ROOT / "va_cbbl_20260917_20260929.csv"

    rows: list[dict] = []
    done: set[str] = set()
    if out_path.exists():
        prev = pd.read_csv(out_path)
        done = set(prev["date"].astype(str))
        rows = prev.to_dict("records")
        print("resuming, done", sorted(done), flush=True)

    stages = list(CONFIG.stage_order)
    d = start
    while d <= end:
        ds = d.isoformat()
        if ds in done:
            d += timedelta(days=1)
            continue
        t0 = time.time()
        print(f"=== {ds} ===", flush=True)
        try:
            df = attribute_all_books_day(client, d, progress_every=0)
        except Exception as e:
            print("FAILED", ds, e, flush=True)
            d += timedelta(days=1)
            continue
        if df.empty:
            print("empty", ds, flush=True)
            d += timedelta(days=1)
            continue

        day_totals = df.xs("DAY TOTAL", level="cycle_id", drop_level=False)
        pieces = [day_totals]
        if ("TEAM", "TOTAL") in df.index:
            pieces.append(df.loc[[("TEAM", "TOTAL")]])
        block = pd.concat(pieces)

        for idx, row in block.iterrows():
            book, _cid = idx
            abs_lots = float(row[("meta", "abs_lots", "")])
            rec: dict = {"date": ds, "book": book, "abs_lots": abs_lots}
            for stage in stages:
                cbbl_key = ("c/bbl", stage, "value_added_by_strategy")
                usd_key = ("$", stage, "value_added_by_strategy")
                rec[f"va_cbbl_{stage}"] = (
                    float(row[cbbl_key]) if cbbl_key in row.index else float("nan")
                )
                rec[f"va_usd_{stage}"] = (
                    float(row[usd_key]) if usd_key in row.index else float("nan")
                )
            rows.append(rec)

        pd.DataFrame(rows).to_csv(out_path, index=False)
        print(
            f"done {ds} in {time.time() - t0:.0f}s, "
            f"books={block.index.get_level_values(0).nunique()}",
            flush=True,
        )
        d += timedelta(days=1)

    print("WROTE", out_path, flush=True)


if __name__ == "__main__":
    main()
