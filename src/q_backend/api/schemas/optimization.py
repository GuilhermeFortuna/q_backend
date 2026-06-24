from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class OptimizationStartResponse(BaseModel):
    study_id: str
    status: str


class OptimizationStatusResponse(BaseModel):
    study_id: str
    status: str
    completed_trials: int
    n_trials: int
    best_value: Optional[float] = None
    best_params: Dict[str, Any] = {}
    error: Optional[str] = None
    workers: int = 1
    backtest_config: Optional[Dict[str, Any]] = None
    optimization_config: Optional[Dict[str, Any]] = None
    trials: Optional[List[Dict[str, Any]]] = None
    best_trial: Optional[Dict[str, Any]] = None


class OptimizationResultsResponse(BaseModel):
    study_id: str
    objective_mode: str
    is_multi_objective: bool
    best_params: Dict[str, Any]
    best_trial: Optional[Dict[str, Any]] = None
    trials: List[Dict[str, Any]]
    pareto_trials: List[Dict[str, Any]]
    failures: List[Dict[str, Any]]


class ParamImportanceEntry(BaseModel):
    param: str
    importance: float


class ParallelCoordinateRow(BaseModel):
    number: int
    params: Dict[str, Any]
    values: List[float]


class ParallelCoordinatePayload(BaseModel):
    params: List[str]
    objectives: List[str]
    rows: List[ParallelCoordinateRow]
    rows_capped: Optional[bool] = None


class ParetoPoint(BaseModel):
    number: int
    values: List[float]
    params: Dict[str, Any]


class ParetoFrontPayload(BaseModel):
    is_multi_objective: bool
    objectives: List[str]
    points: List[ParetoPoint]


class OptimizationAnalyticsResponse(BaseModel):
    study_id: str
    status: str
    is_multi_objective: bool
    n_complete_trials: int
    objective_labels: List[str]
    param_importances: Optional[Dict[str, List[ParamImportanceEntry]]] = None
    parallel_coordinate: ParallelCoordinatePayload
    pareto_front: ParetoFrontPayload


class OptimizationStudyListItem(BaseModel):
    study_id: str
    name: str
    status: str
    best_value: Optional[float] = None
    n_trials: int
    completed_trials: int
    created_at: datetime


class OptimizationStudyListResponse(BaseModel):
    items: List[OptimizationStudyListItem]
    total: int
    limit: int
    offset: int
