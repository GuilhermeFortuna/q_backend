"""Deflated Sharpe Ratio (Bailey & López de Prado, 2014)."""

from __future__ import annotations

import math

import pandas as pd

_EULER_MASCHERONI = 0.5772156649


def _norm_ppf(probability: float) -> float:
    """Inverse standard normal CDF (Acklam's approximation)."""
    if probability <= 0.0:
        return float("-inf")
    if probability >= 1.0:
        return float("inf")

    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758227161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )

    plow = 0.02425
    phigh = 1.0 - plow
    if probability < plow:
        q = math.sqrt(-2.0 * math.log(probability))
        return (
            (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
        )
    if probability > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - probability))
        return -(
            (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
        )

    q = probability - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    )


def _norm_cdf(value: float) -> float:
    return 0.5 * math.erfc(-value / math.sqrt(2.0))


def expected_max_sharpe(
    num_trials: int,
    *,
    mu: float = 0.0,
    sigma: float = 1.0,
) -> float:
    """Expected maximum Sharpe after ``num_trials`` independent trials (Eq. 1)."""
    if num_trials < 1:
        raise ValueError("num_trials must be at least 1")
    if num_trials == 1:
        return mu
    max_z = (1.0 - _EULER_MASCHERONI) * _norm_ppf(1.0 - 1.0 / num_trials) + (
        _EULER_MASCHERONI * _norm_ppf(1.0 - 1.0 / (num_trials * math.e))
    )
    return mu + sigma * max_z


def deflated_sharpe_ratio(
    *,
    sr_observed: float,
    num_trials: int,
    num_observations: int,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
    trials_sr_variance: float = 1.0,
) -> float:
    """Compute DSR = Φ(z) where z deflates ``sr_observed`` for multiple testing (Eq. 2)."""
    if num_observations < 2:
        return 0.0
    if num_trials < 1:
        raise ValueError("num_trials must be at least 1")

    sr0 = expected_max_sharpe(
        num_trials,
        mu=0.0,
        sigma=math.sqrt(max(trials_sr_variance, 0.0)),
    )
    denominator = math.sqrt(
        1.0
        - skewness * sr_observed
        + ((kurtosis - 1.0) / 4.0) * sr_observed * sr_observed
    )
    if denominator <= 0.0:
        return 0.0
    z = (sr_observed - sr0) * math.sqrt(num_observations - 1) / denominator
    return _norm_cdf(z)


def sharpe_from_returns(returns: list[float]) -> tuple[float, float, float, int]:
    """Return (annualized Sharpe, skewness, kurtosis, observation count) from returns."""
    if len(returns) < 2:
        return 0.0, 0.0, 3.0, len(returns)

    series = pd.Series(returns)
    std = float(series.std())
    if std == 0.0 or math.isnan(std):
        return 0.0, float(series.skew()), float(series.kurtosis()), len(returns)
    mean = float(series.mean())
    sharpe = mean / std * math.sqrt(252)
    return sharpe, float(series.skew()), float(series.kurtosis()), len(returns)
