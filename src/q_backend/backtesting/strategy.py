from abc import ABC, abstractmethod
from typing import List, Literal, Optional
import pandas as pd
from pydantic import BaseModel
from q_backend.backtesting.moving_averages import (
    MA_TYPE_LABELS,
    compute_ma,
    normalize_ma_type,
)
from q_backend.backtesting.signal_columns import write_signal_columns


class ChartIndicatorSpec(BaseModel):
    key: str
    label: str
    pane: Literal["price", "oscillator"] = "price"
    color: Optional[str] = None


class TradingStrategy(ABC):
    """
    Abstract base class for all candle trading strategies in the backtesting engine.

    Strategies decide only via columnar ``q_signal_*`` columns written in
    ``compute_indicators`` (entry, exit-long, exit-short, strength). The engine
    and live evaluator read those columns through ``signal_arrays`` /
    ``evaluate_queued_signals``; there is no per-row entry/exit API.

    Execution contract (read before writing a new strategy):

    * A strategy only ever sees *closed* bars. A ``q_signal_entry`` (or exit)
      value on bar ``i`` means "as of bar ``i``'s close, my criteria are met".
    * Strategies must NOT decide their own fill price. The engine executes
      every emitted signal at the *next* bar's open, which is the earliest
      price actually tradable once bar ``i`` has closed. Do not assume you can
      transact at the close of the bar you are evaluating.
    * ``compute_indicators`` must be strictly causal: a value at bar ``i`` may
      only depend on bars ``<= i``. Never use centered windows, ``shift(-n)``,
      or whole-series statistics (max/min/mean over the full frame). Doing so
      leaks future information into the past. ``test_strategy_causality.py``
      enforces this automatically for every registered strategy.
    """

    def __init__(self, **kwargs):
        """
        Initialize strategy parameters.
        """
        self.parameters = kwargs
        from q_backend.backtesting.exit_strategy import ExitStrategy

        self.exit_strategy = ExitStrategy(kwargs)

    @property
    def holding_period_bars(self) -> int | None:
        """Fixed holding period in bars, or None when exits come from columns."""
        return None

    @abstractmethod
    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        Computes necessary technical indicators for the strategy.
        Typically receives historical market data and returns it with new indicator columns.

        Args:
            data (pd.DataFrame): Historical market data (OHLCV or tick).

        Returns:
            pd.DataFrame: Data augmented with technical indicators.
        """
        pass

    @abstractmethod
    def get_chart_indicators(self) -> List[ChartIndicatorSpec]:
        """
        Returns metadata for indicator columns to plot on the backtest chart.
        """
        pass


class MACrossoverStrategy(TradingStrategy):
    """
    Moving Average Crossover Strategy.

    This strategy computes a short-term Moving Average and a long-term Moving Average.
    It calculates the difference (delta = short MA - long MA).
    - A BUY signal is generated when the delta is above a certain positive value (threshold).
    - A SELL signal is generated when the delta goes below the negative of that value (-threshold).
    """

    def __init__(
        self,
        short_period: int = 50,
        long_period: int = 200,
        threshold: float = 0.0,
        short_ma_type: str = "sma",
        long_ma_type: str = "sma",
        symbol: str = "BTCUSDT",
        **kwargs,
    ):
        """
        Initializes the Moving Average Crossover Strategy.

        Args:
            short_period (int): Lookback window for the short moving average.
            long_period (int): Lookback window for the long moving average.
            threshold (float): The threshold value for the delta.
            short_ma_type (str): MA type for the short average (sma, ema, wma, smma, hma).
            long_ma_type (str): MA type for the long average (sma, ema, wma, smma, hma).
            symbol (str): Default symbol for the strategy signals.
        """
        self.short_ma_type = normalize_ma_type(short_ma_type)
        self.long_ma_type = normalize_ma_type(long_ma_type)
        super().__init__(
            short_period=short_period,
            long_period=long_period,
            threshold=threshold,
            short_ma_type=self.short_ma_type,
            long_ma_type=self.long_ma_type,
            symbol=symbol,
            **kwargs,
        )
        self.short_period = short_period
        self.long_period = long_period
        self.threshold = threshold
        self.symbol = symbol

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        Computes the short MA, long MA, delta, and signal columns.

        Args:
            data (pd.DataFrame): Historical price data containing a 'close' column.

        Returns:
            pd.DataFrame: The input DataFrame augmented with indicator columns.
        """
        # Ensure we don't modify the original DataFrame in a way that affects other processes
        df = data.copy()

        if "close" not in df.columns:
            raise ValueError("Data must contain a 'close' column for the MA Crossover strategy.")

        # Compute moving averages
        df["ma_short"] = compute_ma(df["close"], self.short_period, self.short_ma_type)
        df["ma_long"] = compute_ma(df["close"], self.long_period, self.long_ma_type)

        # Compute delta
        df["delta"] = df["ma_short"] - df["ma_long"]

        # Shift delta to check crossovers
        df["prev_delta"] = df["delta"].shift(1)

        # Precompute buy/sell triggers to avoid iterative lookback inside row loop
        # BUY when delta crosses above threshold
        df["buy_signal"] = (df["delta"] > self.threshold) & (df["prev_delta"] <= self.threshold)

        # SELL when delta crosses below -threshold
        df["sell_signal"] = (df["delta"] < -self.threshold) & (df["prev_delta"] >= -self.threshold)

        return write_signal_columns(
            df,
            entry_long=df["buy_signal"],
            entry_short=df["sell_signal"],
            exit_long=df["sell_signal"],
            exit_short=df["buy_signal"],
            strategy_name=type(self).__name__,
        )

    def _ma_label(self, side: str, period: int, ma_type: str) -> str:
        label = MA_TYPE_LABELS.get(ma_type, ma_type.upper())
        return f"{label} {side.title()} ({period})"

    def get_chart_indicators(self) -> List[ChartIndicatorSpec]:
        return [
            ChartIndicatorSpec(
                key="ma_short",
                label=self._ma_label("short", self.short_period, self.short_ma_type),
                pane="price",
                color="#c9a227",
            ),
            ChartIndicatorSpec(
                key="ma_long",
                label=self._ma_label("long", self.long_period, self.long_ma_type),
                pane="price",
                color="#6eb5ff",
            ),
            ChartIndicatorSpec(
                key="delta",
                label="Delta",
                pane="oscillator",
                color="#c9a227",
            ),
        ]
