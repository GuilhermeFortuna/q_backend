"""Tests for Deflated Sharpe Ratio (WO40)."""

from __future__ import annotations

import math

import pytest

from q_backend.optimization.dsr import (
    deflated_sharpe_ratio,
    expected_max_sharpe,
    sharpe_from_returns,
)


def test_expected_max_sharpe_grows_with_trials():
    low = expected_max_sharpe(10)
    high = expected_max_sharpe(1000)
    assert high > low


def test_dsr_decreases_as_trials_increase_with_fixed_sr():
    sr = 1.5
    observations = 252
    dsr_few = deflated_sharpe_ratio(
        sr_observed=sr,
        num_trials=10,
        num_observations=observations,
    )
    dsr_many = deflated_sharpe_ratio(
        sr_observed=sr,
        num_trials=1000,
        num_observations=observations,
    )
    assert dsr_many < dsr_few


def test_dsr_hand_computed_normal_returns():
    """Verify against Bailey & López de Prado Eq. 2 for a toy case."""
    sr_observed = 1.0
    num_trials = 50
    num_observations = 252
    skewness = 0.0
    kurtosis = 3.0

    sr0 = expected_max_sharpe(num_trials, mu=0.0, sigma=1.0)
    denominator = math.sqrt(1.0 - skewness * sr_observed + ((kurtosis - 1.0) / 4.0) * sr_observed * sr_observed)
    z = (sr_observed - sr0) * math.sqrt(num_observations - 1) / denominator

    expected = 0.5 * math.erfc(-z / math.sqrt(2.0))
    actual = deflated_sharpe_ratio(
        sr_observed=sr_observed,
        num_trials=num_trials,
        num_observations=num_observations,
        skewness=skewness,
        kurtosis=kurtosis,
    )
    assert actual == pytest.approx(expected, rel=1e-9)


def test_sharpe_from_returns_empty():
    sharpe, skew, kurt, count = sharpe_from_returns([])
    assert count == 0
    assert sharpe == 0.0
    assert skew == 0.0
    assert kurt == 3.0
