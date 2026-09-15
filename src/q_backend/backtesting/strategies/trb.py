from typing import Any, List

import pandas as pd

from q_backend.backtesting.models import Signal, SignalAction, Trade
from q_backend.backtesting.signal_columns import write_signal_columns
from q_backend.backtesting.strategies.lai_lau_common import (
    add_bar_index,
    build_timestamp_to_bar,
    compute_trb_channel_signals,
    fixed_holding_period_exits,
)
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy, resolve_symbol
from q_backend.backtesting.strategy_registry import StrategyParamSpec, register_strategy


class TRBStrategy(TradingStrategy):
    """
    Trading Range Breakout (TRB) rule from Lai & Lau (2006).

    Channels are built from the previous ``period`` closes (excluding the
    current bar). Breakouts beyond optional bands trigger entries held for a
    fixed number of bars.

    Deviations from the paper: close-based channels (not high/low like
    Donchian); shorts instead of a cash leg; ``band_pct`` as a percent of the
    channel; holding period counted in bars.
    """

    def __init__(
        self,
        period: int = 60,
        band_pct: float = 0.0,
        holding_period: int = 10,
        symbol: str = "BTCUSDT",
        **kwargs,
    ):
        self.period = period
        self.band_pct = band_pct
        self.holding_period = holding_period
        self.symbol = symbol
        self._timestamp_to_bar: pd.Series | None = None
        super().__init__(
            period=period,
            band_pct=band_pct,
            holding_period=holding_period,
            symbol=symbol,
            **kwargs,
        )

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        if "close" not in df.columns:
            raise ValueError("Data must contain a 'close' column for the TRB strategy.")

        df = add_bar_index(df)
        df = compute_trb_channel_signals(df, self.period, self.band_pct)
        self._timestamp_to_bar = build_timestamp_to_bar(df)
        return write_signal_columns(
            df,
            entry_long=df["buy_signal"],
            entry_short=df["sell_signal"],
            exit_long=False,
            exit_short=False,
            strategy_name=type(self).__name__,
        )

    @property
    def holding_period_bars(self) -> int | None:
        return self.holding_period

    def get_chart_indicators(self) -> List[ChartIndicatorSpec]:
        return [
            ChartIndicatorSpec(
                key="channel_high",
                label=f"Channel High ({self.period})",
                pane="price",
                color="#c9a227",
            ),
            ChartIndicatorSpec(
                key="channel_low",
                label=f"Channel Low ({self.period})",
                pane="price",
                color="#6eb5ff",
            ),
            ChartIndicatorSpec(
                key="trb_upper",
                label=f"Upper Band ({self.band_pct}%)",
                pane="price",
                color="#c9a227",
            ),
            ChartIndicatorSpec(
                key="trb_lower",
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
        if self._timestamp_to_bar is None:
            return []
        return fixed_holding_period_exits(
            current_data,
            open_trades,
            symbol,
            self.holding_period,
            self._timestamp_to_bar,
        )


def _build_trb(params: dict[str, Any], symbol: str) -> TRBStrategy:
    return TRBStrategy(
        period=int(params["period"]),
        band_pct=float(params["band_pct"]),
        holding_period=int(params["holding_period"]),
        symbol=symbol,
    )


register_strategy(
    name="TRB",
    label="Trading Range Breakout (Lai–Lau)",
    description=(
        "Close-based trading-range breakout with a fixed bar-count holding " "period (Lai & Lau 2006 TRB rule)."
    ),
    params=[
        StrategyParamSpec(
            name="period",
            label="Period",
            type="int",
            default=60,
            min=3,
            max=240,
            step=1,
            search_min=20,
            search_max=120,
            search_step=20,
            hint="Shorter range = more breakouts, noisier; longer = fewer but wider channels.",
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
            hint="Wider band = fewer entries, requires cleaner break; zero = any channel pierce.",
        ),
        StrategyParamSpec(
            name="holding_period",
            label="Holding Period (bars)",
            type="int",
            default=10,
            min=1,
            max=60,
            step=1,
            search_min=5,
            search_max=30,
            search_step=5,
            hint="Longer = more time for breakout to work, but more exposure if it fails.",
        ),
    ],
    build=_build_trb,
    strategy_class=TRBStrategy,
    category="breakout",
    thesis=(
        "Lai & Lau (2006) TRB rule bets that closing outside the recent close-based "
        "trading range signals a breakout worth riding for a fixed holding period. "
        "Channels built from prior closes with optional bands filter marginal pierces."
    ),
    strong_in="Range compression followed by decisive close breakouts.",
    weak_in="Head-fake breakouts that reverse within the holding window.",
)
