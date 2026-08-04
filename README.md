## PnL Attribution

Class-based tools for Nexus position reconciliation.

## Layout

- `src/pnl_attribution/clients`: ClickHouse client factory
- `src/pnl_attribution/data`: raw repositories
- `src/pnl_attribution/transforms`: tenor, event, and position transforms
- `src/pnl_attribution/reconciliation`: EOD and intraday reconcilers
- `src/pnl_attribution/reporting`: notebook and CLI summaries

## CLI

```bash
pnl-attribution eod --date 2026-07-28
pnl-attribution intraday --date 2026-07-28 --tenor 2027-02-01/2027-03-01 --resample-rule 100ms
```

## Notebook

`src/understanding_positions.ipynb` now imports the package instead of carrying the core logic inline.
