from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from q_backend.ml_filters.adapters import create_classifier
from q_backend.ml_filters.artifacts import save_dataset_artifacts, save_model_artifact
from q_backend.ml_filters.config import EntryFeatureConfig
from q_backend.ml_filters.dataset import build_source_dataset, partition_features
from q_backend.ml_filters.evaluation import compare_filters


def train_filters(request: Any, job_id: str, progress_callback=None) -> dict[str, Any]:
    payload = request.model_dump(mode="json") if hasattr(request, "model_dump") else dict(request)
    hyperparams = payload.get("hyperparameters") or {}
    if hasattr(hyperparams, "model_dump"):
        hyperparams = hyperparams.model_dump(exclude_none=True)
    algorithm_params = {
        name: (values.model_dump(exclude_none=True) if hasattr(values, "model_dump") else values)
        for name, values in hyperparams.items()
        if values is not None
    }
    train_end = payload["train_end"]
    validation_end = payload["validation_end"]
    if isinstance(train_end, str):
        train_end = datetime.fromisoformat(train_end.replace("Z", "+00:00"))
    if isinstance(validation_end, str):
        validation_end = datetime.fromisoformat(validation_end.replace("Z", "+00:00"))
    config = EntryFeatureConfig(
        train_end=train_end,
        validation_end=validation_end,
        feature_names=tuple(payload["selected_features"]),
        algorithms=tuple(payload["algorithms"]),
        hyperparameters=algorithm_params,
        seed=int(payload.get("seed", 42)),
    )
    if progress_callback:
        progress_callback(stage="dataset", progress={"current": 0, "total": None})
    dataset = build_source_dataset(payload["source_run_id"], config)
    dataset_paths = save_dataset_artifacts(dataset)
    X_train, y_train, train_samples = partition_features(dataset, "train")
    if progress_callback:
        progress_callback(
            stage="fitting",
            dataset_id=dataset.dataset_id,
            progress={"current": 0, "total": len(config.algorithms)},
            rejections=dataset.rejections,
        )

    published: list[dict[str, Any]] = []
    for index, algorithm in enumerate(config.algorithms, start=1):
        model = create_classifier(
            algorithm,
            algorithm_params.get(algorithm),
            seed=config.seed,
        )
        model.fit(X_train, y_train)
        artifact = save_model_artifact(
            dataset.dataset_id,
            model,
            selected_features=config.feature_names,
            training_label_availability_cutoff=max(sample.exit_time for sample in train_samples)
            .astimezone(timezone.utc)
            .isoformat(),
        )
        published.append({"algorithm": algorithm, **artifact})
        if progress_callback:
            progress_callback(stage="fitting", progress={"current": index, "total": len(config.algorithms)})

    if progress_callback:
        progress_callback(stage="validation", progress={"current": 0, "total": len(published)})
    comparison = compare_filters(
        {
            "dataset_id": dataset.dataset_id,
            "model_version_ids": [entry["model_version_id"] for entry in published],
            "threshold": 0.5,
        },
        job_id=job_id,
    )
    metrics_by_model = {row["model_version_id"]: row["classification"] for row in comparison["results"]}
    for entry in published:
        entry["validation_metrics"] = metrics_by_model[entry["model_version_id"]]
    return {
        "job_id": job_id,
        "dataset_id": dataset.dataset_id,
        "source_run_id": dataset.source_run_id,
        "symbol": dataset.source_config.get("symbol", ""),
        "timeframe": dataset.source_config.get("timeframe", ""),
        "train_end": config.train_end.astimezone(timezone.utc).isoformat(),
        "selected_features": list(dataset.selected_features),
        "model_versions": published,
        "comparison_id": job_id,
        "rejections": dataset.rejections,
        "sample_counts": {
            part: sum(sample.partition == part for sample in dataset.samples)
            for part in ("train", "validation", "lockbox")
        },
        "dataset_paths": dataset_paths,
        "validation_comparison": comparison,
    }
