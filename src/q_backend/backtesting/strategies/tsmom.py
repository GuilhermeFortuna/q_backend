from typing import Any, List

import numpy as np
import pandas as pd

from q_backend.backtesting.models import Signal, SignalAction, Trade
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy, resolve_symbol
from q_backend.backtesting.strategy_registry import StrategyParamSpec, register_strategy
from q_backend.backtesting.technical_indicators import (
    compute_realized_vol,
    compute_yang_zhang,
)

VOL_ESTIMATOR_CHOICES = ["yang_zhang", "close_to_close"]


class TSMOMStrategy(TradingStrategy):
    """
    Time-series momentum (TSMOM) per Moskowitz, Ooi & Pedersen (2012) and the SIGN
    rule from Baltas & Kosowski (2017).

    Long when past return is positive, short when negative, evaluated every
    ``rebalance_bars`` bars. Positions are held between rebalances; a sign flip
    at a rebalance bar closes the open trade and opens the opposite direction.

    Deviations from the papers: single-instrument runs (no cross-market
    diversification), raw past return instead of excess return (no risk-free
    series), and bar-count rebalancing instead of calendar month-ends.
    """

    def __init__(
        self,
        lookback_bars: int = 252,
        rebalance_bars: int = 21,
        vol_window: int = 63,
        vol_estimator: str = "yang_zhang",
        symbol: str = "BTCUSDT",
        **kwargs,
    ):
        if vol_estimator not in VOL_ESTIMATOR_CHOICES:
            raise ValueError(
                f"vol_estimator must be one of {VOL_ESTIMATOR_CHOICES}, got {vol_estimator!r}"
            )
        self.lookback_bars = lookback_bars
        self.rebalance_bars = rebalance_bars
        self.vol_window = vol_window
        self.vol_estimator = vol_estimator
        self.symbol = symbol
        super().__init__(
            lookback_bars=lookback_bars,
            rebalance_bars=rebalance_bars,
            vol_window=vol_window,
            vol_estimator=vol_estimator,
            symbol=symbol,
            **kwargs,
        )

    def _compute_volatility(self, df: pd.DataFrame) -> pd.Series:
        has_ohlc = all(col in df.columns for col in ("open", "high", "low", "close"))
        use_yz = self.vol_estimator == "yang_zhang" and has_ohlc
        if use_yz:
            return compute_yang_zhang(
                df["open"], df["high"], df["low"], df["close"], self.vol_window
            )
        return compute_realized_vol(df["close"], self.vol_window)

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        if "close" not in df.columns:
            raise ValueError("Data must contain a 'close' column for the TSMOM strategy.")

        df["momentum"] = df["close"] / df["close"].shift(self.lookback_bars) - 1.0
        df["volatility"] = self._compute_volatility(df)

        bar_index = np.arange(len(df))
        df["bar_index"] = bar_index
        rebalance = bar_index % self.rebalance_bars == 0
        df["rebalance"] = rebalance

        eval_sign = np.where(
            df["momentum"].isna(),
            np.nan,
            np.sign(df["momentum"].to_numpy()),
        )
        rebalance_index = df.index[rebalance]
        rebalance_signs = pd.Series(eval_sign[rebalance], index=rebalance_index)
        prev_rebalance_sign = rebalance_signs.shift(1).fillna(0)

        prev_sign = pd.Series(0, index=df.index, dtype=float)
        prev_sign.loc[rebalance_index] = prev_rebalance_sign.to_numpy()

        mom = df["momentum"]
        df["buy_signal"] = (
            rebalance & (mom > 0) & (prev_sign <= 0) & mom.notna()
        )
        df["sell_signal"] = (
            rebalance & (mom < 0) & (prev_sign >= 0) & mom.notna()
        )
        return df

    def get_chart_indicators(self) -> List[ChartIndicatorSpec]:
        return [
            ChartIndicatorSpec(
                key="momentum",
                label=f"Momentum ({self.lookback_bars})",
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


def _build_tsmom(params: dict[str, Any], symbol: str) -> TSMOMStrategy:
    return TSMOMStrategy(
        lookback_bars=int(params["lookback_bars"]),
        rebalance_bars=int(params["rebalance_bars"]),
        vol_window=int(params["vol_window"]),
        vol_estimator=str(params["vol_estimator"]),
        symbol=symbol,
    )


register_strategy(
    name="TSMOM",
    label="Time-Series Momentum",
    description=(
        "SIGN rule TSMOM: long/short from the sign of past return, "
        "rebalanced every N bars with Yang–Zhang or close-to-close volatility "
        "(MOP 2012 / Baltas & Kosowski 2017, single-instrument)."
    ),
    params=[
        StrategyParamSpec(
            name="lookback_bars",
            label="Lookback (bars)",
            type="int",
            default=252,
            min=20,
            max=1000,
            step=1,
            hint="Longer lookback = smoother momentum signal, slower regime shifts; shorter = faster flips.",
        ),
        StrategyParamSpec(
            name="rebalance_bars",
            label="Rebalance (bars)",
            type="int",
            default=21,
            min=1,
            max=252,
            step=1,
            hint="More frequent = quicker response to sign changes, more turnover; less = stickier positions.",
        ),
        StrategyParamSpec(
            name="vol_window",
            label="Volatility window",
            type="int",
            default=63,
            min=2,
            max=400,
            step=1,
            hint="Shorter = more reactive vol estimate; longer = smoother, less responsive to recent spikes.",
        ),
        StrategyParamSpec(
            name="vol_estimator",
            label="Volatility estimator",
            type="categorical",
            default="yang_zhang",
            choices=VOL_ESTIMATOR_CHOICES,
            hint="Yang-Zhang uses OHLC for gap-aware vol; close-to-close is simpler when only close is available.",
        ),
    ],
    build=_build_tsmom,
    strategy_class=TSMOMStrategy,
    category="momentum",
    thesis=(
        "Moskowitz, Ooi & Pedersen (2012) document time-series momentum — assets "
        "that rose tend to keep rising over intermediate horizons. The SIGN rule "
        "goes long when past return is positive and short when negative, rebalanced "
        "every N bars."
    ),
    strong_in="Persistent multi-month trends with clear sign regimes.",
    weak_in="Rapid sign flips in mean-reverting or crisis-volatile markets — rebalances at the wrong time.",
)
