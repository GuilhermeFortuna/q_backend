"""Injectable fakes for broker contract tests (paper and future live adapters)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from q_backend.execution.brokers.base import (
    BrokerHealth,
    BrokerOrderLookupStatus,
    BrokerOrderState,
    BrokerSubmissionResult,
    ExecutableQuote,
    MarketOrderRequest,
    PaperCostConfig,
)
from q_backend.execution.domain import BrokerMode
from q_backend.storage.db.execution_models import ExecutionFill, ExecutionLedgerEntry


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class FakeQuoteSource:
    def __init__(
        self,
        quotes: dict[str, tuple[Decimal, Decimal]],
        *,
        timestamp: datetime,
    ) -> None:
        self._quotes = quotes
        self.timestamp = timestamp

    def get_quote(self, symbol: str) -> Optional[ExecutableQuote]:
        if symbol not in self._quotes:
            return None
        bid, ask = self._quotes[symbol]
        return ExecutableQuote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            timestamp=self.timestamp,
            source="fake",
        )


class FakeReconciliationBroker:
    """Configurable broker double for reconciliation tests.

    Only ``lookup_order`` is meaningful: it returns a per-order override or a
    shared default (``NOT_FOUND`` unless configured otherwise). ``submit_market_order``
    is intentionally unsupported so tests cannot accidentally place new orders
    through the reconciliation path.
    """

    def __init__(
        self,
        *,
        default: Optional[BrokerOrderState] = None,
        broker_mode: BrokerMode = BrokerMode.PAPER,
    ) -> None:
        self._default = default or BrokerOrderState(
            status=BrokerOrderLookupStatus.NOT_FOUND
        )
        self._states: dict[UUID, BrokerOrderState] = {}
        self._broker_mode = broker_mode
        self.lookups: list[UUID] = []

    def set_state(self, order_id: UUID, state: BrokerOrderState) -> None:
        self._states[order_id] = state

    def set_default(self, state: BrokerOrderState) -> None:
        self._default = state

    def health(self) -> BrokerHealth:
        available = self._default.status != BrokerOrderLookupStatus.UNAVAILABLE
        return BrokerHealth(
            broker_mode=self._broker_mode,
            is_available=available,
            message="fake reconciliation broker",
            checked_at=datetime.now(timezone.utc),
        )

    def submit_market_order(
        self,
        request: MarketOrderRequest,
        *,
        cost_config: PaperCostConfig,
    ) -> BrokerSubmissionResult:
        raise NotImplementedError(
            "FakeReconciliationBroker does not submit orders"
        )

    def lookup_order(self, request: MarketOrderRequest) -> BrokerOrderState:
        self.lookups.append(request.order_id)
        return self._states.get(request.order_id, self._default)


def ledger_row_count(session: Session) -> int:
    return session.execute(
        select(func.count()).select_from(ExecutionLedgerEntry)
    ).scalar_one()


def fill_row_count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(ExecutionFill)).scalar_one()
