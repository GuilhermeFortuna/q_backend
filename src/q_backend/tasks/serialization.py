"""(De)serialization of fan-out leaf results for the Redis staging area.

Leaf actors produce rich domain objects (walk-forward window results carrying Trade
models, discovery candidate results carrying pandas equity Series). These helpers
flatten them to JSON-safe dicts for staging and rebuild them in the finalizer, so the
existing aggregation/persistence code can run unchanged on the reconstructed objects.
"""

from datetime import datetime
from typing import Any, Optional

import pandas as pd

from q_backend.backtesting.models import Trade
from q_backend.optimization.strategy_search import CandidateResult
from q_backend.optimization.walkforward import WalkForwardWindowResult

# --- walk-forward window partials -------------------------------------------------


def window_partial_to_dict(
    result: WalkForwardWindowResult, is_objective: Optional[float]
) -> dict[str, Any]:
    return {
        "index": result.index,
        "train_start": result.train_start.isoformat(),
        "train_end": result.train_end.isoformat(),
        "test_start": result.test_start.isoformat(),
        "test_end": result.test_end.isoformat(),
        "status": result.status,
        "best_params": result.best_params,
        "is_metrics": result.is_metrics,
        "oos_metrics": result.oos_metrics,
        "oos_trades": [trade.model_dump(mode="json") for trade in result.oos_trades],
        "is_objective": is_objective,
    }


def window_partial_from_dict(
    payload: dict[str, Any],
) -> tuple[WalkForwardWindowResult, Optional[float]]:
    result = WalkForwardWindowResult(
        index=payload["index"],
        train_start=datetime.fromisoformat(payload["train_start"]),
        train_end=datetime.fromisoformat(payload["train_end"]),
        test_start=datetime.fromisoformat(payload["test_start"]),
        test_end=datetime.fromisoformat(payload["test_end"]),
        status=payload["status"],
        best_params=payload.get("best_params") or {},
        is_metrics=payload.get("is_metrics"),
        oos_metrics=payload.get("oos_metrics"),
        oos_trades=[Trade.model_validate(t) for t in payload.get("oos_trades", [])],
    )
    return result, payload.get("is_objective")


# --- discovery candidate partials -------------------------------------------------


def _series_to_obj(series: Optional[pd.Series]) -> Optional[dict[str, list]]:
    if series is None:
        return None
    return {
        "index": [pd.Timestamp(idx).isoformat() for idx in series.index],
        "values": [float(value) for value in series.values],
    }


def _obj_to_series(obj: Optional[dict[str, list]]) -> Optional[pd.Series]:
    if obj is None:
        return None
    return pd.Series(obj["values"], index=pd.to_datetime(obj["index"]))


def candidate_result_to_dict(candidate: CandidateResult) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "strategy": candidate.strategy,
        "status": candidate.status,
        "rank": candidate.rank,
        "objective_value": candidate.objective_value,
        "robustness_score": candidate.robustness_score,
        "efficiency": candidate.efficiency,
        "gate_flags": list(candidate.gate_flags),
        "passed_gates": candidate.passed_gates,
        "oos_metrics": candidate.oos_metrics,
        "is_metrics_summary": candidate.is_metrics_summary,
        "best_params": candidate.best_params,
        "window_count": candidate.window_count,
        "completed_windows": candidate.completed_windows,
        "oos_equity_curve": _series_to_obj(candidate.oos_equity_curve),
        "error": candidate.error,
    }


def candidate_result_from_dict(payload: dict[str, Any]) -> CandidateResult:
    return CandidateResult(
        candidate_id=payload["candidate_id"],
        strategy=payload["strategy"],
        status=payload["status"],
        rank=payload.get("rank"),
        objective_value=payload.get("objective_value"),
        robustness_score=payload.get("robustness_score"),
        efficiency=payload.get("efficiency"),
        gate_flags=list(payload.get("gate_flags", [])),
        passed_gates=payload.get("passed_gates", False),
        oos_metrics=payload.get("oos_metrics"),
        is_metrics_summary=payload.get("is_metrics_summary"),
        best_params=payload.get("best_params"),
        window_count=payload.get("window_count", 0),
        completed_windows=payload.get("completed_windows", 0),
        oos_equity_curve=_obj_to_series(payload.get("oos_equity_curve")),
        error=payload.get("error"),
    )
