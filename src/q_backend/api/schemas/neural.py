from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from q_backend.storage.db.models import NeuralModelStatus


class LatentGateResultResponse(BaseModel):
    evaluation_run_id: str
    baseline_ic: float
    best_latent_ic: float
    n_latents_beating_baseline: int
    passed: bool
    target_name: Optional[str] = None
    target_horizon: Optional[int] = None


class NeuralModelListItem(BaseModel):
    model_hash: str
    model_key: str
    symbol: str
    timeframe: str
    version: int
    status: str
    n_latents: int
    created_at: datetime
    val_metrics: dict[str, Any]


class NeuralModelListResponse(BaseModel):
    models: list[NeuralModelListItem]


class NeuralModelDetailResponse(NeuralModelListItem):
    train_start: datetime
    train_end: datetime
    latent_names: list[str]
    gate_result: Optional[LatentGateResultResponse] = None


class NeuralModelStatusUpdateRequest(BaseModel):
    status: NeuralModelStatus


class NeuralTrainEvaluateRequest(BaseModel):
    target: str
    horizon: int = Field(ge=1)


class NeuralTrainRequest(BaseModel):
    kind: Literal["pca", "autoencoder"] = "pca"
    symbol: str
    timeframe: str
    train_start: datetime
    train_end: datetime
    n_latents: int
    input_features: list[str]
    model_key: Optional[str] = None
    evaluate: Optional[NeuralTrainEvaluateRequest] = None
    hyperparams: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_training_window(self) -> "NeuralTrainRequest":
        if self.train_end <= self.train_start:
            raise ValueError("train_end must be after train_start")
        if self.n_latents < 1:
            raise ValueError("n_latents must be >= 1")
        if not self.input_features:
            raise ValueError("input_features must be non-empty")
        return self


class NeuralTrainStartResponse(BaseModel):
    job_id: str
    status: str


class NeuralTrainStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: Optional[str] = None
    model_hash: Optional[str] = None
    val_metrics: Optional[dict[str, Any]] = None
    gate: Optional[dict[str, Any]] = None
    gate_error: Optional[str] = None
    error: Optional[str] = None
