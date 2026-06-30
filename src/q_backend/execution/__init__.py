"""Broker-neutral forward execution domain (paper and live adapters)."""

from q_backend.execution.bar_coordinator import (
    BarCoordinator,
    BarStreamKey,
    CompletedBarBatch,
    DeploymentBarConsumer,
)
from q_backend.execution.domain import StrategyIdentity
from q_backend.execution.evaluator import StrategyEvaluator
from q_backend.execution.results import EvaluationPhaseTiming, ForwardDecisionResult
from q_backend.execution.warmup import (
    WindowBoundUndeterminedError,
    compute_window_bound_bars,
)

__all__ = [
    "BarCoordinator",
    "BarStreamKey",
    "CompletedBarBatch",
    "DeploymentBarConsumer",
    "EvaluationPhaseTiming",
    "ForwardDecisionResult",
    "StrategyEvaluator",
    "StrategyIdentity",
    "WindowBoundUndeterminedError",
    "compute_window_bound_bars",
]
