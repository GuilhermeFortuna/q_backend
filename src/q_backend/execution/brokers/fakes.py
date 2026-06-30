"""Injectable fakes for broker contract tests (paper and future live adapters)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from q_backend.execution.brokers.base import ExecutableQuote
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


def ledger_row_count(session: Session) -> int:
    return session.execute(
        select(func.count()).select_from(ExecutionLedgerEntry)
    ).scalar_one()


def fill_row_count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(ExecutionFill)).scalar_one()
