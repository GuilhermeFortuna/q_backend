# GENERATED FILE - DO NOT EDIT. Source schemas: schema/catalog/dataset-manifest.schema.json, schema/catalog/ml-entry-filter-manifest.schema.json
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ChecksumHex = str


@dataclass(frozen=True)
class DatasetManifest:
    arrow_schema: dict[str, Any]
    checksum_algorithm: Literal["sha256", "sha512", "blake3", "md5"]
    dataset_id: str
    files: list[dict[str, Any]]
    published_at: str
    row_count: int
    state: Literal["publishing", "published", "tombstoned", "deleted"]
    subject: dict[str, Any]
    supersedes: str | None
    time_range: dict[str, Any]
    tombstone: dict[str, Any] | None
    version: int


FeatureDtype = Literal["float64", "int64", "int8"]


@dataclass(frozen=True)
class LabelDefinition:
    name: Literal["net_profitable_v1"]
    positive_class: Literal[1]


@dataclass(frozen=True)
class MlFilterDatasetManifest:
    bars_checksum: ChecksumHex
    compatibility_fingerprint: ChecksumHex
    dataset_content_id: ChecksumHex
    dataset_id: str
    engine_revision: str
    format_version: Literal[1]
    kind: Literal["ml_filter_dataset"]
    label: LabelDefinition
    partition_counts: PartitionCounts
    selected_features: list[OrderedFeature]
    source_config_revision: str
    source_run_id: str
    trades_checksum: ChecksumHex
    train_end: str
    validation_end: str


@dataclass(frozen=True)
class MlFilterModelManifest:
    algorithm: Literal["lightgbm", "random_forest", "logistic_regression"]
    dataset_id: str
    dependency_versions: dict[str, Any]
    fitted_artifact_checksum: ChecksumHex
    format_version: Literal[1]
    hyperparameters: dict[str, Any]
    kind: Literal["ml_filter_model"]
    model_content_id: ChecksumHex
    model_version_id: str
    preprocessing_recipe: list[PreprocessingStep]
    seed: int
    selected_features: list[OrderedFeature]
    training_label_availability_cutoff: str


@dataclass(frozen=True)
class OrderedFeature:
    dtype: FeatureDtype
    name: Literal[
        "open",
        "high",
        "low",
        "close",
        "tick_volume",
        "real_volume",
        "ma_short",
        "ma_long",
        "delta",
        "prev_delta",
        "side",
    ]


@dataclass(frozen=True)
class PartitionCounts:
    lockbox: SamplePartitionCounts
    train: SamplePartitionCounts
    validation: SamplePartitionCounts


@dataclass(frozen=True)
class PreprocessingStep:
    parameters: dict[str, Any]
    step: str


@dataclass(frozen=True)
class SamplePartitionCounts:
    rejections: dict[str, Any]
    samples: int


MlEntryFilterManifest = MlFilterDatasetManifest | MlFilterModelManifest
