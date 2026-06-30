import pytest

from q_backend.execution.domain import (
    DeploymentLifecycle,
    ExecutionOrderStatus,
    IllegalLifecycleTransition,
    validate_deployment_transition,
    validate_order_transition,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (DeploymentLifecycle.DRAFT, DeploymentLifecycle.RUNNING),
        (DeploymentLifecycle.DRAFT, DeploymentLifecycle.STOPPED),
        (DeploymentLifecycle.RUNNING, DeploymentLifecycle.PAUSED),
        (DeploymentLifecycle.RUNNING, DeploymentLifecycle.STOPPED),
        (DeploymentLifecycle.PAUSED, DeploymentLifecycle.RUNNING),
        (DeploymentLifecycle.PAUSED, DeploymentLifecycle.STOPPED),
        (DeploymentLifecycle.ERROR, DeploymentLifecycle.STOPPED),
    ],
)
def test_legal_deployment_transitions(current, target):
    assert validate_deployment_transition(current, target) == target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (DeploymentLifecycle.STOPPED, DeploymentLifecycle.RUNNING),
        (DeploymentLifecycle.DRAFT, DeploymentLifecycle.PAUSED),
        (DeploymentLifecycle.RUNNING, DeploymentLifecycle.DRAFT),
        (DeploymentLifecycle.ERROR, DeploymentLifecycle.RUNNING),
    ],
)
def test_illegal_deployment_transitions(current, target):
    with pytest.raises(IllegalLifecycleTransition):
        validate_deployment_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ExecutionOrderStatus.INTENT, ExecutionOrderStatus.SUBMITTED),
        (ExecutionOrderStatus.INTENT, ExecutionOrderStatus.REJECTED),
        (ExecutionOrderStatus.INTENT, ExecutionOrderStatus.UNKNOWN),
        (ExecutionOrderStatus.SUBMITTED, ExecutionOrderStatus.FILLED),
        (ExecutionOrderStatus.SUBMITTED, ExecutionOrderStatus.UNKNOWN),
        (ExecutionOrderStatus.UNKNOWN, ExecutionOrderStatus.FILLED),
        (ExecutionOrderStatus.UNKNOWN, ExecutionOrderStatus.REJECTED),
    ],
)
def test_legal_order_transitions(current, target):
    assert validate_order_transition(current, target) == target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ExecutionOrderStatus.FILLED, ExecutionOrderStatus.SUBMITTED),
        (ExecutionOrderStatus.REJECTED, ExecutionOrderStatus.INTENT),
        (ExecutionOrderStatus.INTENT, ExecutionOrderStatus.FILLED),
        (ExecutionOrderStatus.CANCELLED, ExecutionOrderStatus.SUBMITTED),
    ],
)
def test_illegal_order_transitions(current, target):
    with pytest.raises(IllegalLifecycleTransition):
        validate_order_transition(current, target)
