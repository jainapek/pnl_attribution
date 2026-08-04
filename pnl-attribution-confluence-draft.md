# PnL Attribution Design For Transfer-Driven Hedging

## Purpose

We want a transfer-level PnL attribution framework that explains where every dollar comes from across the full hedging lifecycle.

The goal is not only to know the final execution outcome, but to decompose PnL into the strategy decisions that happened before execution and the execution behavior that followed.

This should let us answer questions like:

- How much PnL came from the hedge calculation itself?
- How much PnL came from choosing to hold residual risk?
- How much PnL came from crossing buffer logic?
- How much PnL came from execution timing?
- How much PnL came from order placement behavior?
- How much PnL came from fills versus residual inventory carry?

## Why This Is Needed

Today the data sources give us important pieces:

- transfers
- intentions
- nexus transfers
- nexus trades
- fills
- positions
- marks

But those pieces are not durably stitched into one lifecycle per transfer.

The main missing capability is end-to-end lineage:

`transfer -> strategy decision -> parent order -> child order -> fill -> residual inventory`

Without that lineage, we can measure outcomes, but we cannot reliably explain which strategy decision or execution action caused them.

## High-Level Attribution Flow

The proposed attribution flow is:

1. A transfer arrives.
2. We assign a stable `transfer_id`.
3. Each strategy decision creates one or more `allocation_id`s.
4. Execution tranches create `parent_order_id`s.
5. Market-facing orders create `child_order_id`s.
6. Fills create `fill_id`s.
7. Position snapshots preserve what quantity remains after each stage.
8. Marks captured at each stage allow us to compute PnL components.

## PnL Components

The framework should support at least these components:

- Structural residual PnL: risk deliberately left unhedged by hedge-calculation logic.
- Buffered hold PnL: risk temporarily withheld from execution by crossing buffer logic.
- Execution timing PnL: mark movement between decision time and actual release to market.
- Order placement PnL: mark movement while orders are working in the market.
- Fill edge PnL: difference between fill price and the chosen internal mark at fill time.
- Residual inventory PnL: mark-to-market on remaining open quantity.
- Total transfer PnL: realized plus unrealized PnL across the full lifecycle.

## Identifier Model

We need 5 identifiers.

| Identifier | Meaning | Owner | Recommendation |
| --- | --- | --- | --- |
| `transfer_id` | One incoming transfer lifecycle | algr | Use the transfer payload UUID, but make it an explicit field on the transfer object rather than generating it only inside serialization |
| `allocation_id` | One strategy slice of a transfer | algr | New UUID created whenever quantity is assigned to a strategy bucket |
| `parent_order_id` | One parent execution tranche | algo-runner / TT | Use TT parent order identity and persist it explicitly |
| `child_order_id` | One market-facing child order | algo-runner / TT | Use TT child order `orderId` as the canonical child order identifier |
| `fill_id` | One fill event | TT | Reuse TT fill ID as-is |

### Notes On Current algr Behavior

- algr currently generates a transfer payload `uuid` inside `serialise_to_tradr_transfer()`.
- Tradr then returns a numeric booked trade `id`.
- The payload UUID is the better top-level attribution key.
- The numeric Tradr `id` should still be kept as `tradr_trade_id` for reconciliation.

## Proposed Event Hierarchy

```text
transfer_id
  -> allocation_id
    -> parent_order_id
      -> child_order_id
        -> fill_id
```

Each downstream object should carry its upstream IDs directly to keep ClickHouse queries simple.

## Proposed Tables

We propose 5 core tables.

### 1. `transfer_lifecycle_events`

This table stores major lifecycle and strategy decisions for each transfer.

It answers:

- what happened to the transfer?
- when did it happen?
- which strategy made the decision?
- how much quantity moved between buckets?

#### Suggested columns

| Column | Meaning |
| --- | --- |
| `event_time` | Timestamp of lifecycle event |
| `transfer_id` | Top-level transfer key |
| `allocation_id` | Strategy slice key when applicable |
| `event_type` | `transfer_received`, `pca_decision`, `crossing_buffer_hold`, `execution_requested`, etc. |
| `strategy_category` | `hedge_calculation`, `execution_gate`, `execution_schedule` |
| `strategy_name` | `pca`, `crossing_buffer`, `time_slicer_15m`, etc. |
| `book` | Desk or book |
| `instrument_alias` | Instrument or spread alias |
| `side` | Buy or sell |
| `transfer_qty` | Original transfer quantity |
| `hedge_target_qty` | Quantity chosen for hedging |
| `held_qty` | Quantity deliberately held back |
| `execution_qty` | Quantity released for execution |
| `mark_price` | Internal mark at decision time |
| `reason_code` | Optional reason code |
| `metadata_json` | Optional raw context |

#### Sample rows

| event_time | transfer_id | allocation_id | event_type | strategy_category | strategy_name | transfer_qty | hedge_target_qty | held_qty | execution_qty | mark_price |
| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2026-07-31 09:00:00.000 | T1 |  | transfer_received | transfer | incoming_transfer | 80 | 0 | 0 | 0 | 74.20 |
| 2026-07-31 09:00:02.000 | T1 | A1 | pca_residual_created | hedge_calculation | pca | 80 | 60 | 40 | 0 | 74.21 |
| 2026-07-31 09:00:03.000 | T1 | A2 | crossing_buffer_hold_created | execution_gate | crossing_buffer | 80 | 60 | 20 | 0 | 74.22 |
| 2026-07-31 09:00:04.000 | T1 | A3 | execution_slice_created | execution_schedule | time_slicer_15m | 80 | 60 | 0 | 40 | 74.22 |

### 2. `transfer_strategy_allocations`

This table is the core strategy decomposition table.

It answers:

- how was the transfer split across strategies?
- how much quantity belongs to each strategy bucket?
- which allocations roll up under other allocations?

#### Suggested columns

| Column | Meaning |
| --- | --- |
| `allocation_time` | When the allocation was created |
| `allocation_id` | Unique strategy slice ID |
| `parent_allocation_id` | Parent slice if this is a child tranche |
| `transfer_id` | Top-level transfer key |
| `strategy_category` | Broad bucket |
| `strategy_name` | Exact strategy |
| `allocated_qty` | Quantity in this slice |
| `status` | `open`, `held`, `completed`, etc. |
| `mark_price` | Mark at allocation time |
| `reason_code` | Optional reason |

#### Sample rows

| allocation_time | allocation_id | parent_allocation_id | transfer_id | strategy_category | strategy_name | allocated_qty | status | mark_price |
| --- | --- | --- | --- | --- | --- | ---: | --- | ---: |
| 2026-07-31 09:00:02.000 | A1 |  | T1 | hedge_calculation | pca_structural_residual | 40 | open | 74.21 |
| 2026-07-31 09:00:03.000 | A2 |  | T1 | execution_gate | crossing_buffer_hold | 20 | held | 74.22 |
| 2026-07-31 09:00:04.000 | A3 |  | T1 | execution_schedule | time_slicer_15m | 40 | open | 74.22 |
| 2026-07-31 09:00:04.100 | A3_1 | A3 | T1 | order_placement | tranche_1 | 10 | open | 74.22 |
| 2026-07-31 09:15:04.100 | A3_2 | A3 | T1 | order_placement | tranche_2 | 10 | open | 74.14 |

### 3. `execution_orders`

This table stores both parent and child order events.

It answers:

- what parent execution tranche was created?
- what market-facing child orders were used to work it?
- which strategy slice did each order belong to?

#### Suggested columns

| Column | Meaning |
| --- | --- |
| `event_time` | Order event timestamp |
| `transfer_id` | Top-level transfer key |
| `allocation_id` | Strategy slice key |
| `order_level` | `parent` or `child` |
| `order_event_type` | `created`, `updated`, `cancelled`, `completed` |
| `parent_order_id` | Parent order key |
| `child_order_id` | Child order key, nullable for parent rows |
| `site_order_key` | Venue/site order key |
| `book` | Desk or book |
| `instrument_alias` | Instrument or spread alias |
| `side` | Buy or sell |
| `qty` | Order quantity |
| `remaining_qty` | Remaining quantity |
| `limit_price` | Order price |
| `placement_mark` | Mark at placement/update time |
| `order_style` | `passive`, `aggressive`, `reprice`, etc. |
| `algo_name` | Algo label |
| `identifier` | Existing runner reconciliation key |
| `text_b` | Existing order metadata |
| `text_c` | Existing order metadata |
| `raw_payload` | Optional raw event |

#### Sample rows

| event_time | transfer_id | allocation_id | order_level | order_event_type | parent_order_id | child_order_id | qty | remaining_qty | limit_price | order_style | algo_name |
| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | --- | --- |
| 2026-07-31 09:00:05.000 | T1 | A3_1 | parent | created | P1 |  | 10 | 10 | 74.19 | tranche | vulcan |
| 2026-07-31 09:00:06.000 | T1 | A3_1 | child | created | P1 | C1 | 4 | 4 | 74.19 | passive | vulcan |
| 2026-07-31 09:04:20.000 | T1 | A3_1 | child | created | P1 | C2 | 3 | 3 | 74.18 | reprice | vulcan |
| 2026-07-31 09:08:10.000 | T1 | A3_1 | child | created | P1 | C3 | 3 | 3 | 74.16 | aggressive | vulcan |

### 4. `execution_fills`

This table stores realized fills.

It answers:

- what actually traded?
- which transfer did the fill belong to?
- which strategy slice and parent order did it belong to?
- what was the execution edge versus mark?

#### Suggested columns

| Column | Meaning |
| --- | --- |
| `fill_time` | Fill timestamp |
| `transfer_id` | Top-level transfer key |
| `allocation_id` | Strategy slice key |
| `parent_order_id` | Parent order key |
| `child_order_id` | Child order key |
| `fill_id` | Fill identifier |
| `site_order_key` | Venue/site order key |
| `book` | Desk or book |
| `instrument_alias` | Instrument or spread alias |
| `side` | Buy or sell |
| `fill_qty` | Filled quantity |
| `fill_price` | Fill price |
| `fill_mark` | Internal mark at fill time |
| `arrival_mark` | Transfer arrival mark, optional |
| `decision_mark` | Decision mark, optional |
| `placement_mark` | Placement mark, optional |
| `algo_name` | Algo label |
| `broker_id` | Broker or venue routing ID |
| `is_child` | Child-order flag |
| `aggressor_indicator` | Liquidity-taking flag |
| `last_liquidity_indicator` | Venue liquidity label |
| `raw_payload` | Raw fill payload |

#### Sample rows

| fill_time | transfer_id | allocation_id | parent_order_id | child_order_id | fill_id | fill_qty | fill_price | fill_mark | placement_mark | algo_name |
| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| 2026-07-31 09:01:10.000 | T1 | A3_1 | P1 | C1 | F1 | 2 | 74.18 | 74.19 | 74.19 | vulcan |
| 2026-07-31 09:02:40.000 | T1 | A3_1 | P1 | C1 | F2 | 2 | 74.18 | 74.20 | 74.19 | vulcan |
| 2026-07-31 09:05:30.000 | T1 | A3_1 | P1 | C2 | F3 | 3 | 74.17 | 74.19 | 74.18 | vulcan |
| 2026-07-31 09:08:25.000 | T1 | A3_1 | P1 | C3 | F4 | 3 | 74.16 | 74.17 | 74.16 | vulcan |

### 5. `transfer_position_snapshots`

This table stores the remaining open position after major lifecycle boundaries.

It answers:

- what quantity was still open after each step?
- how much risk remained due to PCA residual, crossing buffer hold, or incomplete execution?
- what was the mark-to-market on the remaining quantity?

#### Suggested columns

| Column | Meaning |
| --- | --- |
| `snapshot_time` | Snapshot timestamp |
| `transfer_id` | Top-level transfer key |
| `allocation_id` | Strategy slice key when needed |
| `snapshot_stage` | `post_transfer`, `post_pca`, `post_crossing_buffer`, `post_parent_1`, etc. |
| `book` | Desk or book |
| `instrument_alias` | Instrument or spread alias |
| `open_qty` | Remaining quantity |
| `avg_cost` | Cost basis |
| `mark_price` | Mark at snapshot time |
| `realized_pnl` | Realized PnL so far |
| `unrealized_pnl` | Unrealized PnL so far |
| `total_pnl` | Total cumulative PnL |

#### Sample rows

| snapshot_time | transfer_id | allocation_id | snapshot_stage | open_qty | mark_price | realized_pnl | unrealized_pnl | total_pnl |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2026-07-31 09:00:00.000 | T1 |  | post_transfer | 80 | 74.20 | 0.00 | 0.00 | 0.00 |
| 2026-07-31 09:00:02.000 | T1 | A1 | post_pca | 80 | 74.21 | 0.00 | 0.80 | 0.80 |
| 2026-07-31 09:00:03.000 | T1 | A2 | post_crossing_buffer | 80 | 74.22 | 0.00 | 1.60 | 1.60 |
| 2026-07-31 09:15:00.000 | T1 | A3_1 | post_parent_1 | 70 | 74.14 | -0.50 | -4.20 | -4.70 |

## Worked Example

We use the following example.

1. 80 lots arrive from the desk.
2. The book position goes up to 100.
3. algr is triggered for hedge calculations.
4. PCA decides to hedge 60.
5. 40 is structural residual.
6. Crossing buffer says to hold another 20 of the 60.
7. Final 40 goes to execution.
8. The 40 is split into 4 sequential parent orders of 10 each.
9. Each parent order can create multiple child orders.
10. Each child order can create multiple fills.

### Allocation view of the example

| transfer_id | allocation_id | parent_allocation_id | strategy_category | strategy_name | qty | meaning |
| --- | --- | --- | --- | --- | ---: | --- |
| T1 | A1 |  | hedge_calculation | pca_structural_residual | 40 | PCA chooses not to hedge this quantity |
| T1 | A2 |  | execution_gate | crossing_buffer_hold | 20 | Crossing buffer withholds this quantity |
| T1 | A3 |  | execution_schedule | time_slicer_15m | 40 | Quantity released to execution |
| T1 | A3_1 | A3 | order_placement | tranche_1 | 10 | First parent execution wave |
| T1 | A3_2 | A3 | order_placement | tranche_2 | 10 | Second parent execution wave |
| T1 | A3_3 | A3 | order_placement | tranche_3 | 10 | Third parent execution wave |
| T1 | A3_4 | A3 | order_placement | tranche_4 | 10 | Fourth parent execution wave |

### Order hierarchy for tranche 1

| transfer_id | allocation_id | parent_order_id | child_order_id | fill_id | qty | meaning |
| --- | --- | --- | --- | --- | ---: | --- |
| T1 | A3_1 | P1 |  |  | 10 | Parent order for tranche 1 |
| T1 | A3_1 | P1 | C1 |  | 4 | Passive child order |
| T1 | A3_1 | P1 | C2 |  | 3 | Repriced child order |
| T1 | A3_1 | P1 | C3 |  | 3 | Aggressive cleanup child order |
| T1 | A3_1 | P1 | C1 | F1 | 2 | Fill on child C1 |
| T1 | A3_1 | P1 | C1 | F2 | 2 | Fill on child C1 |
| T1 | A3_1 | P1 | C2 | F3 | 3 | Fill on child C2 |
| T1 | A3_1 | P1 | C3 | F4 | 3 | Fill on child C3 |

## Event Creation Rules

Each identifier should be born at one place and then propagated.

| Identifier | First created at | First event that records it | Must flow into |
| --- | --- | --- | --- |
| `transfer_id` | Transfer object creation in algr | `transfer_received` | all 5 tables |
| `allocation_id` | Strategy decision point in algr | `*_allocation_created` | allocations, lifecycle events, orders, fills, optional snapshots |
| `parent_order_id` | Parent order creation in runner/TT | `parent_order_created` | orders, fills |
| `child_order_id` | Child order creation in runner/TT | `child_order_created` | orders, fills |
| `fill_id` | TT fill event | `fill_received` | fills |

## Mapping To Current Repository Constraints

### What already exists

- Transfer payload UUID exists, but is generated too late and too implicitly.
- TT fill ID exists and is published.
- TT order ID exists and is published.
- TT parent order relationship exists internally.

### What is missing

- Stable top-level `transfer_id` stored directly on transfer objects.
- `allocation_id` strategy decomposition layer.
- Published and persisted `parent_order_id` on the shared fill stream.
- Generic durable transfer-to-order-to-fill lineage persistence for the spread flow.

## Recommended Implementation Order

1. Make `transfer_id` explicit on the transfer object.
2. Keep `tradr_trade_id` separately for reconciliation.
3. Add `allocation_id` creation at PCA, crossing buffer, and execution scheduling steps.
4. Publish and persist `parent_order_id` from algo-runner.
5. Persist order and fill events with the full upstream ID chain.
6. Add residual position snapshots at stage boundaries.

## Summary

This design makes the transfer the top-level accounting object and builds a durable lineage through strategy decisions, execution tranches, child orders, fills, and residual inventory.

The critical design principle is that every downstream event carries the upstream IDs directly. That gives us a clean transfer-level PnL attribution model that can explain not only where money was made or lost, but which strategy decision caused it.
