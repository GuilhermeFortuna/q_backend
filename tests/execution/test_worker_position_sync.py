"""Worker keeps the evaluator's open trade aligned with the durable net position."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.models import OrderAction, Trade
from q_backend.backtesting.strategy_registry import default_params_for
from q_backend.execution.bar_coordinator import BarCoordinator
from q_backend.execution.bars import bar_close_time
from q_backend.execution.brokers.base import (
    BrokerOrderLookupStatus,
    BrokerOrderState,
    PaperCostConfig,
)
from q_backend.execution.brokers.fakes import FakeQuoteSource, FakeReconciliationBroker, FixedClock
from q_backend.execution.brokers.paper import PaperBroker
from q_backend.execution.domain import (
    BrokerMode,
    DecisionOutcome,
    DeploymentLifecycle,
    ExecutionOrderStatus,
    ExecutionSide,
    PositionSide,
    ReconciliationState,
    SignalAction,
    StrategyIdentity,
)
from q_backend.execution.evaluator import StrategyEvaluator
from q_backend.execution.ledger import ExecutionLedger
from q_backend.execution.position_adapter import execution_position_to_trade, position_trade_id
from q_backend.execution.recovery import ExecutionRecovery, open_trade_for_deployment
from q_backend.execution.reconciliation import OrderReconciler
from q_backend.execution.service import ExecutionService
from q_backend.execution.worker import ExecutionWorker
from q_backend.market_data.models import OHLCV
from q_backend.storage.db.execution_models import ExecutionDecision
from q_backend.storage.db.execution_repositories import (
    create_execution_decision,
    create_execution_deployment,
    create_execution_order_intent,
    create_paper_account,
    get_open_net_position,
    mark_incomplete_orders_unknown,
    set_pending_deployment_action,
)
from q_backend.storage.settings import Settings
from tests.execution.test_worker import StaticOhlcvProvider


def _trailing_identity() -> StrategyIdentity:
    params = {
        **default_params_for("MACrossover"),
        "short_period": 5,
        "long_period": 20,
        "stop_loss_pct": 0.0,
        "take_profit_pct": 0.0,
        "trailing_stop_pct": 0.03,
    }
    return StrategyIdentity(
        strategy_name="MACrossover",
        strategy_version=1,
        compiled_config={
            "strategy": "MACrossover",
            "strategy_params": params,
            "symbol": "WIN$",
            "timeframe": "H1",
        },
        config_hash="trailing-sync",
        symbol="WIN$",
        timeframe="H1",
        sizing_config={"type": "fixed_quantity", "quantity": 1.0},
        risk_config={"max_daily_loss": "5000", "max_notional": "1000000"},
    )


def _two_bar_identity() -> StrategyIdentity:
    params = {
        **default_params_for("MACrossover"),
        "short_period": 5,
        "long_period": 20,
        "trailing_stop_pct": 0.0,
        "take_profit_pct": 0.0,
        "stop_loss_pct": 0.03,
    }
    return StrategyIdentity(
        strategy_name="MACrossover",
        strategy_version=1,
        compiled_config={
            "strategy": "MACrossover",
            "strategy_params": params,
            "symbol": "WIN$",
            "timeframe": "H1",
        },
        config_hash="two-bar-sync",
        symbol="WIN$",
        timeframe="H1",
        sizing_config={"type": "fixed_quantity", "quantity": 1.0},
        risk_config={"max_daily_loss": "5000", "max_notional": "1000000"},
    )


def _trailing_crossover_frame() -> pd.DataFrame:
    n = 120
    close = np.full(n, 100.0)
    close[:26] = np.linspace(110, 90, 26)
    close[26:65] = np.linspace(90, 145, 65 - 26)
    close[65:] = np.linspace(145, 100, n - 65)
    open_ = close.copy()
    high = close + 2.0
    low = close - 2.0
    volume = np.full(n, 1_000.0)
    index = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def _two_bar_poll_frame() -> pd.DataFrame:
    close = np.full(60, 100.0)
    close[:25] = np.linspace(110, 90, 25)
    close[25:31] = np.linspace(90, 104, 6)
    close[31] = 95.0
    open_ = close.copy()
    high = close + 1.0
    low = close - 1.0
    volume = np.full(60, 1_000.0)
    index = pd.date_range("2024-01-01", periods=60, freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def _discover_entry_exit(frame: pd.DataFrame, identity: StrategyIdentity) -> tuple[int, int]:
    compiled = identity.compiled_config
    params = {**compiled["strategy_params"], **compiled.get("exit_params", {})}
    strategy = build_strategy(compiled["strategy"], params, identity.symbol)
    evaluator = StrategyEvaluator(
        deployment_id="discover",
        identity=identity,
        strategy=strategy,
        window_bound=len(frame),
    )
    evaluator.seed_window(frame.iloc[:30])
    entry_idx = None
    for i in range(30, len(frame)):
        result = evaluator.ingest_completed_bars(frame.iloc[i : i + 1])
        if result and result[0].signal_action == SignalAction.BUY:
            entry_idx = i
            break
    if entry_idx is None:
        raise AssertionError("no entry bar discovered")

    trade = execution_position_to_trade(
        deployment_id=uuid.uuid4(),
        symbol=identity.symbol,
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        average_entry_price=Decimal(str(frame.iloc[entry_idx]["close"])),
        opened_at=frame.index[entry_idx].to_pydatetime(),
    )
    exit_evaluator = StrategyEvaluator(
        deployment_id="discover-exit",
        identity=identity,
        strategy=strategy,
        open_trade=trade,
        window_bound=len(frame),
    )
    exit_evaluator.seed_window(frame.iloc[: entry_idx + 1])
    exit_idx = None
    for i in range(entry_idx + 1, len(frame)):
        result = exit_evaluator.ingest_completed_bars(frame.iloc[i : i + 1])
        if result and result[0].signal_action == SignalAction.CLOSE:
            exit_idx = i
    if exit_idx is None:
        raise AssertionError("no trailing exit bar discovered")
    return entry_idx, exit_idx


def _close_at(frame: pd.DataFrame, idx: int, timeframe: str = "H1") -> datetime:
    return bar_close_time(frame.index[idx].to_pydatetime(), timeframe)


def _normalize_close(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _quote_for_frame(frame: pd.DataFrame) -> tuple[Decimal, Decimal]:
    ref = Decimal(str(float(frame.iloc[-1]["close"])))
    return ref - Decimal("1"), ref + Decimal("1")


def _make_worker(
    db_engine,
    *,
    frame: pd.DataFrame,
    identity: StrategyIdentity,
    clock: FixedClock,
    deployment_id: uuid.UUID | None = None,
) -> tuple[ExecutionWorker, sessionmaker, uuid.UUID]:
    bid, ask = _quote_for_frame(frame)
    quotes = FakeQuoteSource(
        quotes={"WIN$": (bid, ask)},
        timestamp=clock.now(),
    )
    provider = StaticOhlcvProvider(frame, symbol="WIN$", timeframe="H1")
    coordinator = BarCoordinator(provider=provider, clock=clock.now)
    broker = PaperBroker(quote_source=quotes, clock=clock)
    ledger = ExecutionLedger()
    service = ExecutionService(
        broker=broker,
        quote_source=quotes,
        ledger=ledger,
        max_bar_age_seconds=7200.0,
        clock=clock.now,
    )
    recovery = ExecutionRecovery(
        coordinator=coordinator,
        quote_source=quotes,
        ledger=ledger,
        point_value=Decimal("0.2"),
        initial_window_bars=60,
    )
    settings = Settings(
        execution_worker_id="worker-a",
        execution_lease_ttl_seconds=60,
        execution_poll_interval_seconds=0.01,
        execution_initial_window_bars=60,
        execution_default_point_value=0.2,
    )
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    worker = ExecutionWorker(
        session_factory=factory,
        coordinator=coordinator,
        quote_source=quotes,
        broker=broker,
        ledger=ledger,
        service=service,
        recovery=recovery,
        settings=settings,
        clock=clock.now,
        poll_interval_seconds=0.01,
    )
    if deployment_id is None:
        with factory() as session:
            account = create_paper_account(
                session, name=f"sync-account-{uuid.uuid4().hex[:8]}", initial_balance=Decimal("100000")
            )
            deployment = create_execution_deployment(
                session,
                paper_account_id=account.id,
                name="sync-dep",
                identity=identity,
                lifecycle=DeploymentLifecycle.RUNNING,
            )
            deployment_id = deployment.id
            session.commit()
    return worker, factory, deployment_id


def _decisions(session: Session, deployment_id: uuid.UUID) -> list[ExecutionDecision]:
    return list(
        session.execute(
            select(ExecutionDecision)
            .where(ExecutionDecision.deployment_id == deployment_id)
            .order_by(ExecutionDecision.bar_close_time)
        ).scalars()
    )


def test_position_trade_id_is_unique_per_opened_at():
    deployment_id = uuid.uuid4()
    first = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    second = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    assert position_trade_id(deployment_id, first) != position_trade_id(deployment_id, second)


def test_open_trade_for_deployment_maps_flat_long_and_short(db_session):
    account = create_paper_account(db_session, name="adapter", initial_balance=Decimal("100000"))
    identity = _trailing_identity()
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="adapter-dep",
        identity=identity,
        lifecycle=DeploymentLifecycle.RUNNING,
    )
    assert open_trade_for_deployment(db_session, deployment, point_value=0.2) is None

    opened = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    from q_backend.storage.db.execution_repositories import upsert_open_net_position

    upsert_open_net_position(
        db_session,
        deployment_id=deployment.id,
        side=PositionSide.LONG,
        quantity=Decimal("2"),
        average_entry_price=Decimal("100"),
        opened_at=opened,
    )
    long_trade = open_trade_for_deployment(db_session, deployment, point_value=0.2)
    assert long_trade is not None
    assert long_trade.id == position_trade_id(deployment.id, opened)
    assert long_trade.action == OrderAction.BUY

    upsert_open_net_position(
        db_session,
        deployment_id=deployment.id,
        side=PositionSide.FLAT,
        quantity=Decimal("0"),
        average_entry_price=None,
    )
    assert open_trade_for_deployment(db_session, deployment, point_value=0.2) is None


def test_in_run_entry_trailing_exit_without_restart(db_engine, db_session):
    frame = _trailing_crossover_frame()
    identity = _trailing_identity()
    entry_idx, exit_idx = _discover_entry_exit(frame, identity)
    warmup_close = _close_at(frame, entry_idx - 1)
    exit_close = _close_at(frame, exit_idx)

    clock = FixedClock(warmup_close)
    worker, factory, deployment_id = _make_worker(db_engine, frame=frame, identity=identity, clock=clock)
    with factory() as session:
        from q_backend.storage.db.execution_models import ExecutionDeployment

        deployment = session.get(ExecutionDeployment, deployment_id)
        deployment.last_bar_close_time = warmup_close
        session.commit()

    for idx in range(entry_idx, exit_idx + 1):
        clock._now = _close_at(frame, idx)
        bid, ask = _quote_for_frame(frame.iloc[: idx + 1])
        worker.quote_source._quotes = {"WIN$": (bid, ask)}
        worker.quote_source.timestamp = clock.now()
        worker.poll_once()

    with factory() as session:
        decisions = _decisions(session, deployment_id)
        close_decisions = [d for d in decisions if d.signal_action == SignalAction.CLOSE.value]
        assert close_decisions, "expected a trailing exit decision while running"
        position = get_open_net_position(session, deployment_id)
        assert position is None or not position.is_open


def _find_later_entry(frame: pd.DataFrame, identity: StrategyIdentity, after_idx: int) -> int:
    compiled = identity.compiled_config
    params = {**compiled["strategy_params"], **compiled.get("exit_params", {})}
    strategy = build_strategy(compiled["strategy"], params, identity.symbol)
    evaluator = StrategyEvaluator(
        deployment_id="later-entry",
        identity=identity,
        strategy=strategy,
        window_bound=len(frame),
    )
    evaluator.seed_window(frame.iloc[: after_idx + 1])
    for i in range(after_idx + 1, len(frame)):
        result = evaluator.ingest_completed_bars(frame.iloc[i : i + 1])
        if result and result[0].signal_action == SignalAction.BUY:
            return i
    raise AssertionError("no later entry bar discovered")


def test_flatten_clears_open_trade_and_next_entry_opens_new_position(db_engine):
    from q_backend.storage.db.execution_models import ExecutionDeployment

    frame = _trailing_crossover_frame()
    identity = _trailing_identity()
    entry_idx, _ = _discover_entry_exit(frame, identity)
    warmup_close = _close_at(frame, entry_idx - 1)
    entry_close = _close_at(frame, entry_idx)

    clock = FixedClock(entry_close)
    worker, factory, deployment_id = _make_worker(db_engine, frame=frame, identity=identity, clock=clock)
    with factory() as session:
        deployment = session.get(ExecutionDeployment, deployment_id)
        deployment.last_bar_close_time = warmup_close
        session.commit()

    worker.poll_once()
    with factory() as session:
        first_position = get_open_net_position(session, deployment_id)
        assert first_position is not None and first_position.is_open
        first_opened = first_position.opened_at
        runtime = worker._runtimes[deployment_id]
        assert runtime.evaluator._open_trade is not None

        set_pending_deployment_action(session, deployment_id, action="flatten")
        session.commit()

    flatten_close = _close_at(frame, entry_idx + 1)
    clock._now = flatten_close
    bid, ask = _quote_for_frame(frame.iloc[: entry_idx + 2])
    worker.quote_source._quotes = {"WIN$": (bid, ask)}
    worker.quote_source.timestamp = clock.now()
    worker.poll_once()
    with factory() as session:
        position = get_open_net_position(session, deployment_id)
        runtime = worker._runtimes[deployment_id]
        assert runtime.evaluator._open_trade is None
        assert position is None or not position.is_open


def test_reconciliation_fill_syncs_open_trade_before_next_bar(db_engine):
    frame = _two_bar_poll_frame()
    identity = _two_bar_identity()
    entry_idx = 30
    warmup_close = _close_at(frame, entry_idx - 1)
    bar_close = _close_at(frame, entry_idx)

    clock = FixedClock(bar_close)
    worker, factory, deployment_id = _make_worker(db_engine, frame=frame, identity=identity, clock=clock)
    with factory() as session:
        from q_backend.storage.db.execution_models import ExecutionDeployment

        deployment = session.get(ExecutionDeployment, deployment_id)
        deployment.last_bar_close_time = warmup_close
        decision = create_execution_decision(
            session,
            deployment_id=deployment_id,
            bar_close_time=warmup_close,
            identity=identity,
            signal_action=SignalAction.BUY,
            outcome=DecisionOutcome.SIGNAL,
            requested_quantity=Decimal("1"),
        )
        order = create_execution_order_intent(
            session,
            deployment_id=deployment_id,
            decision_id=decision.id,
            broker_mode=BrokerMode.PAPER,
            side=ExecutionSide.BUY,
            quantity=Decimal("1"),
        )
        mark_incomplete_orders_unknown(session, deployment_id)
        session.commit()
        order_id = order.id

    bid, ask = _quote_for_frame(frame)
    quotes = FakeQuoteSource(
        quotes={"WIN$": (bid, ask)},
        timestamp=clock.now(),
    )
    paper = PaperBroker(quote_source=quotes, clock=clock)
    from q_backend.execution.brokers.base import MarketOrderRequest

    with factory() as session:
        from q_backend.storage.db.execution_models import ExecutionDeployment

        deployment = session.get(ExecutionDeployment, deployment_id)
        confirmed = paper.submit_market_order(
            MarketOrderRequest(
                deployment_id=deployment_id,
                order_id=order_id,
                symbol=deployment.symbol,
                side=ExecutionSide.BUY,
                quantity=Decimal("1"),
                external_fill_id=f"paper:{order_id}",
                intent_created_at=clock.now(),
            ),
            cost_config=PaperCostConfig(point_value=Decimal("0.2"), max_quote_age_seconds=60.0),
        )
    recon_broker = FakeReconciliationBroker()
    recon_broker.set_state(
        order_id,
        BrokerOrderState(status=BrokerOrderLookupStatus.FILLED, fill=confirmed.fill),
    )
    worker._reconciler = OrderReconciler(
        broker=recon_broker,
        ledger=worker.ledger,
        point_value=Decimal("0.2"),
        clock=clock.now,
    )

    worker.poll_once()
    with factory() as session:
        runtime = worker._runtimes[deployment_id]
        assert runtime.evaluator._open_trade is not None
        position = get_open_net_position(session, deployment_id)
        assert position is not None and position.is_open
        assert runtime.evaluator._open_trade.id == position_trade_id(deployment_id, position.opened_at)


def test_two_bars_in_one_poll_exit_follows_in_run_entry(db_engine):
    frame = _two_bar_poll_frame()
    identity = _two_bar_identity()
    entry_idx = 30
    warmup_close = _close_at(frame, entry_idx - 1)
    exit_close = _close_at(frame, entry_idx + 1)

    clock = FixedClock(exit_close)
    worker, factory, deployment_id = _make_worker(db_engine, frame=frame, identity=identity, clock=clock)
    with factory() as session:
        from q_backend.storage.db.execution_models import ExecutionDeployment

        deployment = session.get(ExecutionDeployment, deployment_id)
        deployment.last_bar_close_time = warmup_close
        session.commit()

    worker.poll_once()
    with factory() as session:
        decisions = _decisions(session, deployment_id)
        actions = [(_normalize_close(d.bar_close_time), d.signal_action) for d in decisions]
        assert (_normalize_close(_close_at(frame, entry_idx)), SignalAction.BUY.value) in actions
        assert (_normalize_close(_close_at(frame, entry_idx + 1)), SignalAction.CLOSE.value) in actions


def test_second_position_has_fresh_exit_rule_state():
    frame = _trailing_crossover_frame()
    identity = _trailing_identity()
    entry_idx, exit_idx = _discover_entry_exit(frame, identity)

    params = {
        **identity.compiled_config["strategy_params"],
        **identity.compiled_config.get("exit_params", {}),
    }
    deployment_id = uuid.uuid4()
    second_bar = min(exit_idx + 20, len(frame) - 2)
    first_trade = execution_position_to_trade(
        deployment_id=deployment_id,
        symbol="WIN$",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        average_entry_price=Decimal(str(frame.iloc[entry_idx]["close"])),
        opened_at=frame.index[entry_idx].to_pydatetime(),
    )
    second_trade = execution_position_to_trade(
        deployment_id=deployment_id,
        symbol="WIN$",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        average_entry_price=Decimal(str(frame.iloc[second_bar]["close"])),
        opened_at=frame.index[second_bar].to_pydatetime(),
    )
    assert first_trade.id != second_trade.id

    evaluator = StrategyEvaluator(
        deployment_id=str(deployment_id),
        identity=identity,
        strategy=build_strategy("MACrossover", params, "WIN$"),
        open_trade=first_trade,
        window_bound=len(frame),
    )
    evaluator.seed_window(frame.iloc[: entry_idx + 1])
    for i in range(entry_idx + 1, exit_idx):
        evaluator.ingest_completed_bars(frame.iloc[i : i + 1])
    first_state = evaluator.strategy.exit_strategy._step.state(first_trade.id)
    assert first_state is not None
    first_extreme = first_state["trailing"]["extreme"]

    evaluator.set_open_trade(second_trade)
    evaluator.seed_window(frame.iloc[: second_bar + 1])
    evaluator.ingest_completed_bars(frame.iloc[second_bar + 1 : second_bar + 2])
    second_state = evaluator.strategy.exit_strategy._step.state(second_trade.id)
    assert second_state is not None
    second_extreme = second_state["trailing"]["extreme"]
    assert second_extreme != first_extreme
    assert second_extreme <= float(frame.iloc[second_bar + 1]["high"])


def test_restart_mid_position_matches_uninterrupted_next_decision(db_engine):
    from q_backend.storage.db.execution_models import ExecutionDeployment

    frame = _trailing_crossover_frame()
    identity = _trailing_identity()
    entry_idx, exit_idx = _discover_entry_exit(frame, identity)
    restart_idx = entry_idx + (exit_idx - entry_idx) // 2
    next_idx = restart_idx + 1
    warmup_close = _close_at(frame, entry_idx - 1)
    next_close = _close_at(frame, next_idx)

    clock = FixedClock(warmup_close)
    worker, factory, deployment_id = _make_worker(db_engine, frame=frame, identity=identity, clock=clock)
    with factory() as session:
        deployment = session.get(ExecutionDeployment, deployment_id)
        deployment.last_bar_close_time = warmup_close
        session.commit()

    for idx in range(entry_idx, next_idx + 1):
        clock._now = _close_at(frame, idx)
        worker.poll_once()

    with factory() as session:
        expected = next(
            d.signal_action
            for d in _decisions(session, deployment_id)
            if _normalize_close(d.bar_close_time) == _normalize_close(next_close)
        )

    clock = FixedClock(warmup_close)
    restarted_worker, restarted_factory, restarted_id = _make_worker(
        db_engine, frame=frame, identity=identity, clock=clock
    )
    with restarted_factory() as session:
        deployment = session.get(ExecutionDeployment, restarted_id)
        deployment.last_bar_close_time = warmup_close
        session.commit()

    for idx in range(entry_idx, restart_idx + 1):
        clock._now = _close_at(frame, idx)
        restarted_worker.poll_once()

    with restarted_factory() as session:
        restarted_worker._release_deployment(session, restarted_id)
        session.commit()

    clock._now = next_close
    restarted_worker.poll_once()
    with restarted_factory() as session:
        restarted = next(
            d
            for d in _decisions(session, restarted_id)
            if _normalize_close(d.bar_close_time) == _normalize_close(next_close)
        )
        assert restarted.signal_action == expected
