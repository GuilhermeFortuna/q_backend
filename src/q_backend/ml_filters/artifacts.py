from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

from q_backend.ml_filters.adapters import EntryClassifier, FittedEntryModel, SklearnEntryClassifier
from q_backend.ml_filters.config import MLFilterDataset
from q_backend.storage.lake.artifacts import lake_root


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def _commit_directory(temp_dir: Path, target: Path) -> None:
    try:
        os.rename(temp_dir, target)
    except FileExistsError:
        shutil.rmtree(temp_dir)


def _dataset_directory(dataset_id: str) -> Path:
    return lake_root() / "ml_filters" / "datasets" / dataset_id


def save_dataset_artifacts(dataset: MLFilterDataset) -> dict[str, str]:
    """Publish a source snapshot atomically; the manifest is the ready marker."""
    from q_contracts.catalog import (
        LabelDefinition,
        MlFilterDatasetManifest,
        OrderedFeature,
        PartitionCounts,
        SamplePartitionCounts,
    )

    target = _dataset_directory(dataset.dataset_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if (target / "manifest.json").is_file():
        existing = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        if existing.get("dataset_content_id") != dataset.dataset_id:
            raise ValueError("Existing frozen dataset manifest has a conflicting identity")
        _verify_dataset_snapshot(target)
        return {
            "bars": f"ml_filters/datasets/{dataset.dataset_id}/bars.parquet",
            "samples": f"ml_filters/datasets/{dataset.dataset_id}/samples.parquet",
            "source_config": f"ml_filters/datasets/{dataset.dataset_id}/source-config.json",
            "manifest": f"ml_filters/datasets/{dataset.dataset_id}/manifest.json",
        }
    if target.exists():
        shutil.rmtree(target)

    temp_dir = Path(tempfile.mkdtemp(prefix=f".{dataset.dataset_id}.", dir=target.parent))
    try:
        dataset.bars.to_parquet(temp_dir / "bars.parquet", index=True)
        sample_frame = pd.DataFrame([asdict(sample) for sample in dataset.samples])
        sample_frame.to_parquet(temp_dir / "samples.parquet", index=False)
        (temp_dir / "source-config.json").write_text(
            json.dumps(dataset.source_config, sort_keys=True, default=str), encoding="utf-8"
        )
        snapshot_checksums = {
            name: _sha256((temp_dir / name).read_bytes())
            for name in ("bars.parquet", "samples.parquet", "source-config.json")
        }
        _atomic_json(temp_dir / "snapshot-checksums.json", snapshot_checksums)
        sample_counts = {
            part: SamplePartitionCounts(
                samples=sum(sample.partition == part for sample in dataset.samples),
                rejections=dataset.rejections.get(part, {}),
            )
            for part in ("train", "validation", "lockbox")
        }
        feature_specs = [
            OrderedFeature(name=name, dtype="int8" if name == "side" else "float64")
            for name in dataset.selected_features
        ]
        try:
            engine_revision = importlib.metadata.version("q-core")
        except importlib.metadata.PackageNotFoundError:
            engine_revision = "unknown"
        manifest = MlFilterDatasetManifest(
            bars_checksum=dataset.bars_checksum,
            compatibility_fingerprint=dataset.compatibility_fingerprint,
            dataset_content_id=dataset.dataset_id,
            dataset_id=dataset.dataset_id,
            engine_revision=engine_revision,
            format_version=1,
            kind="ml_filter_dataset",
            label=LabelDefinition(name="net_profitable_v1", positive_class=1),
            partition_counts=PartitionCounts(**sample_counts),
            selected_features=feature_specs,
            source_config_revision=dataset.source_config_revision,
            source_run_id=dataset.source_run_id,
            trades_checksum=dataset.trades_checksum,
            train_end=dataset.train_end.isoformat(),
            validation_end=dataset.validation_end.isoformat(),
        )
        manifest_path = temp_dir / "manifest.json"
        _atomic_json(manifest_path, asdict(manifest))
        _commit_directory(temp_dir, target)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise
    return {
        "bars": f"ml_filters/datasets/{dataset.dataset_id}/bars.parquet",
        "samples": f"ml_filters/datasets/{dataset.dataset_id}/samples.parquet",
        "source_config": f"ml_filters/datasets/{dataset.dataset_id}/source-config.json",
        "manifest": f"ml_filters/datasets/{dataset.dataset_id}/manifest.json",
    }


def _verify_dataset_snapshot(directory: Path) -> None:
    checksums_path = directory / "snapshot-checksums.json"
    if not checksums_path.is_file():
        raise ValueError("Frozen ML filter dataset is missing snapshot checksums")
    checksums = json.loads(checksums_path.read_text(encoding="utf-8"))
    for filename in ("bars.parquet", "samples.parquet", "source-config.json"):
        path = directory / filename
        if not path.is_file() or _sha256(path.read_bytes()) != checksums.get(filename):
            raise ValueError(f"Frozen ML filter dataset checksum mismatch: {filename}")


def load_dataset_snapshot(
    dataset_id: str,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Load only a complete, checksummed published dataset snapshot."""
    import re

    from q_contracts.catalog import MlFilterDatasetManifest

    if re.fullmatch(r"[0-9a-f]{64}", dataset_id) is None:
        raise ValueError("dataset_id must be a 64-character lowercase SHA-256 id")
    directory = _dataset_directory(dataset_id)
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Published ML filter dataset '{dataset_id}' was not found")
    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = MlFilterDatasetManifest(**manifest_data)
    except Exception as exc:
        raise ValueError("Published ML filter dataset manifest is invalid") from exc
    if manifest.kind != "ml_filter_dataset" or manifest.format_version != 1:
        raise ValueError("Unsupported ML filter dataset manifest")
    if manifest.dataset_id != dataset_id or manifest.dataset_content_id != dataset_id:
        raise ValueError("Dataset manifest identity does not match the requested dataset")
    _verify_dataset_snapshot(directory)
    return (
        manifest_data,
        pd.read_parquet(directory / "bars.parquet"),
        pd.read_parquet(directory / "samples.parquet"),
        json.loads((directory / "source-config.json").read_text(encoding="utf-8")),
    )


def read_dataset_manifest(dataset_id: str) -> dict[str, Any]:
    """Read one published dataset manifest without loading its snapshot frames."""
    from q_contracts.catalog import MlFilterDatasetManifest

    if re.fullmatch(r"[0-9a-f]{64}", dataset_id) is None:
        raise ValueError("dataset_id must be a 64-character lowercase SHA-256 id")
    manifest_path = _dataset_directory(dataset_id) / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Published ML filter dataset '{dataset_id}' was not found")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = MlFilterDatasetManifest(**data)
    except Exception as exc:
        raise ValueError("Published ML filter dataset manifest is invalid") from exc
    if manifest.dataset_id != dataset_id or manifest.kind != "ml_filter_dataset":
        raise ValueError("Dataset manifest identity does not match the requested dataset")
    return data


def _library_versions() -> dict[str, str]:
    names = ("lightgbm", "scikit-learn", "joblib", "numpy", "pandas")
    versions: dict[str, str] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "unavailable"
    return versions


def write_ml_filter_result(job_id: str, payload: dict[str, Any]) -> str:
    if re.fullmatch(r"[0-9a-f-]{32,36}", job_id) is None:
        raise ValueError("job_id is malformed")
    relative = Path("ml_filters") / "jobs" / job_id / "result.json"
    target = lake_root() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(target, payload)
    return relative.as_posix()


def read_ml_filter_result(relative_path: str) -> dict[str, Any]:
    root = (lake_root() / "ml_filters" / "jobs").resolve()
    path = (lake_root() / relative_path).resolve()
    if root not in path.parents or path.name != "result.json" or not path.is_file():
        raise FileNotFoundError("ML filter result artifact is unavailable")
    return json.loads(path.read_text(encoding="utf-8"))


def write_ml_filter_partition_artifacts(
    job_id: str,
    key: str,
    trades: list[dict[str, Any]],
    *,
    initial_capital: float,
    start: str,
    end: str,
) -> dict[str, str]:
    """Atomically persist engine trade and equity outputs as lake artifacts."""
    if re.fullmatch(r"[0-9a-f-]{32,36}", job_id) is None or re.fullmatch(r"[0-9a-z_-]{1,100}", key) is None:
        raise ValueError("ML filter partition artifact key is malformed")
    parent = lake_root() / "ml_filters" / "jobs" / job_id / "artifacts"
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / key
    if (target / "equity.parquet").is_file() and (target / "trades.parquet").is_file():
        return {
            "equity": f"ml_filters/jobs/{job_id}/artifacts/{key}/equity.parquet",
            "trades": f"ml_filters/jobs/{job_id}/artifacts/{key}/trades.parquet",
        }
    if target.exists():
        shutil.rmtree(target)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{key}.", dir=parent))
    try:
        trade_frame = pd.DataFrame(trades)
        trade_frame.to_parquet(temp_dir / "trades.parquet", index=False)
        start_time = pd.Timestamp(start)
        end_time = pd.Timestamp(end)
        points = [(start_time, float(initial_capital))]
        equity = float(initial_capital)
        for trade in sorted(trades, key=lambda row: row.get("exit_time") or row.get("entry_time", "")):
            exit_time = trade.get("exit_time")
            if exit_time is None:
                continue
            stamp = pd.Timestamp(exit_time)
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize("America/Sao_Paulo")
            stamp = stamp.tz_convert("UTC")
            equity += float(trade.get("pnl") or 0.0)
            points.append((stamp, equity))
        points.append((end_time, equity))
        equity_frame = pd.DataFrame(
            {"time": [stamp.isoformat() for stamp, _value in points], "equity": [value for _stamp, value in points]}
        )
        equity_frame.to_parquet(temp_dir / "equity.parquet", index=False)
        _commit_directory(temp_dir, target)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise
    return {
        "equity": f"ml_filters/jobs/{job_id}/artifacts/{key}/equity.parquet",
        "trades": f"ml_filters/jobs/{job_id}/artifacts/{key}/trades.parquet",
    }


def save_model_artifact(
    dataset_id: str,
    classifier: EntryClassifier,
    *,
    selected_features: tuple[str, ...],
    training_label_availability_cutoff: str,
) -> dict[str, Any]:
    """Store an immutable, checksummed fitted pipeline and manifest."""
    from q_contracts.catalog import MlFilterModelManifest, OrderedFeature, PreprocessingStep

    if not isinstance(classifier, SklearnEntryClassifier):
        raise ValueError("Only registered Q ML filter adapters can be persisted")
    if classifier.feature_names != selected_features:
        raise ValueError("Model feature order does not match the frozen dataset")
    artifact = classifier.dump()
    checksum = _sha256(artifact)
    algorithm = classifier.algorithm
    preprocessing = [
        PreprocessingStep(
            step="standard_scaler" if algorithm == "logistic_regression" else "identity",
            parameters={},
        )
    ]
    versions = _library_versions()
    model_content_id = hashlib.sha256(
        json.dumps(
            {
                "dataset_id": dataset_id,
                "algorithm": algorithm,
                "hyperparameters": classifier.hyperparameters,
                "seed": classifier.seed,
                "features": selected_features,
                "preprocessing": [asdict(step) for step in preprocessing],
                "dependency_versions": versions,
                "fitted_artifact_checksum": checksum,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    manifest = MlFilterModelManifest(
        algorithm=algorithm,
        dataset_id=dataset_id,
        dependency_versions=versions,
        fitted_artifact_checksum=checksum,
        format_version=1,
        hyperparameters=classifier.hyperparameters,
        kind="ml_filter_model",
        model_content_id=model_content_id,
        model_version_id=model_content_id,
        preprocessing_recipe=preprocessing,
        seed=classifier.seed,
        selected_features=[
            OrderedFeature(name=name, dtype="int8" if name == "side" else "float64") for name in selected_features
        ],
        training_label_availability_cutoff=training_label_availability_cutoff,
    )
    target = lake_root() / "ml_filters" / "models" / dataset_id / model_content_id
    target.parent.mkdir(parents=True, exist_ok=True)
    if (target / "manifest.json").is_file():
        published = load_model_version(model_content_id)
        if published.algorithm != classifier.algorithm:
            raise ValueError("Existing model artifact conflicts with fitted model")
        return {"model_version_id": model_content_id, "artifact_path": str(target.relative_to(lake_root()))}
    if target.exists():
        shutil.rmtree(target)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{model_content_id}.", dir=target.parent))
    try:
        artifact_path = temp_dir / "model.joblib"
        artifact_path.write_bytes(artifact)
        _atomic_json(temp_dir / "manifest.json", asdict(manifest))
        _commit_directory(temp_dir, target)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise
    return {"model_version_id": model_content_id, "artifact_path": str(target.relative_to(lake_root()))}


def load_model_version(model_version_id: str) -> FittedEntryModel:
    """Load one published model by its immutable content ID after checksum checks."""
    if re.fullmatch(r"[0-9a-f]{64}", model_version_id) is None:
        raise ValueError("model_version_id must be a 64-character lowercase SHA-256 id")
    manifest_data, artifact = _read_model_payload(model_version_id)
    from q_contracts.catalog import MlFilterModelManifest

    try:
        manifest = MlFilterModelManifest(**manifest_data)
    except Exception as exc:
        raise ValueError("Published ML filter model manifest is invalid") from exc
    if manifest.kind != "ml_filter_model" or manifest.format_version != 1:
        raise ValueError("Unsupported ML filter model manifest")
    if _sha256(artifact) != manifest.fitted_artifact_checksum:
        raise ValueError("Published ML filter model checksum mismatch")
    for name, expected in manifest.dependency_versions.items():
        if expected == "unavailable":
            continue
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ValueError(f"Model runtime dependency '{name}' is unavailable") from exc
        if actual != expected:
            raise ValueError(f"Model runtime dependency '{name}' version mismatch")
    classifier = SklearnEntryClassifier.load(artifact)
    if classifier.algorithm != manifest.algorithm or list(classifier.feature_names) != [
        feature["name"] for feature in manifest.selected_features
    ]:
        raise ValueError("Fitted model and manifest metadata disagree")
    if manifest.model_version_id != model_version_id or manifest.model_content_id != model_version_id:
        raise ValueError("Model manifest identity does not match the requested version")
    return FittedEntryModel(
        model_version_id=model_version_id,
        dataset_id=manifest.dataset_id,
        classifier=classifier,
    )


def _read_model_payload(model_version_id: str) -> tuple[dict[str, Any], bytes]:
    root = lake_root() / "ml_filters" / "models"
    matches = list(root.glob(f"*/{model_version_id}"))
    if len(matches) != 1:
        raise FileNotFoundError(f"Published ML filter model '{model_version_id}' was not found")
    directory = matches[0]
    manifest_path = directory / "manifest.json"
    artifact_path = directory / "model.joblib"
    if not manifest_path.is_file() or not artifact_path.is_file():
        raise ValueError("Published ML filter model artifact is incomplete")
    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("Published ML filter model manifest is invalid") from exc
    return manifest_data, artifact_path.read_bytes()


def read_model_manifest(model_version_id: str) -> dict[str, Any]:
    manifest, artifact = _read_model_payload(model_version_id)
    if _sha256(artifact) != manifest.get("fitted_artifact_checksum"):
        raise ValueError("Published ML filter model checksum mismatch")
    return manifest
