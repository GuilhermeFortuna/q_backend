from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

from q_backend.backtesting.engine import ParallelMode


class ObjectiveMode(str, Enum):
    MAXIMIZE_NET_PROFIT = "maximize_net_profit"
    MAXIMIZE_SHARPE = "maximize_sharpe"
    MINIMIZE_DRAWDOWN = "minimize_drawdown"
    MAXIMIZE_RETURN_DRAWDOWN = "maximize_return_drawdown"
    MULTI_OBJECTIVE_RETURN_DRAWDOWN = "multi_objective_return_drawdown"


class IntParam(BaseModel):
    type: Literal["int"] = "int"
    low: int
    high: int
    step: int = 1


class FloatParam(BaseModel):
    type: Literal["float"] = "float"
    low: float
    high: float
    step: Optional[float] = None


class LogFloatParam(BaseModel):
    type: Literal["log-float"] = "log-float"
    low: float = Field(gt=0)
    high: float = Field(gt=0)


class CategoricalParam(BaseModel):
    type: Literal["categorical"] = "categorical"
    choices: list[str | int | float]


SearchParam = Annotated[
    Union[IntParam, FloatParam, LogFloatParam, CategoricalParam],
    Field(discriminator="type"),
]


class SearchSpaceConfig(BaseModel):
    strategy_params: dict[str, SearchParam] = Field(default_factory=dict)
    risk_params: dict[str, SearchParam] = Field(default_factory=dict)


class StorageConfig(BaseModel):
    type: Literal["memory", "sqlite", "url"] = "memory"
    path: Optional[str] = None
    url: Optional[str] = None

    @model_validator(mode="after")
    def validate_storage(self):
        if self.type == "sqlite" and not self.path:
            raise ValueError("storage.path is required when storage.type is sqlite")
        if self.type == "url" and not self.url:
            raise ValueError("storage.url is required when storage.type is url")
        return self


class StudyConfig(BaseModel):
    name: str
    n_trials: int = Field(default=50, ge=1)
    seed: int = 42
    direction: Optional[Literal["maximize", "minimize"]] = None
    continue_on_trial_error: bool = False
    sampler: Optional[Literal["tpe", "random", "nsgaii"]] = None
    pruner: Literal["none", "median", "hyperband"] = "none"
    storage: StorageConfig = Field(default_factory=lambda: StorageConfig(type="memory"))


class ObjectiveConfig(BaseModel):
    mode: ObjectiveMode


class BacktestConfig(BaseModel):
    symbol: str
    timeframe: str = "D1"
    start: datetime
    end: datetime
    initial_capital: float = Field(default=100_000.0, gt=0)
    point_value: float = Field(default=1.0, gt=0)
    strategy: str = "MACrossover"
    parallel_mode: ParallelMode = ParallelMode.SEQUENTIAL

    @model_validator(mode="after")
    def start_before_end(self):
        if self.start >= self.end:
            raise ValueError("backtest.start must be before backtest.end")
        return self


class OptimizationConfig(BaseModel):
    study: StudyConfig
    objective: ObjectiveConfig
    backtest: BacktestConfig
    search_space: SearchSpaceConfig

    @model_validator(mode="after")
    def validate_study_direction(self):
        is_multi = self.objective.mode == ObjectiveMode.MULTI_OBJECTIVE_RETURN_DRAWDOWN
        if is_multi and self.study.direction is not None:
            raise ValueError(
                "study.direction must be omitted for multi-objective studies"
            )
        return self

    def is_multi_objective(self) -> bool:
        return self.objective.mode == ObjectiveMode.MULTI_OBJECTIVE_RETURN_DRAWDOWN

    def optuna_directions(self) -> list[str]:
        mode = self.objective.mode
        if mode == ObjectiveMode.MAXIMIZE_NET_PROFIT:
            return ["maximize"]
        if mode == ObjectiveMode.MAXIMIZE_SHARPE:
            return ["maximize"]
        if mode == ObjectiveMode.MINIMIZE_DRAWDOWN:
            return ["minimize"]
        if mode == ObjectiveMode.MAXIMIZE_RETURN_DRAWDOWN:
            return ["maximize"]
        if mode == ObjectiveMode.MULTI_OBJECTIVE_RETURN_DRAWDOWN:
            return ["maximize", "minimize"]
        raise ValueError(f"Unknown objective mode: {mode}")


class TrialParams(BaseModel):
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    risk_params: dict[str, Any] = Field(default_factory=dict)
