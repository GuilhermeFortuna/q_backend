from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from q_backend.optimization.strategy_search import StrategySearchConfig

MAX_AB_SEEDS = 32
MAX_ABLATION_CONFIGS = 8

_DEFAULT_INPUT_FEATURES: tuple[str, ...] = (
    "rsi",
    "ma",
    "macd",
    "realized_vol",
)


class DiscoveryAbRequest(BaseModel):
    """Discovery A/B harness: paired latents-OFF vs latents-ON per seed."""

    config: StrategySearchConfig
    seeds: list[int] = Field(min_length=1, max_length=MAX_AB_SEEDS)

    @model_validator(mode="after")
    def validate_request(self) -> "DiscoveryAbRequest":
        if self.config.genetic is None:
            raise ValueError("Discovery A/B requires genetic search config")
        if len(self.seeds) != len(set(self.seeds)):
            raise ValueError("seeds must be unique")
        return self


class DiscoveryAbArmSummary(BaseModel):
    values: list[float]
    mean: float


class DiscoveryAbPairedDelta(BaseModel):
    values: list[float]
    mean: float
    cohens_d: float
    p_value: float


class DiscoveryAbResult(BaseModel):
    verdict: Literal["helps", "no_effect", "hurts"]
    n_seeds: int
    metric: Literal["lockbox_objective", "oos_objective"]
    control: DiscoveryAbArmSummary
    treatment: DiscoveryAbArmSummary
    paired_delta: DiscoveryAbPairedDelta
    child_runs: list[dict[str, Any]] = Field(default_factory=list)


class DiscoveryAbStartResponse(BaseModel):
    job_id: str
    status: str


class DiscoveryAbStatusResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    progress: float = 0.0
    detail: Optional[str] = None
    result: Optional[DiscoveryAbResult] = None
    error: Optional[str] = None


class EncoderConfigSpec(BaseModel):
    label: str
    encoder_kind: Literal["pca", "ae"]
    hyperparams: dict[str, Any] = Field(default_factory=dict)


class EncoderAblationRequest(BaseModel):
    symbol: str
    timeframe: str
    target: str = "fwd_return"
    horizon: int = Field(default=5, ge=1)
    train_start: datetime
    train_end: datetime
    n_latents: int = Field(default=2, ge=1)
    input_features: list[str] = Field(default_factory=lambda: list(_DEFAULT_INPUT_FEATURES))
    configs: list[EncoderConfigSpec]

    @model_validator(mode="after")
    def validate_ablation_request(self) -> "EncoderAblationRequest":
        if self.train_end <= self.train_start:
            raise ValueError("train_end must be after train_start")
        if not self.input_features:
            raise ValueError("input_features must be non-empty")
        count = len(self.configs)
        if count < 1 or count > MAX_ABLATION_CONFIGS:
            raise ValueError(
                f"configs must contain between 1 and {MAX_ABLATION_CONFIGS} items"
            )
        labels = [spec.label for spec in self.configs]
        if len(labels) != len(set(labels)):
            raise ValueError("config labels must be unique")
        return self


class EncoderAblationRow(BaseModel):
    label: str
    encoder_kind: Literal["pca", "ae"]
    model_hash: Optional[str] = None
    recon_r2: Optional[float] = None
    best_latent_ic: Optional[float] = None
    baseline_ic: Optional[float] = None
    ic_delta_vs_baseline: Optional[float] = None
    passed: Optional[bool] = None
    gate_error: Optional[str] = None


class EncoderAblationResult(BaseModel):
    rows: list[EncoderAblationRow]
    best_label: Optional[str] = None
    symbol: str
    timeframe: str
    target: str
    horizon: int


class EncoderAblationStartResponse(BaseModel):
    job_id: str
    status: str


class EncoderAblationStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: Optional[str] = None
    result: Optional[EncoderAblationResult] = None
    error: Optional[str] = None
