"""Completed-bar decision processing: evaluate, risk, intent, broker, ledger."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

from q_backend.execution.brokers.base import (
    BrokerSubmissionOutcome,
    ExecutionBroker,
    MarketOrderRequest,
    PaperCostConfig,
    QuoteSource,
)
from q_backend.execution.domain import (
    BrokerMode,
    DecisionOutcome,
    DeploymentLifecycle,
    ExecutionOrderStatus,
    ExecutionSide,
    PositionSide,
    ReconciliationState,
    RiskRejection,
    SignalAction,
)
from q_backend.execution.ledger import ExecutionLedger
from q_backend.execution.results import ForwardDecisionResult
from q_backend.execution.risk import RiskContext, RiskGate, deployment_has_unknown_orders
from q_backend.execution.timing import DecisionProcessTiming
from q_backend.storage.db.execution_models import ExecutionDecision, ExecutionDeployment
from q_backend.storage.db.execution_repositories import (
    create_execution_decision,
    create_execution_order_intent,
    get_decision_for_bar,
    get_execution_control_state,
    get_open_net_position,
    get_paper_account,
    get_worker_lease,
    list_orders_for_deployment,
    record_risk_event,
    sum_realized_pnl_since,
    transition_execution_order,
    update_deployment_last_bar_close,
    update_execution_decision_outcome,
)

logger = logging.getLogger(__name__)


class CrashInjector:
    """Test hook for crash-window assertions."""

    def __init__(self, checkpoints: Optional[set[str]] = None) -> None:
        self.checkpoints = checkpoints or set()

    def maybe_raise(self, name: str) -> None:
        if name in self.checkpoints:
            raise RuntimeError(f"crash injected at {name}")


@dataclass(frozen=True)
class BarProcessResult:
    deployment_id: uuid.UUID
    bar_close_time: datetime
    decision_id: Optional[uuid.UUID]
    outcome: DecisionOutcome
    order_id: Optional[uuid.UUID] = None
    duplicate: bool = False
    timing: DecisionProcessTiming = field(default_factory=DecisionProcessTiming)
    rejection: Optional[RiskRejection] = None


def _utc_day_start(now: datetime) -> datetime:
    ts = now.astimezone(timezone.utc)
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def map_signal_to_order(
    *,
    signal: SignalAction,
    position_side: PositionSide,
    position_quantity: Decimal,
    requested_quantity: Optional[Decimal],
) -> Optional[tuple[ExecutionSide, Decimal]]:
    if signal == SignalAction.HOLD:
        return None
    if signal in {SignalAction.BUY, SignalAction.SELL}:
        if requested_quantity is None or requested_quantity <= 0:
            return None
        side = ExecutionSide.BUY if signal == SignalAction.BUY else ExecutionSide.SELL
        return side, requested_quantity
    if signal == SignalAction.CLOSE:
        if position_side == PositionSide.LONG and position_quantity > 0:
            return ExecutionSide.SELL, position_quantity
        if position_side == PositionSide.SHORT and position_quantity > 0:
            return ExecutionSide.BUY, position_quantity
    return None


class ExecutionService:
    """Process one completed-bar decision through risk, broker, and ledger."""

    def __init__(
        self,
        *,
        broker: ExecutionBroker,
        quote_source: QuoteSource,
        ledger: ExecutionLedger,
        risk_gate: Optional[RiskGate] = None,
        crash_injector: Optional[CrashInjector] = None,
        max_bar_age_seconds: float = 7200.0,
        commit: Optional[Callable[[Session], None]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._broker = broker
        self._quote_source = quote_source
        self._ledger = ledger
        self._risk_gate = risk_gate or RiskGate()
        self._crash = crash_injector or CrashInjector()
        self._max_bar_age_seconds = max_bar_age_seconds
        self._commit = commit or (lambda session: session.commit())
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def process_completed_bar(
        self,
        session: Session,
        *,
        deployment: ExecutionDeployment,
        eval_result: ForwardDecisionResult,
        lease_token: str,
        worker_id: str,
        cost_config: PaperCostConfig,
        point_value: Decimal,
        allow_lifecycle_bypass: bool = False,
        flatten: bool = False,
    ) -> BarProcessResult:
        started = time.perf_counter()
        timing = DecisionProcessTiming(
            evaluation=eval_result.timing,
            bar_detection_ms=0.0,
        )

        if not eval_result.emits_decision:
            return BarProcessResult(
                deployment_id=deployment.id,
                bar_close_time=eval_result.bar_close_time,
                decision_id=None,
                outcome=DecisionOutcome.HOLD,
                duplicate=eval_result.skipped_duplicate,
                timing=timing,
            )

        existing = get_decision_for_bar(
            session,
            deployment_id=deployment.id,
            bar_close_time=eval_result.bar_close_time,
        )
        if existing is not None:
            return BarProcessResult(
                deployment_id=deployment.id,
                bar_close_time=eval_result.bar_close_time,
                decision_id=existing.id,
                outcome=DecisionOutcome(existing.outcome),
                duplicate=True,
                timing=timing,
            )

        from q_backend.execution.domain import StrategyIdentity

        identity = StrategyIdentity(
            strategy_name=deployment.strategy_name,
            strategy_version=deployment.strategy_version,
            compiled_config=deployment.compiled_config,
            config_hash=deployment.config_hash,
            symbol=deployment.symbol,
            timeframe=deployment.timeframe,
            sizing_config=deployment.sizing_config,
            risk_config=deployment.risk_config,
        )
        requested_qty = (
            Decimal(str(eval_result.requested_quantity))
            if eval_result.requested_quantity is not None
            else None
        )
        decision = create_execution_decision(
            session,
            deployment_id=deployment.id,
            bar_close_time=eval_result.bar_close_time,
            identity=identity,
            signal_action=eval_result.signal_action,
            outcome=DecisionOutcome.HOLD
            if eval_result.signal_action == SignalAction.HOLD
            else DecisionOutcome.SIGNAL,
            requested_quantity=requested_qty,
            reason=eval_result.reason,
            context={"timing": eval_result.timing.model_dump()},
        )

        if eval_result.signal_action == SignalAction.HOLD and not flatten:
            update_deployment_last_bar_close(
                session, deployment.id, eval_result.bar_close_time
            )
            self._commit(session)
            timing = timing.model_copy(update={"total_ms": _elapsed_ms(started)})
            return BarProcessResult(
                deployment_id=deployment.id,
                bar_close_time=eval_result.bar_close_time,
                decision_id=decision.id,
                outcome=DecisionOutcome.HOLD,
                timing=timing,
            )

        position = get_open_net_position(session, deployment.id)
        position_side = (
            PositionSide(position.side)
            if position is not None and position.is_open
            else PositionSide.FLAT
        )
        position_qty = position.quantity if position is not None else Decimal("0")

        if flatten:
            mapped = map_signal_to_order(
                signal=SignalAction.CLOSE,
                position_side=position_side,
                position_quantity=position_qty,
                requested_quantity=None,
            )
            if mapped is None:
                update_execution_decision_outcome(
                    session,
                    decision.id,
                    outcome=DecisionOutcome.HOLD,
                    context={"flatten": "no open position"},
                )
                self._commit(session)
                timing = timing.model_copy(update={"total_ms": _elapsed_ms(started)})
                return BarProcessResult(
                    deployment_id=deployment.id,
                    bar_close_time=eval_result.bar_close_time,
                    decision_id=decision.id,
                    outcome=DecisionOutcome.HOLD,
                    timing=timing,
                )
            side, quantity = mapped
        else:
            mapped = map_signal_to_order(
                signal=eval_result.signal_action,
                position_side=position_side,
                position_quantity=position_qty,
                requested_quantity=requested_qty,
            )
            if mapped is None:
                update_execution_decision_outcome(
                    session, decision.id, outcome=DecisionOutcome.HOLD
                )
                update_deployment_last_bar_close(
                    session, deployment.id, eval_result.bar_close_time
                )
                self._commit(session)
                timing = timing.model_copy(update={"total_ms": _elapsed_ms(started)})
                return BarProcessResult(
                    deployment_id=deployment.id,
                    bar_close_time=eval_result.bar_close_time,
                    decision_id=decision.id,
                    outcome=DecisionOutcome.HOLD,
                    timing=timing,
                )
            side, quantity = mapped

        risk_started = time.perf_counter()
        account = get_paper_account(session, deployment.paper_account_id)
        if account is None:
            raise ValueError(f"PaperAccount {deployment.paper_account_id} not found")

        quote = self._quote_source.get_quote(deployment.symbol)
        now = self._clock()
        lease = get_worker_lease(session, deployment.id)
        lease_held = (
            lease is not None
            and lease.worker_id == worker_id
            and lease.lease_token == lease_token
        )
        orders = list_orders_for_deployment(session, deployment.id)
        control = get_execution_control_state(session)
        risk_cfg = deployment.risk_config or account.risk_config or {}
        max_daily_loss = risk_cfg.get("max_daily_loss")
        max_notional = risk_cfg.get("max_notional")
        snapshot = self._ledger.account_snapshot(
            session,
            paper_account_id=account.id,
            deployment_id=deployment.id,
            symbol=deployment.symbol,
            quote=quote,
            point_value=point_value,
            as_of=now,
        )
        ctx = RiskContext(
            deployment_id=deployment.id,
            paper_account_id=account.id,
            lifecycle=DeploymentLifecycle(deployment.lifecycle),
            symbol=deployment.symbol,
            bar_close_time=eval_result.bar_close_time,
            side=side,
            quantity=quantity,
            quote=quote,
            now=now,
            kill_switch_enabled=control.kill_switch_enabled,
            lease_held=lease_held,
            has_unknown_orders=deployment_has_unknown_orders(orders),
            position_side=position_side,
            position_quantity=position_qty,
            cash_balance=account.cash_balance,
            equity=snapshot.equity,
            daily_realized_pnl=sum_realized_pnl_since(
                session, account.id, _utc_day_start(now)
            ),
            max_daily_loss=Decimal(str(max_daily_loss))
            if max_daily_loss is not None
            else None,
            max_notional=Decimal(str(max_notional)) if max_notional is not None else None,
            point_value=point_value,
            cost_config=cost_config,
            max_bar_age_seconds=self._max_bar_age_seconds,
            allow_lifecycle_bypass=allow_lifecycle_bypass or flatten,
        )
        rejection = self._risk_gate.evaluate(ctx)
        timing = timing.model_copy(
            update={"risk_ms": (time.perf_counter() - risk_started) * 1000.0}
        )
        if rejection is not None:
            record_risk_event(
                session,
                deployment_id=deployment.id,
                rejection_code=rejection.code,
                message=rejection.message,
                decision_id=decision.id,
                context=rejection.context,
            )
            update_execution_decision_outcome(
                session,
                decision.id,
                outcome=DecisionOutcome.RISK_REJECTED,
                context={"rejection": rejection.model_dump(mode="json")},
            )
            update_deployment_last_bar_close(
                session, deployment.id, eval_result.bar_close_time
            )
            self._commit(session)
            timing = timing.model_copy(update={"total_ms": _elapsed_ms(started)})
            return BarProcessResult(
                deployment_id=deployment.id,
                bar_close_time=eval_result.bar_close_time,
                decision_id=decision.id,
                outcome=DecisionOutcome.RISK_REJECTED,
                timing=timing,
                rejection=rejection,
            )

        persistence_started = time.perf_counter()
        order = create_execution_order_intent(
            session,
            deployment_id=deployment.id,
            decision_id=decision.id,
            broker_mode=BrokerMode(deployment.broker_mode),
            side=side,
            quantity=quantity,
            metadata={"flatten": flatten, "bar_close_time": eval_result.bar_close_time.isoformat()},
        )
        session.flush()
        self._crash.maybe_raise("before_intent_commit")
        self._commit(session)
        self._crash.maybe_raise("after_intent_commit")

        broker_started = time.perf_counter()
        submission = self._broker.submit_market_order(
            MarketOrderRequest(
                deployment_id=deployment.id,
                order_id=order.id,
                symbol=deployment.symbol,
                side=side,
                quantity=quantity,
                external_fill_id=f"paper:{order.id}",
            ),
            cost_config=cost_config,
        )
        quote_broker_ms = (time.perf_counter() - broker_started) * 1000.0
        timing = timing.model_copy(update={"quote_broker_ms": quote_broker_ms})
        self._crash.maybe_raise("after_broker_response")

        if submission.outcome == BrokerSubmissionOutcome.UNKNOWN:
            transition_execution_order(
                session,
                order.id,
                ExecutionOrderStatus.UNKNOWN,
                reconciliation_state=ReconciliationState.PENDING,
                rejection_reason="broker outcome unknown",
                external_order_id=submission.external_order_id,
            )
            update_execution_decision_outcome(
                session,
                decision.id,
                outcome=DecisionOutcome.ORDER_UNKNOWN,
            )
            update_deployment_last_bar_close(
                session, deployment.id, eval_result.bar_close_time
            )
            self._commit(session)
            timing = timing.model_copy(update={"total_ms": _elapsed_ms(started)})
            return BarProcessResult(
                deployment_id=deployment.id,
                bar_close_time=eval_result.bar_close_time,
                decision_id=decision.id,
                outcome=DecisionOutcome.ORDER_UNKNOWN,
                order_id=order.id,
                timing=timing,
            )

        if not submission.accepted or submission.fill is None:
            reason = (
                submission.rejection.message
                if submission.rejection is not None
                else "broker rejected order"
            )
            transition_execution_order(
                session,
                order.id,
                ExecutionOrderStatus.REJECTED,
                rejection_reason=reason,
                completed_at=now,
            )
            update_execution_decision_outcome(
                session,
                decision.id,
                outcome=DecisionOutcome.ORDER_REJECTED,
            )
            update_deployment_last_bar_close(
                session, deployment.id, eval_result.bar_close_time
            )
            self._commit(session)
            timing = timing.model_copy(update={"total_ms": _elapsed_ms(started)})
            return BarProcessResult(
                deployment_id=deployment.id,
                bar_close_time=eval_result.bar_close_time,
                decision_id=decision.id,
                outcome=DecisionOutcome.ORDER_REJECTED,
                order_id=order.id,
                timing=timing,
            )

        try:
            self._ledger.apply_fill(
                session,
                paper_account_id=account.id,
                deployment_id=deployment.id,
                order_id=order.id,
                fill=submission.fill,
                point_value=point_value,
                symbol=deployment.symbol,
            )
            self._crash.maybe_raise("before_fill_commit")
            update_execution_decision_outcome(
                session,
                decision.id,
                outcome=DecisionOutcome.ORDER_FILLED,
            )
            update_deployment_last_bar_close(
                session, deployment.id, eval_result.bar_close_time
            )
            self._commit(session)
        except Exception:
            # Already-handled by the state machine: the fill may or may not have
            # reached the broker, so the order becomes UNKNOWN/PENDING for the
            # WO178 reconciler and we re-raise. Log the traceback first so the
            # persistence failure is never silent.
            logger.exception(
                "Fill persistence failed for order %s; marking UNKNOWN", order.id
            )
            session.rollback()
            with session.begin():
                transition_execution_order(
                    session,
                    order.id,
                    ExecutionOrderStatus.UNKNOWN,
                    reconciliation_state=ReconciliationState.PENDING,
                    rejection_reason="fill persistence failed",
                )
                update_execution_decision_outcome(
                    session,
                    decision.id,
                    outcome=DecisionOutcome.ORDER_UNKNOWN,
                )
            raise

        timing = timing.model_copy(
            update={
                "persistence_ms": (time.perf_counter() - persistence_started) * 1000.0,
                "total_ms": _elapsed_ms(started),
            }
        )
        return BarProcessResult(
            deployment_id=deployment.id,
            bar_close_time=eval_result.bar_close_time,
            decision_id=decision.id,
            outcome=DecisionOutcome.ORDER_FILLED,
            order_id=order.id,
            timing=timing,
        )

    def flatten_deployment(
        self,
        session: Session,
        *,
        deployment: ExecutionDeployment,
        lease_token: str,
        worker_id: str,
        cost_config: PaperCostConfig,
        point_value: Decimal,
        bar_close_time: Optional[datetime] = None,
    ) -> BarProcessResult:
        now = bar_close_time or self._clock()
        from q_backend.execution.domain import StrategyIdentity
        from q_backend.execution.results import EvaluationPhaseTiming

        identity = StrategyIdentity(
            strategy_name=deployment.strategy_name,
            strategy_version=deployment.strategy_version,
            compiled_config=deployment.compiled_config,
            config_hash=deployment.config_hash,
            symbol=deployment.symbol,
            timeframe=deployment.timeframe,
            sizing_config=deployment.sizing_config,
            risk_config=deployment.risk_config,
        )
        eval_result = ForwardDecisionResult(
            deployment_id=str(deployment.id),
            bar_close_time=now,
            bar_close_price=0.0,
            signal_action=SignalAction.CLOSE,
            reason="flatten",
            strategy_name=identity.strategy_name,
            strategy_version=identity.strategy_version,
            config_hash=identity.config_hash,
            symbol=identity.symbol,
            timeframe=identity.timeframe,
            timing=EvaluationPhaseTiming(),
        )
        return self.process_completed_bar(
            session,
            deployment=deployment,
            eval_result=eval_result,
            lease_token=lease_token,
            worker_id=worker_id,
            cost_config=cost_config,
            point_value=point_value,
            allow_lifecycle_bypass=True,
            flatten=True,
        )
