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


class HurstTrendBlendStrategy(TradingStrategy):
    """
    Hurst, Ooi & Pedersen (2017) Trend Blend strategy.
    
    Blends momentum signals across three different horizons (e.g., 1-month, 3-month,
    and 12-month) and takes the equal-weighted average of their return signs.
    The signal strength is proportional to the alignment of the trends (1.0 if all agree,
    0.33 if 2 vs 1 agree).
    
    Positions can be rebalanced monthly (on every bar) or held until the direction flips.
    """

    def __init__(
        self,
        lookback_1: int = 21,
        lookback_2: int = 63,
        lookback_3: int = 252,
        rebalance_bars: int = 21,
        vol_window: int = 63,
        vol_estimator: str = "yang_zhang",
        risk_free_rate_annual: float = 0.0,
        signal_lag_bars: int = 0,
        rebalance_on_every_bar: bool = True,
        symbol: str = "BTCUSDT",
        **kwargs,
    ):
        if vol_estimator not in VOL_ESTIMATOR_CHOICES:
            raise ValueError(
                f"vol_estimator must be one of {VOL_ESTIMATOR_CHOICES}, got {vol_estimator!r}"
            )
        self.lookback_1 = lookback_1
        self.lookback_2 = lookback_2
        self.lookback_3 = lookback_3
        self.rebalance_bars = rebalance_bars
        self.vol_window = vol_window
        self.vol_estimator = vol_estimator
        self.risk_free_rate_annual = risk_free_rate_annual
        self.signal_lag_bars = signal_lag_bars
        self.rebalance_on_every_bar = rebalance_on_every_bar
        self.symbol = symbol
        super().__init__(
            lookback_1=lookback_1,
            lookback_2=lookback_2,
            lookback_3=lookback_3,
            rebalance_bars=rebalance_bars,
            vol_window=vol_window,
            vol_estimator=vol_estimator,
            risk_free_rate_annual=risk_free_rate_annual,
            signal_lag_bars=signal_lag_bars,
            rebalance_on_every_bar=rebalance_on_every_bar,
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
            raise ValueError("Data must contain a 'close' column for the Trend Blend strategy.")

        # Compute return signs for each lookback window
        # We subtract daily risk-free rate: rf_daily = risk_free_rate_annual / 252.0
        rf_daily = self.risk_free_rate_annual / 252.0

        signs = []
        for i, lookback in enumerate([self.lookback_1, self.lookback_2, self.lookback_3]):
            ret = df["close"] / df["close"].shift(lookback) - 1.0
            excess_ret = ret - (rf_daily * lookback)
            # Safe sign calculation
            sig = np.where(excess_ret.isna(), np.nan, np.sign(excess_ret))
            df[f"sig_{i+1}"] = sig
            signs.append(sig)

        # Equal-weighted blend
        blend = (df["sig_1"] + df["sig_2"] + df["sig_3"]) / 3.0

        # Apply optional signal lag
        if self.signal_lag_bars > 0:
            blend = blend.shift(self.signal_lag_bars)

        df["momentum"] = blend
        df["signal_strength"] = np.abs(blend).fillna(0.0)
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
                label=f"Trend Blend ({self.lookback_1}/{self.lookback_2}/{self.lookback_3})",
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
        is_rebalance_bar = current_data.get("rebalance", False)
        mom = current_data.get("momentum", np.nan)

        if pd.isna(mom):
            return []

        strength = float(current_data.get("signal_strength", 1.0))

        if self.rebalance_on_every_bar:
            if is_rebalance_bar:
                if mom > 0:
                    signals.append(Signal(symbol=symbol, action=SignalAction.BUY, strength=strength))
                elif mom < 0:
                    signals.append(Signal(symbol=symbol, action=SignalAction.SELL, strength=strength))
        else:
            if current_data.get("buy_signal", False):
                signals.append(Signal(symbol=symbol, action=SignalAction.BUY, strength=strength))
            elif current_data.get("sell_signal", False):
                signals.append(Signal(symbol=symbol, action=SignalAction.SELL, strength=strength))
        return signals

    def check_exit_conditions(
        self, current_data: pd.Series, open_trades: List[Trade]
    ) -> List[Signal]:
        symbol = resolve_symbol(current_data, self.symbol)
        if not open_trades:
            return []

        signals: List[Signal] = []
        is_rebalance_bar = current_data.get("rebalance", False)

        for trade in open_trades:
            if trade.symbol != symbol:
                continue

            is_sell_trigger = current_data.get("sell_signal", False)
            is_buy_trigger = current_data.get("buy_signal", False)

            if trade.action == SignalAction.BUY and is_sell_trigger:
                signals.append(Signal(symbol=symbol, action=SignalAction.CLOSE))
            elif trade.action == SignalAction.SELL and is_buy_trigger:
                signals.append(Signal(symbol=symbol, action=SignalAction.CLOSE))
            elif self.rebalance_on_every_bar and is_rebalance_bar:
                signals.append(Signal(symbol=symbol, action=SignalAction.CLOSE))

        return signals


def _build_hurst(params: dict[str, Any], symbol: str) -> HurstTrendBlendStrategy:
    return HurstTrendBlendStrategy(
        lookback_1=int(params["lookback_1"]),
        lookback_2=int(params["lookback_2"]),
        lookback_3=int(params["lookback_3"]),
        rebalance_bars=int(params["rebalance_bars"]),
        vol_window=int(params["vol_window"]),
        vol_estimator=str(params["vol_estimator"]),
        risk_free_rate_annual=float(params.get("risk_free_rate_annual", 0.0)),
        signal_lag_bars=int(params.get("signal_lag_bars", 0)),
        rebalance_on_every_bar=str(params.get("rebalance_on_every_bar", "true")).lower() == "true",
        symbol=symbol,
    )


register_strategy(
    name="HurstTrendBlend",
    label="Hurst Trend Blend",
    description=(
        "Equally weighted blend of 1-month, 3-month, and 12-month sign-of-past-excess-return "
        "strategies (Hurst, Ooi & Pedersen 2017)."
    ),
    params=[
        StrategyParamSpec(
            name="lookback_1",
            label="Lookback 1 (bars)",
            type="int",
            default=21,
            min=5,
            max=1000,
            step=1,
            hint="Shortest trend lookback horizon (typically 1 month).",
        ),
        StrategyParamSpec(
            name="lookback_2",
            label="Lookback 2 (bars)",
            type="int",
            default=63,
            min=5,
            max=1000,
            step=1,
            hint="Medium trend lookback horizon (typically 3 months).",
        ),
        StrategyParamSpec(
            name="lookback_3",
            label="Lookback 3 (bars)",
            type="int",
            default=252,
            min=5,
            max=1000,
            step=1,
            hint="Longest trend lookback horizon (typically 12 months).",
        ),
        StrategyParamSpec(
            name="rebalance_bars",
            label="Rebalance (bars)",
            type="int",
            default=21,
            min=1,
            max=252,
            step=1,
            hint="Frequency to rebalance/size positions (typically 21 bars for monthly).",
        ),
        StrategyParamSpec(
            name="vol_window",
            label="Volatility window",
            type="int",
            default=63,
            min=2,
            max=400,
            step=1,
            hint="Lookback window for the volatility estimator.",
        ),
        StrategyParamSpec(
            name="vol_estimator",
            label="Volatility estimator",
            type="categorical",
            default="yang_zhang",
            choices=VOL_ESTIMATOR_CHOICES,
            hint="Yang-Zhang uses OHLC for gap-aware vol; close-to-close is simpler when only close is available.",
        ),
        StrategyParamSpec(
            name="risk_free_rate_annual",
            label="Risk-free rate (annual %)",
            type="float",
            default=0.0,
            min=0.0,
            max=20.0,
            step=0.01,
            hint="Annual risk-free interest rate (e.g., 0.05 for 5%) to subtract for excess returns.",
        ),
        StrategyParamSpec(
            name="signal_lag_bars",
            label="Signal lag (bars)",
            type="int",
            default=0,
            min=0,
            max=252,
            step=1,
            hint="Lags signals by N bars to evaluate robustness (e.g., monthly lag).",
        ),
        StrategyParamSpec(
            name="rebalance_on_every_bar",
            label="Rebalance on every bar",
            type="categorical",
            default="true",
            choices=["true", "false"],
            hint="If true, closes and re-opens positions at every rebalancing interval to adjust for size/vol changes.",
        ),
    ],
    build=_build_hurst,
    strategy_class=HurstTrendBlendStrategy,
    category="momentum",
    thesis=(
        "Brian Hurst, Yao Hua Ooi, and Lasse Heje Pedersen (2017) document that blending "
        "multiple momentum horizons (1m, 3m, 12m) significantly improves the stability and "
        "robustness of trend-following strategies, providing a more consistent performance profile."
    ),
    strong_in="Diversified markets with multi-speed trends; reduces whipsaws compared to single lookbacks.",
    weak_in="Rapidly mean-reverting markets where short, medium, and long horizons all generate false transitions.",
)
