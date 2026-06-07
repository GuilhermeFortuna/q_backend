from q_backend.optimization.models import ObjectiveMode
from q_backend.optimization.objectives import resolve_objective, worst_objective_value


def test_maximize_net_profit():
    assert (
        resolve_objective({"total_pnl": 500.0}, ObjectiveMode.MAXIMIZE_NET_PROFIT)
        == 500.0
    )


def test_maximize_sharpe():
    assert (
        resolve_objective({"sharpe_ratio": 1.5}, ObjectiveMode.MAXIMIZE_SHARPE) == 1.5
    )


def test_minimize_drawdown():
    assert (
        resolve_objective({"max_drawdown_pct": 0.12}, ObjectiveMode.MINIMIZE_DRAWDOWN)
        == 0.12
    )


def test_maximize_return_drawdown():
    assert (
        resolve_objective(
            {"return_drawdown_ratio": 3.0}, ObjectiveMode.MAXIMIZE_RETURN_DRAWDOWN
        )
        == 3.0
    )


def test_multi_objective():
    result = resolve_objective(
        {"total_return_pct": 0.2, "max_drawdown_pct": 0.05},
        ObjectiveMode.MULTI_OBJECTIVE_RETURN_DRAWDOWN,
    )
    assert result == [0.2, 0.05]


def test_worst_objective_multi():
    worst = worst_objective_value(ObjectiveMode.MULTI_OBJECTIVE_RETURN_DRAWDOWN)
    assert worst == [float("-inf"), float("inf")]
