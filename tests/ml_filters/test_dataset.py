from datetime import datetime, timezone

import pandas as pd
import pytest

from q_backend.ml_filters.config import EntryFeatureConfig
from q_backend.ml_filters.dataset import build_dataset_from_frames, build_source_dataset, partition_features


def _config():
    return EntryFeatureConfig(
        train_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        validation_end=datetime(2026, 3, 1, tzinfo=timezone.utc),
        feature_names=("close", "ma_short", "side"),
        algorithms=("random_forest",),
    )


def _source():
    return {
        "status": "completed",
        "engine": "candle",
        "strategy": "MACrossover",
        "strategy_params": {
            "short_period": 2,
            "long_period": 3,
            "short_ma_type": "sma",
            "long_ma_type": "sma",
            "threshold": 0.0,
        },
        "entry_manager": {"kind": "or", "params": {}},
    }


def test_dataset_rejects_duplicate_or_nonmonotonic_frozen_bars():
    bars = pd.DataFrame(
        {
            "time": ["2026-01-02T12:00:00Z", "2026-01-02T12:00:00Z"],
            "open": [1, 2],
            "high": [1, 2],
            "low": [1, 2],
            "close": [1, 2],
        }
    )
    with pytest.raises(ValueError, match="unique, strictly increasing"):
        build_dataset_from_frames("run", _config(), bars, pd.DataFrame(), _source())


def test_dataset_rejects_non_macrossover_sources():
    bars = pd.DataFrame(columns=["time", "open", "high", "low", "close"])
    source = {**_source(), "strategy": "RSI"}
    with pytest.raises(ValueError, match="exactly one original MACrossover"):
        build_dataset_from_frames("run", _config(), bars, pd.DataFrame(), source)


def test_dataset_uses_previous_bar_and_partitions_by_real_utc_timestamps():
    times = pd.date_range("2026-01-01T00:00:00Z", periods=900, freq="5min")
    bars = pd.DataFrame(
        {
            "time": times,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": range(900),
            "tick_volume": 10,
            "e0__ma_short": 1.0,
            "e0__ma_long": 0.0,
            "e0__delta": 1.0,
            "e0__prev_delta": 0.0,
        }
    )
    positions = [i * 3 for i in range(24)] + [300, 600]
    trades = []
    for i, signal_pos in enumerate(positions):
        side = 1 if i % 2 == 0 else -1
        bars.loc[signal_pos, "e0__delta"] = 1.0 if side == 1 else -1.0
        entry_pos = signal_pos + 1
        bars.loc[entry_pos, ["open", "high", "low", "close"]] = [90_000, 99_999, 1, 80_000]
        trades.append(
            {
                "entry_time": times[entry_pos],
                "exit_time": times[entry_pos + 1],
                "action": "BUY" if side == 1 else "SELL",
                "pnl": 1.0 if i % 3 else -1.0,
            }
        )
    # This malformed candidate belongs to validation because its preceding
    # signal bar is in that partition, even though it never becomes a sample.
    bars.loc[303, "close"] = float("nan")
    bars.loc[303, "e0__delta"] = 1.0
    bars.loc[303, "e0__prev_delta"] = 0.0
    trades.append(
        {
            "entry_time": times[304],
            "exit_time": times[305],
            "action": "BUY",
            "pnl": 1.0,
        }
    )
    config = EntryFeatureConfig(
        train_end=datetime(2026, 1, 2, tzinfo=timezone.utc),
        validation_end=datetime(2026, 1, 3, tzinfo=timezone.utc),
        feature_names=("close", "side"),
        algorithms=("random_forest",),
    )

    dataset = build_dataset_from_frames("run", config, bars, pd.DataFrame(trades), _source())
    X, y, samples = partition_features(dataset, "train")

    assert len(samples) == 24
    assert X.iloc[0]["close"] == 0
    assert X.iloc[0]["close"] != bars.iloc[1]["close"]
    assert len(y) == 24
    assert {sample.partition for sample in dataset.samples} == {"train", "validation", "lockbox"}
    assert dataset.rejections["validation"]["nonfinite_feature"] == 1


def test_source_loader_imports_runtime_dependencies_before_validating_run_id():
    with pytest.raises(ValueError, match="source_run_id must be a UUID"):
        build_source_dataset("not-a-uuid", _config())
