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
    pause_requested: bool = False
    risk_rejection_code: Optional[str] = None
    risk_rejection_message: Optional[str] = None
    # Protective and resting-order management decided on this bar (see protective.py).
    entry_stop_loss: Optional[float] = None
    entry_take_profit: Optional[float] = None
    pending_entry: Optional[dict[str, Any]] = None
    protect_levels: Optional[dict[str, Optional[float]]] = None
    cancel_pending: bool = False

    @property
    def manages_orders(self) -> bool:
        """True when the bar asks for working-order or protective-level changes."""
        return self.pending_entry is not None or self.protect_levels is not None or self.cancel_pending

    @property
    def emits_decision(self) -> bool:
        """Replay and duplicate deliveries must not create durable decisions."""
        return not self.replay and not self.skipped_duplicate
