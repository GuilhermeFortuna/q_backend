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
from q_backend.storage.db.outbox_models import OutboxEvent
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


# --- Repository emission tests (Plan step 3) ---


def _events_for_topic(session: Session, topic: str) -> list[OutboxEvent]:
    from sqlalchemy import select
    from q_backend.storage.db.outbox_models import OutboxEvent

    return list(
        session.execute(select(OutboxEvent).where(OutboxEvent.topic == topic).order_by(OutboxEvent.seq)).scalars().all()
    )


def test_create_execution_deployment_emits_event(db_session: Session):
    account = create_paper_account(db_session, name="acc-dep-emit", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-emit",
        identity=_identity(),
        producer="test-worker",
    )
    events = _events_for_topic(db_session, "deployments")
    assert len(events) == 1
    ev = events[0]
    assert ev.seq == 1
    assert ev.producer_id == "test-worker"
    assert ev.payload == execution_events.deployment_state(deployment)


def test_transition_deployment_lifecycle_emits_event(db_session: Session):
    from q_backend.storage.db.execution_repositories import transition_deployment_lifecycle

    account = create_paper_account(db_session, name="acc-trans-dep", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-trans",
        identity=_identity(),
    )
    transition_deployment_lifecycle(
        db_session,
        deployment.id,
        DeploymentLifecycle.RUNNING,
        producer="worker-lifecycle",
    )
    events = _events_for_topic(db_session, "deployments")
    assert len(events) == 2
    assert events[1].seq == 2
    assert events[1].producer_id == "worker-lifecycle"
    assert events[1].payload["lifecycle"] == "running"
    assert events[1].payload == execution_events.deployment_state(deployment)


def test_set_and_clear_pending_deployment_action_emits_events(db_session: Session):
    from q_backend.storage.db.execution_repositories import (
        clear_pending_deployment_action,
        set_pending_deployment_action,
    )

    account = create_paper_account(db_session, name="acc-action", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-action",
        identity=_identity(),
    )
    set_pending_deployment_action(db_session, deployment.id, action="stop", producer="api-action")
    events = _events_for_topic(db_session, "deployments")
    assert len(events) == 2
    assert events[1].payload["pending_action"] == "stop"
    assert events[1].producer_id == "api-action"

    clear_pending_deployment_action(db_session, deployment.id, producer="api-action")
    events = _events_for_topic(db_session, "deployments")
    assert len(events) == 3
    assert events[2].payload["pending_action"] is None


def test_update_deployment_last_bar_close_emits_event(db_session: Session):
    from q_backend.storage.db.execution_repositories import update_deployment_last_bar_close

    account = create_paper_account(db_session, name="acc-bar", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-bar",
        identity=_identity(),
    )
    bar_time = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)
    update_deployment_last_bar_close(db_session, deployment.id, bar_time, producer="worker-bar")
    events = _events_for_topic(db_session, "deployments")
    assert len(events) == 2
    assert events[1].seq == 2
    assert events[1].producer_id == "worker-bar"
    assert events[1].payload["last_bar_close_time"] == "2026-09-18T15:00:00Z"


def test_create_and_update_execution_decision_emits_events(db_session: Session):
    from q_backend.storage.db.execution_repositories import update_execution_decision_outcome

    account = create_paper_account(db_session, name="acc-dec-emit", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-dec-emit",
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
        producer="worker-decision",
    )
    events = _events_for_topic(db_session, "decisions")
    assert len(events) == 1
    assert events[0].seq == 1
    assert events[0].producer_id == "worker-decision"
    assert events[0].payload == execution_events.decision_state(decision)

    update_execution_decision_outcome(
        db_session,
        decision.id,
        outcome=DecisionOutcome.ORDER_FILLED,
        producer="worker-decision",
    )
    events = _events_for_topic(db_session, "decisions")
    assert len(events) == 2
    assert events[1].seq == 2
    assert events[1].payload["outcome"] == "order_filled"
    assert events[1].payload == execution_events.decision_state(decision)


def test_create_and_transition_execution_order_emits_events(db_session: Session):
    from q_backend.storage.db.execution_repositories import transition_execution_order

    account = create_paper_account(db_session, name="acc-ord-emit", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-ord-emit",
        identity=_identity(),
    )
    order = create_execution_order_intent(
        db_session,
        deployment_id=deployment.id,
        decision_id=None,
        broker_mode=BrokerMode.PAPER,
        side=ExecutionSide.BUY,
        quantity=Decimal("1.0"),
        producer="worker-ord",
    )
    events = _events_for_topic(db_session, "orders")
    assert len(events) == 1
    assert events[0].seq == 1
    assert events[0].producer_id == "worker-ord"
    assert events[0].payload == execution_events.order_state(order)

    transition_execution_order(
        db_session,
        order.id,
        ExecutionOrderStatus.SUBMITTED,
        producer="worker-ord",
    )
    events = _events_for_topic(db_session, "orders")
    assert len(events) == 2
    assert events[1].seq == 2
    assert events[1].payload["status"] == "submitted"
    assert events[1].payload == execution_events.order_state(order)


def test_mark_incomplete_orders_unknown_emits_events(db_session: Session):
    from q_backend.storage.db.execution_repositories import mark_incomplete_orders_unknown

    account = create_paper_account(db_session, name="acc-unk", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-unk",
        identity=_identity(),
    )
    o1 = create_execution_order_intent(
        db_session,
        deployment_id=deployment.id,
        decision_id=None,
        broker_mode=BrokerMode.PAPER,
        side=ExecutionSide.BUY,
        quantity=Decimal("1.0"),
    )
    o2 = create_execution_order_intent(
        db_session,
        deployment_id=deployment.id,
        decision_id=None,
        broker_mode=BrokerMode.PAPER,
        side=ExecutionSide.SELL,
        quantity=Decimal("2.0"),
    )
    initial_events = _events_for_topic(db_session, "orders")
    assert len(initial_events) == 2

    marked = mark_incomplete_orders_unknown(db_session, deployment.id, producer="recovery-worker")
    assert marked == 2
    events = _events_for_topic(db_session, "orders")
    assert len(events) == 4
    assert events[2].seq == 3
    assert events[2].payload["status"] == "unknown"
    assert events[2].producer_id == "recovery-worker"
    assert events[3].seq == 4
    assert events[3].payload["status"] == "unknown"


def test_record_and_finalize_order_reconciliation_emits_events(db_session: Session):
    from q_backend.storage.db.execution_repositories import (
        finalize_order_reconciliation,
        record_reconciliation_attempt,
        transition_execution_order,
    )

    account = create_paper_account(db_session, name="acc-recon", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-recon",
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
    transition_execution_order(db_session, order.id, ExecutionOrderStatus.UNKNOWN)

    record_reconciliation_attempt(db_session, order.id, error="timeout", producer="reconciler-1")
    events = _events_for_topic(db_session, "orders")
    assert events[-1].payload["reconciliation_error"] == "timeout"
    assert events[-1].producer_id == "reconciler-1"

    finalize_order_reconciliation(
        db_session,
        order.id,
        reconciled_by="manual",
        detail="matched fill",
        producer="reconciler-1",
    )
    events = _events_for_topic(db_session, "orders")
    assert events[-1].payload["reconciliation_state"] == "reconciled"
    assert events[-1].payload["reconciled_by"] == "manual"


def test_create_execution_fill_emits_event(db_session: Session):
    account = create_paper_account(db_session, name="acc-fill-repo", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-fill-repo",
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
        external_fill_id="ext-fill-repo-1",
        side=ExecutionSide.BUY,
        quantity=Decimal("1.0"),
        price=Decimal("100.00"),
        filled_at=datetime(2026, 9, 18, 14, 30, tzinfo=timezone.utc),
        producer="worker-fill",
    )
    events = _events_for_topic(db_session, "fills")
    assert len(events) == 1
    assert events[0].seq == 1
    assert events[0].producer_id == "worker-fill"
    assert events[0].payload == execution_events.fill_event(fill, None)


def test_append_ledger_entry_emits_event(db_session: Session):
    account = create_paper_account(db_session, name="acc-ledg-repo", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-ledg-repo",
        identity=_identity(),
    )
    entry = append_ledger_entry(
        db_session,
        paper_account_id=account.id,
        deployment_id=deployment.id,
        entry_type=LedgerEntryType.INITIAL_BALANCE,
        amount=Decimal("100000.00"),
        balance_after=Decimal("100000.00"),
        description="Funded",
        producer="worker-ledg",
    )
    events = _events_for_topic(db_session, "ledger")
    assert len(events) == 1
    assert events[0].seq == 1
    assert events[0].producer_id == "worker-ledg"
    assert events[0].payload == execution_events.ledger_event(entry, account)


def test_record_risk_event_emits_event(db_session: Session):
    account = create_paper_account(db_session, name="acc-risk-repo", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-risk-repo",
        identity=_identity(),
    )
    risk = record_risk_event(
        db_session,
        deployment_id=deployment.id,
        rejection_code=RiskRejectionCode.KILL_SWITCH,
        message="Kill switch engaged",
        producer="worker-risk",
    )
    events = _events_for_topic(db_session, "risk")
    assert len(events) == 1
    assert events[0].seq == 1
    assert events[0].producer_id == "worker-risk"
    assert events[0].payload == execution_events.risk_rejection_event(risk)


def test_set_kill_switch_emits_event(db_session: Session):
    from q_backend.storage.db.execution_repositories import set_kill_switch

    state = set_kill_switch(
        db_session,
        enabled=True,
        reason="Halt for maintenance",
        updated_by="admin-user",
        producer="admin-cli",
    )
    events = _events_for_topic(db_session, "risk")
    assert len(events) == 1
    assert events[0].seq == 1
    assert events[0].producer_id == "admin-cli"
    assert events[0].payload["kind"] == "kill_switch"
    assert events[0].payload["kill_switch_enabled"] is True
    assert events[0].payload["updated_by"] == "admin-user"


def test_apply_fill_composite_emission(db_session: Session):
    from q_backend.execution.domain import FillRecord
    from q_backend.execution.ledger import ExecutionLedger
    from q_backend.storage.db.execution_repositories import (
        get_open_net_position,
        get_paper_account,
        transition_execution_order,
    )

    account = create_paper_account(db_session, name="acc-apply-fill", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-apply-fill",
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
    ledger = ExecutionLedger()
    fill_record = FillRecord(
        broker_mode=BrokerMode.PAPER,
        external_fill_id="ext-fill-apply-1",
        side=ExecutionSide.BUY,
        quantity=Decimal("1.0"),
        price=Decimal("125000.00"),
        fee=Decimal("2.50"),
        slippage=Decimal("0.00"),
        filled_at=datetime(2026, 9, 18, 14, 30, tzinfo=timezone.utc),
    )
    ledger.apply_fill(
        db_session,
        paper_account_id=account.id,
        deployment_id=deployment.id,
        order_id=order.id,
        fill=fill_record,
        point_value=Decimal("0.2"),
        symbol="WIN$",
        producer="worker-fill-composite",
    )
    transition_execution_order(
        db_session,
        order.id,
        ExecutionOrderStatus.FILLED,
        producer="worker-fill-composite",
    )

    # 1. Orders topic: status=filled
    order_events = _events_for_topic(db_session, "orders")
    assert order_events[-1].payload["status"] == "filled"
    assert order_events[-1].producer_id == "worker-fill-composite"

    # 2. Fills topic: position_after matches the committed net position
    fill_events = _events_for_topic(db_session, "fills")
    assert len(fill_events) == 1
    assert fill_events[0].producer_id == "worker-fill-composite"
    current_pos = get_open_net_position(db_session, deployment.id)
    assert fill_events[0].payload["position_after"] == execution_events.position_state(current_pos)

    # 3. Ledger topic: account_after matches the committed account
    ledger_events = _events_for_topic(db_session, "ledger")
    assert len(ledger_events) >= 1
    assert ledger_events[-1].producer_id == "worker-fill-composite"
    current_acc = get_paper_account(db_session, account.id)
    assert ledger_events[-1].payload["account_after"] == execution_events.account_state(current_acc)


def test_rollback_leaves_no_events_or_sequence(db_session: Session):
    from sqlalchemy import select
    from q_backend.storage.db.outbox_models import OutboxEvent, OutboxTopicState

    account = create_paper_account(db_session, name="acc-rb", initial_balance=Decimal("100000.00"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="dep-rb",
        identity=_identity(),
    )
    db_session.commit()

    # Get initial sequence
    init_seq = db_session.execute(
        select(OutboxTopicState.last_seq).where(OutboxTopicState.topic == "orders")
    ).scalar_one()

    # Begin transaction, create order, then rollback
    create_execution_order_intent(
        db_session,
        deployment_id=deployment.id,
        decision_id=None,
        broker_mode=BrokerMode.PAPER,
        side=ExecutionSide.BUY,
        quantity=Decimal("1.0"),
    )
    db_session.rollback()

    # Confirm no events were committed
    events = list(
        db_session.execute(
            select(OutboxEvent).where(
                OutboxEvent.topic == "orders",
                OutboxEvent.payload["deployment_id"].as_string() == str(deployment.id),
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 0

    # Confirm sequence counter was not incremented
    after_seq = db_session.execute(
        select(OutboxTopicState.last_seq).where(OutboxTopicState.topic == "orders")
    ).scalar_one()
    assert after_seq == init_seq


def test_invalid_payload_fails_transaction(db_session: Session):
    from q_backend.streaming.outbox import OutboxEnvelopeError

    with pytest.raises(OutboxEnvelopeError):
        execution_events.emit(
            db_session,
            "orders",
            {"entity": "order", "id": str(uuid.uuid4())},  # missing required fields
            producer_id="test",
        )
