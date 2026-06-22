from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel

from q_backend.api.schemas.backtest import EquityArtifactPoint


class StrategySearchStartResponse(BaseModel):
    run_id: str
    status: str


class StrategySearchStatusResponse(BaseModel):
    run_id: str
    status: str
    current_candidate: float
    total_candidates: int
    candidate_id: Optional[str] = None
    strategy: Optional[str] = None
    phase: Optional[Literal["optimizing", "testing", "done"]] = None
    window_index: Optional[int] = None
    total_windows: Optional[int] = None
    generation: Optional[int] = None
    total_generations: Optional[int] = None
    error: Optional[str] = None
    search_config: Optional[Dict[str, Any]] = None
    backtest_config: Optional[Dict[str, Any]] = None
    logs: Optional[List[str]] = None


class StrategySearchCandidateResponse(BaseModel):
    candidate_id: str
    strategy: str
    status: str
    rank: Optional[int] = None
    objective_value: Optional[float] = None
    robustness_score: Optional[float] = None
    efficiency: Optional[float] = None
    gate_flags: List[str] = []
    passed_gates: bool = False
    oos_metrics: Optional[Dict[str, Any]] = None
    is_metrics_summary: Optional[Dict[str, Any]] = None
    best_params: Optional[Dict[str, Any]] = None
    window_count: int = 0
    completed_windows: int = 0
    error: Optional[str] = None
    generation: Optional[int] = None
    genome: Optional[Dict[str, Any]] = None
    genome_node_count: Optional[int] = None
    dsr: Optional[float] = None
    complexity_penalty: Optional[float] = None
    exit_preset_id: Optional[str] = None
    exit_preset_label: Optional[str] = None
    exit_param_names: Optional[List[str]] = None
    exit_policy_id: Optional[str] = None
    exit_policy_label: Optional[str] = None
    last_exit_mutation_op: Optional[str] = None
    exit_quality: Optional[Dict[str, Any]] = None
    diagnostics: Optional[Dict[str, Any]] = None


class StrategySearchResultsResponse(BaseModel):
    run_id: str
    status: str
    objective_mode: Optional[str] = None
    summary: Dict[str, Any] = {}
    candidates: List[StrategySearchCandidateResponse]
    best: Optional[StrategySearchCandidateResponse] = None
    search_config: Optional[Dict[str, Any]] = None
    lake_paths: Optional[Dict[str, Any]] = None


class StrategySearchRunListItem(BaseModel):
    run_id: str
    name: str
    status: str
    symbol: Optional[str] = None
    candidate_count: int = 0
    best_strategy: Optional[str] = None
    best_objective_value: Optional[float] = None
    created_at: datetime


class StrategySearchRunListResponse(BaseModel):
    items: List[StrategySearchRunListItem]
    total: int
    limit: int
    offset: int


class StrategySearchCandidateEquityArtifactResponse(BaseModel):
    run_id: str
    candidate_id: str
    points: List[EquityArtifactPoint]


class StrategySearchCandidateGenomeResponse(BaseModel):
    run_id: str
    candidate_id: str
    genome: Dict[str, Any]
