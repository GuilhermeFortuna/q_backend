from typing import Any, List

import pandas as pd

from q_backend.backtesting.models import Signal, SignalAction, Trade
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy, resolve_symbol
from q_backend.backtesting.strategy_registry import StrategyParamSpec, register_strategy
from q_backend.backtesting.technical_indicators import compute_bollinger_bands


class BollingerReversionStrategy(TradingStrategy):
    def __init__(
        self,
        period: int = 20,
        num_std: float = 2.0,
        symbol: str = "BTCUSDT",
        **kwargs,
    ):
        super().__init__(period=period, num_std=num_std, symbol=symbol, **kwargs)
        self.period = period
        self.num_std = num_std
        self.symbol = symbol

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        if "close" not in df.columns:
            raise ValueError(
                "Data must contain a 'close' column for Bollinger strategy."
            )

        upper, middle, lower = compute_bollinger_bands(
            df["close"], self.period, self.num_std
        )
        df["bb_upper"] = upper
        df["bb_middle"] = middle
        df["bb_lower"] = lower

        df["prev_close"] = df["close"].shift(1)
        df["prev_bb_lower"] = df["bb_lower"].shift(1)
        df["prev_bb_upper"] = df["bb_upper"].shift(1)
        df["prev_bb_middle"] = df["bb_middle"].shift(1)

        df["buy_signal"] = (df["prev_close"] >= df["prev_bb_lower"]) & (
            df["close"] < df["bb_lower"]
        )
        df["sell_signal"] = (df["prev_close"] <= df["prev_bb_upper"]) & (
            df["close"] > df["bb_upper"]
        )

        df["exit_long_signal"] = (df["prev_close"] <= df["prev_bb_middle"]) & (
            df["close"] > df["bb_middle"]
        )
        df["exit_short_signal"] = (df["prev_close"] >= df["prev_bb_middle"]) & (
            df["close"] < df["bb_middle"]
        )
        return df

    def get_chart_indicators(self) -> List[ChartIndicatorSpec]:
        return [
            ChartIndicatorSpec(
                key="bb_upper",
                label=f"BB Upper ({self.period}, {self.num_std}σ)",
                pane="price",
                color="#c9a227",
            ),
            ChartIndicatorSpec(
                key="bb_middle",
                label=f"BB Middle ({self.period})",
                pane="price",
                color="#6eb5ff",
            ),
            ChartIndicatorSpec(
                key="bb_lower",
                label=f"BB Lower ({self.period}, {self.num_std}σ)",
                pane="price",
                color="#c9a227",
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

    def check_exit_conditions(
        self, current_data: pd.Series, open_trades: List[Trade]
    ) -> List[Signal]:
        symbol = resolve_symbol(current_data, self.symbol)
        if not open_trades:
            return []

        exit_long = current_data.get("exit_long_signal", False)
        exit_short = current_data.get("exit_short_signal", False)
        signals: List[Signal] = []
        for trade in open_trades:
            if trade.symbol != symbol:
                continue
            if trade.action == SignalAction.BUY and exit_long:
                signals.append(Signal(symbol=symbol, action=SignalAction.CLOSE))
            elif trade.action == SignalAction.SELL and exit_short:
                signals.append(Signal(symbol=symbol, action=SignalAction.CLOSE))
        return signals


def _build_bollinger_reversion(
    params: dict[str, Any], symbol: str
) -> BollingerReversionStrategy:
    return BollingerReversionStrategy(
        period=int(params["period"]),
        num_std=float(params["num_std"]),
        symbol=symbol,
    )


register_strategy(
    name="BollingerReversion",
    label="Bollinger Band Reversion",
    description="Enter on band touch; exit on mean reversion to the middle band.",
    params=[
        StrategyParamSpec(
            name="period",
            label="Period",
            type="int",
            default=20,
            min=2,
            max=400,
            step=1,
        ),
        StrategyParamSpec(
            name="num_std",
            label="Std Dev Multiplier",
            type="float",
            default=2.0,
            min=0.5,
            max=5.0,
            step=0.1,
        ),
    ],
    build=_build_bollinger_reversion,
    strategy_class=BollingerReversionStrategy,
)
