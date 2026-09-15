from typing import Any, List
import math
import numpy as np
import pandas as pd
from numba import njit

from q_backend.backtesting.signal_columns import write_signal_columns
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy
from q_backend.backtesting.strategy_registry import StrategyParamSpec, register_strategy
from q_backend.backtesting.technical_indicators import (
    compute_realized_vol,
    compute_yang_zhang,
)

VOL_ESTIMATOR_CHOICES = ["yang_zhang", "close_to_close"]
TRADING_RULE_CHOICES = ["sign", "trend"]


@njit(cache=True)
def compute_rolling_newey_west_t_stat(returns: np.ndarray, window: int, lags: int) -> np.ndarray:
    n = len(returns)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < window:
        return out

    for i in range(window - 1, n):
        sub = returns[i - window + 1 : i + 1]

        # Calculate mean
        m = 0.0
        for val in sub:
            m += val
        m /= window

        # Gamma 0 (variance)
        gamma_0 = 0.0
        for val in sub:
            diff = val - m
            gamma_0 += diff * diff
        gamma_0 /= window

        # Autocovariances
        S = gamma_0
        for j in range(1, lags + 1):
            gamma_j = 0.0
            for k in range(j, window):
                gamma_j += (sub[k] - m) * (sub[k - j] - m)
            gamma_j /= window

            weight = 1.0 - (float(j) / (lags + 1))
            S += 2.0 * weight * gamma_j

        if S > 1e-12:
            se = np.sqrt(S / window)
            out[i] = m / se
        else:
            out[i] = 0.0

    return out


class TSMOMStrategy(TradingStrategy):
    """
    Time-series momentum (TSMOM) per Moskowitz, Ooi & Pedersen (2012) and the SIGN
    or TREND rules from Baltas & Kosowski (2017).

    - SIGN rule: Long when past return is positive, short when negative, evaluated every
      ``rebalance_bars`` bars.
    - TREND rule: Uses a Heteroskedasticity and Autocorrelation Consistent (HAC) Newey-West
      t-statistic of daily log returns over the lookback window. The signal direction is determined
      by the sign of the t-statistic, and the signal strength is scaled by the t-statistic capped
      at ``trend_signal_cap``.

    Positions can be held between rebalances (flipping on sign change) or adjusted on every
    rebalance bar by enabling ``rebalance_on_every_bar``.
    """

    def __init__(
        self,
        lookback_bars: int = 252,
        rebalance_bars: int = 21,
        vol_window: int = 63,
        vol_estimator: str = "yang_zhang",
        trading_rule: str = "sign",
        trend_signal_cap: float = 1.0,
        nw_lags: int = 4,
        rebalance_on_every_bar: bool = False,
        symbol: str = "BTCUSDT",
        **kwargs,
    ):
        if vol_estimator not in VOL_ESTIMATOR_CHOICES:
            raise ValueError(f"vol_estimator must be one of {VOL_ESTIMATOR_CHOICES}, got {vol_estimator!r}")
        if trading_rule not in TRADING_RULE_CHOICES:
            raise ValueError(f"trading_rule must be one of {TRADING_RULE_CHOICES}, got {trading_rule!r}")
        self.lookback_bars = lookback_bars
        self.rebalance_bars = rebalance_bars
        self.vol_window = vol_window
        self.vol_estimator = vol_estimator
        self.trading_rule = trading_rule
        self.trend_signal_cap = trend_signal_cap
        self.nw_lags = nw_lags
        self.rebalance_on_every_bar = rebalance_on_every_bar
        self.symbol = symbol
        super().__init__(
            lookback_bars=lookback_bars,
            rebalance_bars=rebalance_bars,
            vol_window=vol_window,
            vol_estimator=vol_estimator,
            trading_rule=trading_rule,
            trend_signal_cap=trend_signal_cap,
            nw_lags=nw_lags,
            rebalance_on_every_bar=rebalance_on_every_bar,
            symbol=symbol,
            **kwargs,
        )

    def _compute_volatility(self, df: pd.DataFrame) -> pd.Series:
        has_ohlc = all(col in df.columns for col in ("open", "high", "low", "close"))
        use_yz = self.vol_estimator == "yang_zhang" and has_ohlc
        if use_yz:
            return compute_yang_zhang(df["open"], df["high"], df["low"], df["close"], self.vol_window)
        return compute_realized_vol(df["close"], self.vol_window)

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        if "close" not in df.columns:
            raise ValueError("Data must contain a 'close' column for the TSMOM strategy.")

        if self.trading_rule == "trend":
            log_ret = (
                np.log(df["close"] / df["close"].shift(1)).replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy()
            )
            df["t_stat"] = compute_rolling_newey_west_t_stat(log_ret, self.lookback_bars, self.nw_lags)
            df["momentum"] = df["t_stat"]
            df["signal_strength"] = np.minimum(np.abs(df["t_stat"]), self.trend_signal_cap) / self.trend_signal_cap
        else:
            df["momentum"] = df["close"] / df["close"].shift(self.lookback_bars) - 1.0
            df["signal_strength"] = 1.0

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
        df["buy_signal"] = rebalance & (mom > 0) & (prev_sign <= 0) & mom.notna()
        df["sell_signal"] = rebalance & (mom < 0) & (prev_sign >= 0) & mom.notna()

        rebalance_s = df["rebalance"].astype(bool)
        if self.rebalance_on_every_bar:
            entry_long = (rebalance_s & (mom > 0)).astype(bool)
            entry_short = (rebalance_s & (mom < 0)).astype(bool)
            exit_long = (df["sell_signal"] | rebalance_s).astype(bool)
            exit_short = (df["buy_signal"] | rebalance_s).astype(bool)
        else:
            entry_long = df["buy_signal"]
            entry_short = df["sell_signal"]
            exit_long = df["sell_signal"]
            exit_short = df["buy_signal"]

        return write_signal_columns(
            df,
            entry_long=entry_long,
            entry_short=entry_short,
            exit_long=exit_long,
            exit_short=exit_short,
            strength=df["signal_strength"],
            strategy_name=type(self).__name__,
        )

    def get_chart_indicators(self) -> List[ChartIndicatorSpec]:
        label = (
            f"Newey-West t-stat ({self.lookback_bars})"
            if self.trading_rule == "trend"
            else f"Momentum ({self.lookback_bars})"
        )
        return [
            ChartIndicatorSpec(
                key="momentum",
                label=label,
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


def _build_tsmom(params: dict[str, Any], symbol: str) -> TSMOMStrategy:
    return TSMOMStrategy(
        lookback_bars=int(params["lookback_bars"]),
        rebalance_bars=int(params["rebalance_bars"]),
        vol_window=int(params["vol_window"]),
        vol_estimator=str(params["vol_estimator"]),
        trading_rule=str(params.get("trading_rule", "sign")),
        trend_signal_cap=float(params.get("trend_signal_cap", 1.0)),
        nw_lags=int(params.get("nw_lags", 4)),
        rebalance_on_every_bar=str(params.get("rebalance_on_every_bar", "false")).lower() == "true",
        symbol=symbol,
    )


register_strategy(
    name="TSMOM",
    label="Time-Series Momentum",
    description=(
        "SIGN/TREND rule TSMOM: long/short from the sign of past return or Newey-West t-statistic, "
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
            search_min=63,
            search_max=252,
            search_step=63,
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
            search_min=21,
            search_max=63,
            search_step=21,
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
            search_min=21,
            search_max=126,
            search_step=21,
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
        StrategyParamSpec(
            name="trading_rule",
            label="Trading rule",
            type="categorical",
            default="sign",
            choices=TRADING_RULE_CHOICES,
            hint="SIGN uses past return sign; TREND scales exposure by Newey-West t-statistic of daily returns.",
        ),
        StrategyParamSpec(
            name="trend_signal_cap",
            label="Trend signal cap",
            type="float",
            default=1.0,
            min=0.1,
            max=10.0,
            step=0.1,
            search_min=1.0,
            search_max=3.0,
            search_step=0.5,
            hint="Caps the maximum absolute value of the t-statistic for exposure scaling.",
        ),
        StrategyParamSpec(
            name="nw_lags",
            label="Newey-West lags",
            type="int",
            default=4,
            min=0,
            max=100,
            step=1,
            search_min=0,
            search_max=8,
            search_step=2,
            hint="Number of lags used in Newey-West standard error calculation.",
        ),
        StrategyParamSpec(
            name="rebalance_on_every_bar",
            label="Rebalance on every bar",
            type="categorical",
            default="false",
            choices=["true", "false"],
            hint="If true, closes and re-opens positions at every rebalancing interval to adjust for size/vol changes.",
        ),
    ],
    build=_build_tsmom,
    strategy_class=TSMOMStrategy,
    category="momentum",
    thesis=(
        "Moskowitz, Ooi & Pedersen (2012) document time-series momentum — assets "
        "that rose tend to keep rising over intermediate horizons. The SIGN rule "
        "goes long when past return is positive and short when negative. Baltas & "
        "Kosowski (2017) introduce the TREND rule, which scales exposure using a "
        "Newey-West t-statistic of daily log returns."
    ),
    strong_in="Persistent multi-month trends with clear sign or t-stat regimes.",
    weak_in="Rapid flips in mean-reverting or crisis-volatile markets — rebalances at the wrong time.",
)
