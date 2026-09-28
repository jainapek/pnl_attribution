# Nexus gross PnL — identity and attribution split

The target is to **match `gross`** from
`algo-research` / `dashboard/pnl_attribution` (written to
`algo.nexus_pnl_attribution`). Strategy lines we add must sum back to that
number (plus one pile that is allowed to sit outside it).

---

## 1. Gross PnL (the thing we must match)

From `PnlAttribution._build_aggregate_pnl`:

```text
gross    = overnight + m2m_pnl + trade_pnl
net_pnl  = gross + total_clearing
```

Lot size is `CONTRACT_SIZE = 1000` barrels.

### 1.1 Overnight (repo definition)

Quantity is **frozen at yesterday’s EOD position**. Only the curve moves:

```text
prev_EOD_qty      = last spread-position snapshot yesterday
overnight         = prev_EOD_qty × (BOD_curve − prev_EOD_curve) × 1000
                  = BOD_book(on prev_EOD_qty) − yesterday_EOD_book
```

Day 1 has no previous EOD → overnight is empty.

### 1.2 Intraday mark-to-market

```text
live_book_value   = live_qty × live_curve × 1000
BOD_book_value    = prev_EOD_qty × BOD_curve × 1000
m2m_pnl           = live_book_value − BOD_book_value
```

`live_qty` updates when transfers, crosses, and fills change the book.
`m2m_pnl` does not know *why* lots are still there.

### 1.3 Combined inventory mark

If `live` and `BOD` use the same curve/position source as the repo:

```text
overnight + m2m_pnl  = live_book_value − yesterday_EOD_book
```

That is the entire **inventory / MTM** half of gross. The other half is cash
(`trade_pnl`).

---

## 2. Our MTM split (this document’s scope)

We attribute **`overnight + m2m_pnl` only**. We start today’s process from
**BOD quantity** (`P0`), even if that differs from yesterday EOD. Then our
overnight line may differ from the repo’s; the **sum**
`our_overnight + our_m2m` must still equal `repo overnight + repo m2m`
(i.e. `live_book − yesterday_EOD_book` on the same books/curves).

```text
yesterday EOD book
        │
        │  §A  overnight
        ▼
BOD book  =  P0 × BOD_curve × 1000
        │
        │  §B  today’s MTM, split by why we still hold
        ▼
live book =  live_qty × live_curve × 1000
```

### 2.1 §A — Overnight

Plug from yesterday’s close to this morning’s book. Quantity is allowed to
change (late snapshot, overnight booking). This is **not** the repo formula.

```text
yesterday_EOD_book  =  yesterday_EOD_qty × yesterday_EOD_curve × 1000
today_BOD_book      =  P0                × BOD_curve            × 1000

our_overnight       =  today_BOD_book − yesterday_EOD_book
```

Split if useful:

```text
curve on overlapping lots     P0 ∩ yesterday_EOD_qty  × (BOD_curve − yesterday_EOD_curve) × 1000
overnight qty change          (P0 − yesterday_EOD_qty) marked in at BOD
```

`P0` is the starting position for every stage below. It is yesterday’s leftover
inventory (PCA warehouse, unroutable crumbs, prediction that never came back,
anything else still on the book). Buffer lots are usually already cleared
overnight.

### 2.2 §B — Today’s MTM (`live_book − BOD_book`)

Partition **live quantity** by the pre-execution peels. Those buckets must add
up to `live_qty` (per book / tenor). Then:

```text
m2m_bucket  =  qty_bucket × live_curve × 1000  −  that slice’s BOD carry
```

BOD carry is `P0` marked at the BOD curve, allocated onto the lots that came
from `P0`. New lots from today’s transfer inherit the transfer as their
entry; their MTM from BOD→live is curve move after they land (the cash of
the transfer itself is **not** here — see §3.1).

| Bucket | What it is | MTM meaning |
|---|---|---|
| **Held crossing buffer** | Timed prompt hold, not yet crossed or expired | Curve on `held_cb` |
| **Held remaining-volume** | Daily inventory budget (Hedger, prompt, before 17:30) | Curve on `held_pred` |
| **PCA warehouse `W`** | Residual we *choose* to keep after killing PC1/2/3 | Curve on `W` |
| **Router leftover `L`** | In hedge target `H` but no ICE path / unsent crumb | Curve on `L` |
| **Unclassified / still `P0`** | BOD lots we never peeled (flatten books, inactive tenors) | Curve on leftover starting inventory |

Netting and **crossed** buffer lots are **gone**. They do not sit in MTM.
Their economic is exec-save (and, if two transfers killed each other, cash
already in `trade_pnl`).

Router output `T` is split by the execution strategy (§3). The **passive /
unfilled** part is MTM (`T_working`). The **filled** part leaves the book and
is `trade_pnl` — not an MTM bucket.

Tag `T_working` by algo so MTM shows *which* strategy is still warehousing
the hedge (Vulcan passive vs VWAP body). Do not fold it into unclassified.

Conservation for **live** lots after the peels, before counting fills:

```text
live_qty  ≈  held_cb + held_pred + W + L + unclassified_P0
          +  T_working_vulcan + T_working_vwap
```

After fills, `T_working` shrinks and `trade_pnl` grows.

### 2.3 MTM identity we must hit

```text
our_overnight
  + MTM(held_cb) + MTM(held_pred) + MTM(W) + MTM(L)
  + MTM(unclassified_P0)
  + MTM(T_working_vulcan) + MTM(T_working_vwap)
  = overnight + m2m_pnl          ← repo
  = live_book − yesterday_EOD_book
```

If BOD qty ≠ yesterday EOD qty, do **not** expect `our_overnight` to equal
repo `overnight`. Expect the **sum** on the left to equal the **sum** on the
right.

`W` is counted **once** (PCA market risk). Do not add it again as “held after
the hedging portfolio is traded.”

---

## 3. Execution strategies (split of `T` and of `trade_pnl`)

After the router emits `T`, algr picks an ADL. Default is **Vulcan**; **VWAP**
is the special case. Execution is **not** only cash. The passive / unfilled
slice of `T` is still inventory — same two P&L types as buffer / prediction /
PCA.

```text
T
  ├─ T_working   rests (Vulcan passive, initial-passive, VWAP body)
  │              → still on the book
  │              → MTM  =  T_working × (live_curve − entry_mid) × 1000
  │              → belongs in §2.2 / §2.3
  │
  └─ T_filled    aggress / VWAP tail / later passive fill
                 → leaves the book
                 → trade_pnl (fill vs mid, or repo cash)
```

| | Market risk | Execution (fill vs mid) |
|---|---|---|
| **Passive working** | yes — this *is* `T_working` | none yet |
| **Passive fill** | stops on that lot | usually saved vs mid |
| **Aggress fill** | ~zero dwell | usually paid vs mid |

Initial-passive is “more of `T` stays in MTM for longer.”

### 3.1 How `T` gets an algo

```text
T
  ├─ Hedger Spreads
  │    + tenor in VWAP_CONFIG   (today: Dec26/Dec27)
  │    + use_vwap_algo on
  │    + 07:00–18:00
  │         → VWAP
  │
  └─ everything else
        → Vulcan
```

Pre-hedge probes (`probe_*`) are a **different path**. They are stripped from
this live-order set. Their P&L stays in the existing prehedge waterfall.

FR pre-position (`nexus-fr`) uses the same Vulcan/VWAP picker; tag those
fills by *reason* (pre-position vs post-transfer hedge) so they do not mix
into this `T`.

### 3.2 Vulcan (default)

| Book | ADL |
|---|---|
| Almost all Nexus books | `vulcan_v3` |
| Spreadgr Alt 3 | `vulcan_v4` |

Hybrid: sits in the book, then aggresses if realised vol is below a
tenor-pair threshold (`oneCentAggressVol` / `twoCentAggressVol`). Many back
pairs are `DONT_AGGRESS_VOL` (stay passive). Also: `disclosedQuantityRatio`,
`depthToWork`.

**Initial-passive** is an A/B *inside* Vulcan (high-vol, eligible prompt
spreads, flagged books):

| Arm | What runs |
|---|---|
| Treatment | separate ADL (`vulcan_initial_passive_algo_name`), starts passive |
| Control | desk-default Vulcan, tagged `_control` on `textC` only |

Same `T`, different start-of-order behaviour. Tag `T_working` and fills by
arm.

### 3.3 VWAP

Only **Hedger Spreads**, only the configured long calendar (Dec26/Dec27),
only in hours, only if the flag is on. Participation / wait from vol and
depth; a tail is dumped more aggressively. Orders are not stacked: qty
change > 1 kb → kill and replace.

### 3.4 `trade_pnl` — remaining inside gross

Repo (daily cumsum, **cash**, not fill-vs-mid):

```text
trade_pnl  =  Σ  (−qty × booked_price × 1000)     over screen fills
           +  Σ  (−qty × booked_price × 1000)     over transfers
```

| Slice | Source | Attribution |
|---|---|---|
| **Transfer cash** | Desk → Nexus booking | Entry vs mid is the warehouse fee. Booked cash is this line. |
| **Screen cash** | `T_filled` (`algo.nexus_trades`) | Split by strategy: Vulcan v3/v4 (default aggress/passive, initial-passive treatment vs control), VWAP. |

```text
trade_pnl
    ├─ transfer cash
    └─ screen cash
            ├─ Vulcan v3 / v4
            │     ├─ default (aggress / passive by vol)
            │     └─ initial-passive treatment vs control
            └─ VWAP
```

Identity after MTM is done:

```text
trade_pnl  =  gross − (overnight + m2m_pnl)
           =  gross − (our MTM buckets, summed)
```

Do **not** put all of Vulcan/VWAP under `trade_pnl`. Only `T_filled`.
`T_working` stays in MTM.

### 3.5 `total_clearing` — not in gross

```text
net_pnl  =  gross + total_clearing
```

Fees/clearing. Out of scope for strategy attribution. Do not put them in MTM
or exec-save.

### 3.6 Exec-save — not in gross at all

Counterfactual: lots we **did not** send × an assumed $ / lot. Same assumed
cost everywhere.

| Stage | Lots that create exec-save |
|---|---|
| Netting | `|P0| + |x| − |P_net|` |
| Crossing buffer | `crossed + held_cb` (held only until expire) |
| Remaining-volume | `held_pred` |
| PCA | `|W|` vs flattening `P_pred` |
| Router | cheaper / smaller `T` vs trading `H` naïvely |

```text
exec_save  ∉  gross
```

Never add exec-save into `overnight`, `m2m_pnl`, or `trade_pnl`. It is a
fourth headline for “what we avoided paying the screen.”

---

## 4. Picture

```text
                    yesterday EOD book
                           │
                    §A overnight          ← our def; may ≠ repo overnight
                           │
                      BOD book (P0)
                           │
         ┌─────────────────┼─────────────────┐
         │                 │                 │
    held_cb           held_pred              W
    held L            unclassified P0        T_working (Vulcan / VWAP)
         │                 │                 │
         └──────────── today’s MTM ──────────┘
                           │
                      live book
                           │
     overnight + m2m  ════════════════  must match repo
                           │
                     + trade_pnl
                        ├─ transfer cash
                        └─ T_filled (Vulcan / VWAP)
                           │
                        GROSS           ← must match repo
                           │
                     + clearing         ← net only
                           │
                         NET

     exec_save  ── parallel, never added ──
```

---

## 5. What “done” means

1. Per book / day, `Σ` MTM buckets (including `T_working` by algo) `=` repo
   `overnight + m2m_pnl`.
2. `gross − that sum =` repo `trade_pnl`, split into transfer cash +
   `T_filled` by Vulcan / VWAP (and Vulcan arms).
3. Exec-save is reported next to gross, not inside it.
