from typing import Any, List

import pandas as pd

from q_backend.backtesting.signal_columns import write_signal_columns
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy
from q_backend.backtesting.strategy_registry import StrategyParamSpec, register_strategy
from q_backend.backtesting.technical_indicators import compute_rsi


class RSIMeanReversionStrategy(TradingStrategy):
    def __init__(
        self,
        period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
        symbol: str = "BTCUSDT",
        **kwargs,
    ):
        super().__init__(
            period=period,
            oversold=oversold,
            overbought=overbought,
            symbol=symbol,
            **kwargs,
        )
        self.period = period
        self.oversold = oversold
        self.overbought = overbought
        self.symbol = symbol

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        if "close" not in df.columns:
            raise ValueError("Data must contain a 'close' column for RSI strategy.")

        df["rsi"] = compute_rsi(df["close"], self.period)
        df["prev_rsi"] = df["rsi"].shift(1)

        df["buy_signal"] = (df["prev_rsi"] <= self.oversold) & (df["rsi"] > self.oversold)
        df["sell_signal"] = (df["prev_rsi"] <= self.overbought) & (df["rsi"] > self.overbought)
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
                key="rsi",
                label=f"RSI ({self.period})",
                pane="oscillator",
                color="#6eb5ff",
            ),
        ]


def _build_rsi_mean_reversion(params: dict[str, Any], symbol: str) -> RSIMeanReversionStrategy:
    return RSIMeanReversionStrategy(
        period=int(params["period"]),
        oversold=float(params["oversold"]),
        overbought=float(params["overbought"]),
        symbol=symbol,
    )


register_strategy(
    name="RSIMeanReversion",
    label="RSI Mean Reversion",
    description="Buy when RSI crosses up out of oversold; exit or short on overbought.",
    params=[
        StrategyParamSpec(
            name="period",
            label="Period",
            type="int",
            default=14,
            min=2,
            max=200,
            step=1,
            search_min=7,
            search_max=21,
            search_step=7,
            hint="Shorter = more extreme RSI readings, more signals; longer = smoother, fewer extremes.",
        ),
        StrategyParamSpec(
            name="oversold",
            label="Oversold",
            type="float",
            default=30.0,
            min=0.0,
            max=50.0,
            step=0.5,
            search_min=15.0,
            search_max=35.0,
            search_step=5.0,
            hint="Lower = fewer but more extreme buy setups; higher = earlier entries, more false positives.",
        ),
        StrategyParamSpec(
            name="overbought",
            label="Overbought",
            type="float",
            default=70.0,
            min=50.0,
            max=100.0,
            step=0.5,
            search_min=65.0,
            search_max=85.0,
            search_step=5.0,
            hint="Lower = earlier short entries; higher = waits for stronger overbought before fading rallies.",
        ),
    ],
    build=_build_rsi_mean_reversion,
    strategy_class=RSIMeanReversionStrategy,
    category="mean_reversion",
    thesis=(
        "Sharp selloffs often overshoot fair value as liquidity dries up and stops "
        "cascade; the bounce when panic exhausts can be captured. Buys when RSI "
        "crosses up through oversold and shorts when RSI crosses up through "
        "overbought, betting on reversal rather than breakout continuation."
    ),
    strong_in="Range-bound markets with sharp but temporary dislocations.",
    weak_in="Strong trends — oversold/overbought signals fire early and keep losing.",
)
