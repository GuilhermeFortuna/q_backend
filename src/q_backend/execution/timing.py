"""Structured hot-path timings for forward execution."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from q_backend.execution.results import EvaluationPhaseTiming


class DecisionProcessTiming(BaseModel):
    """Milliseconds per phase from bar detection through durable fill."""

    model_config = ConfigDict(frozen=True)

    bar_detection_ms: float = 0.0
    evaluation: EvaluationPhaseTiming = Field(default_factory=EvaluationPhaseTiming)
    risk_ms: float = 0.0
    quote_broker_ms: float = 0.0
    persistence_ms: float = 0.0
    total_ms: float = 0.0

    def to_log_dict(self) -> dict[str, float]:
        return {
            "bar_detection_ms": self.bar_detection_ms,
            "indicators_ms": self.evaluation.indicators_ms,
            "evaluate_ms": self.evaluation.evaluate_ms,
            "sizing_ms": self.evaluation.sizing_ms,
            "evaluation_total_ms": self.evaluation.total_ms,
            "risk_ms": self.risk_ms,
            "quote_broker_ms": self.quote_broker_ms,
            "persistence_ms": self.persistence_ms,
            "total_ms": self.total_ms,
        }
