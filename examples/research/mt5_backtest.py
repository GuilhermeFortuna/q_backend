#!/usr/bin/env python3
"""Run a backtest on fresh MT5 market data loaded via the Q gateway.

Demonstrates the workflow:
1. Load bars via load_bars() (requires running MT5 terminal and gateway, no DB/worker/Redis)
2. Add indicators (or use built-in strategies)
3. Run synchronous backtest() with fixed sizing and execution semantics.

See docs/research-library.md for documentation.
"""

from __future__ import annotations

import argparse
import os

from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.research import (
    ResearchStrategy,
    TradeOrder,
    backtest,
    indicators,
    load_bars,
)


class MovingAverageBreakout(ResearchStrategy):
    """Simple MA breakout research strategy."""

    def __init__(self, period: int = 20) -> None:
        self.period = period

    def compute_indicators(self, frame):
        frame = frame.copy()
        frame["ma"] = indicators.ma(frame["close"], period=self.period, kind="sma")
        return frame

    def entry_strategy(self, frame) -> TradeOrder | None:
        if len(frame) < 2:
            return None
        prev_close = frame["close"].iloc[-2]
        curr_close = frame["close"].iloc[-1]
        prev_ma = frame["ma"].iloc[-2]
        curr_ma = frame["ma"].iloc[-1]
        if prev_close <= prev_ma and curr_close > curr_ma:
            return TradeOrder.buy()
        if prev_close >= prev_ma and curr_close < curr_ma:
            return TradeOrder.sell()
        return None

    def exit_strategy(self, frame) -> TradeOrder | None:
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load fresh MT5 bars via the Q gateway and run a local backtest.")
    parser.add_argument("--symbol", default="WIN$N", help="MT5 symbol (default: WIN$N, unadjusted)")
    parser.add_argument("--timeframe", default="M5", help="Timeframe (default: M5)")
    parser.add_argument("--start", required=True, help="Inclusive bar-open bound (ISO or YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="Inclusive bar-open bound; default is now")
    parser.add_argument("--strategy", default="custom", choices=["custom", "builtin"], help="Strategy type")
    parser.add_argument("--quantity", type=int, default=1, help="Fixed trade quantity (default: 1)")
    parser.add_argument("--point-value", type=float, default=0.20, help="Value per point/multiplier (default: 0.20)")
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital (default: 10000.0)")
    parser.add_argument(
        "--cost-per-contract",
        type=float,
        default=0.0,
        help="Transaction cost per contract per side in BRL (default: 0.0)",
    )
    parser.add_argument("--gateway-url", default=os.getenv("Q_MT5_GATEWAY_URL"))
    parser.add_argument("--gateway-token", default=os.getenv("Q_MT5_GATEWAY_TOKEN"))
    return parser


def run_mt5_workflow(
    symbol: str,
    timeframe: str,
    start: str,
    end: str | None = None,
    strategy_mode: str = "custom",
    quantity: int = 1,
    point_value: float = 0.20,
    capital: float = 10000.0,
    cost_per_contract: float = 0.0,
    gateway_url: str | None = None,
    gateway_token: str | None = None,
):
    # 1. Load bars from MT5 gateway
    bars = load_bars(
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        gateway_url=gateway_url,
        gateway_token=gateway_token,
    )
    print(f"Loaded {len(bars)} completed bars for {symbol} ({timeframe})")

    # 2. Run backtest
    costs = TransactionCostConfig(cost_per_contract=cost_per_contract)
    if strategy_mode == "builtin":
        result = backtest(
            bars,
            strategy="MACrossover",
            symbol=symbol,
            strategy_params={"short_period": 9, "long_period": 21},
            quantity=quantity,
            point_value=point_value,
            initial_capital=capital,
            costs=costs,
        )
    else:
        result = backtest(
            bars,
            strategy=MovingAverageBreakout(period=20),
            symbol=symbol,
            quantity=quantity,
            point_value=point_value,
            initial_capital=capital,
            costs=costs,
        )
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_mt5_workflow(
        symbol=args.symbol,
        timeframe=args.timeframe,
        start=args.start,
        end=args.end,
        strategy_mode=args.strategy,
        quantity=args.quantity,
        point_value=args.point_value,
        capital=args.capital,
        cost_per_contract=args.cost_per_contract,
        gateway_url=args.gateway_url,
        gateway_token=args.gateway_token,
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
