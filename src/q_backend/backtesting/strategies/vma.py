from typing import Any, List

import pandas as pd

from q_backend.backtesting.moving_averages import (
    MA_TYPE_LABELS,
    compute_ma,
    normalize_ma_type,
)
from q_backend.backtesting.models import Signal, SignalAction, Trade
from q_backend.backtesting.signal_columns import write_signal_columns
from q_backend.backtesting.strategies.lai_lau_common import (
    MA_TYPE_CHOICES,
    compute_ma_band_signals,
)
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy, resolve_symbol
from q_backend.backtesting.strategy_registry import StrategyParamSpec, register_strategy


class VMAStrategy(TradingStrategy):
    """
    Variable Moving Average (VMA) rule from Lai & Lau (2006).

    Compares the close to a ``period``-day moving average with optional bands.
    Long while close is above the upper band; short while below the lower band;
    positions flip on opposite crossovers (variable holding).

    Deviations from the paper: sell signals open shorts rather than parking
    proceeds in a risk-free asset; ``band_pct`` is expressed as a percent of
    the MA (multiplicative band).
    """

    def __init__(
        self,
        period: int = 20,
        band_pct: float = 0.0,
        ma_type: str = "sma",
        symbol: str = "BTCUSDT",
        **kwargs,
    ):
        self.period = period
        self.band_pct = band_pct
        self.ma_type = normalize_ma_type(ma_type)
        self.symbol = symbol
        super().__init__(
            period=period,
            band_pct=band_pct,
            ma_type=self.ma_type,
            symbol=symbol,
            **kwargs,
        )

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        if "close" not in df.columns:
            raise ValueError("Data must contain a 'close' column for the VMA strategy.")

        ma = compute_ma(df["close"], self.period, self.ma_type)
        df["ma"] = ma
        df = compute_ma_band_signals(df, ma, self.band_pct)
        return write_signal_columns(
            df,
            entry_long=df["buy_signal"],
            entry_short=df["sell_signal"],
            exit_long=df["sell_signal"],
            exit_short=df["buy_signal"],
            strategy_name=type(self).__name__,
        )

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


def _build_vma(params: dict[str, Any], symbol: str) -> VMAStrategy:
    return VMAStrategy(
        period=int(params["period"]),
        band_pct=float(params["band_pct"]),
        ma_type=normalize_ma_type(params["ma_type"]),
        symbol=symbol,
    )


register_strategy(
    name="VMA",
    label="Variable MA (Lai–Lau)",
    description=(
        "Price vs moving-average crossover with optional bands; "
        "hold until the opposite trigger (Lai & Lau 2006 VMA rule)."
    ),
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
            search_max=50,
            search_step=10,
            hint="Longer = smoother MA and fewer flips; shorter = more responsive, more whipsaws.",
        ),
        StrategyParamSpec(
            name="band_pct",
            label="Band (%)",
            type="float",
            default=0.0,
            min=0.0,
            max=5.0,
            step=0.01,
            search_min=0.0,
            search_max=1.0,
            search_step=0.25,
            hint="Wider band = fewer entries, filters small oscillations around the MA.",
        ),
        StrategyParamSpec(
            name="ma_type",
            label="MA Type",
            type="categorical",
            default="sma",
            choices=MA_TYPE_CHOICES,
            hint="Faster types flip sooner; slower types hold longer through noise.",
        ),
    ],
    build=_build_vma,
    strategy_class=VMAStrategy,
    category="trend",
    thesis=(
        "Lai & Lau (2006) VMA rule stays with the trend until the opposite trigger — "
        "price must cross back through the MA band to exit. Captures sustained moves "
        "without a fixed time stop, flipping only when price convincingly reverses."
    ),
    strong_in="Extended trends where price stays on one side of the MA.",
    weak_in="Oscillating markets — repeated band crosses generate whipsaw entries and exits.",
)
