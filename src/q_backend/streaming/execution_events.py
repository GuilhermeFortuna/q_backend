from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session, object_session

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
from q_backend.storage.db.outbox_models import OutboxEvent
from q_backend.streaming.outbox import OutboxTopicError, record_event
from q_contracts.topics import TOPICS


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    formatted = value.isoformat()
    if formatted.endswith("+00:00"):
        return formatted[:-6] + "Z"
    return formatted


def _dec_str(value: Decimal | float | int | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _resolve_account_id(row: Any) -> str:
    if hasattr(row, "paper_account_id") and getattr(row, "paper_account_id") is not None:
        return str(row.paper_account_id)
    if hasattr(row, "account_id") and getattr(row, "account_id") is not None:
        return str(row.account_id)
    dep = getattr(row, "deployment", None)
    if dep is not None and getattr(dep, "paper_account_id", None) is not None:
        return str(dep.paper_account_id)
    session = object_session(row)
    dep_id = getattr(row, "deployment_id", None)
    if session is not None and dep_id is not None:
        dep = session.get(ExecutionDeployment, dep_id)
        if dep is not None and getattr(dep, "paper_account_id", None) is not None:
            return str(dep.paper_account_id)
    raise ValueError(f"Cannot resolve account_id for entity row {row!r}")


def position_state(row: ExecutionNetPosition) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "deployment_id": str(row.deployment_id),
        "side": str(row.side),
        "quantity": _dec_str(row.quantity),
        "average_entry_price": _dec_str(row.average_entry_price),
        "is_open": bool(row.is_open),
        "opened_at": _dt(row.opened_at),
        "closed_at": _dt(row.closed_at),
        "updated_at": _dt(row.updated_at) or _dt(row.created_at) or _dt(_utcnow()),
    }


def account_state(row: PaperAccount) -> dict[str, Any]:
    session = object_session(row)
    if hasattr(row, "realized_pnl") and getattr(row, "realized_pnl") is not None:
        realized = getattr(row, "realized_pnl")
    elif session is not None:
        realized = session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(ExecutionLedgerEntry.amount), 0)).where(
                ExecutionLedgerEntry.paper_account_id == row.id,
                ExecutionLedgerEntry.entry_type == "realized_pnl",
            )
        ).scalar_one()
    else:
        realized = Decimal("0")

    return {
        "id": str(row.id),
        "name": row.name,
        "currency": row.currency,
        "initial_balance": _dec_str(row.initial_balance),
        "cash_balance": _dec_str(row.cash_balance),
        "realized_pnl": _dec_str(realized),
        "sizing_config": row.sizing_config or {},
        "risk_config": row.risk_config or {},
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at) or _dt(row.created_at) or _dt(_utcnow()),
    }


def deployment_state(row: ExecutionDeployment) -> dict[str, Any]:
    acc_id = _resolve_account_id(row)
    return {
        "entity": "deployment",
        "id": str(row.id),
        "deployment_id": str(row.id),
        "account_id": acc_id,
        "paper_account_id": acc_id,
        "name": row.name,
        "broker_mode": str(row.broker_mode),
        "lifecycle": str(row.lifecycle),
        "strategy_name": row.strategy_name,
        "strategy_version": int(row.strategy_version),
        "compiled_config": row.compiled_config or {},
        "config_hash": row.config_hash,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "sizing_config": row.sizing_config or {},
        "risk_config": row.risk_config or {},
        "live_activation_enabled": bool(row.live_activation_enabled),
        "pending_action": row.pending_action,
        "pending_action_requested_at": _dt(row.pending_action_requested_at),
        "last_bar_close_time": _dt(row.last_bar_close_time),
        "started_at": _dt(row.started_at),
        "stopped_at": _dt(row.stopped_at),
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at) or _dt(row.created_at) or _dt(_utcnow()),
    }


def decision_state(row: ExecutionDecision) -> dict[str, Any]:
    acc_id = _resolve_account_id(row)
    return {
        "entity": "decision",
        "id": str(row.id),
        "deployment_id": str(row.deployment_id),
        "account_id": acc_id,
        "bar_close_time": _dt(row.bar_close_time),
        "strategy_name": row.strategy_name,
        "strategy_version": int(row.strategy_version),
        "config_hash": row.config_hash,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "signal_action": str(row.signal_action),
        "outcome": str(row.outcome),
        "requested_quantity": _dec_str(row.requested_quantity),
        "reason": row.reason,
        "compiled_config": row.compiled_config or {},
        "sizing_config": row.sizing_config or {},
        "risk_config": row.risk_config or {},
        "context": row.context or {},
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at) or _dt(row.created_at) or _dt(_utcnow()),
    }


def order_state(row: ExecutionOrder) -> dict[str, Any]:
    acc_id = _resolve_account_id(row)
    return {
        "entity": "order",
        "id": str(row.id),
        "intent_id": str(row.id),
        "deployment_id": str(row.deployment_id),
        "account_id": acc_id,
        "decision_id": str(row.decision_id) if row.decision_id is not None else None,
        "broker_mode": str(row.broker_mode),
        "side": str(row.side),
        "order_type": str(row.order_type),
        "quantity": _dec_str(row.quantity),
        "status": str(row.status),
        "reconciliation_state": str(row.reconciliation_state),
        "external_order_id": row.external_order_id,
        "rejection_reason": row.rejection_reason,
        "intent_committed_at": _dt(row.intent_committed_at),
        "submitted_at": _dt(row.submitted_at),
        "completed_at": _dt(row.completed_at),
        "reconciliation_attempted_at": _dt(row.reconciliation_attempted_at),
        "reconciliation_error": row.reconciliation_error,
        "reconciled_at": _dt(row.reconciled_at),
        "reconciled_by": row.reconciled_by,
        "reconciliation_detail": row.reconciliation_detail,
        "details": row.details or {},
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at) or _dt(row.created_at) or _dt(_utcnow()),
    }


def fill_event(row: ExecutionFill, position: ExecutionNetPosition | None) -> dict[str, Any]:
    acc_id = _resolve_account_id(row)
    if position is not None:
        pos_after = position_state(position)
    else:
        pos_after = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_OID, f"flat-{row.deployment_id}")),
            "deployment_id": str(row.deployment_id),
            "side": "flat",
            "quantity": "0",
            "average_entry_price": None,
            "is_open": False,
            "opened_at": None,
            "closed_at": _dt(row.filled_at),
            "updated_at": _dt(row.updated_at) or _dt(row.filled_at) or _dt(_utcnow()),
        }

    return {
        "entity": "fill",
        "id": str(row.id),
        "deployment_id": str(row.deployment_id),
        "account_id": acc_id,
        "order_id": str(row.order_id),
        "broker_mode": str(row.broker_mode),
        "external_fill_id": str(row.external_fill_id),
        "side": str(row.side),
        "quantity": _dec_str(row.quantity),
        "price": _dec_str(row.price),
        "fee": _dec_str(row.fee),
        "slippage": _dec_str(row.slippage),
        "quote_bid": _dec_str(row.quote_bid),
        "quote_ask": _dec_str(row.quote_ask),
        "quote_timestamp": _dt(row.quote_timestamp),
        "filled_at": _dt(row.filled_at),
        "position_after": pos_after,
        "details": row.details or {},
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at) or _dt(row.created_at) or _dt(_utcnow()),
    }


def ledger_event(row: ExecutionLedgerEntry, account: PaperAccount) -> dict[str, Any]:
    acc_id = str(row.paper_account_id)
    return {
        "entity": "ledger_entry",
        "id": str(row.id),
        "account_id": acc_id,
        "paper_account_id": acc_id,
        "deployment_id": str(row.deployment_id) if row.deployment_id is not None else None,
        "fill_id": str(row.fill_id) if row.fill_id is not None else None,
        "entry_type": str(row.entry_type),
        "amount": _dec_str(row.amount),
        "balance_after": _dec_str(row.balance_after),
        "description": row.description,
        "account_after": account_state(account),
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at) or _dt(row.created_at) or _dt(_utcnow()),
    }


def risk_rejection_event(row: ExecutionRiskEvent) -> dict[str, Any]:
    acc_id = _resolve_account_id(row)
    return {
        "entity": "risk",
        "kind": "risk_rejection",
        "id": str(row.id),
        "deployment_id": str(row.deployment_id),
        "account_id": acc_id,
        "decision_id": str(row.decision_id) if row.decision_id is not None else None,
        "order_id": str(row.order_id) if row.order_id is not None else None,
        "rejection_code": str(row.rejection_code),
        "message": str(row.message),
        "context": row.context or {},
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at) or _dt(row.created_at) or _dt(_utcnow()),
    }


def kill_switch_event(row: ExecutionControlState, *, actor: str | None = None) -> dict[str, Any]:
    act = actor or row.updated_by
    reason = row.kill_switch_reason
    now_iso = _dt(getattr(row, "updated_at", None)) or _dt(_utcnow())
    created_iso = _dt(getattr(row, "created_at", None)) or now_iso
    event_id = str(uuid.uuid4())
    return {
        "entity": "risk",
        "kind": "kill_switch",
        "id": event_id,
        "deployment_id": None,
        "account_id": None,
        "kill_switch_enabled": bool(row.kill_switch_enabled),
        "enabled": bool(row.kill_switch_enabled),
        "kill_switch_reason": reason,
        "reason": reason,
        "updated_by": act,
        "actor": act,
        "created_at": created_iso,
        "updated_at": now_iso,
    }


def control_state(row: ExecutionControlState) -> dict[str, Any]:
    now_iso = _dt(getattr(row, "updated_at", None)) or _dt(getattr(row, "created_at", None)) or _dt(_utcnow())
    return {
        "kill_switch_enabled": bool(row.kill_switch_enabled),
        "kill_switch_reason": row.kill_switch_reason,
        "updated_by": row.updated_by,
        "updated_at": now_iso,
    }


def emit(
    session: Session,
    topic: str,
    payload: Mapping[str, Any],
    *,
    producer_id: str = "api",
) -> OutboxEvent:
    if topic not in TOPICS:
        raise OutboxTopicError(f"Topic {topic!r} is not a declared topic")
    policy = TOPICS[topic]
    routing_key = None
    if policy.coalesce_key:
        routing_key = {k: str(payload[k]) for k in policy.coalesce_key if k in payload and payload[k] is not None}

    return record_event(
        session,
        topic,
        payload,
        payload_schema=policy.payload_schema,
        producer_id=producer_id,
        routing_key=routing_key,
    )
