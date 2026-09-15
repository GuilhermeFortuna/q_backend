from typing import Any, List

import pandas as pd

from q_backend.backtesting.models import Signal, SignalAction, Trade
from q_backend.backtesting.signal_columns import write_signal_columns
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy, resolve_symbol
from q_backend.backtesting.strategy_registry import StrategyParamSpec, register_strategy
from q_backend.backtesting.technical_indicators import compute_donchian_channels


class DonchianBreakoutStrategy(TradingStrategy):
    def __init__(
        self,
        period: int = 20,
        symbol: str = "BTCUSDT",
        **kwargs,
    ):
        super().__init__(period=period, symbol=symbol, **kwargs)
        self.period = period
        self.symbol = symbol

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        if "close" not in df.columns or "high" not in df.columns or "low" not in df.columns:
            raise ValueError("Data must contain 'close', 'high', and 'low' columns for Donchian strategy.")

        upper, lower = compute_donchian_channels(df["high"], df["low"], self.period)
        df["donchian_upper"] = upper
        df["donchian_lower"] = lower

        df["prev_close"] = df["close"].shift(1)
        df["prev_donchian_upper"] = df["donchian_upper"].shift(1)
        df["prev_donchian_lower"] = df["donchian_lower"].shift(1)

        df["buy_signal"] = (df["prev_close"] <= df["prev_donchian_upper"]) & (df["close"] > df["donchian_upper"])
        df["sell_signal"] = (df["prev_close"] >= df["prev_donchian_lower"]) & (df["close"] < df["donchian_lower"])
        return write_signal_columns(
            df,
            entry_long=df["buy_signal"],
            entry_short=df["sell_signal"],
            exit_long=df["sell_signal"],
            exit_short=df["buy_signal"],
            strategy_name=type(self).__name__,
        )

    def get_chart_indicators(self) -> List[ChartIndicatorSpec]:
        return [
            ChartIndicatorSpec(
                key="donchian_upper",
                label=f"Donchian Upper ({self.period})",
                pane="price",
                color="#c9a227",
            ),
            ChartIndicatorSpec(
                key="donchian_lower",
                label=f"Donchian Lower ({self.period})",
                pane="price",
                color="#6eb5ff",
            ),
        ]

    def check_entry_conditions(self, current_data: pd.Series) -> List[Signal]:
        symbol = resolve_symbol(current_data, self.symbol)
        signals: List[Signal] = []
        if current_data.get("buy_signal", False):
            signals.append(Signal(symbol=symbol, action=SignalAction.BUY))
        elif current_data.get("sell_signal", False):
            signals.append(Signal(symbol=symbol, action=SignalAction.SELL))
        return signals

    def check_exit_conditions(self, current_data: pd.Series, open_trades: List[Trade]) -> List[Signal]:
        symbol = resolve_symbol(current_data, self.symbol)
        if not open_trades:
            return []

        is_sell_trigger = current_data.get("sell_signal", False)
        is_buy_trigger = current_data.get("buy_signal", False)
        signals: List[Signal] = []
        for trade in open_trades:
            if trade.symbol != symbol:
                continue
            if trade.action == SignalAction.BUY and is_sell_trigger:
                signals.append(Signal(symbol=symbol, action=SignalAction.CLOSE))
            elif trade.action == SignalAction.SELL and is_buy_trigger:
                signals.append(Signal(symbol=symbol, action=SignalAction.CLOSE))
        return signals


def _build_donchian_breakout(params: dict[str, Any], symbol: str) -> DonchianBreakoutStrategy:
    return DonchianBreakoutStrategy(period=int(params["period"]), symbol=symbol)


register_strategy(
    name="DonchianBreakout",
    label="Donchian Breakout",
    description="Enter on upper/lower channel breakouts.",
    params=[
        StrategyParamSpec(
            name="period",
            label="Period",
            type="int",
            default=20,
            min=2,
            max=400,
            step=1,
            search_min=10,
            search_max=60,
            search_step=10,
            hint="Shorter channel = more breakouts and false signals; longer = rarer but potentially larger moves.",
        ),
    ],
    build=_build_donchian_breakout,
    strategy_class=DonchianBreakoutStrategy,
    category="breakout",
    thesis=(
        "New highs and lows signal that supply or demand has cleared the recent "
        "range — the classic turtle logic that breakouts precede continuation. "
        "Enters when close breaks the N-period high or low channel."
    ),
    strong_in="Clean breakouts after consolidation with follow-through momentum.",
    weak_in="False breakouts in low-volatility ranges — entries at tops and bottoms that reverse.",
)
