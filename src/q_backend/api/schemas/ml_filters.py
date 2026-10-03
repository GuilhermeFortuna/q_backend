from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from q_backend.ml_filters.features import FEATURE_ALLOWLIST

Algorithm = Literal["lightgbm", "random_forest", "logistic_regression"]
JobStatus = Literal["queued", "running", "completed", "failed"]
JobStage = Literal["dataset", "fitting", "validation", "evaluation", "persisting"]


class LightgbmHyperparams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n_estimators: int = Field(default=100, ge=1, le=2000, strict=True)
    learning_rate: float = Field(default=0.1, gt=0, le=1)
    num_leaves: int = Field(default=31, ge=2, le=256, strict=True)


class RandomForestHyperparams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n_estimators: int = Field(default=200, ge=1, le=2000, strict=True)
    max_depth: int | None = Field(default=None, ge=1, le=100, strict=True)
    min_samples_leaf: int = Field(default=1, ge=1, le=100, strict=True)


class LogisticRegressionHyperparams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    C: float = Field(default=1.0, gt=0, le=1_000_000)
    max_iter: int = Field(default=1000, ge=100, le=10_000, strict=True)


class AlgorithmHyperparams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lightgbm: LightgbmHyperparams | None = None
    random_forest: RandomForestHyperparams | None = None
    logistic_regression: LogisticRegressionHyperparams | None = None


class TrainingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_run_id: str = Field(min_length=1)
    train_end: datetime
    validation_end: datetime
    selected_features: list[str] = Field(min_length=1)
    algorithms: list[Algorithm] = Field(min_length=1)
    hyperparameters: AlgorithmHyperparams = Field(default_factory=AlgorithmHyperparams)
    seed: int = Field(default=42, ge=0, le=0xFFFFFFFF, strict=True)

    @model_validator(mode="after")
    def validate_training_config(self):
        if self.train_end.tzinfo is None or self.validation_end.tzinfo is None:
            raise ValueError("train_end and validation_end must be timezone-aware timestamps")
        if self.train_end.astimezone(timezone.utc) >= self.validation_end.astimezone(timezone.utc):
            raise ValueError("train_end must be earlier than validation_end")
        if len(self.selected_features) != len(set(self.selected_features)):
            raise ValueError("selected_features must be distinct and ordered")
        if "side" not in self.selected_features or len(self.selected_features) < 2:
            raise ValueError("selected_features must include side and at least one other feature")
        if set(self.selected_features) - set(FEATURE_ALLOWLIST):
            raise ValueError("selected_features contains an unsupported feature")
        if len(self.algorithms) != len(set(self.algorithms)):
            raise ValueError("algorithms must not contain duplicates")
        hyperparam_keys = {
            name
            for name in ("lightgbm", "random_forest", "logistic_regression")
            if getattr(self.hyperparameters, name) is not None
        }
        if hyperparam_keys - set(self.algorithms):
            raise ValueError("hyperparameters can only be provided for selected algorithms")
        return self


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str = Field(min_length=1)
    model_version_ids: list[str] = Field(min_length=1)
    threshold: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def validate_models(self):
        if len(self.model_version_ids) != len(set(self.model_version_ids)):
            raise ValueError("model_version_ids must not contain duplicates")
        return self


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str = Field(min_length=1)
    model_version_id: str = Field(min_length=1)
    threshold: float = Field(default=0.5, ge=0, le=1)


class JobStartResponse(BaseModel):
    job_id: str
    status: JobStatus


class SourceSummary(BaseModel):
    run_id: str
    eligible: bool
    symbol: str
    timeframe: str
    strategy: str = ""
    date_range_start: datetime | None = None
    date_range_end: datetime | None = None
    source_sample_count: int = 0
    available_features: list[str] = Field(default_factory=list)
    eligibility_reason: str | None = None


class SourceListResponse(BaseModel):
    items: list[SourceSummary]
    total: int
    limit: int
    offset: int


class SplitSuggestion(BaseModel):
    train_end: datetime
    validation_end: datetime


class SourceDetailResponse(SourceSummary):
    eligibility_errors: list[str] = Field(default_factory=list)
    split_suggestion: SplitSuggestion | None = None
    volume_readiness: str | None = None


class TrainingStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    stage: JobStage | None = None
    progress: dict[str, int | None] | None = None
    dataset_id: str | None = None
    model_version_ids: list[str] = Field(default_factory=list)
    comparison_id: str | None = None
    rejections: dict[str, dict[str, int]] | None = None
    error: dict[str, Any] | None = None


class ModelSummary(BaseModel):
    model_version_id: str
    dataset_id: str
    algorithm: Algorithm
    ready: bool
    selected_features: list[str] = Field(default_factory=list)
    symbol: str = ""
    timeframe: str = ""
    train_end: datetime | None = None
    compatibility_reasons: list[str] = Field(default_factory=list)


class ModelListResponse(BaseModel):
    items: list[ModelSummary]
    total: int
    limit: int
    offset: int


class ModelDetailResponse(BaseModel):
    model_version_id: str
    dataset_id: str
    algorithm: Algorithm
    selected_features: list[str] = Field(default_factory=list)
    manifest_identity: dict[str, Any] = Field(default_factory=dict)
    pipeline_versions: dict[str, str] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    validation_metrics: dict[str, Any] = Field(default_factory=dict)


class ComparisonStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    results: list[dict[str, Any]] | None = None
    error: dict[str, Any] | None = None


class EvaluationStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    dataset_id: str | None = None
    model_version_id: str | None = None
    threshold: float | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
