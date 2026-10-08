#!/usr/bin/env python3
"""Offline RSI mean-reversion strategy backtest reading a local Parquet file.

Runs completely offline without API, database, worker, Redis, GPU, or MT5 terminal.
See docs/research-library.md for documentation.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.research import (
    ResearchStrategy,
    TradeOrder,
    backtest,
    indicators,
)


class RSIReversion(ResearchStrategy):
    """Simple RSI mean-reversion strategy demonstrating ResearchStrategy hooks."""

    def __init__(self, period: int = 14, lower: float = 30.0, upper: float = 50.0) -> None:
        self.period = period
        self.lower = lower
        self.upper = upper

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame = frame.copy()
        frame["rsi"] = indicators.rsi(frame["close"], self.period)
        return frame

    def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
        if len(frame) < 2:
            return None
        previous, current = frame["rsi"].iloc[-2:]
        if pd.isna(previous) or pd.isna(current):
            return None
        # Crossing below lower threshold
        if previous >= self.lower and current < self.lower:
            return TradeOrder.buy()
        return None

    def exit_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
        if len(frame) < 1:
            return None
        current = frame["rsi"].iloc[-1]
        if pd.isna(current):
            return None
        # Crossing above exit threshold
        if current > self.upper:
            return TradeOrder.close()
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an offline RSI reversion backtest on a Parquet file.")
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to input Parquet file containing historical OHLC bars",
    )
    parser.add_argument("--symbol", default="WIN$N", help="Traded instrument symbol (default: WIN$N)")
    parser.add_argument("--period", type=int, default=14, help="RSI lookback period (default: 14)")
    parser.add_argument("--quantity", type=int, default=1, help="Fixed trade quantity (default: 1)")
    parser.add_argument("--point-value", type=float, default=0.20, help="Value per point/multiplier (default: 0.20)")
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital (default: 10000.0)")
    parser.add_argument(
        "--cost-per-contract",
        type=float,
        default=0.0,
        help="Transaction cost per contract per side in BRL (default: 0.0)",
    )
    return parser


def run_rsi_backtest(
    input_path: Path,
    *,
    symbol: str = "WIN$N",
    period: int = 14,
    quantity: int = 1,
    point_value: float = 0.20,
    capital: float = 10000.0,
    cost_per_contract: float = 0.0,
):
    frame = pd.read_parquet(input_path)
    strategy = RSIReversion(period=period)
    costs = TransactionCostConfig(cost_per_contract=cost_per_contract)
    result = backtest(
        frame,
        strategy=strategy,
        symbol=symbol,
        quantity=quantity,
        point_value=point_value,
        initial_capital=capital,
        costs=costs,
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.input.exists():
        raise FileNotFoundError(f"Input file not found: {args.input}")

    result = run_rsi_backtest(
        args.input,
        symbol=args.symbol,
        period=args.period,
        quantity=args.quantity,
        point_value=args.point_value,
        capital=args.capital,
        cost_per_contract=args.cost_per_contract,
    )

    print(f"Total trades: {result.metrics['total_trades']}")
    print(f"Total net PnL: {result.metrics['total_pnl']:.2f}")
    print(f"Total commission: {result.metrics['total_commission']:.2f}")
    if args.cost_per_contract == 0.0:
        print("No transaction costs were applied.")
    print(f"Win rate: {result.metrics['win_rate']:.2%}")
    if len(result.trades) > 0:
        print("\nTrades:")
        print(result.trades.head())
    print("\nFinal Realized Equity:")
    print(result.equity.tail(1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
