from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel

from q_backend.storage.db.models import FeatureStatus


class FeatureListItem(BaseModel):
    name: str
    category: str
    latest_version: int
    status: str
    usage_count: int
    score: Optional[float] = None


class FeatureListResponse(BaseModel):
    features: list[FeatureListItem]


class FeatureVersionDetail(BaseModel):
    version: int
    status: str
    node_kind: str
    param_keys: list[str]
    default_params: dict[str, Any]
    forward_window: int
    leakage_status: str
    provenance: dict[str, Any]


class FeaturePassportResponse(BaseModel):
    name: str
    category: str
    description: Optional[str] = None
    usage_count: int
    versions: list[FeatureVersionDetail]
    score: Optional[float] = None
    evaluation_history: list[Any]


class FeatureEvalTargetRequest(BaseModel):
    name: str
    horizon: int


class FeatureEvalFeatureRequest(BaseModel):
    name: str
    version: Optional[int] = None
    params: dict[str, Any] = {}


class FeatureEvalCreateRequest(BaseModel):
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    target: FeatureEvalTargetRequest
    features: list[FeatureEvalFeatureRequest]


class FeatureEvalStartResponse(BaseModel):
    run_id: str
    status: str


class FeatureEvalLeaderboardItem(BaseModel):
    feature_id: str
    feature_name: str
    ic: Optional[float] = None
    rank_ic: Optional[float] = None
    mutual_info: Optional[float] = None
    stability: Optional[float] = None
    global_score: Optional[float] = None
    cluster_id: int
    is_representative: bool
    leakage_status: str
    regime_ics: dict[str, Any]


class FeatureEvalClusterItem(BaseModel):
    cluster_id: int
    feature_ids: list[str]
    representative: str


class FeatureEvalHeatmapRow(BaseModel):
    feature_id: str
    feature_name: str
    ic: Optional[float] = None
    rank_ic: Optional[float] = None
    mutual_info: Optional[float] = None
    stability: Optional[float] = None


class FeatureEvalHeatmap(BaseModel):
    metrics: list[str]
    rows: list[FeatureEvalHeatmapRow]


class FeatureEvalRunResponse(BaseModel):
    run_id: str
    status: str
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    target_name: str
    target_horizon: int
    matrix_id: str
    feature_count: int
    result_summary: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    leaderboard: list[FeatureEvalLeaderboardItem]
    clusters: list[FeatureEvalClusterItem]
    heatmap: FeatureEvalHeatmap


class FeatureLeaderboardItem(BaseModel):
    feature_name: str
    global_score: float


class FeatureLeaderboardResponse(BaseModel):
    features: list[FeatureLeaderboardItem]


class FeatureStatusUpdateRequest(BaseModel):
    status: FeatureStatus
