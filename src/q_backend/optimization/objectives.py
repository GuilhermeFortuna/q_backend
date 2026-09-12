from typing import Any

from q_backend.optimization.models import ObjectiveMode


def resolve_objective(metrics: dict[str, Any], mode: ObjectiveMode) -> float | list[float]:
    if mode == ObjectiveMode.MAXIMIZE_NET_PROFIT:
        return float(metrics.get("total_pnl", 0.0))
    if mode == ObjectiveMode.MAXIMIZE_SHARPE:
        return float(metrics.get("sharpe_ratio", 0.0))
    if mode == ObjectiveMode.MINIMIZE_DRAWDOWN:
        return float(metrics.get("max_drawdown_pct", 0.0))
    if mode == ObjectiveMode.MAXIMIZE_RETURN_DRAWDOWN:
        return float(metrics.get("return_drawdown_ratio", 0.0))
    if mode == ObjectiveMode.MULTI_OBJECTIVE_RETURN_DRAWDOWN:
        return [
            float(metrics.get("total_return_pct", 0.0)),
            float(metrics.get("max_drawdown_pct", 0.0)),
        ]
    raise ValueError(f"Unknown objective mode: {mode}")


def worst_objective_value(mode: ObjectiveMode) -> float | list[float]:
    if mode == ObjectiveMode.MINIMIZE_DRAWDOWN:
        return float("inf")
    if mode == ObjectiveMode.MULTI_OBJECTIVE_RETURN_DRAWDOWN:
        return [float("-inf"), float("inf")]
    return float("-inf")
