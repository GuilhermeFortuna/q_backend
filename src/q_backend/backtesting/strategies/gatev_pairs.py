from typing import Any, List
import numpy as np
import pandas as pd
from numba import njit

from q_backend.backtesting.models import Signal, SignalAction, Trade
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy, resolve_symbol
from q_backend.backtesting.strategy_registry import StrategyParamSpec, register_strategy
from q_backend.backtesting.technical_indicators import (
    compute_realized_vol,
    compute_yang_zhang,
)

VOL_ESTIMATOR_CHOICES = ["yang_zhang", "close_to_close"]


@njit(cache=True)
def _compute_pairs_signals_numba(
    price_a: np.ndarray,
    price_b: np.ndarray,
    formation_bars: int,
    trading_bars: int,
    open_threshold_sd: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    n = len(price_a)
    buy_signals = np.zeros(n, dtype=np.bool_)
    sell_signals = np.zeros(n, dtype=np.bool_)
    exit_signals = np.zeros(n, dtype=np.bool_)
    spread_values = np.zeros(n, dtype=np.float64)

    cycle_len = formation_bars + trading_bars
    
    # State variables
    position = 0  # 0: flat, 1: long spread (buy A, short B), -1: short spread (short A, buy B)
    start_price_a = 0.0
    start_price_b = 0.0
    sd_spread = 0.0

    for i in range(n):
        k = i % cycle_len
        
        # At start of cycle, record starting prices
        if k == 0:
            start_price_a = price_a[i]
            start_price_b = price_b[i]
            position = 0

        # Calculate normalized prices relative to cycle start
        p_norm_a = price_a[i] / start_price_a if start_price_a > 1e-12 else 1.0
        p_norm_b = price_b[i] / start_price_b if start_price_b > 1e-12 else 1.0
        spread = p_norm_a - p_norm_b
        spread_values[i] = spread

        # End of formation period: calculate standard deviation of spread
        if k == formation_bars - 1:
            formation_spreads = spread_values[i - formation_bars + 1 : i + 1]
            
            mean_spread = 0.0
            for val in formation_spreads:
                mean_spread += val
            mean_spread /= formation_bars
            
            var_spread = 0.0
            for val in formation_spreads:
                diff = val - mean_spread
                var_spread += diff * diff
            var_spread /= formation_bars
            
            sd_spread = np.sqrt(var_spread)

        # Trading period
        elif k >= formation_bars:
            if position == 0:
                # Flat: look for entry
                if sd_spread > 1e-12:
                    if spread > open_threshold_sd * sd_spread:
                        position = -1
                        sell_signals[i] = True
                    elif spread < -open_threshold_sd * sd_spread:
                        position = 1
                        buy_signals[i] = True
            elif position == 1:
                # Long spread: exit if spread >= 0 or end of cycle
                if spread >= 0.0 or k == cycle_len - 1:
                    position = 0
                    exit_signals[i] = True
            elif position == -1:
                # Short spread: exit if spread <= 0 or end of cycle
                if spread <= 0.0 or k == cycle_len - 1:
                    position = 0
                    exit_signals[i] = True

    return buy_signals, sell_signals, exit_signals, spread_values


class GatevPairsStrategy(TradingStrategy):
    """
    Gatev, Goetzmann & Rouwenhorst (2006) Pairs Trading strategy.
    
    Trades a synthetic spread asset: price = close_a - close_b.
    During the formation period, it calculates the historical standard deviation of the
    normalized spread. During the trading period, it goes long/short the spread when
    it diverges by more than `open_threshold_sd` standard deviations, and closes the trade
    when the spread crosses back through zero (prices cross).
    """

    def __init__(
        self,
        col_a: str = "close_a",
        col_b: str = "close_b",
        open_col_a: str = "open_a",
        open_col_b: str = "open_b",
        formation_bars: int = 252,
        trading_bars: int = 126,
        open_threshold_sd: float = 2.0,
        vol_window: int = 63,
        vol_estimator: str = "close_to_close",
        symbol: str = "PAIR",
        **kwargs,
    ):
        if vol_estimator not in VOL_ESTIMATOR_CHOICES:
            raise ValueError(
                f"vol_estimator must be one of {VOL_ESTIMATOR_CHOICES}, got {vol_estimator!r}"
            )
        self.col_a = col_a
        self.col_b = col_b
        self.open_col_a = open_col_a
        self.open_col_b = open_col_b
        self.formation_bars = formation_bars
        self.trading_bars = trading_bars
        self.open_threshold_sd = open_threshold_sd
        self.vol_window = vol_window
        self.vol_estimator = vol_estimator
        self.symbol = symbol
        super().__init__(
            col_a=col_a,
            col_b=col_b,
            open_col_a=open_col_a,
            open_col_b=open_col_b,
            formation_bars=formation_bars,
            trading_bars=trading_bars,
            open_threshold_sd=open_threshold_sd,
            vol_window=vol_window,
            vol_estimator=vol_estimator,
            symbol=symbol,
            **kwargs,
        )

    def _compute_volatility(self, df: pd.DataFrame) -> pd.Series:
        # Volatility is computed as the average of the log-return volatilities of both assets A and B
        vol_a = compute_realized_vol(df[self.col_a], self.vol_window)
        vol_b = compute_realized_vol(df[self.col_b], self.vol_window)
        return 0.5 * (vol_a + vol_b)

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        if self.col_a not in df.columns or self.col_b not in df.columns:
            raise ValueError(f"Data must contain both {self.col_a!r} and {self.col_b!r} columns for Pairs Trading.")

        # Compute synthetic close as price difference
        df["close"] = df[self.col_a] - df[self.col_b]
        
        # Open price spread
        if self.open_col_a in df.columns and self.open_col_b in df.columns:
            df["open"] = df[self.open_col_a] - df[self.open_col_b]
        else:
            df["open"] = df["close"]
            
        df["high"] = df["close"]
        df["low"] = df["close"]

        price_a = df[self.col_a].to_numpy().astype(np.float64)
        price_b = df[self.col_b].to_numpy().astype(np.float64)

        buy_sigs, sell_sigs, exit_sigs, spreads = _compute_pairs_signals_numba(
            price_a,
            price_b,
            self.formation_bars,
            self.trading_bars,
            self.open_threshold_sd,
        )

        df["buy_signal"] = buy_sigs
        df["sell_signal"] = sell_sigs
        df["exit_signal"] = exit_sigs
        df["spread"] = spreads

        # Volatility of the synthetic spread closes
        df["volatility"] = self._compute_volatility(df)

        bar_index = np.arange(len(df))
        df["bar_index"] = bar_index
        df["rebalance"] = False

        return df

    def get_chart_indicators(self) -> List[ChartIndicatorSpec]:
        return [
            ChartIndicatorSpec(
                key="spread",
                label="Normalized Spread",
                pane="oscillator",
                color="#c9a227",
            ),
            ChartIndicatorSpec(
                key="volatility",
                label=f"Volatility ({self.vol_window})",
                pane="oscillator",
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
        if not open_trades:
            return []

        signals: List[Signal] = []
        is_exit_trigger = current_data.get("exit_signal", False)

        for trade in open_trades:
            if trade.symbol != symbol:
                continue
            if is_exit_trigger:
                signals.append(Signal(symbol=symbol, action=SignalAction.CLOSE))
        return signals


def _build_pairs(params: dict[str, Any], symbol: str) -> GatevPairsStrategy:
    return GatevPairsStrategy(
        col_a=str(params.get("col_a", "close_a")),
        col_b=str(params.get("col_b", "close_b")),
        open_col_a=str(params.get("open_col_a", "open_a")),
        open_col_b=str(params.get("open_col_b", "open_b")),
        formation_bars=int(params.get("formation_bars", 252)),
        trading_bars=int(params.get("trading_bars", 126)),
        open_threshold_sd=float(params.get("open_threshold_sd", 2.0)),
        vol_window=int(params.get("vol_window", 63)),
        vol_estimator=str(params.get("vol_estimator", "close_to_close")),
        symbol=symbol,
    )


register_strategy(
    name="GatevPairs",
    label="Gatev Pairs Trading",
    description=(
        "Stateful Pairs Trading (Gatev, Goetzmann & Rouwenhorst 2006): forms pairs by minimizing SSD "
        "and trades on standard deviation spread divergences."
    ),
    params=[
        StrategyParamSpec(
            name="col_a",
            label="Column Asset A",
            type="categorical",
            default="close_a",
            choices=["close_a", "close"],
            hint="Price column for Asset A in the input dataframe.",
        ),
        StrategyParamSpec(
            name="col_b",
            label="Column Asset B",
            type="categorical",
            default="close_b",
            choices=["close_b", "close"],
            hint="Price column for Asset B in the input dataframe.",
        ),
        StrategyParamSpec(
            name="open_col_a",
            label="Open Column Asset A",
            type="categorical",
            default="open_a",
            choices=["open_a", "open"],
            hint="Open price column for Asset A (optional).",
        ),
        StrategyParamSpec(
            name="open_col_b",
            label="Open Column Asset B",
            type="categorical",
            default="open_b",
            choices=["open_b", "open"],
            hint="Open price column for Asset B (optional).",
        ),
        StrategyParamSpec(
            name="formation_bars",
            label="Formation Bars",
            type="int",
            default=252,
            min=10,
            max=1000,
            step=1,
            search_min=126,
            search_max=378,
            search_step=126,
            hint="Length of the pair formation/parameter-estimation window (typically 12 months).",
        ),
        StrategyParamSpec(
            name="trading_bars",
            label="Trading Bars",
            type="int",
            default=126,
            min=5,
            max=1000,
            step=1,
            search_min=63,
            search_max=189,
            search_step=63,
            hint="Length of the trading window (typically 6 months).",
        ),
        StrategyParamSpec(
            name="open_threshold_sd",
            label="Open Threshold (SD)",
            type="float",
            default=2.0,
            min=0.5,
            max=5.0,
            step=0.1,
            search_min=1.5,
            search_max=3.0,
            search_step=0.5,
            hint="Standard deviation multiplier to trigger entry (typically 2.0).",
        ),
        StrategyParamSpec(
            name="vol_window",
            label="Volatility Window",
            type="int",
            default=63,
            min=2,
            max=400,
            step=1,
            search_min=21,
            search_max=126,
            search_step=21,
            hint="Lookback window to compute the volatility of the spread.",
        ),
        StrategyParamSpec(
            name="vol_estimator",
            label="Volatility Estimator",
            type="categorical",
            default="close_to_close",
            choices=["close_to_close"],
            hint="Estimator used for spread volatility.",
        ),
    ],
    build=_build_pairs,
    strategy_class=GatevPairsStrategy,
    category="mean_reversion",
    thesis=(
        "Evan Gatev, William N. Goetzmann, and K. Geert Rouwenhorst (2006) demonstrate that "
        "relative value mean-reversion trading (pairs trading) yields persistent, risk-adjusted "
        "excess returns. Positions are opened when the spread diverges by more than 2 historical "
        "standard deviations and closed when prices cross back to equilibrium."
    ),
    strong_in="Highly correlated asset pairs exhibiting temporary, mean-reverting price spread divergence.",
    weak_in="Structural breaks, mergers, bankruptcies, or permanent decoupling where the spread diverges indefinitely.",
)
