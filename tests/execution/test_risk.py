"""Structured risk gate rejection tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from q_backend.execution.brokers.base import ExecutableQuote, PaperCostConfig
from q_backend.execution.brokers.fakes import ledger_row_count
from q_backend.execution.domain import (
    DeploymentLifecycle,
    ExecutionSide,
    PositionSide,
    RiskRejectionCode,
)
from q_backend.execution.risk import RiskContext, RiskGate
from q_backend.storage.db.execution_repositories import set_kill_switch
from tests.execution.conftest import strategy_identity


def _ctx(**overrides) -> RiskContext:
    base = dict(
        deployment_id=overrides.pop("deployment_id", "00000000-0000-0000-0000-000000000001"),
        paper_account_id=overrides.pop("paper_account_id", "00000000-0000-0000-0000-000000000002"),
        lifecycle=DeploymentLifecycle.RUNNING,
        symbol="WIN$",
        bar_close_time=datetime(2024, 6, 1, 14, 0, tzinfo=timezone.utc),
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
        quote=ExecutableQuote(
            symbol="WIN$",
            bid=Decimal("130000"),
            ask=Decimal("130010"),
            timestamp=datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc),
        ),
        now=datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc),
        kill_switch_enabled=False,
        lease_held=True,
        has_unknown_orders=False,
        position_side=PositionSide.FLAT,
        position_quantity=Decimal("0"),
        cash_balance=Decimal("100000"),
        equity=Decimal("100000"),
        daily_realized_pnl=Decimal("0"),
        max_daily_loss=Decimal("5000"),
        max_notional=Decimal("1000000"),
        point_value=Decimal("0.2"),
        cost_config=PaperCostConfig(max_quote_age_seconds=60.0),
        max_bar_age_seconds=7200.0,
    )
    from uuid import UUID

    base["deployment_id"] = UUID(str(base["deployment_id"]))
    base["paper_account_id"] = UUID(str(base["paper_account_id"]))
    base.update(overrides)
    return RiskContext(**base)


@pytest.mark.parametrize(
    "mutator,code",
    [
        (lambda c: replace(c, kill_switch_enabled=True), RiskRejectionCode.KILL_SWITCH),
        (lambda c: replace(c, lifecycle=DeploymentLifecycle.PAUSED), RiskRejectionCode.LIFECYCLE),
        (lambda c: replace(c, lease_held=False), RiskRejectionCode.LEASE_LOST),
        (lambda c: replace(c, has_unknown_orders=True), RiskRejectionCode.UNKNOWN_PRIOR_ORDER),
        (
            lambda c: replace(
                c,
                bar_close_time=datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc),
                max_bar_age_seconds=60.0,
            ),
            RiskRejectionCode.STALE_BAR,
        ),
        (lambda c: replace(c, quote=None), RiskRejectionCode.SYMBOL_UNAVAILABLE),
        (lambda c: replace(c, quantity=Decimal("0")), RiskRejectionCode.INVALID_QUANTITY),
        (
            lambda c: replace(c, max_notional=Decimal("1")),
            RiskRejectionCode.NOTIONAL_LIMIT,
        ),
        (lambda c: replace(c, equity=Decimal("0")), RiskRejectionCode.INSUFFICIENT_EQUITY),
        (
            lambda c: replace(c, daily_realized_pnl=Decimal("-6000")),
            RiskRejectionCode.DAILY_LOSS_LIMIT,
        ),
    ],
)
def test_risk_gate_rejection_codes(mutator, code):
    ctx = mutator(_ctx())
    rejection = RiskGate().evaluate(ctx)
    assert rejection is not None
    assert rejection.code == code


def test_risk_rejection_leaves_ledger_unchanged(db_session, seeded_deployment, paper_cost_config, execution_service):
    _, deployment, token = seeded_deployment
    set_kill_switch(db_session, enabled=True)
    from q_backend.execution.results import EvaluationPhaseTiming, ForwardDecisionResult
    from q_backend.execution.domain import SignalAction, DecisionOutcome
    from datetime import datetime, timezone

    before = ledger_row_count(db_session)
    result = execution_service.process_completed_bar(
        db_session,
        deployment=deployment,
        eval_result=ForwardDecisionResult(
            deployment_id=str(deployment.id),
            bar_close_time=datetime(2024, 6, 1, 14, 0, tzinfo=timezone.utc),
            bar_close_price=130005.0,
            signal_action=SignalAction.BUY,
            requested_quantity=1.0,
            strategy_name="MACrossover",
            strategy_version=1,
            config_hash="test-hash",
            symbol="WIN$",
            timeframe="H1",
            timing=EvaluationPhaseTiming(),
        ),
        lease_token=token,
        worker_id="worker-a",
        cost_config=paper_cost_config,
        point_value=Decimal("0.2"),
    )
    assert result.outcome == DecisionOutcome.RISK_REJECTED
    assert ledger_row_count(db_session) == before
