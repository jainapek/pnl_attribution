# Tradr Gross PnL Calculation

## Purpose

This note describes the gross PnL calculation currently implemented in the `algo-research` dashboard, which is the working reference for the Tradr-style PnL view.

The goal is to document the exact formula, explain each component in plain language, and make clear what data is required to reproduce it.

## Top-Level Formula

The dashboard computes:

\[
\text{gross} = \text{overnight} + \text{m2m\_pnl} + \text{trade\_pnl}
\]

and then:

\[
\text{net\_pnl} = \text{gross} + \text{total\_clearing}
\]

This is implemented in:

- `~/apeksha/algo-research/dashboard/pnl_attribution/attribution.py`

The contract multiplier used throughout is:

- `CONTRACT_SIZE = 1000`

from:

- `~/apeksha/algo-research/dashboard/dashboard_constants.py`

So gross PnL is built from three pieces:

1. overnight PnL
2. intraday mark-to-market PnL
3. trade cashflow PnL

## 1. Overnight PnL

The dashboard defines overnight PnL as:

\[
\text{overnight PnL} = (\text{BOD curve} - \text{previous EOD curve}) \times \text{previous EOD position} \times 1000
\]

This is implemented in:

- `~/apeksha/algo-research/dashboard/pnl_attribution/overnight.py`

### Business Meaning

This answers the question:

"If I carried yesterday's closing position into today, how much money did I make or lose purely because the market moved from yesterday's close to today's open?"

### How It Is Calculated

The code does the following:

1. Combines price timestamps and position timestamps so the last valid state of the day is aligned.
2. Takes the last combined timestamp of each day as the effective EOD state.
3. Takes the first curve of each day as the BOD curve.
4. Shifts yesterday's EOD curve and yesterday's EOD position forward to today's date.
5. Computes the curve move from previous EOD to current BOD.
6. Multiplies that curve move by the previous EOD position and by contract size.

### Related Baseline Quantity

The overnight function also computes the BOD book value:

\[
\text{BOD book value} = \text{previous EOD position} \times \text{BOD curve} \times 1000
\]

This is not itself a PnL term. It is the baseline used for intraday mark-to-market.

## 2. Intraday Mark-To-Market PnL

The dashboard defines live intraday mark-to-market PnL as:

\[
\text{live book value}(t) = \sum_{\text{tenors}} \text{position}(t) \times \text{curve}(t) \times 1000
\]

and then:

\[
\text{m2m\_pnl}(t) = \text{live book value}(t) - \text{BOD book value}
\]

This is implemented in:

- `~/apeksha/algo-research/dashboard/pnl_attribution/live.py`

### Business Meaning

This answers the question:

"How much is my current live inventory worth now, relative to what that same inventory baseline was worth at the beginning of the day?"

### How It Is Calculated

The code does the following:

1. Unions price timestamps and position timestamps so PnL can change whenever either price or position changes.
2. Forward-fills prices and positions onto the combined timestamp grid.
3. Multiplies position by curve price tenor by tenor.
4. Multiplies by contract size.
5. Sums across all tenors to get total live book value.
6. Subtracts the BOD book value for that day.

So this is pure intraday inventory valuation change.

## 3. Trade PnL

The dashboard computes a trade cashflow term from both Nexus trades and Nexus transfers.

This is implemented in:

- `~/apeksha/algo-research/dashboard/pnl_attribution/trade.py`

The formulas are:

\[
\text{trade trade pnl} = - (\text{signed trade qty} \times \text{trade price} \times 1000)
\]

\[
\text{transfer trade pnl} = - (\text{signed transfer qty} \times \text{transfer price} \times 1000)
\]

and then:

\[
\text{trade\_pnl} = \text{trade trade pnl} + \text{transfer trade pnl}
\]

### Important Note

This is not realized PnL in the usual accounting sense.

It is a signed cashflow term.

It answers:

"How much cash did we spend or receive from today's trades and transfers?"

## Sign Conventions

The sign conventions come from:

- `~/apeksha/algo-research/dashboard/pnl_attribution/utils.py`

### Trades

Trades are signed as:

- Buy -> positive quantity
- Sell -> negative quantity

### Transfers

Transfers are signed as:

- Sell -> positive quantity
- Buy -> negative quantity

### Why The Leading Minus Sign Exists

Because the formula is:

\[
-1 \times \text{signed qty} \times \text{price} \times 1000
\]

it behaves like a cashflow term:

- buys create negative cashflow
- sells create positive cashflow

That is exactly what you would expect.

## Full Expanded Gross Formula

Putting the three components together:

\[
\text{gross}(t) = \text{overnight} + \text{m2m\_pnl}(t) + \text{trade\_pnl}(t)
\]

Expanded:

\[
\text{gross}(t)
=
(\text{BOD curve} - \text{prev EOD curve}) \times \text{prev EOD pos} \times 1000
\]

\[
+
\left(
\sum \text{current pos}(t) \times \text{current curve}(t) \times 1000
- \text{BOD book value}
\right)
\]

\[
+
\left(
- \sum \text{signed trades} \times \text{trade price} \times 1000
- \sum \text{signed transfers} \times \text{transfer price} \times 1000
\right)
\]

## Intuition

In plain English, the dashboard gross PnL says:

1. What did yesterday's position make or lose overnight?
2. What is today's live inventory worth relative to the start of the day?
3. What cash did we spend or receive from today's trades and transfers?

Add those three together and you get gross PnL.

## Intraday Aggregation Behavior

The dashboard builds an intraday cumulative series, not just a single daily number.

Some important details:

1. Trade and transfer cashflows are grouped by trading day and cumulatively summed within the day.
2. Weekend timestamps are snapped forward to Monday for grouping purposes.
3. Live mark-to-market, trade cashflow, and clearing are resampled to a common frequency.
4. Values are forward-filled within each day so the displayed time series is cumulative intraday PnL.

## Per-Tenor Attribution

The dashboard also creates a tenor-level decomposition.

### Overnight and M2M

For overnight and live mark-to-market, it uses actual tenor-level positions and tenor-level curves.

### Trade PnL By Tenor

For trade cashflow, it expands a spread across its monthly tenors and allocates the total trade cashflow evenly across those months.

So, for example, if a spread spans multiple calendar months, the trade cashflow is spread evenly across those months in the tenor report.

## Trade Breakdown Helper

There is also a helper in:

- `~/apeksha/algo-research/dashboard/tabs/pnl/utils.py`

which further decomposes trades and transfers into:

- placement PnL
- market risk PnL

using mid-price.

### Placement PnL

\[
\text{placement PnL} = -1000 \times (\text{price} - \text{mid}) \times \text{signed qty}
\]

### Market Risk PnL

\[
\text{market risk PnL} = -1000 \times \text{mid} \times \text{signed qty}
\]

This is not the main gross formula, but it is useful because it already starts separating:

- how much cashflow comes from trading away from mid
- how much comes from the market level itself

## Inventory Identity Behind The PnL

A useful way to think about the position side of the system is:

\[
\text{EOD position} = \text{BOD position} + \text{signed transfers} + \text{signed hedge fills}
\]

or equivalently, if hedge fills are expressed as unsigned quantity in the offsetting direction:

\[
\text{EOD position} = \text{BOD position} + \text{transfers} - \text{hedge fills}
\]

This is the core inventory identity that should reconcile before any deeper PnL attribution is attempted.

## What You Need To Reproduce Gross PnL

To replicate the dashboard gross PnL exactly, you need:

1. Previous EOD positions
2. Previous EOD curves
3. Current BOD curves
4. Intraday live positions
5. Intraday live curves
6. Signed transfer quantities and transfer prices
7. Signed trade or fill quantities and execution prices
8. Contract size = 1000

## Why This Matters For Transfer-Lifecycle Attribution

If the transfer-lifecycle attribution framework is meant to tie back to official gross PnL, then it must be able to reconstruct all three gross components:

1. overnight carry
2. intraday inventory mark-to-market
3. signed trade and transfer cashflows

Only once that gross identity ties out should the PnL then be decomposed into lifecycle strategy components such as:

- residual risk PnL
- crossing-buffer hold PnL
- execution timing PnL
- order placement PnL
- fill edge PnL
- remaining inventory PnL

## Summary

The Tradr-style dashboard gross PnL is not a single realized formula. It is the sum of:

- overnight PnL on the previous day's closing inventory
- intraday mark-to-market on current inventory relative to BOD
- signed cashflow from trades and transfers

In compact form:

\[
\text{gross} = \text{overnight} + \text{m2m\_pnl} + \text{trade\_pnl}
\]

That is the reference formula any transfer-level PnL attribution model must tie back to.
