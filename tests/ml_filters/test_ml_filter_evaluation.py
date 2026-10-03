from datetime import datetime, timezone

import pandas as pd

from q_backend.api.schemas.backtest import BacktestRequest
from q_backend.ml_filters.config import MLFilterDataset
from q_backend.ml_filters import evaluation
from q_backend.ml_filters.evaluation import _run_partition


class _RejectModel:
    feature_names = ("close", "side")

    def predict_good_entry_probability(self, X):
        return [0.0] * len(X)


def test_validation_uses_actual_flat_start_engine_reruns_and_partition_close():
    index = pd.date_range("2026-01-01T12:00:00Z", periods=30, freq="5min")
    close = [10, 11, 12, 9, 8, 10, 13, 9, 7, 12, 14, 8, 6, 13, 15, 7, 5, 12, 16, 8, 6, 14, 17, 9, 4, 15, 18, 7, 3, 16]
    bars = pd.DataFrame(
        {
            "open": close,
            "high": [value + 1 for value in close],
            "low": [value - 1 for value in close],
            "close": close,
            "tick_volume": 10,
        },
        index=index,
    )
    source_config = {
        "symbol": "TEST",
        "timeframe": "M5",
        "start": "2026-01-01T12:00:00Z",
        "end": "2026-01-01T15:00:00Z",
        "strategy": "MACrossover",
        "strategy_params": {
            "short_period": 2,
            "long_period": 3,
            "short_ma_type": "sma",
            "long_ma_type": "sma",
            "threshold": 0.0,
        },
        "initial_capital": 10000.0,
        "point_value": 1.0,
        "day_trade": False,
        "position_sizing": {"type": "fixed_quantity", "quantity": 1.0},
    }
    request = BacktestRequest.model_validate(source_config)
    dataset = MLFilterDataset(
        dataset_id="a" * 64,
        source_run_id="run",
        bars=bars,
        samples=(),
        selected_features=("close", "side"),
        train_end=index[5].to_pydatetime(),
        validation_end=index[28].to_pydatetime(),
        rejections={"train": {}, "validation": {}, "lockbox": {}},
        source_config=source_config,
        source_config_revision="legacy-unversioned",
        compatibility_fingerprint="b" * 64,
        bars_checksum="c" * 64,
        trades_checksum="d" * 64,
    )

    baseline = _run_partition(dataset, request, "validation", None)
    filtered = _run_partition(dataset, request, "validation", _RejectModel())

    assert baseline["flat_start"] is True
    assert baseline["warmup_used"] is True
    assert baseline["end_close_policy"] == "force_close_last_bar"
    assert isinstance(baseline["metrics"]["total_pnl"], float)
    assert filtered["candidate_count"] >= filtered["trade_count"]
    assert filtered["accepted_count"] == 0


def test_comparison_runs_only_validation_and_never_touches_lockbox(monkeypatch):
    calls = []
    dataset = object()
    monkeypatch.setattr(evaluation, "_dataset_from_snapshot", lambda _id: (dataset, {}))
    monkeypatch.setattr(evaluation, "_source_request", lambda _dataset: object())
    monkeypatch.setattr(evaluation, "read_model_manifest", lambda _id: {"dataset_id": "dataset"})
    monkeypatch.setattr(evaluation, "load_model_version", lambda _id: _RejectModel())
    monkeypatch.setattr(evaluation, "_classification_report", lambda *_args: {"roc_auc": None})

    def run_partition(_dataset, _request, partition, model, _threshold=0.5):
        calls.append((partition, model is not None))
        return {
            "metrics": {"total_pnl": 0.0},
            "trades": [],
            "partition_start": "2026-01-01T00:00:00+00:00",
            "partition_end": "2026-01-02T00:00:00+00:00",
        }

    monkeypatch.setattr(evaluation, "_run_partition", run_partition)
    result = evaluation.compare_filters({"dataset_id": "dataset", "model_version_ids": ["model"]}, job_id=None)

    assert calls == [("validation", False), ("validation", True)]
    assert result["lockbox_used"] is False


def test_final_evaluation_runs_and_persists_lockbox_engine_results(monkeypatch):
    calls = []
    dataset = object()
    model = _RejectModel()
    monkeypatch.setattr(evaluation, "_dataset_from_snapshot", lambda _id: (dataset, {}))
    monkeypatch.setattr(evaluation, "_source_request", lambda _dataset: type("Request", (), {"initial_capital": 100})())
    monkeypatch.setattr(evaluation, "read_model_manifest", lambda _id: {"dataset_id": "dataset"})
    monkeypatch.setattr(evaluation, "load_model_version", lambda _id: model)
    monkeypatch.setattr(evaluation, "_classification_report", lambda *_args: {"sample_count": 1})

    def run_partition(_dataset, _request, partition, selected_model, threshold=0.5):
        calls.append((partition, selected_model is not None, threshold))
        return {
            "metrics": {"total_pnl": 10.0},
            "trades": [],
            "partition_start": "2026-01-01T00:00:00+00:00",
            "partition_end": "2026-01-02T00:00:00+00:00",
        }

    artifacts = []

    def write_artifacts(_job_id, name, *_args, **_kwargs):
        artifacts.append(name)
        return {"equity": f"{name}/equity.parquet", "trades": f"{name}/trades.parquet"}

    monkeypatch.setattr(evaluation, "_run_partition", run_partition)
    monkeypatch.setattr(evaluation, "write_ml_filter_partition_artifacts", write_artifacts)
    result = evaluation.evaluate_filter(
        {"dataset_id": "dataset", "model_version_id": "model", "threshold": 0.7}, job_id="job"
    )

    assert calls == [("lockbox", False, 0.5), ("lockbox", True, 0.7)]
    assert result["baseline"]["metrics"]["total_pnl"] == 10.0
    assert result["filtered"]["metrics"]["total_pnl"] == 10.0
    assert artifacts == ["lockbox_baseline", "lockbox_filtered_model"]
