"""Typed forward-evaluation results."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from q_backend.execution.domain import SignalAction


class EvaluationPhaseTiming(BaseModel):
    """Per-phase timings for one completed-bar evaluation (milliseconds)."""

    model_config = ConfigDict(frozen=True)

    indicators_ms: float = 0.0
    evaluate_ms: float = 0.0
    sizing_ms: float = 0.0
    total_ms: float = 0.0


class ForwardDecisionResult(BaseModel):
    """Output contract for one deployment/bar close evaluation."""

    model_config = ConfigDict(frozen=True)

    deployment_id: str
    bar_close_time: datetime
    bar_close_price: float
    signal_action: SignalAction
    reason: Optional[str] = None
    requested_quantity: Optional[float] = None
    sizing_inputs: dict[str, Any] = Field(default_factory=dict)
    strategy_name: str
    strategy_version: int
    config_hash: str
    symbol: str
    timeframe: str
    queued_exit_signals: tuple[dict[str, Any], ...] = ()
    queued_entry_signals: tuple[dict[str, Any], ...] = ()
    timing: EvaluationPhaseTiming
    replay: bool = False
    skipped_duplicate: bool = False

    @property
    def emits_decision(self) -> bool:
        """Replay and duplicate deliveries must not create durable decisions."""
        return not self.replay and not self.skipped_duplicate
