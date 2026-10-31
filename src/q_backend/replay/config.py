"""Validated replay run configuration (mirrors ``api/replay/run-config`` in q_contracts).

Every field is checked before a run is created, so an invalid manifest reference, latency,
speed or resource limit fails before any trading can start. Times are exact UTC instants;
market time is kept as integer microseconds since the Unix epoch everywhere else.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

MIN_RATE = 0.1
MAX_RATE = 10.0
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MONEY = re.compile(r"^[0-9]+(\.[0-9]+)?$")
_ISO_CURRENCY = re.compile(r"^[A-Z]{3}$")

Symbol = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]
Rate = Annotated[float, Field(ge=MIN_RATE, le=MAX_RATE, allow_inf_nan=False)]


def to_market_us(instant: datetime) -> int:
    """Return the integer microseconds since the Unix epoch for an aware instant."""
    if instant.tzinfo is None:
        raise ValueError("market instants must be timezone-aware")
    return (instant.astimezone(UTC) - EPOCH) // timedelta(microseconds=1)


def from_market_us(market_us: int) -> datetime:
    """Return the UTC instant for integer microseconds since the Unix epoch."""
    return EPOCH + timedelta(microseconds=market_us)


class ReplayLatency(BaseModel):
    """Modeled market-time delays in integer milliseconds; all default to zero."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    market_data_ms: int = Field(0, ge=0)
    order_transport_ms: int = Field(0, ge=0)
    broker_accept_ms: int = Field(0, ge=0)
    broker_execution_ms: int = Field(0, ge=0)
    broker_response_ms: int = Field(0, ge=0)


class ReplayAccount(BaseModel):
    """Dedicated numeric demo account exposed through the simulated broker edge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    login: int = Field(ge=1)
    currency: str

    @field_validator("currency")
    @classmethod
    def _currency_code(cls, value: str) -> str:
        if not _ISO_CURRENCY.fullmatch(value):
            raise ValueError("currency must be an ISO 4217 code such as 'BRL'")
        return value


class ReplayLimits(BaseModel):
    """Resource bounds; exceeding one fails the run closed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_queued_events: int = Field(ge=1)
    max_journal_bytes: int = Field(ge=1)


class ReplayRunConfig(BaseModel):
    """Create-time configuration. Latency, account, data and limits are fixed per run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_path: Annotated[str, StringConstraints(min_length=1)]
    symbols: list[Symbol] = Field(min_length=1)
    start_utc: AwareDatetime
    end_utc: AwareDatetime
    warmup_start_utc: AwareDatetime | None = None
    initial_balance: str
    account: ReplayAccount
    latency: ReplayLatency = Field(default_factory=ReplayLatency)
    rate: Rate = 1.0
    limits: ReplayLimits

    @field_validator("symbols")
    @classmethod
    def _unique_symbols(cls, symbols: list[str]) -> list[str]:
        if len(set(symbols)) != len(symbols):
            raise ValueError("symbols must be unique")
        return symbols

    @field_validator("initial_balance")
    @classmethod
    def _positive_money(cls, value: str) -> str:
        if not _MONEY.fullmatch(value):
            raise ValueError("initial_balance must be an exact non-negative decimal string")
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:  # pragma: no cover - guarded by the pattern
            raise ValueError("initial_balance is not a decimal") from exc
        if amount <= 0:
            raise ValueError("initial_balance must be positive")
        return value

    @model_validator(mode="after")
    def _interval_order(self) -> ReplayRunConfig:
        if self.end_utc <= self.start_utc:
            raise ValueError("end_utc must be after start_utc (the interval is [start, end))")
        if self.warmup_start_utc is not None and self.warmup_start_utc >= self.start_utc:
            raise ValueError("warmup_start_utc must be before start_utc")
        return self

    @property
    def start_us(self) -> int:
        return to_market_us(self.start_utc)

    @property
    def end_us(self) -> int:
        return to_market_us(self.end_utc)

    def canonical_json(self) -> str:
        """Stable JSON of the configuration used for hashing and journal export."""
        payload = self.model_dump(mode="json")
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def config_hash(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return f"sha256:{digest}"
