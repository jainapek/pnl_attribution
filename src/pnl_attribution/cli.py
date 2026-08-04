"""CLI entrypoints."""

from __future__ import annotations

import argparse

import pandas as pd

from pnl_attribution import (
    AppConfig,
    ClickHouseClientFactory,
    EODReconciler,
    EventTransformer,
    IntradayReconciler,
    PositionRepository,
    PositionTransformer,
    ReconciliationReporter,
    ReconciliationRequest,
    TenorConverter,
    TradeRepository,
    TransferRepository,
)


class CliApp:
    """Run CLI commands."""

    def __init__(self) -> None:
        """Build dependencies."""

        config = AppConfig.from_env()
        client = ClickHouseClientFactory(config).create()
        tenor_converter = TenorConverter()
        position_transformer = PositionTransformer(tenor_converter)
        event_transformer = EventTransformer(tenor_converter)

        self._config = config
        self._eod_reconciler = EODReconciler(
            PositionRepository(client),
            TradeRepository(client),
            TransferRepository(client),
            position_transformer,
            event_transformer,
        )
        self._intraday_reconciler = IntradayReconciler(
            PositionRepository(client),
            TradeRepository(client),
            TransferRepository(client),
            position_transformer,
            event_transformer,
        )
        self._reporter = ReconciliationReporter()

    def run(self, argv: list[str] | None = None) -> int:
        """Run the CLI."""

        parser = self._build_parser()
        args = parser.parse_args(argv)
        request = ReconciliationRequest(date=args.date, book_ids=tuple(args.book_ids or self._config.default_book_ids))

        if args.command == "eod":
            result = self._eod_reconciler.build(request)
            print(self._build_eod_summary(result).to_string())
            print()
            print(self._reporter.top_residuals(result, limit=args.limit).to_string())
            return 0

        result = self._intraday_reconciler.build(
            request,
            tenor=args.tenor,
            resample_rule=args.resample_rule,
        )
        print(self._build_intraday_summary(result).to_string())
        print()
        print(result.timeseries.head(args.limit).to_string())
        return 0

    def _build_eod_summary(self, result) -> pd.DataFrame:
        """Build the EOD summary."""

        comparison = result.comparison
        summary = pd.Series(
            {
                "first_transfer_ts": result.first_transfer_ts,
                "num_spreads": len(comparison),
                "num_nonzero_residual_spreads": int((comparison["residual"] != 0).sum()),
                "total_abs_actual_eod": comparison["actual_eod"].abs().sum(),
                "total_abs_calc_eod": comparison["calc_eod"].abs().sum(),
                "total_abs_residual": comparison["abs_residual"].sum(),
                "max_abs_residual": comparison["abs_residual"].max(),
                "signed_residual_sum": comparison["residual"].sum(),
            }
        )
        return summary.to_frame("value")

    def _build_intraday_summary(self, result) -> pd.DataFrame:
        """Build the intraday summary."""

        timeseries = result.timeseries
        summary = pd.Series(
            {
                "tenor": result.tenor,
                "first_transfer_ts": result.first_transfer_ts,
                "resample_rule": result.resample_rule,
                "num_points": len(timeseries),
                "total_abs_residual": timeseries["residual"].abs().sum(),
                "max_abs_residual": timeseries["residual"].abs().max(),
                "eod_residual": timeseries["residual"].iloc[-1],
            }
        )
        return summary.to_frame("value")

    def _build_parser(self) -> argparse.ArgumentParser:
        """Build the parser."""

        parser = argparse.ArgumentParser(prog="pnl-attribution")
        subparsers = parser.add_subparsers(dest="command", required=True)

        eod = subparsers.add_parser("eod")
        eod.add_argument("--date", required=True)
        eod.add_argument("--book-ids", nargs="*", type=int)
        eod.add_argument("--limit", type=int, default=20)

        intraday = subparsers.add_parser("intraday")
        intraday.add_argument("--date", required=True)
        intraday.add_argument("--tenor", required=True)
        intraday.add_argument("--book-ids", nargs="*", type=int)
        intraday.add_argument("--resample-rule")
        intraday.add_argument("--limit", type=int, default=20)
        return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI app."""

    return CliApp().run(argv)
