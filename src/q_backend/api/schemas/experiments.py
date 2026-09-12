from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from q_backend.optimization.strategy_search import StrategySearchConfig
from q_backend.optimization.walkforward import WalkForwardConfig

MAX_AB_SEEDS = 32
MAX_ABLATION_CONFIGS = 8
DEFAULT_MINIMUM_COMPLETE_PAIRS = 2

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
    minimum_complete_pairs: int = Field(
        default=DEFAULT_MINIMUM_COMPLETE_PAIRS,
        ge=1,
        le=MAX_AB_SEEDS,
    )

    @model_validator(mode="after")
    def validate_request(self) -> "DiscoveryAbRequest":
        if self.config.genetic is None:
            raise ValueError("Discovery A/B requires genetic search config")
        if len(self.seeds) != len(set(self.seeds)):
            raise ValueError("seeds must be unique")
        if self.minimum_complete_pairs > len(self.seeds):
            raise ValueError("minimum_complete_pairs cannot exceed the number of requested seeds")
        return self


class DiscoveryAbArmSummary(BaseModel):
    values: list[float]
    mean: Optional[float] = None


class DiscoveryAbPairedDelta(BaseModel):
    values: list[float]
    mean: Optional[float] = None
    cohens_d: Optional[float] = None
    p_value: Optional[float] = None


class DiscoveryAbResult(BaseModel):
    verdict: Literal["helps", "no_effect", "hurts", "inconclusive"]
    n_seeds: int
    requested_seeds: int
    complete_pairs: int
    minimum_complete_pairs: int = DEFAULT_MINIMUM_COMPLETE_PAIRS
    dropped_pair_reasons: list[str] = Field(default_factory=list)
    metric: Literal["lockbox_objective", "oos_objective"]
    control: DiscoveryAbArmSummary
    treatment: DiscoveryAbArmSummary
    paired_delta: DiscoveryAbPairedDelta
    child_runs: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_payload(cls, data: Any) -> Any:
        """Preserve readability of stored reports written before WO166."""
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        if "complete_pairs" not in migrated and "n_seeds" in migrated:
            migrated["complete_pairs"] = migrated["n_seeds"]
        if "requested_seeds" not in migrated:
            child_runs = migrated.get("child_runs") or []
            seeds = {child.get("seed") for child in child_runs if child.get("seed") is not None}
            migrated["requested_seeds"] = len(seeds) if seeds else migrated.get("n_seeds", 0)
        if "minimum_complete_pairs" not in migrated:
            migrated["minimum_complete_pairs"] = DEFAULT_MINIMUM_COMPLETE_PAIRS
        if "dropped_pair_reasons" not in migrated:
            migrated["dropped_pair_reasons"] = []
        return migrated


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
            raise ValueError(f"configs must contain between 1 and {MAX_ABLATION_CONFIGS} items")
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


class AlphaResearchComputeBudget(BaseModel):
    """Bounded compute overrides; profile acceptance thresholds are not mutable."""

    study_n_trials: int | None = Field(default=None, ge=1, le=500)
    optimization_seeds: int | None = Field(default=None, ge=1, le=10)
    walkforward: WalkForwardConfig | None = None

    @model_validator(mode="after")
    def reject_threshold_overrides(self) -> "AlphaResearchComputeBudget":
        forbidden = {
            key for key, value in self.model_dump().items() if key.startswith("min_") or key.endswith("_threshold")
        }
        if forbidden:
            raise ValueError("Alpha-research compute budget cannot override profile acceptance thresholds.")
        return self


class AlphaResearchRequest(BaseModel):
    profile_id: str
    start: datetime
    end: datetime
    catalog_version: int | None = Field(default=None, ge=1)
    profile_version: int | None = Field(default=None, ge=1)
    target_name: str = "fwd_return"
    compute_budget: AlphaResearchComputeBudget | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "AlphaResearchRequest":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class AlphaResearchStageStatus(BaseModel):
    name: str
    status: Literal["pending", "running", "completed", "failed", "skipped"]
    detail: str | None = None


class AlphaResearchResult(BaseModel):
    verdict: Literal["ready_for_paper", "inconclusive", "rejected"]
    profile_id: str
    provenance: dict[str, Any] = Field(default_factory=dict)
    split_manifest: dict[str, Any] | None = None
    feature_evidence_summary: list[dict[str, Any]] = Field(default_factory=list)
    hypothesis_manifest: list[dict[str, Any]] = Field(default_factory=list)
    champion: dict[str, Any] | None = None
    acceptance: dict[str, Any] | None = None
    stages: list[AlphaResearchStageStatus] = Field(default_factory=list)
    inconclusive_reasons: list[str] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)


class AlphaResearchStartResponse(BaseModel):
    job_id: str
    status: str


class AlphaResearchStatusResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    progress: float = 0.0
    detail: str | None = None
    stages: list[AlphaResearchStageStatus] = Field(default_factory=list)
    result: AlphaResearchResult | None = None
    error: str | None = None
    checkpoint: dict[str, Any] | None = None
