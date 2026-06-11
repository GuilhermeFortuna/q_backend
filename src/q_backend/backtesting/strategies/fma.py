from typing import Any, List

import pandas as pd

from q_backend.backtesting.moving_averages import (
    MA_TYPE_LABELS,
    compute_ma,
    normalize_ma_type,
)
from q_backend.backtesting.models import Signal, SignalAction, Trade
from q_backend.backtesting.strategies.lai_lau_common import (
    MA_TYPE_CHOICES,
    add_bar_index,
    build_timestamp_to_bar,
    compute_ma_band_signals,
    fixed_holding_period_exits,
)
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy, resolve_symbol
from q_backend.backtesting.strategy_registry import StrategyParamSpec, register_strategy


class FMAStrategy(TradingStrategy):
    """
    Fixed Moving Average (FMA) rule from Lai & Lau (2006).

    Same price-vs-MA cross triggers as VMA, but each entry is held for exactly
    ``holding_period`` bars regardless of intervening signals.

    Deviations from the paper: shorts instead of a risk-free cash leg;
    ``band_pct`` as a percent of the MA; holding period counted in bars
    (trading bars in the engine, not calendar days).
    """

    def __init__(
        self,
        period: int = 60,
        band_pct: float = 0.0,
        ma_type: str = "sma",
        holding_period: int = 10,
        symbol: str = "BTCUSDT",
        **kwargs,
    ):
        self.period = period
        self.band_pct = band_pct
        self.ma_type = normalize_ma_type(ma_type)
        self.holding_period = holding_period
        self.symbol = symbol
        self._timestamp_to_bar: pd.Series | None = None
        super().__init__(
            period=period,
            band_pct=band_pct,
            ma_type=self.ma_type,
            holding_period=holding_period,
            symbol=symbol,
            **kwargs,
        )

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        if "close" not in df.columns:
            raise ValueError("Data must contain a 'close' column for the FMA strategy.")

        df = add_bar_index(df)
        ma = compute_ma(df["close"], self.period, self.ma_type)
        df["ma"] = ma
        df = compute_ma_band_signals(df, ma, self.band_pct)
        self._timestamp_to_bar = build_timestamp_to_bar(df)
        return df

    def _ma_label(self) -> str:
        label = MA_TYPE_LABELS.get(self.ma_type, self.ma_type.upper())
        return f"{label} ({self.period})"

    def get_chart_indicators(self) -> List[ChartIndicatorSpec]:
        return [
            ChartIndicatorSpec(
                key="ma",
                label=self._ma_label(),
                pane="price",
                color="#c9a227",
            ),
            ChartIndicatorSpec(
                key="ma_band_upper",
                label=f"Upper Band ({self.band_pct}%)",
                pane="price",
                color="#6eb5ff",
            ),
            ChartIndicatorSpec(
                key="ma_band_lower",
                label=f"Lower Band ({self.band_pct}%)",
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

    def check_exit_conditions(
        self, current_data: pd.Series, open_trades: List[Trade]
    ) -> List[Signal]:
        symbol = resolve_symbol(current_data, self.symbol)
        if self._timestamp_to_bar is None:
            return []
        return fixed_holding_period_exits(
            current_data,
            open_trades,
            symbol,
            self.holding_period,
            self._timestamp_to_bar,
        )


def _build_fma(params: dict[str, Any], symbol: str) -> FMAStrategy:
    return FMAStrategy(
        period=int(params["period"]),
        band_pct=float(params["band_pct"]),
        ma_type=normalize_ma_type(params["ma_type"]),
        holding_period=int(params["holding_period"]),
        symbol=symbol,
    )


register_strategy(
    name="FMA",
    label="Fixed MA (Lai–Lau)",
    description=(
        "Price vs moving-average crossover with a fixed bar-count holding "
        "period (Lai & Lau 2006 FMA rule)."
    ),
    params=[
        StrategyParamSpec(
            name="period",
            label="Period",
            type="int",
            default=60,
            min=2,
            max=400,
            step=1,
        ),
        StrategyParamSpec(
            name="band_pct",
            label="Band (%)",
            type="float",
            default=0.0,
            min=0.0,
            max=5.0,
            step=0.01,
        ),
        StrategyParamSpec(
            name="ma_type",
            label="MA Type",
            type="categorical",
            default="sma",
            choices=MA_TYPE_CHOICES,
        ),
        StrategyParamSpec(
            name="holding_period",
            label="Holding Period (bars)",
            type="int",
            default=10,
            min=1,
            max=60,
            step=1,
        ),
    ],
    build=_build_fma,
    strategy_class=FMAStrategy,
)
