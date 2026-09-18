from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import uuid

import jsonschema
import pytest
import referencing
from sqlalchemy.orm import Session

from q_backend.execution.domain import (
    BrokerMode,
    DecisionOutcome,
    ExecutionOrderStatus,
    ExecutionSide,
    LedgerEntryType,
    PositionSide,
    RiskRejectionCode,
    SignalAction,
    StrategyIdentity,
)
from q_backend.storage.db.execution_models import (
    ExecutionControlState,
    ExecutionDecision,
    ExecutionDeployment,
    ExecutionFill,
    ExecutionLedgerEntry,
    ExecutionNetPosition,
    ExecutionOrder,
    ExecutionRiskEvent,
    PaperAccount,
)
from q_backend.storage.db.execution_repositories import (
    append_ledger_entry,
    create_execution_decision,
    create_execution_deployment,
    create_execution_fill,
    create_execution_order_intent,
    create_paper_account,
    record_risk_event,
    upsert_open_net_position,
)
from q_backend.streaming import execution_events


def _load_registry() -> referencing.Registry:
    stream_dir = Path("contracts/schema/stream")
    resources = []
    for file in stream_dir.rglob("*.schema.json"):
        schema = json.loads(file.read_text(encoding="utf-8"))
        res = referencing.Resource.from_contents(schema)
        resources.append((file.name, res))
        resources.append((file.as_posix(), res))
        resources.append((f"stream/payloads/{file.name}", res))
        resources.append((f"stream/replay/{file.name}", res))
        schema_id = schema.get("$id")
        if schema_id:
            resources.append((schema_id, res))
            resources.append((f"{schema_id}.schema.json", res))
    return referencing.Registry().with_resources(resources)


def _load_payload_validator(schema_filename: str) -> jsonschema.Draft202012Validator:
    stream_dir = Path("contracts/schema/stream")
    schema_path = stream_dir / "payloads" / schema_filename
    schema_data = json.loads(schema_path.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema_data, registry=_load_registry())


def _assert_valid(schema_filename: str, payload: dict) -> None:
    validator = _load_payload_validator(schema_filename)
    errors = list(validator.iter_errors(payload))
    assert not errors, f"Validation errors against {schema_filename}: {[e.message for e in errors]}"


def _identity() -> StrategyIdentity:
    return StrategyIdentity(
        strategy_name="demo_strategy",
        strategy_version=1,
        compiled_config={"fast": 10, "slow": 20},
        config_hash="a1b2c3d4e5f67890abcdef1234567890abcdef12",
        symbol="WIN$",
        timeframe="M1",
        sizing_config={"mode": "fixed", "quantity": "1"},
        risk_config={"max_daily_loss": "500"},
    )


def test_deployment_state_validates_schema(db_session: Session):
    account = create_paper_account(db_session, name="acc-dep", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-1",
        identity=_identity(),
    )
    payload = execution_events.deployment_state(deployment)
    _assert_valid("execution-deployment.schema.json", payload)


def test_decision_state_validates_schema(db_session: Session):
    account = create_paper_account(db_session, name="acc-dec", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-dec",
        identity=_identity(),
    )
    decision = create_execution_decision(
        db_session,
        deployment_id=deployment.id,
        bar_close_time=datetime(2026, 9, 18, 14, 30, tzinfo=timezone.utc),
        identity=_identity(),
        signal_action=SignalAction.BUY,
        outcome=DecisionOutcome.ORDER_INTENT,
        requested_quantity=Decimal("1.0"),
        reason="cross",
        context={"spread": 1.0},
    )
    payload = execution_events.decision_state(decision)
    _assert_valid("execution-decision.schema.json", payload)


def test_order_state_validates_schema(db_session: Session):
    account = create_paper_account(db_session, name="acc-ord", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-ord",
        identity=_identity(),
    )
    order = create_execution_order_intent(
        db_session,
        deployment_id=deployment.id,
        decision_id=None,
        broker_mode=BrokerMode.PAPER,
        side=ExecutionSide.BUY,
        quantity=Decimal("2.0"),
    )
    payload = execution_events.order_state(order)
    _assert_valid("execution-order.schema.json", payload)


def test_position_state_validates_schema(db_session: Session):
    account = create_paper_account(db_session, name="acc-pos", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-pos",
        identity=_identity(),
    )
    position = upsert_open_net_position(
        db_session,
        deployment_id=deployment.id,
        side=PositionSide.LONG,
        quantity=Decimal("2.0"),
        average_entry_price=Decimal("125000.00"),
    )
    payload = execution_events.position_state(position)
    common_schema = json.loads(Path("contracts/schema/stream/payloads/execution-common.schema.json").read_text())
    schema = {"$defs": common_schema["$defs"], "$ref": "#/$defs/ExecutionPosition"}
    sub_validator = jsonschema.Draft202012Validator(schema)
    errors = list(sub_validator.iter_errors(payload))
    assert not errors, f"Position validation failed: {errors}"


def test_account_state_validates_schema(db_session: Session):
    account = create_paper_account(db_session, name="acc-state", initial_balance=Decimal("100000.00"))
    payload = execution_events.account_state(account)
    common_schema = json.loads(Path("contracts/schema/stream/payloads/execution-common.schema.json").read_text())
    schema = {"$defs": common_schema["$defs"], "$ref": "#/$defs/ExecutionAccount"}
    sub_validator = jsonschema.Draft202012Validator(schema)
    errors = list(sub_validator.iter_errors(payload))
    assert not errors, f"Account validation failed: {errors}"


def test_fill_event_validates_schema(db_session: Session):
    account = create_paper_account(db_session, name="acc-fill", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-fill",
        identity=_identity(),
    )
    order = create_execution_order_intent(
        db_session,
        deployment_id=deployment.id,
        decision_id=None,
        broker_mode=BrokerMode.PAPER,
        side=ExecutionSide.BUY,
        quantity=Decimal("1.0"),
    )
    fill = create_execution_fill(
        db_session,
        deployment_id=deployment.id,
        order_id=order.id,
        broker_mode=BrokerMode.PAPER,
        external_fill_id="ext-fill-100",
        side=ExecutionSide.BUY,
        quantity=Decimal("1.0"),
        price=Decimal("125500.00"),
        filled_at=datetime(2026, 9, 18, 14, 30, tzinfo=timezone.utc),
        fee=Decimal("2.50"),
        slippage=Decimal("0.50"),
    )
    position = upsert_open_net_position(
        db_session,
        deployment_id=deployment.id,
        side=PositionSide.LONG,
        quantity=Decimal("1.0"),
        average_entry_price=Decimal("125500.00"),
    )
    payload = execution_events.fill_event(fill, position)
    _assert_valid("execution-fill.schema.json", payload)


def test_ledger_event_validates_schema(db_session: Session):
    account = create_paper_account(db_session, name="acc-ledg", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-ledg",
        identity=_identity(),
    )
    entry = append_ledger_entry(
        db_session,
        paper_account_id=account.id,
        deployment_id=deployment.id,
        entry_type=LedgerEntryType.INITIAL_BALANCE,
        amount=Decimal("100000.00"),
        balance_after=Decimal("100000.00"),
        description="Initial funding",
    )
    payload = execution_events.ledger_event(entry, account)
    _assert_valid("execution-ledger.schema.json", payload)


def test_risk_rejection_event_validates_schema(db_session: Session):
    account = create_paper_account(db_session, name="acc-risk", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-risk",
        identity=_identity(),
    )
    risk = record_risk_event(
        db_session,
        deployment_id=deployment.id,
        rejection_code=RiskRejectionCode.INSUFFICIENT_EQUITY,
        message="Margin exceeded",
        context={"margin": 5000},
    )
    payload = execution_events.risk_rejection_event(risk)
    _assert_valid("execution-risk.schema.json", payload)


def test_kill_switch_event_validates_schema():
    control = ExecutionControlState(
        kill_switch_enabled=True,
        kill_switch_reason="Market crash",
        updated_by="risk_officer",
    )
    payload = execution_events.kill_switch_event(control, actor="risk_officer")
    _assert_valid("execution-risk.schema.json", payload)
