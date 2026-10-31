"""Market and operational clocks, kept in separate domains.

*Market time* drives decisions about the market: bar closes, session and day-trade boundaries,
activation cutoffs, decision age, quote freshness and order eligibility. *Operational time* is the
UTC wall clock and the monotonic clock. It drives worker leases, heartbeats, health freshness,
HTTP deadlines and retries. Storage keeps the two apart: audit timestamps are operational, market
event times are market.

ordinary live operation uses ``SystemMarketClock``. A replay run injects ``RemoteMarketClock``,
which reads the simulator's read-only ``/v1/clock`` snapshot. The remote clock fails closed: it
refuses a different run generation, and it raises when the snapshot is unreachable or too old
(measured on the monotonic clock), so market actions never silently fall back to today's date.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx


class ClockUnavailableError(RuntimeError):
    """The remote market clock could not be read; market actions must not proceed."""


class ClockStaleError(ClockUnavailableError):
    """The last snapshot is older than the freshness bound."""


class ClockGenerationError(ClockUnavailableError):
    """The source belongs to a different run generation than this consumer was configured for."""


class MarketClock(Protocol):
    def now(self) -> datetime:
        """Current market instant as an aware UTC datetime."""


class SystemMarketClock:
    """Ordinary live time: the UTC wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class OperationalClock:
    """Wall-clock and monotonic operational time for leases, heartbeats and deadlines."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()


class RemoteMarketClock:
    """Market time read from a replay simulator's ``GET /v1/clock`` snapshot.

    Between refreshes the clock advances from its snapshot at the snapshot's rate on the monotonic
    clock, and it freezes while the source is paused. A refresh happens at most every
    ``refresh_seconds``. A failed refresh past ``max_staleness_seconds`` raises rather than guessing.
    """

    def __init__(
        self,
        url: str,
        *,
        expected_generation: str,
        token: str | None = None,
        refresh_seconds: float = 1.0,
        max_staleness_seconds: float = 5.0,
        timeout_seconds: float = 2.0,
        monotonic: Callable[[], float] = time.monotonic,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not expected_generation:
            raise ValueError("expected_generation is required; a remote market clock is never generation-free")
        self._url = url.rstrip("/")
        self._generation = expected_generation
        self._token = token
        self._refresh = refresh_seconds
        self._max_staleness = max_staleness_seconds
        self._timeout = timeout_seconds
        self._monotonic = monotonic
        self._transport = transport
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._fetched_at: float = 0.0
        self._anchor_market: datetime | None = None

    def now(self) -> datetime:
        with self._lock:
            mono = self._monotonic()
            if self._snapshot is None or mono - self._fetched_at >= self._refresh:
                try:
                    self._fetch(mono)
                except ClockGenerationError:
                    raise
                except ClockUnavailableError:
                    # A failed refresh is tolerated only while the last snapshot is within bound.
                    if self._snapshot is None or mono - self._fetched_at > self._max_staleness:
                        raise
            assert self._snapshot is not None and self._anchor_market is not None
            if mono - self._fetched_at > self._max_staleness:
                raise ClockStaleError("remote market clock snapshot is older than the freshness bound")
            if self._snapshot["state"] != "running":
                return self._anchor_market
            elapsed = mono - self._fetched_at
            rate = float(self._snapshot["rate"])
            return self._anchor_market + timedelta(seconds=elapsed * rate)

    def _fetch(self, mono: float) -> None:
        headers = {"X-Gateway-Token": self._token} if self._token else {}
        try:
            with httpx.Client(
                base_url=self._url, timeout=self._timeout, headers=headers, transport=self._transport
            ) as client:
                response = client.get("/v1/clock")
        except httpx.HTTPError as exc:
            raise ClockUnavailableError(f"remote market clock unreachable: {exc}") from exc
        if response.status_code != 200:
            raise ClockUnavailableError(f"remote market clock answered HTTP {response.status_code}")
        try:
            snapshot = response.json()
            market = datetime.fromtimestamp(int(snapshot["market_time_us"]) / 1_000_000, tz=UTC)
            generation = str(snapshot["generation"])
            state = str(snapshot["state"])
            float(snapshot["rate"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ClockUnavailableError("remote market clock returned an invalid snapshot") from exc
        if generation != self._generation:
            raise ClockGenerationError(
                f"remote market clock generation {generation!r} does not match {self._generation!r}"
            )
        self._snapshot = snapshot
        self._anchor_market = market
        self._fetched_at = mono


MARKET_CLOCK_URL_ENV = "Q_MARKET_CLOCK_URL"
MARKET_CLOCK_GENERATION_ENV = "Q_MARKET_CLOCK_GENERATION"
MARKET_DATA_CACHE_NAMESPACE_ENV = "Q_MARKET_DATA_CACHE_NAMESPACE"


class MarketClockConfigError(ValueError):
    """The market clock configuration is incomplete or would share caches across generations."""


def market_clock_from_env(environ: Mapping[str, str]) -> MarketClock:
    """Ordinary live time unless a remote market clock URL is configured explicitly.

    A remote clock requires its generation, and the market data cache namespace must equal that
    generation. Otherwise a new run could be served entries cached by an earlier one.
    """
    url = environ.get(MARKET_CLOCK_URL_ENV, "").strip()
    if not url:
        return SystemMarketClock()
    generation = environ.get(MARKET_CLOCK_GENERATION_ENV, "").strip()
    if not generation:
        raise MarketClockConfigError(f"{MARKET_CLOCK_GENERATION_ENV} is required with {MARKET_CLOCK_URL_ENV}")
    namespace = environ.get(MARKET_DATA_CACHE_NAMESPACE_ENV, "").strip()
    if namespace != generation:
        raise MarketClockConfigError(
            f"{MARKET_DATA_CACHE_NAMESPACE_ENV} must equal the market clock generation {generation!r}"
        )
    return RemoteMarketClock(url, expected_generation=generation, token=environ.get("Q_MT5_GATEWAY_TOKEN"))
