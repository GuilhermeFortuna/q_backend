"""Operational lifecycle commands for execution deployments."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from q_backend.execution.domain import DeploymentLifecycle
from q_backend.storage.db.execution_models import ExecutionDeployment
from q_backend.storage.db.execution_repositories import (
    get_execution_deployment,
    set_kill_switch,
    transition_deployment_lifecycle,
)


def start_deployment(session: Session, deployment_id: UUID) -> ExecutionDeployment:
    return transition_deployment_lifecycle(
        session, deployment_id, DeploymentLifecycle.RUNNING
    )


def pause_deployment(session: Session, deployment_id: UUID) -> ExecutionDeployment:
    """Stop evaluating new bars; retain the open position."""
    return transition_deployment_lifecycle(
        session, deployment_id, DeploymentLifecycle.PAUSED
    )


def stop_deployment(session: Session, deployment_id: UUID) -> ExecutionDeployment:
    """Terminate evaluation; retain the open position unless flatten is requested."""
    return transition_deployment_lifecycle(
        session, deployment_id, DeploymentLifecycle.STOPPED
    )


def enable_kill_switch(
    session: Session,
    *,
    reason: Optional[str] = None,
    updated_by: Optional[str] = None,
) -> None:
    set_kill_switch(session, enabled=True, reason=reason, updated_by=updated_by)


def disable_kill_switch(
    session: Session,
    *,
    updated_by: Optional[str] = None,
) -> None:
    set_kill_switch(session, enabled=False, reason=None, updated_by=updated_by)


def get_deployment_or_raise(session: Session, deployment_id: UUID) -> ExecutionDeployment:
    deployment = get_execution_deployment(session, deployment_id)
    if deployment is None:
        raise ValueError(f"ExecutionDeployment {deployment_id} not found")
    return deployment
