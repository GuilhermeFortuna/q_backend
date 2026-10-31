"""One-minute order-flow columns derived from trade prints."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PATH = Path(__file__).resolve().parents[2] / "experiments" / "tick_signals" / "features.py"
SPEC = importlib.util.spec_from_file_location("tick_signal_features", PATH)
features = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = features
SPEC.loader.exec_module(features)

BUCKETS = features.Spec(tick=5.0, small=5, large=100, huge=500)
BUY, SELL, LAST = 32, 64, 8


def write_session(tmp_path: Path, rows: list[tuple[int, float, float, int]]) -> str:
    path = tmp_path / "2026-01-05.parquet"
    frame = pd.DataFrame(rows, columns=["time_msc", "last", "volume", "flags"])
    frame.to_parquet(path)
    return str(path)


def test_tick_rule_uses_own_move_then_prior_move_then_prior_side() -> None:
    first = np.array([100.0, 105.0, 105.0, 100.0])
    last = np.array([100.0, 105.0, 105.0, 95.0])
    assert features.tick_rule(first, last).tolist() == [0.0, 1.0, 1.0, -1.0]


def test_prints_in_one_millisecond_merge_into_one_order(tmp_path: Path) -> None:
    rows = [
        (0, 100.0, 1, LAST),
        (1000, 105.0, 60, LAST),
        (1000, 110.0, 60, LAST),
        (2000, 105.0, 2, LAST),
    ]
    minute = features.day_features(write_session(tmp_path, rows), BUCKETS).iloc[0]
    assert minute["n_orders"] == 3
    assert minute["n_prints"] == 4
    assert minute["signed_large"] == 120
    assert minute["signed_sweep"] == 120
    assert minute["signed_small"] == -2
    assert minute["volume"] == 123


def test_a_constant_flag_is_not_an_aggressor_side(tmp_path: Path) -> None:
    rows = [(0, 100.0, 5, BUY | LAST), (1000, 95.0, 5, BUY | LAST)]
    minute = features.day_features(write_session(tmp_path, rows), BUCKETS).iloc[0]
    assert not minute["has_true"]
    assert minute["true_signed"] == 0


def test_real_flags_are_kept_for_calibration(tmp_path: Path) -> None:
    rows = [(0, 100.0, 5, BUY | LAST), (1000, 95.0, 3, SELL | LAST)]
    minute = features.day_features(write_session(tmp_path, rows), BUCKETS).iloc[0]
    assert minute["has_true"]
    assert minute["true_signed"] == pytest.approx(2)


def test_quote_rows_are_ignored(tmp_path: Path) -> None:
    assert features.day_features(write_session(tmp_path, [(0, 100.0, 0, 2)]), BUCKETS) is None
