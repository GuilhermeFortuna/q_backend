import numpy as np
import pandas as pd


def compute_realized_vol(
    close: pd.Series, window: int, periods_per_year: int = 252
) -> pd.Series:
    """Rolling annualized close-to-close volatility from log returns."""
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(window=window, min_periods=window).std() * np.sqrt(
        periods_per_year
    )


def compute_yang_zhang(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    window: int,
    periods_per_year: int = 252,
) -> pd.Series:
    """Rolling annualized Yang–Zhang (2000) volatility. Causal: value at bar i
    uses bars <= i only."""
    prev_close = close.shift(1)
    overnight = np.log(open_ / prev_close)
    open_close = np.log(close / open_)

    u = np.log(high / open_)
    d = np.log(low / open_)
    c = np.log(close / open_)
    rogers_satchell = u * (u - c) + d * (d - c)

    var_overnight = overnight.rolling(window=window, min_periods=window).var()
    var_open_close = open_close.rolling(window=window, min_periods=window).var()
    mean_rs = rogers_satchell.rolling(window=window, min_periods=window).mean()

    k = 0.34 / (1.34 + (window + 1) / (window - 1))
    yz_var = var_overnight + k * var_open_close + (1.0 - k) * mean_rs
    return np.sqrt(yz_var * periods_per_year)


def compute_rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_bollinger_bands(
    close: pd.Series, period: int, num_std: float
) -> tuple[pd.Series, pd.Series, pd.Series]:
    middle = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower


def compute_macd(
    close: pd.Series, fast_period: int, slow_period: int, signal_period: int
) -> tuple[pd.Series, pd.Series, pd.Series]:
    fast_ema = close.ewm(span=fast_period, adjust=False).mean()
    slow_ema = close.ewm(span=slow_period, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_donchian_channels(
    high: pd.Series, low: pd.Series, period: int
) -> tuple[pd.Series, pd.Series]:
    upper = high.rolling(window=period).max().shift(1)
    lower = low.rolling(window=period).min().shift(1)
    return upper, lower


def compute_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int
) -> pd.Series:
    """
    Computes Wilder's Average True Range (ATR) using Wilder's smoothing/exponential moving average.
    """
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

