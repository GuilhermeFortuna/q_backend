from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sklearn.metrics import confusion_matrix, roc_auc_score

from q_backend.api.schemas.backtest import BacktestRequest
from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.entry_config import normalize_entries
from q_backend.backtesting.factory import build_composite_entry
from q_backend.backtesting.position_sizing import build_position_sizer
from q_backend.ml_filters.adapters import EntryClassifier
from q_backend.ml_filters.artifacts import (
    load_dataset_snapshot,
    load_model_version,
    read_model_manifest,
    write_ml_filter_partition_artifacts,
)
from q_backend.ml_filters.config import EntrySample, MLFilterDataset
from q_backend.ml_filters.dataset import partition_features
from q_backend.ml_filters.filter import EntryFilteredStrategy

_BRASILIA = ZoneInfo("America/Sao_Paulo")


def _as_utc(value: Any) -> datetime:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(_BRASILIA)
    return stamp.tz_convert("UTC").to_pydatetime()


def _as_engine_time(value: datetime) -> datetime:
    return value.astimezone(_BRASILIA).replace(tzinfo=None)


def _dataset_from_snapshot(dataset_id: str) -> tuple[MLFilterDataset, dict[str, Any]]:
    manifest, bars, sample_frame, source_config = load_dataset_snapshot(dataset_id)
    samples = tuple(
        EntrySample(
            partition=row.partition,
            signal_position=int(row.signal_position),
            signal_time=_as_utc(row.signal_time),
            entry_time=_as_utc(row.entry_time),
            exit_time=_as_utc(row.exit_time),
            side=int(row.side),
            label=int(row.label),
            net_pnl=float(row.net_pnl),
        )
        for row in sample_frame.itertuples(index=False)
    )
    selected_features = tuple(feature["name"] for feature in manifest["selected_features"])
    dataset = MLFilterDataset(
        dataset_id=dataset_id,
        source_run_id=manifest["source_run_id"],
        bars=bars,
        samples=samples,
        selected_features=selected_features,
        train_end=_as_utc(manifest["train_end"]),
        validation_end=_as_utc(manifest["validation_end"]),
        rejections={
            part: manifest["partition_counts"][part]["rejections"] for part in ("train", "validation", "lockbox")
        },
        source_config=source_config,
        source_config_revision=manifest["source_config_revision"],
        compatibility_fingerprint=manifest["compatibility_fingerprint"],
        bars_checksum=manifest["bars_checksum"],
        trades_checksum=manifest["trades_checksum"],
    )
    return dataset, manifest


def _classification_report(dataset: MLFilterDataset, model: EntryClassifier, partition: str) -> dict[str, Any]:
    X, y, samples = partition_features(dataset, partition)
    if not samples:
        return {
            "sample_count": 0,
            "class_counts": {"0": 0, "1": 0},
            "confusion_matrix": [[0, 0], [0, 0]],
            "roc_auc": None,
            "roc_auc_reason": "No completed source trades are available in this partition",
            "accuracy": None,
            "threshold": 0.5,
        }
    scores = model.predict_good_entry_probability(X)
    predictions = (scores >= 0.5).astype(np.int8)
    matrix = confusion_matrix(y, predictions, labels=[0, 1]).tolist()
    if y.nunique() < 2:
        auc, auc_reason = None, "ROC AUC requires both classes in the partition"
    else:
        auc, auc_reason = float(roc_auc_score(y, scores)), None
    return {
        "sample_count": len(samples),
        "class_counts": {"0": int((y == 0).sum()), "1": int((y == 1).sum())},
        "confusion_matrix": matrix,
        "roc_auc": auc,
        "roc_auc_reason": auc_reason,
        "accuracy": float((predictions == y.to_numpy()).mean()) if len(y) else None,
        "threshold": 0.5,
    }


def _make_base_strategy(request: BacktestRequest):
    entries, manager, exit_params = normalize_entries(request)
    return build_composite_entry(
        [{"strategy": entry.strategy, "params": entry.params} for entry in entries],
        manager.kind,
        manager.params,
        exit_params,
        request.symbol,
    )


def _run_partition(
    dataset: MLFilterDataset,
    source_request: BacktestRequest,
    partition: str,
    model: EntryClassifier | None,
    threshold: float = 0.5,
) -> dict[str, Any]:
    if partition == "validation":
        start = dataset.train_end
        end = dataset.validation_end
    elif partition == "lockbox":
        start = dataset.validation_end
        end_value = source_request.end or dataset.bars.index[-1].to_pydatetime()
        end = _as_utc(end_value)
    else:
        raise ValueError("Only validation and lockbox engine reruns are supported")
    end_engine = _as_engine_time(end)
    index = pd.DatetimeIndex(dataset.bars.index)
    utc_index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    bars = dataset.bars.loc[utc_index < end].copy()
    if bars.empty:
        raise ValueError(f"No frozen source bars are available for {partition}")
    bars.index = pd.DatetimeIndex([_as_engine_time(ts.to_pydatetime()) for ts in bars.index])
    market_columns = [name for name in ("open", "high", "low", "close", "tick_volume", "real_volume") if name in bars]
    bars = bars.loc[:, market_columns]
    base = _make_base_strategy(source_request)
    strategy = (
        base if model is None else EntryFilteredStrategy(base, model, threshold, entry_start=start, entry_end=end)
    )
    engine = BacktestEngine(
        strategy,
        build_position_sizer(source_request.position_sizing, point_value=source_request.point_value),
        initial_capital=source_request.initial_capital,
        point_values={source_request.symbol: source_request.point_value},
        day_trade=source_request.day_trade,
        day_trade_start_time=source_request.day_trade_start_time,
        day_trade_end_time=source_request.day_trade_end_time,
        day_trade_close_time=source_request.day_trade_close_time,
        costs=source_request.costs,
    )
    registry = engine.run(
        bars,
        parallel_mode=ParallelMode.SEQUENTIAL,
        trade_start=_as_engine_time(start),
        force_close_at_end=True,
    )
    trades = registry.get_closed_trades()
    result: dict[str, Any] = {
        "metrics": registry.get_performance_metrics(source_request.initial_capital),
        "trade_count": len(trades),
        "trades": [trade.model_dump(mode="json") for trade in trades],
        "candidate_count": strategy.candidate_count if isinstance(strategy, EntryFilteredStrategy) else None,
        "accepted_count": strategy.accepted_count if isinstance(strategy, EntryFilteredStrategy) else None,
        "not_ready_count": strategy.not_ready_count if isinstance(strategy, EntryFilteredStrategy) else None,
        "partition_start": start.isoformat(),
        "partition_end": end.isoformat(),
        "flat_start": True,
        "warmup_used": True,
        "end_close_policy": "force_close_last_bar",
    }
    return result


def _source_request(dataset: MLFilterDataset) -> BacktestRequest:
    return BacktestRequest.model_validate(dataset.source_config)


def compare_filters(request: Any, job_id: str | None = None) -> dict[str, Any]:
    payload = request.model_dump(mode="json") if hasattr(request, "model_dump") else dict(request)
    dataset_id = payload["dataset_id"]
    model_ids = payload["model_version_ids"]
    threshold = float(payload.get("threshold", 0.5))
    if not np.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("threshold must be finite and in [0, 1]")
    if not model_ids or len(model_ids) != len(set(model_ids)):
        raise ValueError("model_version_ids must be non-empty and distinct")
    dataset, _manifest = _dataset_from_snapshot(dataset_id)
    source_request = _source_request(dataset)
    baseline = _run_partition(dataset, source_request, "validation", None)
    baseline_artifacts = (
        write_ml_filter_partition_artifacts(
            job_id,
            "validation_baseline",
            baseline["trades"],
            initial_capital=source_request.initial_capital,
            start=baseline["partition_start"],
            end=baseline["partition_end"],
        )
        if job_id
        else None
    )
    results = []
    for model_id in model_ids:
        model_manifest = read_model_manifest(model_id)
        if model_manifest.get("dataset_id") != dataset_id:
            raise ValueError(f"Model '{model_id}' was trained from a different frozen dataset")
        model = load_model_version(model_id)
        filtered = _run_partition(dataset, source_request, "validation", model, threshold)
        filtered_artifacts = (
            write_ml_filter_partition_artifacts(
                job_id,
                f"validation_filtered_{model_id[:16]}",
                filtered["trades"],
                initial_capital=source_request.initial_capital,
                start=filtered["partition_start"],
                end=filtered["partition_end"],
            )
            if job_id
            else None
        )
        results.append(
            {
                "model_version_id": model_id,
                "threshold": threshold,
                "classification": _classification_report(dataset, model, "validation"),
                "baseline": baseline,
                "filtered": filtered,
                "baseline_artifacts": baseline_artifacts,
                "filtered_artifacts": filtered_artifacts,
            }
        )
    return {
        "job_id": job_id,
        "dataset_id": dataset_id,
        "partition": "validation",
        "lockbox_used": False,
        "results": results,
    }


def evaluate_filter(request: Any, job_id: str | None = None) -> dict[str, Any]:
    payload = request.model_dump(mode="json") if hasattr(request, "model_dump") else dict(request)
    dataset_id = payload["dataset_id"]
    model_id = payload["model_version_id"]
    threshold = float(payload.get("threshold", 0.5))
    if not np.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("threshold must be finite and in [0, 1]")
    dataset, _manifest = _dataset_from_snapshot(dataset_id)
    model_manifest = read_model_manifest(model_id)
    if model_manifest.get("dataset_id") != dataset_id:
        raise ValueError("Selected model was trained from a different frozen dataset")
    model = load_model_version(model_id)
    source_request = _source_request(dataset)
    baseline = _run_partition(dataset, source_request, "lockbox", None)
    filtered = _run_partition(dataset, source_request, "lockbox", model, threshold)
    baseline_artifacts = (
        write_ml_filter_partition_artifacts(
            job_id,
            "lockbox_baseline",
            baseline["trades"],
            initial_capital=source_request.initial_capital,
            start=baseline["partition_start"],
            end=baseline["partition_end"],
        )
        if job_id
        else None
    )
    filtered_artifacts = (
        write_ml_filter_partition_artifacts(
            job_id,
            f"lockbox_filtered_{model_id[:16]}",
            filtered["trades"],
            initial_capital=source_request.initial_capital,
            start=filtered["partition_start"],
            end=filtered["partition_end"],
        )
        if job_id
        else None
    )
    return {
        "job_id": job_id,
        "dataset_id": dataset_id,
        "model_version_id": model_id,
        "threshold": threshold,
        "partition": "lockbox",
        "historically_unseen": False,
        "lockbox_reserved_by_workflow": True,
        "classification": _classification_report(dataset, model, "lockbox"),
        "baseline": baseline,
        "filtered": filtered,
        "baseline_artifacts": baseline_artifacts,
        "filtered_artifacts": filtered_artifacts,
    }
