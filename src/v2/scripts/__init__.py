"""Nexus cycle / day PnL attribution helpers."""

from .config import CONFIG, load_config
from .client import get_ch_client
from .cycle_attribution import attribute_cycle, stage_summary, with_cents_per_bbl
from .day_attribution import attribute_all_books_day, attribute_book_day
from .diagnostics import (
    check_intention_lot_sums,
    check_position_received_vs_transfers,
    check_position_received_vs_transfers_range,
    intention_positions_by_cycle,
)
from .stages import (
    attach_executed,
    build_stages_from_row,
    check_risk_roll_continuity,
    cycle_stage_positions,
    stages_to_frame,
    stages_to_wide,
)
from .trades import load_executed_lots_for_cycle, load_trades_for_cycle

__all__ = [
    "CONFIG",
    "load_config",
    "get_ch_client",
    "build_stages_from_row",
    "attach_executed",
    "cycle_stage_positions",
    "stages_to_frame",
    "stages_to_wide",
    "check_risk_roll_continuity",
    "load_trades_for_cycle",
    "load_executed_lots_for_cycle",
    "attribute_cycle",
    "stage_summary",
    "with_cents_per_bbl",
    "attribute_book_day",
    "attribute_all_books_day",
    "check_intention_lot_sums",
    "check_position_received_vs_transfers",
    "check_position_received_vs_transfers_range",
    "intention_positions_by_cycle",
]
