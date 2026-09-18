"""CLI tests for the standalone execution worker entrypoint."""

from __future__ import annotations

import uuid
from argparse import ArgumentTypeError
from unittest.mock import MagicMock, patch

import pytest

from q_backend.cli import q_execution
from q_backend.execution.domain import DecisionOutcome
from q_backend.execution.timing import DecisionProcessTiming


def test_build_parser_run_and_flatten_subcommands():
    parser = q_execution.build_parser()
    run_args = parser.parse_args(["run"])
    assert run_args.command == "run"
    assert run_args.poll_interval is None

    deployment_id = "22222222-2222-2222-2222-222222222222"
    flatten_args = parser.parse_args(["flatten", deployment_id])
    assert flatten_args.command == "flatten"
    assert str(flatten_args.deployment_id) == deployment_id


def test_parse_deployment_id_rejects_invalid_uuid():
    with pytest.raises(ArgumentTypeError):
        q_execution._parse_deployment_id("not-a-uuid")


@patch.object(q_execution, "ExecutionWorker")
@patch.object(q_execution, "_build_worker")
@patch.object(q_execution, "_build_components")
@patch.object(q_execution, "_market_data_session")
def test_cmd_run_starts_worker_with_market_data_lifecycle(
    market_data_cm,
    build_components,
    build_worker,
    worker_cls,
):
    md = MagicMock()
    market_data_cm.return_value.__enter__.return_value = md
    components = MagicMock()
    build_components.return_value = components
    worker = MagicMock()
    build_worker.return_value = worker

    code = q_execution.cmd_run(q_execution.build_parser().parse_args(["run", "--poll-interval", "2.5"]))

    assert code == 0
    build_components.assert_called_once()
    assert build_components.call_args.args[0] is md
    assert build_components.call_args.kwargs.get("crash_injector") is None
    build_worker.assert_called_once_with(components, poll_interval_seconds=2.5)
    worker.run.assert_called_once()
    market_data_cm.return_value.__enter__.assert_called_once()
    market_data_cm.return_value.__exit__.assert_called_once()


@patch.object(q_execution, "session_scope")
@patch.object(q_execution, "_market_data_session")
def test_cmd_flatten_acquires_lease_and_clears_pending_action(
    market_data_cm,
    session_scope_cm,
):
    deployment_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    md = MagicMock()
    market_data_cm.return_value.__enter__.return_value = md
    session = MagicMock()
    session_scope_cm.return_value.__enter__.return_value = session

    deployment = MagicMock()
    deployment.id = deployment_id

    timing = DecisionProcessTiming()
    flatten_result = MagicMock(
        outcome=DecisionOutcome.ORDER_FILLED,
        timing=timing,
    )

    components = MagicMock()
    components.service.flatten_deployment.return_value = flatten_result

    with (
        patch.object(q_execution, "_build_components", return_value=components),
        patch.object(
            q_execution,
            "get_deployment_or_raise",
            return_value=deployment,
        ),
        patch.object(q_execution, "acquire_worker_lease") as acquire,
        patch.object(q_execution, "clear_pending_deployment_action") as clear_pending,
        patch.object(q_execution, "release_worker_lease") as release,
    ):
        code = q_execution.cmd_flatten(q_execution.build_parser().parse_args(["flatten", str(deployment_id)]))

    assert code == 0
    acquire.assert_called_once()
    components.service.flatten_deployment.assert_called_once()
    clear_pending.assert_called_once_with(session, deployment_id)
    release.assert_called_once()


@patch.object(q_execution, "session_scope")
@patch.object(q_execution, "_market_data_session")
def test_cmd_flatten_returns_nonzero_on_failed_outcome(
    market_data_cm,
    session_scope_cm,
):
    deployment_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    market_data_cm.return_value.__enter__.return_value = MagicMock()
    session_scope_cm.return_value.__enter__.return_value = MagicMock()

    deployment = MagicMock()
    deployment.id = deployment_id
    flatten_result = MagicMock(
        outcome=DecisionOutcome.RISK_REJECTED,
        timing=DecisionProcessTiming(),
    )
    components = MagicMock()
    components.service.flatten_deployment.return_value = flatten_result

    with (
        patch.object(q_execution, "_build_components", return_value=components),
        patch.object(q_execution, "get_deployment_or_raise", return_value=deployment),
        patch.object(q_execution, "acquire_worker_lease"),
        patch.object(q_execution, "clear_pending_deployment_action"),
        patch.object(q_execution, "release_worker_lease"),
    ):
        code = q_execution.cmd_flatten(q_execution.build_parser().parse_args(["flatten", str(deployment_id)]))

    assert code == 1
