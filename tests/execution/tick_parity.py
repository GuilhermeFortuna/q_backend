"""Tick-replay parity harness (Q-137).

The same tape of trade prints drives two runs of one research strategy:

* the research backtest, which fills against the ticks with the research rules;
* the forward path: the real ``ResearchEvaluator``, ``ExecutionService``, ``ProtectiveOrders``
  and ``ExecutionWorker`` steps, with a broker that fills pending and protective orders from the
  same ticks by the research rules.

Each decision is made at its bar's close, and the worker polls after every tick, so the poll
interval equals the tick spacing. The two runs must produce the same fills.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import numpy as np
import pandas as pd
from sqlalchemy.orm import sessionmaker

from q_backend.execution.brokers.base import (
    BrokerOrderLookupStatus,
    BrokerOrderState,
    BrokerRejection,
    BrokerRejectionCode,
    BrokerSubmissionOutcome,
    BrokerSubmissionResult,
    CancelOrderRequest,
    ExecutableQuote,
    PendingOrderRequest,
    PositionExitRequest,
    ProtectionRequest,
)
from q_backend.execution.domain import (
    BrokerMode,
    DeploymentLifecycle,
    ExecutionOrderType,
    ExecutionSide,
    FillRecord,
    StrategyIdentity,
)
from q_backend.execution.ledger import ExecutionLedger
from q_backend.execution.recovery import DeploymentRuntime
from q_backend.execution.research_evaluator import ResearchEvaluator
from q_backend.execution.service import ExecutionService
from q_backend.execution.worker import ExecutionWorker
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import TickStore, backtest
from q_backend.research.tick_store import _slug_symbol, _write_day_atomic
from q_backend.storage.db.execution_repositories import (
    acquire_worker_lease,
    create_execution_deployment,
    create_paper_account,
    get_execution_deployment,
)
from q_backend.storage.settings import Settings

SYMBOL = "WDO$N"
DAY = "2026-10-05"
WORKER = "worker-parity"
TOKEN = "lease-parity"
BAR = pd.Timedelta(minutes=10)
D = Decimal


def tape_from_clocks(rows: list[tuple[str, float]]) -> list[tuple[pd.Timestamp, float]]:
    """Trade prints as ``(HH:MM:SS, price)`` on the parity day, in Brasília time."""
    return [(pd.Timestamp(f"{DAY} {clock}", tz=BRASILIA_TZ), float(price)) for clock, price in rows]


def write_store(root, tape: list[tuple[pd.Timestamp, float]]) -> TickStore:
    # MT5 stores broker wall-clock time as the UTC components of the epoch; mirror that here.
    times = [int(pd.Timestamp(ts.strftime("%Y-%m-%d %H:%M:%S"), tz="UTC").timestamp() * 1000) for ts, _ in tape]
    prices = np.asarray([price for _, price in tape], dtype=np.float64)
    arrays = {
        "time_msc": np.asarray(times, dtype=np.int64),
        "bid": prices,
        "ask": prices,
        "last": prices,
        "volume": np.ones(len(tape), dtype=np.float64),
        "flags": np.full(len(tape), 32, dtype=np.int32),
    }
    directory = root / _slug_symbol(SYMBOL)
    directory.mkdir(parents=True, exist_ok=True)
    _write_day_atomic(directory / f"{DAY}.parquet", arrays)
    return TickStore(SYMBOL, root=root)


def research_fills(result) -> list[tuple[pd.Timestamp, str, float]]:
    """Entry and exit fills of a research backtest, in order: (tick time, side, price)."""
    fills: list[tuple[pd.Timestamp, str, float]] = []
    for _, trade in result.trades.iterrows():
        side = "buy" if str(trade["side"]).lower() == "long" else "sell"
        opposite = "sell" if side == "buy" else "buy"
        entry_time = trade["entry_tick_time"] if pd.notna(trade["entry_tick_time"]) else trade["entry_time"]
        exit_time = trade["exit_tick_time"] if pd.notna(trade["exit_tick_time"]) else trade["exit_time"]
        fills.append((pd.Timestamp(entry_time), side, float(trade["entry_price"])))
        fills.append((pd.Timestamp(exit_time), opposite, float(trade["exit_price"])))
    return fills


def run_research(store: TickStore, strategy, frame: pd.DataFrame):
    return backtest(
        frame,
        strategy=strategy,
        symbol=SYMBOL,
        ticks=store,
        point_value=1.0,
        day_trade=True,
        day_trade_start_time="09:00",
        day_trade_end_time="16:00",
        day_trade_close_time="17:00",
    )


class _Clock:
    def __init__(self) -> None:
        self.value: datetime = pd.Timestamp(f"{DAY} 09:00", tz=BRASILIA_TZ).to_pydatetime()

    def now(self) -> datetime:
        return self.value


class TapeQuoteSource:
    """Executable quote at the last trade the driver has reached."""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self.price = Decimal("0")

    def get_quote(self, symbol: str) -> Optional[ExecutableQuote]:
        # A non-zero spread: the risk gate rejects crossed or zero-spread quotes. Fills come from the tape.
        return ExecutableQuote(
            symbol=symbol,
            bid=self.price - D("0.5"),
            ask=self.price + D("0.5"),
            timestamp=self._clock.now(),
        )


@dataclass
class _Pending:
    request: PendingOrderRequest
    external: str
    status: str = "working"
    fill: Optional[FillRecord] = None
    position_ticket: Optional[str] = None


@dataclass
class _Position:
    side: ExecutionSide
    quantity: Decimal
    price: Decimal
    stop_loss: Optional[Decimal]
    take_profit: Optional[Decimal]
    ticket: str
    opened_at: datetime
    exit_fill: Optional[FillRecord] = None
    exit_reason: Optional[str] = None


@dataclass
class TapeBroker:
    """Fills working orders and protective levels from trade prints, by the research rules."""

    tape: list[tuple[pd.Timestamp, float]]
    now: Optional[datetime] = None
    fills: list[tuple[pd.Timestamp, str, float]] = field(default_factory=list)
    _pending: dict[Any, _Pending] = field(default_factory=dict)
    _counter: int = 1000
    position: Optional[_Position] = None
    last_closed: Optional[_Position] = None

    def _next(self) -> str:
        self._counter += 1
        return str(self._counter)

    def _first_trade_at_or_after(self, moment: datetime) -> Optional[tuple[datetime, Decimal]]:
        # SQLite returns naive datetimes; intents are committed in the parity day's local time.
        instant = pd.Timestamp(moment)
        if instant.tzinfo is None:
            instant = instant.tz_localize(BRASILIA_TZ)
        for when, price in self.tape:
            if when >= instant:
                return when.to_pydatetime(), D(str(price))
        return None

    def process_tick(self, when: datetime, price: float) -> None:
        """Advance the broker by one trade print: expiries, pending fills, then protective exits."""
        trade = D(str(price))
        for pending in self._pending.values():
            expires = pending.request.expires_at
            if pending.status == "working" and expires is not None and when >= expires:
                pending.status = "expired"
        if self.position is None:
            for pending in self._pending.values():
                if pending.status != "working":
                    continue
                fill_price = _trigger(pending.request, trade)
                if fill_price is None:
                    continue
                ticket = self._next()
                req = pending.request
                self.position = _Position(
                    side=req.side,
                    quantity=req.quantity,
                    price=fill_price,
                    stop_loss=req.stop_loss,
                    take_profit=req.take_profit,
                    ticket=ticket,
                    opened_at=when,
                )
                pending.status = "filled"
                pending.position_ticket = ticket
                pending.fill = _fill(req.side, req.quantity, fill_price, when, f"pend-{pending.external}")
                self.fills.append((pd.Timestamp(when), req.side.value, float(fill_price)))
                break
        elif when > self.position.opened_at:
            self._protective_exit(when, trade)

    def _protective_exit(self, when: datetime, trade: Decimal) -> None:
        position = self.position
        long = position.side == ExecutionSide.BUY
        stop, target = position.stop_loss, position.take_profit
        if stop is not None and ((long and trade <= stop) or (not long and trade >= stop)):
            self._close(when, trade, "sl")
        elif target is not None and ((long and trade > target) or (not long and trade < target)):
            self._close(when, target, "tp")

    def _close(self, when: datetime, price: Decimal, reason: str) -> None:
        position = self.position
        side = ExecutionSide.SELL if position.side == ExecutionSide.BUY else ExecutionSide.BUY
        position.exit_fill = _fill(side, position.quantity, price, when, f"exit-{position.ticket}")
        position.exit_reason = reason
        self.fills.append((pd.Timestamp(when), side.value, float(price)))
        self.last_closed = position
        self.position = None

    # ----------------------------------------------------------------- broker protocol

    def submit_market_order(self, request, *, cost_config):
        # The simulated clock, not the intent's wall-clock stamp, decides which trade the order meets.
        first = self._first_trade_at_or_after(self.now)
        if first is None:
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=BrokerRejection(code=BrokerRejectionCode.QUOTE_UNAVAILABLE, message="no trade after intent"),
            )
        when, price = first
        if self.position is None:
            ticket = self._next()
            self.position = _Position(
                side=request.side,
                quantity=request.quantity,
                price=price,
                stop_loss=request.stop_loss,
                take_profit=request.take_profit,
                ticket=ticket,
                opened_at=when,
            )
            self.fills.append((pd.Timestamp(when), request.side.value, float(price)))
            fill = _fill(request.side, request.quantity, price, when, request.external_fill_id)
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.FILLED,
                fill=fill,
                external_position_id=ticket,
            )
        self._close(when, price, "client")
        fill = _fill(request.side, request.quantity, price, when, request.external_fill_id)
        return BrokerSubmissionResult(outcome=BrokerSubmissionOutcome.FILLED, fill=fill)

    def submit_pending_order(self, request: PendingOrderRequest) -> BrokerSubmissionResult:
        external = self._next()
        self._pending[request.order_id] = _Pending(request=request, external=external)
        return BrokerSubmissionResult(outcome=BrokerSubmissionOutcome.WORKING, external_order_id=external)

    def lookup_pending_order(self, request: PendingOrderRequest) -> BrokerOrderState:
        pending = self._pending.get(request.order_id)
        if pending is None:
            return BrokerOrderState(status=BrokerOrderLookupStatus.NOT_FOUND)
        if pending.status == "filled":
            return BrokerOrderState(
                status=BrokerOrderLookupStatus.FILLED,
                fill=pending.fill,
                external_position_id=pending.position_ticket,
            )
        status = {
            "working": BrokerOrderLookupStatus.WORKING,
            "cancelled": BrokerOrderLookupStatus.CANCELLED,
            "expired": BrokerOrderLookupStatus.EXPIRED,
        }[pending.status]
        return BrokerOrderState(status=status, external_order_id=pending.external)

    def cancel_order(self, request: CancelOrderRequest) -> BrokerSubmissionResult:
        pending = self._by_external(request.external_order_id)
        if pending is None or pending.status != "working":
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=BrokerRejection(code=BrokerRejectionCode.ORDER_SEND_FAILED, message="not working"),
            )
        pending.status = "cancelled"
        return BrokerSubmissionResult(outcome=BrokerSubmissionOutcome.CANCELLED)

    def lookup_cancellation(self, request: CancelOrderRequest) -> BrokerOrderState:
        pending = self._by_external(request.external_order_id)
        if pending is not None and pending.status == "working":
            return BrokerOrderState(status=BrokerOrderLookupStatus.NOT_FOUND)
        return BrokerOrderState(status=BrokerOrderLookupStatus.CANCELLED)

    def set_protection(self, request: ProtectionRequest) -> BrokerSubmissionResult:
        if self.position is None or self.position.ticket != request.position_ticket:
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=BrokerRejection(code=BrokerRejectionCode.PROTECTIVE_LEVEL_REJECTED, message="no position"),
            )
        self.position.stop_loss = request.stop_loss
        self.position.take_profit = request.take_profit
        return BrokerSubmissionResult(
            outcome=BrokerSubmissionOutcome.APPLIED, external_position_id=request.position_ticket
        )

    def lookup_protection(self, request: ProtectionRequest) -> BrokerOrderState:
        position = self.position
        if position is None or position.ticket != request.position_ticket:
            return BrokerOrderState(status=BrokerOrderLookupStatus.UNAVAILABLE)
        if position.stop_loss == request.stop_loss and position.take_profit == request.take_profit:
            return BrokerOrderState(status=BrokerOrderLookupStatus.FILLED, external_position_id=position.ticket)
        return BrokerOrderState(status=BrokerOrderLookupStatus.NOT_FOUND, external_position_id=position.ticket)

    def lookup_position_exit(self, request: PositionExitRequest) -> BrokerOrderState:
        closed = self.last_closed
        if closed is None or closed.ticket != request.position_ticket or closed.exit_reason not in ("sl", "tp"):
            return BrokerOrderState(status=BrokerOrderLookupStatus.NOT_FOUND)
        reason = {"sl": "stop_loss", "tp": "take_profit"}[closed.exit_reason]
        fill = closed.exit_fill.model_copy(update={"metadata": {"exit_reason": reason}})
        return BrokerOrderState(
            status=BrokerOrderLookupStatus.FILLED,
            fill=fill,
            external_position_id=closed.ticket,
        )

    def _by_external(self, external: str) -> Optional[_Pending]:
        return next((p for p in self._pending.values() if p.external == external), None)


def _trigger(request: PendingOrderRequest, trade: Decimal) -> Optional[Decimal]:
    """Research rules: a stop fills at the first trade at or beyond it; a limit at its level once beaten."""
    level = D(str(request.price))
    buy = request.side == ExecutionSide.BUY
    if request.order_type == ExecutionOrderType.STOP:
        if (buy and trade >= level) or (not buy and trade <= level):
            return trade
        return None
    if (buy and trade < level) or (not buy and trade > level):
        return level
    return None


def _fill(side: ExecutionSide, quantity: Decimal, price: Decimal, when: datetime, fill_id: str) -> FillRecord:
    return FillRecord(
        broker_mode=BrokerMode.MT5_LIVE,
        external_fill_id=fill_id,
        side=side,
        quantity=quantity,
        price=price,
        filled_at=when.astimezone(timezone.utc) if when.tzinfo is not None else when,
        metadata={},
    )


def run_forward(
    engine, tape, frame: pd.DataFrame, strategy, *, quantity: int = 1
) -> list[tuple[pd.Timestamp, str, float]]:
    """Drive the real evaluator, service and worker over the tape; return the fills in order."""
    clock = _Clock()
    quotes = TapeQuoteSource(clock)
    broker = TapeBroker(tape=tape)
    ledger = ExecutionLedger()
    service = ExecutionService(
        broker=broker,
        quote_source=quotes,
        ledger=ledger,
        max_bar_age_seconds=10**9,
        clock=clock.now,
    )
    settings = Settings(
        execution_worker_id=WORKER,
        execution_lease_ttl_seconds=3600,
        execution_order_poll_interval_s=0.0,
        execution_default_point_value=1.0,
    )
    worker = ExecutionWorker(
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
        coordinator=None,
        quote_source=quotes,
        broker=broker,
        ledger=ledger,
        service=service,
        recovery=None,
        settings=settings,
        clock=clock.now,
    )
    identity = StrategyIdentity(
        strategy_name="parity",
        strategy_version=1,
        compiled_config={
            "day_trade": True,
            "day_trade_start_time": "09:00",
            "day_trade_end_time": "16:00",
            "day_trade_close_time": "17:00",
        },
        config_hash="parity",
        symbol=SYMBOL,
        timeframe="M10",
        sizing_config={"type": "fixed_quantity", "quantity": float(quantity)},
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        account = create_paper_account(session, name="parity", initial_balance=D("1000000"))
        deployment = create_execution_deployment(
            session,
            paper_account_id=account.id,
            name="parity",
            identity=identity,
            broker_mode=BrokerMode.MT5_LIVE,
            lifecycle=DeploymentLifecycle.RUNNING,
        )
        acquire_worker_lease(
            session,
            deployment_id=deployment.id,
            worker_id=WORKER,
            lease_token=TOKEN,
            ttl_seconds=3600,
            now=clock.now(),
        )
        session.commit()
        deployment_id = deployment.id

    evaluator = ResearchEvaluator(
        deployment_id=str(deployment_id),
        identity=identity,
        strategy_instance=strategy,
        window_bound=10_000,
    )
    with factory() as session:
        runtime = DeploymentRuntime(
            deployment=get_execution_deployment(session, deployment_id),
            identity=identity,
            evaluator=evaluator,
            lease_token=TOKEN,
        )
    worker._runtimes[deployment_id] = runtime
    worker._lease_tokens[deployment_id] = TOKEN

    bars = list(frame.index)
    closes = [pd.Timestamp(open_time) + BAR for open_time in bars]
    cursor = 0

    def decide(index: int) -> None:
        broker.now = clock.value
        with factory() as session:
            runtime.deployment = get_execution_deployment(session, deployment_id)
            cost = worker._cost_config(runtime.deployment)
            worker._sync_open_trade(session, runtime, point_value=1.0)
            result = evaluator.ingest_completed_bar(frame.loc[[bars[index]]])
            if result is not None:
                service.process_completed_bar(
                    session,
                    deployment=runtime.deployment,
                    eval_result=result,
                    lease_token=TOKEN,
                    worker_id=WORKER,
                    cost_config=cost,
                    point_value=D("1"),
                )
            worker._sync_open_trade(session, runtime, point_value=1.0)
            session.commit()

    def poll(when: datetime) -> None:
        broker.now = when
        with factory() as session:
            runtime.deployment = get_execution_deployment(session, deployment_id)
            worker._reconcile_working_orders(session, now=when)
            worker._sync_open_trade(session, runtime, point_value=1.0)
            session.commit()

    for when, price in tape:
        while cursor < len(bars) and closes[cursor] <= when:
            clock.value = closes[cursor].to_pydatetime()
            decide(cursor)
            cursor += 1
        clock.value = when.to_pydatetime()
        quotes.price = D(str(price))
        broker.process_tick(clock.value, price)
        poll(clock.value)
    while cursor < len(bars):
        clock.value = closes[cursor].to_pydatetime()
        decide(cursor)
        cursor += 1
    return broker.fills
