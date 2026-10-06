"""Running book from BOD ``algo.position`` plus tagged transfers and fills."""

from __future__ import annotations

from datetime import date

import pandas as pd
from clickhouse_connect.driver import Client

from .config import CONFIG, AppConfig
from .library import load_library_eod_pnl
from .market_cache import _ch_dt64_utc, _naive as _curve_naive, load_curve_asof_many
from .intentions import cycles_for_day
from .overnight import (
    _first_curve_on_london_date,
    _last_position_before,
    _library_eod_ts,
)
from .spreads import position_map_to_series
from .trades import load_trades_for_book_day, pack_to_consecutive_lots, parse_brn_calendar
from .transfers import (
    book_signed_qty,
    load_transfers_for_book_day,
    naive_utc,
    normalize_cycle_id,
)

_REASON_RANK = {"transfer": 0, "fill": 1}

TIMELINE_COLUMNS = [
    "timestamp",
    "reason",
    "position_before",
    "position_after",
    "id",
    "cycle_id",
    "price",
    "outbound",
    "source_book",
]


def map_to_series(pos_map) -> pd.Series:
    """Inverse of ``series_to_map``."""
    if not pos_map or not isinstance(pos_map, dict):
        return pd.Series(dtype=float)
    s = pd.Series({pd.Timestamp(k): float(v) for k, v in pos_map.items()})
    s.index = pd.DatetimeIndex(s.index).normalize()
    return s.sort_index()


def series_to_map(qty: pd.Series) -> dict:
    """Non-zero consecutive-1m lots → ``{front_month: lots}``."""
    if qty is None or len(qty) == 0:
        return {}
    s = qty.copy()
    s.index = pd.DatetimeIndex(s.index).normalize()
    s = s[s.abs() > 1e-12]
    return {pd.Timestamp(k).normalize(): float(v) for k, v in s.items()}


def _apply_delta(book: pd.Series, delta: pd.Series) -> pd.Series:
    if delta is None or len(delta) == 0:
        return book
    out = book.add(delta, fill_value=0.0)
    out.index = pd.DatetimeIndex(out.index).normalize()
    return out[out.abs() > 1e-12].sort_index()


def load_bod_spreads(
    client: Client,
    asof_date: date,
    book: str,
    cfg: AppConfig | None = None,
) -> tuple[pd.Timestamp | None, pd.Series]:
    """Last ``algo.position`` strictly before the first London-day curve.

    Same BOD inventory as overnight close→BOD. Empty series if none.
    """
    c = cfg or CONFIG
    book_id = c.books.get(book)
    if book_id is None:
        return None, pd.Series(dtype=float)
    bod_ts = _first_curve_on_london_date(client, asof_date, c)
    if bod_ts is None:
        return None, pd.Series(dtype=float)
    prev = _last_position_before(client, book_id, bod_ts)
    if prev is None:
        return bod_ts, pd.Series(dtype=float)
    _pos_ts, qty = prev
    return bod_ts, qty


def _transfer_delta_and_prices(row) -> tuple[pd.Series, dict]:
    start = pd.Timestamp(row["start_tenor"]).normalize()
    end = pd.Timestamp(row["end_tenor"]).normalize()
    signed = book_signed_qty(row["side"], row["quantity"])
    if signed == 0.0:
        return pd.Series(dtype=float), {}
    legs = pack_to_consecutive_lots(start, end, signed)
    n = len(legs)
    if n == 0:
        return pd.Series(dtype=float), {}
    price_per_1m = float(row["price"]) / n
    prices = {pd.Timestamp(t).normalize(): price_per_1m for t in legs.index}
    return legs, prices


def _fill_delta_and_prices(row) -> tuple[pd.Series, dict]:
    parsed = parse_brn_calendar(row["instrument_key"])
    if parsed is None:
        return pd.Series(dtype=float), {}
    start, end = parsed
    side = str(row["side"]).strip().lower()
    qty = float(row["quantity"])
    signed = qty if side == "buy" else -qty if side == "sell" else 0.0
    if signed == 0.0:
        return pd.Series(dtype=float), {}
    legs = pack_to_consecutive_lots(start, end, signed)
    n = len(legs)
    if n == 0:
        return pd.Series(dtype=float), {}
    price_per_1m = float(row["price"]) / n
    prices = {pd.Timestamp(t).normalize(): price_per_1m for t in legs.index}
    return legs, prices


def _collect_events(
    transfers: pd.DataFrame,
    trades: pd.DataFrame,
) -> list[dict]:
    events: list[dict] = []
    if transfers is not None and not transfers.empty:
        for i, row in transfers.iterrows():
            delta, prices = _transfer_delta_and_prices(row)
            if delta.empty:
                continue
            outbound = bool(row.get("outbound", False))
            if outbound:
                delta = -delta
            events.append(
                {
                    "timestamp": naive_utc(row["timestamp"]),
                    "reason": "transfer",
                    "id": row.get("id"),
                    "cycle_id": normalize_cycle_id(row.get("cycle_id")),
                    "delta": delta,
                    "price": prices,
                    "outbound": outbound,
                    "source_book": row.get("source_book"),
                    "_rank": _REASON_RANK["transfer"],
                    "_ord": i,
                }
            )
    if trades is not None and not trades.empty:
        for i, row in trades.iterrows():
            delta, prices = _fill_delta_and_prices(row)
            if delta.empty:
                continue
            events.append(
                {
                    "timestamp": naive_utc(row["transaction_timestamp"]),
                    "reason": "fill",
                    "id": row.get("parent_id"),
                    "cycle_id": normalize_cycle_id(row.get("cycle_id")),
                    "delta": delta,
                    "price": prices,
                    "outbound": False,
                    "source_book": None,
                    "_rank": _REASON_RANK["fill"],
                    "_ord": i,
                }
            )
    events.sort(key=lambda e: (e["timestamp"], e["_rank"], e["_ord"]))
    return events


def build_position_timeline(
    client: Client,
    asof_date: date,
    book: str,
    cfg: AppConfig | None = None,
    *,
    exclude_eod_roll: bool = True,
) -> pd.DataFrame:
    """Event-by-event running inventory (consecutive 1m spreads).

    Starts at BOD ``algo.position``. Each row is one transfer or fill:

    - ``timestamp`` — event time (UTC-naive)
    - ``reason`` — ``transfer`` or ``fill``
    - ``position_before`` / ``position_after`` — maps front month → lots
    - ``id`` — transfer ``id`` or fill ``parent_id``
    - ``cycle_id`` — tag on the transfer or fill (``None`` if untagged)
    - ``price`` — map front month → deal price per 1m leg (``price / n`` on packs)
    - ``outbound`` — True for desk-sent rolls booked only on the receiving
      ``source_book`` (lot sign flipped). Cash for those stays on the receiver.

    Same-timestamp ties: transfers apply before fills.
    """
    c = cfg or CONFIG
    _bod_ts, book_qty = load_bod_spreads(client, asof_date, book, c)
    transfers = load_transfers_for_book_day(
        client,
        book,
        asof_date,
        exclude_eod_roll=exclude_eod_roll,
        include_outbound=True,
    )
    trades = load_trades_for_book_day(client, book, asof_date)

    rows: list[dict] = []
    running = book_qty.copy()
    if not running.empty:
        running.index = pd.DatetimeIndex(running.index).normalize()
        running = running[running.abs() > 1e-12].sort_index()

    for ev in _collect_events(transfers, trades):
        before = running
        after = _apply_delta(running, ev["delta"])
        rows.append(
            {
                "timestamp": ev["timestamp"],
                "reason": ev["reason"],
                "position_before": series_to_map(before),
                "position_after": series_to_map(after),
                "id": ev.get("id"),
                "cycle_id": ev["cycle_id"],
                "price": ev["price"],
                "outbound": ev.get("outbound", False),
                "source_book": ev.get("source_book"),
            }
        )
        running = after

    return pd.DataFrame(rows, columns=TIMELINE_COLUMNS)


def stamp_overnight_on_timeline(
    client: Client,
    timeline: pd.DataFrame,
    asof_date: date,
    book: str,
    cfg: AppConfig | None = None,
    *,
    overnight: float | None = None,
) -> pd.DataFrame:
    """Day overnight $ as a fixed column on every ledger row (no cumsum).

    Uses the same overnight as ``reconcile_timeline_vs_library`` (prev session
    last fill/transfer → today's first transfer) unless ``overnight`` is given.
    """
    out = timeline.copy()
    if overnight is None:
        recon = reconcile_timeline_vs_library(
            client, asof_date, book, timeline=out, cfg=cfg
        )
        overnight = float(recon.loc["overnight", "ours"])
    out["overnight"] = round(float(overnight), 2)
    return out


def stamp_timing_mr_on_timeline(
    timeline: pd.DataFrame,
    timing: pd.DataFrame,
) -> pd.DataFrame:
    """Put transfer→intention timing MR on the running book.

    ``timing`` is the assigned-transfer table with ``id`` and ``timing_mr``.
    The PnL is booked on the transfer row where lots hit the book. Fills,
    Hedger→Base 2 rollers, and unassigned transfers stay 0.
    ``timing_mr_cum`` is that sleeve as inventory walks.
    """
    out = timeline.copy()
    extra = ["timing_mr", "lag_ms", "cycle_ts", "assigned_from"]
    for col in extra:
        if col in timing.columns:
            m = (
                timing.dropna(subset=["id"])
                .drop_duplicates("id")
                .set_index("id")[col]
            )
            out[col] = out["id"].map(m) if "id" in out.columns else pd.NA
        elif col == "timing_mr":
            out[col] = 0.0
        else:
            out[col] = pd.NA

    not_xfer = out["reason"].ne("transfer")
    out.loc[not_xfer, "timing_mr"] = 0.0
    out["timing_mr"] = pd.to_numeric(out["timing_mr"], errors="coerce").fillna(0.0)
    out["timing_mr_cum"] = out["timing_mr"].cumsum().round(2)

    if "cycle_id_assigned" in timing.columns and "id" in out.columns:
        cid = (
            timing.dropna(subset=["id"])
            .drop_duplicates("id")
            .set_index("id")["cycle_id_assigned"]
        )
        mapped = out["id"].map(cid)
        hit = out["reason"].eq("transfer") & mapped.notna()
        out["cycle_id"] = out["cycle_id"].astype(object)
        out.loc[hit, "cycle_id"] = mapped[hit].astype(object)
    return out


def stamp_pca_held_on_timeline(
    timeline: pd.DataFrame,
    intentions: pd.DataFrame,
) -> pd.DataFrame:
    """Attach cycle ``position_held_pca`` as ``PCA_held`` (same map style as book).

    First intention per ``cycle_id``. Rows with no cycle stay empty ``{}``.
    """
    out = timeline.copy()
    if intentions is None or intentions.empty or "cycle_id" not in out.columns:
        out["PCA_held"] = [{} for _ in range(len(out))]
        return out

    cycles = (
        intentions.sort_values("timestamp")
        .drop_duplicates("cycle_id", keep="first")
    )
    held: dict = {}
    for row in cycles.itertuples(index=False):
        cid = normalize_cycle_id(getattr(row, "cycle_id", None))
        if cid is None:
            continue
        raw = getattr(row, "position_held_pca", None) or {}
        held[cid] = series_to_map(position_map_to_series(raw))

    def _lookup(cid):
        key = normalize_cycle_id(cid)
        if key is None:
            return {}
        return held.get(key, {})

    out["PCA_held"] = out["cycle_id"].map(_lookup)
    out["PCA_held"] = out["PCA_held"].apply(lambda x: x if isinstance(x, dict) else {})
    return out


def _event_delta_series(row) -> pd.Series:
    after = map_to_series(getattr(row, "position_after", None) or {})
    before = map_to_series(getattr(row, "position_before", None) or {})
    out = after.sub(before, fill_value=0.0)
    return out[out.abs() > 1e-12].sort_index()


def _first_inbound_xfer_index(timeline: pd.DataFrame, cycle_id: str) -> int | None:
    cid = normalize_cycle_id(cycle_id)
    if cid is None or timeline is None or timeline.empty:
        return None
    for i, row in enumerate(timeline.itertuples(index=False)):
        if getattr(row, "reason", None) != "transfer":
            continue
        if bool(getattr(row, "outbound", False)):
            continue
        if normalize_cycle_id(getattr(row, "cycle_id", None)) != cid:
            continue
        return i
    return None


def _mark_to_fill_pnl(
    delta: pd.Series,
    price: dict,
    curve: pd.Series | None,
    size: int,
) -> float:
    """Σ lots × size × (curve_at_fill − price/n). Buy below mark → +."""
    if delta is None or delta.empty or not price or curve is None or curve.empty:
        return 0.0
    s = curve.copy()
    s.index = pd.DatetimeIndex(s.index).normalize()
    px_map = {
        pd.Timestamp(k).normalize(): float(v) for k, v in price.items()
    }
    total = 0.0
    for tenor, lots in delta.items():
        key = pd.Timestamp(tenor).normalize()
        mark = float(s.reindex([key]).fillna(0.0).iloc[0])
        total += float(lots) * size * (mark - px_map.get(key, 0.0))
    return round(total, 2)


def stamp_cycle_component_mr_on_timeline(
    client: Client,
    timeline: pd.DataFrame,
    intentions: pd.DataFrame,
    cfg: AppConfig | None = None,
) -> pd.DataFrame:
    """PCA-held / fill / unexecuted MR + mark-to-fill on the running book.

    Per cycle ``i`` (intention order):

    - ``pca_held_mr`` — ``PCA_held`` marked intention_ts → first inbound
      transfer of cycle ``i+1`` (fallback: intention ``next_cycle_start``).
      Booked once on the first inbound transfer of cycle ``i``.
    - ``fill_mr`` — risk lots closed by each fill (``−fill_delta``) marked
      cycle start → fill time; on the fill row. Fills hedge (opp. sign to
      the book); MR is on the risk that was on the book until the print.
    - ``mark_to_fill`` — actual fill lots × size × (curve_at_fill − price/n);
      on the fill row (same as library ``mark_to_exec``).
    - ``unexecuted_mr`` — ``book − PCA_held + Σ fills`` = leftover risk after
      carving PCA and the hedged slice (e.g. 30 − 5 + (−7) = 18). Same
      window as PCA held; booked with ``pca_held_mr``.

    ``unexecuted`` is the residual lot map (same style as ``PCA_held``).
    """
    c = cfg or CONFIG
    size = c.contract.size
    out = timeline.copy()
    for col in ("pca_held_mr", "fill_mr", "mark_to_fill", "unexecuted_mr"):
        out[col] = 0.0
    out["unexecuted"] = [{} for _ in range(len(out))]

    def _zero_cums(frame: pd.DataFrame) -> pd.DataFrame:
        frame["pca_held_mr_cum"] = 0.0
        frame["fill_mr_cum"] = 0.0
        frame["mark_to_fill_cum"] = 0.0
        frame["unexecuted_mr_cum"] = 0.0
        return frame

    if out.empty or intentions is None or intentions.empty:
        return _zero_cums(out)

    if "PCA_held" not in out.columns:
        out = stamp_pca_held_on_timeline(out, intentions)

    asof_date = naive_utc(out["timestamp"].iloc[0]).date()
    cycles = cycles_for_day(intentions, asof_date)
    if cycles.empty:
        return _zero_cums(out)

    cycle_ids = [normalize_cycle_id(x) for x in cycles["cycle_id"]]
    cycle_ids = [x for x in cycle_ids if x is not None]
    t0_by = {
        normalize_cycle_id(r.cycle_id): naive_utc(r.timestamp)
        for r in cycles.itertuples(index=False)
        if normalize_cycle_id(r.cycle_id) is not None
    }
    t1_fallback = {
        normalize_cycle_id(r.cycle_id): naive_utc(r.next_cycle_start)
        for r in cycles.itertuples(index=False)
        if normalize_cycle_id(r.cycle_id) is not None
    }

    next_xfer_t1: dict[str, pd.Timestamp] = {}
    for i, cid in enumerate(cycle_ids[:-1]):
        nxt = cycle_ids[i + 1]
        j = _first_inbound_xfer_index(out, nxt)
        if j is not None:
            next_xfer_t1[cid] = naive_utc(out.iloc[j]["timestamp"])

    # Collect curve stamps: cycle starts, windows ends, every fill ts.
    times: list[pd.Timestamp] = []
    fill_rows: list[tuple[int, str, pd.Timestamp, pd.Series, dict]] = []
    for idx, row in enumerate(out.itertuples(index=False)):
        if getattr(row, "reason", None) != "fill":
            continue
        cid = normalize_cycle_id(getattr(row, "cycle_id", None))
        if cid is None or cid not in t0_by:
            continue
        delta = _event_delta_series(row)
        if delta.empty:
            continue
        ts = naive_utc(getattr(row, "timestamp"))
        price = getattr(row, "price", None) or {}
        if not isinstance(price, dict):
            price = {}
        fill_rows.append((idx, cid, ts, delta, price))
        times.append(ts)

    for cid in cycle_ids:
        times.append(t0_by[cid])
        times.append(next_xfer_t1.get(cid, t1_fallback[cid]))

    curves = load_curve_asof_many(client, times, c.curves) if times else None

    def _curve(ts: pd.Timestamp) -> pd.Series | None:
        if curves is None:
            return None
        return curves.asof(_curve_naive(ts))

    fills_by_cycle: dict[str, pd.Series] = {}
    for idx, cid, ts, delta, price in fill_rows:
        s0 = _curve(t0_by[cid])
        s1 = _curve(ts)
        ix = out.index[idx]
        # Risk closed by the hedge = opposite of the fill's book delta.
        risk_closed = -delta
        out.at[ix, "fill_mr"] = round(_mtm(risk_closed, s0, s1, size), 2)
        out.at[ix, "mark_to_fill"] = _mark_to_fill_pnl(delta, price, s1, size)
        prev = fills_by_cycle.get(cid)
        fills_by_cycle[cid] = (
            delta if prev is None else prev.add(delta, fill_value=0.0)
        )

    for cid in cycle_ids:
        t0 = t0_by[cid]
        t1 = next_xfer_t1.get(cid, t1_fallback[cid])
        if t1 <= t0:
            continue
        pca = map_to_series(
            out.loc[out["cycle_id"].map(normalize_cycle_id) == cid, "PCA_held"].iloc[0]
            if (out["cycle_id"].map(normalize_cycle_id) == cid).any()
            else {}
        )
        # Prefer intention map even if no ledger row yet.
        if pca.empty:
            crow = cycles.loc[
                cycles["cycle_id"].map(normalize_cycle_id) == cid
            ].iloc[0]
            pca = position_map_to_series(crow.get("position_held_pca") or {})

        book = map_to_series(position_asof(out, t0))
        fill_sum = fills_by_cycle.get(cid, pd.Series(dtype=float))
        # book − PCA − risk_closed, with risk_closed = −fills
        # → book − PCA + fills  (e.g. 30 − 5 + (−7) = 18)
        unex = book.sub(pca, fill_value=0.0).add(fill_sum, fill_value=0.0)
        unex = unex[unex.abs() > 1e-12].sort_index()

        s0 = _curve(t0)
        s1 = _curve(t1)
        pca_pnl = round(_mtm(pca, s0, s1, size), 2)
        unex_pnl = round(_mtm(unex, s0, s1, size), 2)
        unex_map = series_to_map(unex)

        book_idx = _first_inbound_xfer_index(out, cid)
        if book_idx is None:
            # Fall back to first row of the cycle (often a fill).
            for i, row in enumerate(out.itertuples(index=False)):
                if normalize_cycle_id(getattr(row, "cycle_id", None)) == cid:
                    book_idx = i
                    break
        if book_idx is None:
            continue
        ix = out.index[book_idx]
        out.at[ix, "pca_held_mr"] = pca_pnl
        out.at[ix, "unexecuted_mr"] = unex_pnl
        cycle_mask = out["cycle_id"].map(normalize_cycle_id) == cid
        if cycle_mask.any():
            out.loc[cycle_mask, "unexecuted"] = pd.Series(
                [unex_map] * int(cycle_mask.sum()),
                index=out.index[cycle_mask],
                dtype=object,
            )

    out["pca_held_mr"] = pd.to_numeric(out["pca_held_mr"], errors="coerce").fillna(0.0)
    out["fill_mr"] = pd.to_numeric(out["fill_mr"], errors="coerce").fillna(0.0)
    out["mark_to_fill"] = pd.to_numeric(out["mark_to_fill"], errors="coerce").fillna(
        0.0
    )
    out["unexecuted_mr"] = pd.to_numeric(out["unexecuted_mr"], errors="coerce").fillna(
        0.0
    )
    out["pca_held_mr_cum"] = out["pca_held_mr"].cumsum().round(2)
    out["fill_mr_cum"] = out["fill_mr"].cumsum().round(2)
    out["mark_to_fill_cum"] = out["mark_to_fill"].cumsum().round(2)
    out["unexecuted_mr_cum"] = out["unexecuted_mr"].cumsum().round(2)
    return out


def position_asof(timeline: pd.DataFrame, ts) -> dict:
    """Running book immediately after the last event at or before ``ts``."""
    if timeline is None or timeline.empty:
        return {}
    t = naive_utc(ts)
    hit = timeline.loc[timeline["timestamp"] <= t]
    if hit.empty:
        first = timeline.iloc[0]["position_before"]
        return first if isinstance(first, dict) else {}
    last = hit.iloc[-1]["position_after"]
    return last if isinstance(last, dict) else {}


def load_last_fill_before(
    client: Client,
    book: str,
    before: pd.Timestamp,
) -> pd.Timestamp | None:
    """Latest ``nexus_trades`` fill strictly before ``before``."""
    raw = client.query_df(
        f"""
        SELECT transaction_timestamp
        FROM algo.nexus_trades
        WHERE text_tt = '{book}'
          AND transaction_timestamp < {_ch_dt64_utc(before)}
        ORDER BY transaction_timestamp DESC
        LIMIT 1
        """
    )
    if raw is None or raw.empty:
        return None
    ts = naive_utc(raw.iloc[0]["transaction_timestamp"])
    if ts.year < 1990:
        return None
    return ts


def load_last_transfer_before(
    client: Client,
    book: str,
    before: pd.Timestamp,
    *,
    exclude_eod_roll: bool = True,
) -> pd.Timestamp | None:
    """Latest ``nexus_transfers`` row strictly before ``before``."""
    hour_filter = "AND toHour(timestamp) < 21" if exclude_eod_roll else ""
    raw = client.query_df(
        f"""
        SELECT timestamp
        FROM algo.nexus_transfers
        WHERE timestamp < {_ch_dt64_utc(before)}
          AND (
            (source_book = '{book}' {hour_filter})
            OR (desk = '{book}' AND source_book != '{book}')
          )
        ORDER BY timestamp DESC
        LIMIT 1
        """
    )
    if raw is None or raw.empty:
        return None
    ts = naive_utc(raw.iloc[0]["timestamp"])
    if ts.year < 1990:
        return None
    return ts


def _book_value(qty: pd.Series, curve: pd.Series | None, size: int) -> float:
    if qty is None or len(qty) == 0 or curve is None or curve.empty:
        return 0.0
    curve = curve.copy()
    curve.index = pd.DatetimeIndex(curve.index).normalize()
    qty = qty.copy()
    qty.index = pd.DatetimeIndex(qty.index).normalize()
    idx = curve.index.intersection(qty.index)
    if len(idx) == 0:
        return 0.0
    q = qty.reindex(idx).fillna(0.0)
    m = curve.reindex(idx)
    ok = m.notna()
    if not ok.any():
        return 0.0
    return float((q[ok] * size * m[ok]).sum())


def _mtm(qty: pd.Series, s0: pd.Series | None, s1: pd.Series | None, size: int) -> float:
    if qty is None or len(qty) == 0 or s0 is None or s1 is None or s0.empty or s1.empty:
        return 0.0
    s0 = s0.copy()
    s1 = s1.copy()
    s0.index = pd.DatetimeIndex(s0.index).normalize()
    s1.index = pd.DatetimeIndex(s1.index).normalize()
    qty = qty.copy()
    qty.index = pd.DatetimeIndex(qty.index).normalize()
    idx = s0.index.intersection(s1.index)
    if len(idx) == 0:
        return 0.0
    q = qty.reindex(idx).fillna(0.0)
    m0 = s0.reindex(idx)
    m1 = s1.reindex(idx)
    ok = m0.notna() & m1.notna()
    if not ok.any():
        return 0.0
    return float((q[ok] * size * (m1[ok] - m0[ok])).sum())


def _event_cash(delta: pd.Series, price: dict, size: int, *, outbound: bool = False) -> float:
    """Library cash: ``−signed_qty × price × size`` on lots that hit the book.

    Lot sign matches the running book (desk buy transfer → −, screen buy → +).
    Pack legs use ``price / n``. Outbound rolls book cash on the receiving
    book only — sender qty changes, cash is 0 here.
    """
    if outbound or not price:
        return 0.0
    total = 0.0
    d = delta.copy()
    d.index = pd.DatetimeIndex(d.index).normalize()
    for tenor, px in price.items():
        key = pd.Timestamp(tenor).normalize()
        lots = float(d.reindex([key]).fillna(0.0).iloc[0])
        total += -lots * size * float(px)
    return total


def _split_window(
    t0: pd.Timestamp,
    t1: pd.Timestamp,
    cut: pd.Timestamp | None,
) -> tuple[tuple[pd.Timestamp, pd.Timestamp] | None, tuple[pd.Timestamp, pd.Timestamp] | None]:
    """Split ``[t0, t1]`` at ``cut`` into overnight vs day."""
    if t1 <= t0:
        return None, None
    if cut is None or t1 <= cut:
        return (t0, t1), None
    if t0 >= cut:
        return None, (t0, t1)
    return (t0, cut), (cut, t1)


def reconcile_timeline_vs_library(
    client: Client,
    asof_date: date,
    book: str,
    timeline: pd.DataFrame | None = None,
    cfg: AppConfig | None = None,
    *,
    exclude_eod_roll: bool = True,
) -> pd.DataFrame:
    """Running-book PnL vs EOD ``algo.nexus_pnl_attribution``.

    Overnight starts at whichever of the previous session's **last fill**
    or **last transfer** is later, and runs to this day's **first transfer**.
    If neither exists, the inventory mark starts at BOD. After that:
    running-book MTM + transfer/fill cash through the library EOD snapshot.

    Library live book is ``algo.position`` at that EOD bar, not our leftover
    lots, so the EOD qty gap is marked at the EOD curve (into ``m2m_pnl``).
    Then ``overnight + m2m`` = live_BV − start_BV and ``gross`` matches
    even if overnight vs library overnight still differ as a split.
    """
    c = cfg or CONFIG
    size = c.contract.size
    bod_ts, bod = load_bod_spreads(client, asof_date, book, c)
    tl = (
        timeline
        if timeline is not None
        else build_position_timeline(
            client, asof_date, book, c, exclude_eod_roll=exclude_eod_roll
        )
    )

    xfers = tl.loc[tl["reason"] == "transfer"] if not tl.empty else tl
    t_first = naive_utc(xfers.iloc[0]["timestamp"]) if len(xfers) else None
    t_anchor = t_first if t_first is not None else bod_ts
    t_last_fill = (
        load_last_fill_before(client, book, t_anchor)
        if t_anchor is not None
        else None
    )
    t_last_xfer = (
        load_last_transfer_before(
            client, book, t_anchor, exclude_eod_roll=exclude_eod_roll
        )
        if t_anchor is not None
        else None
    )
    later = [t for t in (t_last_fill, t_last_xfer) if t is not None]
    t_last = max(later) if later else None
    lib_eod = _library_eod_ts(client, asof_date, book)
    lib = load_library_eod_pnl(client, asof_date, book) or {}

    times = [t for t in (t_last, t_first, bod_ts, lib_eod) if t is not None]
    if not tl.empty:
        times.extend(naive_utc(t) for t in tl["timestamp"])
    curves = load_curve_asof_many(client, times, c.curves) if times else None

    overnight = 0.0
    m2m = 0.0
    trade = 0.0

    def add_mtm(qty: pd.Series, a: pd.Timestamp, b: pd.Timestamp) -> None:
        nonlocal overnight, m2m
        if curves is None or a is None or b is None or b <= a:
            return
        ov, day = _split_window(a, b, t_first)
        if ov is not None:
            overnight += _mtm(qty, curves.asof(ov[0]), curves.asof(ov[1]), size)
        if day is not None:
            m2m += _mtm(qty, curves.asof(day[0]), curves.asof(day[1]), size)

    def add_entry(delta: pd.Series, ts: pd.Timestamp) -> None:
        nonlocal overnight, m2m
        if curves is None or delta is None or len(delta) == 0:
            return
        val = _book_value(delta, curves.asof(ts), size)
        if t_first is not None and ts < t_first:
            overnight += val
        else:
            m2m += val

    t_mark = t_last
    qty = bod.copy()
    if not qty.empty:
        qty.index = pd.DatetimeIndex(qty.index).normalize()

    if not tl.empty:
        for row in tl.itertuples(index=False):
            ts = naive_utc(row.timestamp)
            before = map_to_series(row.position_before)
            after = map_to_series(row.position_after)
            if t_mark is not None:
                add_mtm(before, t_mark, ts)
            delta = after.subtract(before, fill_value=0.0)
            add_entry(delta, ts)
            outbound = bool(getattr(row, "outbound", False))
            trade += _event_cash(delta, row.price or {}, size, outbound=outbound)
            qty = after
            t_mark = ts

    if t_mark is not None and lib_eod is not None:
        add_mtm(qty, t_mark, lib_eod)
    elif t_last is not None and lib_eod is not None and (tl is None or tl.empty):
        add_mtm(bod, t_last, lib_eod)

    eod_align = 0.0
    eod_lots_missing = 0.0
    book_id = c.books.get(book)
    if lib_eod is not None and book_id is not None:
        algo_eod = _last_position_before(
            client, book_id, lib_eod + pd.Timedelta(milliseconds=1)
        )
        if algo_eod is not None and curves is not None:
            gap = algo_eod[1].subtract(qty, fill_value=0.0)
            gap = gap[gap.abs() > 1e-12]
            eod_lots_missing = float(gap.abs().sum())
            eod_align = _book_value(gap, curves.asof(lib_eod), size)
            m2m += eod_align

    our = {
        "overnight": round(overnight, 2),
        "m2m_pnl": round(m2m, 2),
        "trade_pnl": round(trade, 2),
    }
    our["gross"] = round(our["overnight"] + our["m2m_pnl"] + our["trade_pnl"], 2)
    our["overnight+m2m"] = round(our["overnight"] + our["m2m_pnl"], 2)
    lib_inv = (
        round(float(lib["overnight"]) + float(lib["m2m_pnl"]), 2)
        if lib
        else float("nan")
    )
    rows = []
    for col in ("overnight", "m2m_pnl", "overnight+m2m", "trade_pnl", "gross"):
        if col == "overnight+m2m":
            lib_v = lib_inv
        else:
            lib_v = float(lib[col]) if lib and col in lib else float("nan")
        ours = our[col]
        rows.append(
            {
                "component": col,
                "ours": ours,
                "library": lib_v,
                "diff": round(ours - lib_v, 2) if pd.notna(lib_v) else float("nan"),
            }
        )
    out = pd.DataFrame(rows).set_index("component")
    out.attrs["t_last_transfer"] = t_last_xfer
    out.attrs["t_last_fill"] = t_last_fill
    out.attrs["overnight_start"] = t_last
    out.attrs["overnight_end"] = t_first
    out.attrs["t_first_transfer"] = t_first
    out.attrs["lib_eod"] = lib_eod
    out.attrs["bod_ts"] = bod_ts
    out.attrs["eod_align"] = round(eod_align, 2)
    out.attrs["eod_lots_missing"] = eod_lots_missing
    return out


__all__ = [
    "TIMELINE_COLUMNS",
    "build_position_timeline",
    "load_bod_spreads",
    "load_last_fill_before",
    "load_last_transfer_before",
    "map_to_series",
    "position_asof",
    "reconcile_timeline_vs_library",
    "series_to_map",
    "stamp_cycle_component_mr_on_timeline",
    "stamp_overnight_on_timeline",
    "stamp_pca_held_on_timeline",
    "stamp_timing_mr_on_timeline",
]
