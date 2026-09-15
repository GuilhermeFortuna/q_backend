from typing import Any, List

import pandas as pd

from q_backend.backtesting.signal_columns import write_signal_columns
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy
from q_backend.backtesting.strategy_registry import StrategyParamSpec, register_strategy
from q_backend.backtesting.technical_indicators import compute_macd


class MACDStrategy(TradingStrategy):
    def __init__(
        self,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
        symbol: str = "BTCUSDT",
        **kwargs,
    ):
        super().__init__(
            fast_period=fast_period,
            slow_period=slow_period,
            signal_period=signal_period,
            symbol=symbol,
            **kwargs,
        )
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period
        self.symbol = symbol

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        if "close" not in df.columns:
            raise ValueError("Data must contain a 'close' column for MACD strategy.")

        macd_line, signal_line, histogram = compute_macd(
            df["close"], self.fast_period, self.slow_period, self.signal_period
        )
        df["macd"] = macd_line
        df["macd_signal"] = signal_line
        df["macd_histogram"] = histogram

        df["prev_macd"] = df["macd"].shift(1)
        df["prev_macd_signal"] = df["macd_signal"].shift(1)

        df["buy_signal"] = (df["prev_macd"] <= df["prev_macd_signal"]) & (df["macd"] > df["macd_signal"])
        df["sell_signal"] = (df["prev_macd"] >= df["prev_macd_signal"]) & (df["macd"] < df["macd_signal"])
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
                key="macd",
                label=f"MACD ({self.fast_period}/{self.slow_period})",
                pane="oscillator",
                color="#c9a227",
            ),
            ChartIndicatorSpec(
                key="macd_signal",
                label=f"Signal ({self.signal_period})",
                pane="oscillator",
                color="#6eb5ff",
            ),
            ChartIndicatorSpec(
                key="macd_histogram",
                label="Histogram",
                pane="oscillator",
                color="#888888",
            ),
        ]


def _build_macd(params: dict[str, Any], symbol: str) -> MACDStrategy:
    return MACDStrategy(
        fast_period=int(params["fast_period"]),
        slow_period=int(params["slow_period"]),
        signal_period=int(params["signal_period"]),
        symbol=symbol,
    )


register_strategy(
    name="MACD",
    label="MACD Crossover",
    description="Signal-line crossovers on MACD.",
    params=[
        StrategyParamSpec(
            name="fast_period",
            label="Fast Period",
            type="int",
            default=12,
            min=2,
            max=100,
            step=1,
            search_min=8,
            search_max=16,
            search_step=4,
            hint="Lower = more sensitive to recent moves, more crosses; higher = smoother MACD.",
        ),
        StrategyParamSpec(
            name="slow_period",
            label="Slow Period",
            type="int",
            default=26,
            min=2,
            max=400,
            step=1,
            search_min=20,
            search_max=32,
            search_step=6,
            hint="Higher = longer trend baseline, fewer but more sustained signals.",
        ),
        StrategyParamSpec(
            name="signal_period",
            label="Signal Period",
            type="int",
            default=9,
            min=2,
            max=100,
            step=1,
            search_min=6,
            search_max=12,
            search_step=3,
            hint="Lower = faster entry/exit flips; higher = dampened signal line, delayed reversals.",
        ),
    ],
    build=_build_macd,
    strategy_class=MACDStrategy,
    category="trend",
    thesis=(
        "MACD blends two trend horizons — when the fast component crosses above "
        "the slow, recent upside is outpacing the broader trend, suggesting momentum "
        "is building. Entries fire on the MACD line crossing its signal line and "
        "hold until the cross reverses."
    ),
    strong_in="Gradual trends with momentum building before price extremes.",
    weak_in="Sideways chop — signal-line crosses fire without follow-through.",
)
