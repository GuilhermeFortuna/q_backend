"""Research indicator genome and feature-store tests (Q-112 criteria 8–10)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.backtesting.genome.schema import Genome
from q_backend.backtesting.genome.validate import GenomeValidationError, validate_genome
from q_backend.features.compute import compute_feature
from q_backend.features.registry import FEATURE_SPECS, assert_catalog_consistent, get_feature_spec
from pathlib import Path


def _ohlcv_frame(n: int = 400) -> pd.DataFrame:
    idx = pd.date_range("2024-01-02 09:00", periods=n, freq="5min", tz="UTC")
    close = 100 + np.cumsum(np.random.default_rng(1).normal(0, 0.2, n))
    return pd.DataFrame(
        {
            "time": idx,
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1000.0,
        }
    )


def test_normalized_momentum_genome_compiles_and_warmups():
    genome = {
        "version": 1,
        "genome_id": "norm-mom",
        "nodes": [
            {"id": "c", "kind": "source.close", "params": {}, "inputs": []},
            {
                "id": "m",
                "kind": "ind.normalized_momentum",
                "params": {
                    "horizon": 10,
                    "per_bar_vol_estimator": "realized",
                    "vol_window": 20,
                    "vol_half_life": 32.0,
                    "vol_lag": 1,
                },
                "inputs": ["c"],
            },
            {"id": "sig", "kind": "cmp.cross_above", "params": {"threshold": 0.0}, "inputs": ["m"]},
        ],
        "entry_long": {"ref": "sig"},
        "entry_short": {"ref": "sig"},
        "exit_long": {"ref": "sig"},
        "exit_short": {"ref": "sig"},
    }
    validate_genome(Genome.model_validate(genome))
    strategy = CompositeStrategy(genome=genome)
    out = strategy.compute_indicators(_ohlcv_frame())
    first_valid = out["g_m"].first_valid_index()
    assert first_valid is not None
    assert out.loc[:first_valid, "g_sig"].fillna(False).eq(False).all()


def test_volatility_log_ratio_compiles_and_warmups():
    genome = {
        "version": 1,
        "genome_id": "vol-ratio-run",
        "nodes": [
            {
                "id": "v",
                "kind": "ind.volatility_log_ratio",
                "params": {
                    "per_bar_vol_estimator": "realized",
                    "short_window": 10,
                    "long_window": 40,
                    "short_half_life": 8.0,
                    "long_half_life": 32.0,
                },
                "inputs": [],
            },
            {"id": "sig", "kind": "cmp.cross_above", "params": {"threshold": 0.0}, "inputs": ["v"]},
        ],
        "entry_long": {"ref": "sig"},
        "entry_short": {"ref": "sig"},
        "exit_long": {"ref": "sig"},
        "exit_short": {"ref": "sig"},
    }
    validate_genome(Genome.model_validate(genome))
    strategy = CompositeStrategy(genome=genome)
    frame = _ohlcv_frame()
    out = strategy.compute_indicators(frame.copy())
    first_valid = out["g_v"].first_valid_index()
    assert first_valid is not None
    assert out.loc[:first_valid, "g_sig"].fillna(False).eq(False).all()


def test_volatility_log_ratio_genome_rejects_inverted_windows():
    genome = {
        "version": 1,
        "genome_id": "vol-ratio",
        "nodes": [
            {
                "id": "v",
                "kind": "ind.volatility_log_ratio",
                "params": {
                    "per_bar_vol_estimator": "realized",
                    "short_window": 40,
                    "long_window": 10,
                    "short_half_life": 8.0,
                    "long_half_life": 32.0,
                },
                "inputs": [],
            }
        ],
        "entry_long": {"ref": "sig"},
        "entry_short": {"ref": "sig"},
        "exit_long": {"ref": "sig"},
        "exit_short": {"ref": "sig"},
    }
    genome["nodes"].append(
        {"id": "sig", "kind": "cmp.cross_above", "params": {"threshold": 0.0}, "inputs": ["v"]},
    )
    with pytest.raises(GenomeValidationError, match="short_window"):
        validate_genome(Genome.model_validate(genome))


def test_per_bar_ewma_vol_warmup_is_214_bars():
    spec = get_feature_spec("per_bar_ewma_vol")
    frame = _ohlcv_frame(500)
    result = compute_feature(frame, spec, {"half_life": 32.0})
    assert result.warmup_bars == 214


def test_goldens_and_catalog_unchanged():
    assert_catalog_consistent()
    golden_dir = Path(__file__).resolve().parents[1] / "backtesting" / "goldens"
    assert len(list(golden_dir.glob("*.json"))) >= 12


def test_existing_feature_ids_stable():
    for name in ("rsi", "atr", "realized_vol", "momentum", "zscore"):
        assert name in FEATURE_SPECS
