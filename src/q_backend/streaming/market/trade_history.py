"""Immutable session-trade snapshots and the Redis request channel that serves them.

The publisher process owns ingestion (``trades.py``). The API never polls the source and
never reconstructs a watermark: it asks the publisher for a snapshot through a Redis
request/result channel, and afterwards reads the immutable files the publisher wrote into
a bounded cache directory they share.

* A snapshot is one Arrow IPC file per opaque token plus a JSON descriptor, published by
  an atomic directory rename, so a reader sees the whole token or none of it.
* Tokens live ``ttl`` long. The cache never evicts an active token and never truncates:
  when active tokens leave no room, the request gets an explicit resource-limit outcome.
* Publisher restarts and source-generation changes invalidate tokens: both are recorded in
  Redis and checked on every page read.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import secrets
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import redis

from q_backend.streaming.keys import (
    TRADE_DIAGNOSTICS_KEY,
    TRADE_INSTANCE_KEY,
    TRADE_REQUEST_QUEUE_KEY,
    trade_generation_key,
    trade_result_key,
)
from q_backend.streaming.market.arrow import trades_record_batch, trades_schema
from q_backend.streaming.market.trades import (
    Columns,
    FrozenSession,
    TradeContext,
    TradeSessionCoordinator,
    iso_utc,
)

logger = logging.getLogger(__name__)

DEFAULT_SNAPSHOT_TTL = timedelta(minutes=10)
DEFAULT_CACHE_BYTES = 1 << 30
DEFAULT_PAGE_LIMIT = 10_000
MAX_PAGE_LIMIT = 50_000
BATCH_ROWS = 10_000
# Upper bound of an encoded row (five 8-byte columns, one 4-byte column, validity bits)
# plus the fixed schema, footer and descriptor cost of one token.
ROW_BYTES_ESTIMATE = 48
TOKEN_OVERHEAD_BYTES = 4096
TOMBSTONE_TTL = timedelta(hours=1)
MIN_REUSE_REMAINING = timedelta(seconds=60)
REQUEST_MAX_AGE_S = 30.0
RESULT_TTL_S = 30
INSTANCE_TTL_S = 30
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_SYMBOL = re.compile(r"^[A-Za-z0-9_.$#@!&-]{1,32}$")

Descriptor = dict[str, Any]


class SnapshotNotFound(LookupError):
    """The token is unknown, malformed or points outside the cache."""


class SnapshotExpired(LookupError):
    """The token expired, its source generation was replaced, or the publisher restarted."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CacheResourceLimit(RuntimeError):
    """Active snapshots leave no room for another; nothing is ever truncated to fit."""


class PublisherUnavailable(RuntimeError):
    """The publisher did not answer: it is down or has not started the trade service."""


class InvalidCursor(ValueError):
    pass


def valid_symbol(symbol: str) -> bool:
    return bool(_SYMBOL.match(symbol))


def bump(client: redis.Redis, name: str, amount: int = 1) -> None:
    """Count a diagnostic event in the shared Redis hash; diagnostics never break a request."""
    try:
        client.hincrby(TRADE_DIAGNOSTICS_KEY, name, amount)
    except redis.RedisError:
        logger.debug("trade diagnostics counter %s not recorded", name)


def _dir_size(path: Path) -> int:
    total = 0
    for entry in path.iterdir():
        try:
            total += entry.stat().st_size
        except FileNotFoundError:
            continue
    return total


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class TradeSnapshotCache:
    """A bounded directory of immutable snapshot tokens, safe across processes."""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = DEFAULT_CACHE_BYTES,
        ttl: timedelta = DEFAULT_SNAPSHOT_TTL,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._root = self.root.resolve()
        self.max_bytes = max_bytes
        self.ttl = ttl
        self.clock = clock

    # -- paths ---------------------------------------------------------------------------
    def _token_dir(self, snapshot_id: str) -> Path:
        if not _TOKEN.match(snapshot_id):
            raise SnapshotNotFound(snapshot_id)
        path = (self._root / snapshot_id).resolve()
        if path.parent != self._root:
            raise SnapshotNotFound(snapshot_id)
        return path

    def _tokens(self) -> list[tuple[Path, Descriptor]]:
        found: list[tuple[Path, Descriptor]] = []
        for path in self._root.iterdir():
            if path.name.startswith(".") or not path.is_dir():
                continue
            try:
                found.append((path, json.loads((path / "descriptor.json").read_text(encoding="utf-8"))))
            except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
                continue
        return found

    # -- writing -------------------------------------------------------------------------
    def write(
        self, frozen: FrozenSession, *, instance_id: str, first_page_limit: int = DEFAULT_PAGE_LIMIT
    ) -> Descriptor:
        """Persist ``frozen`` as a new immutable token, evicting only expired ones."""
        estimate = frozen.trade_count * ROW_BYTES_ESTIMATE + TOKEN_OVERHEAD_BYTES
        self._make_room(estimate)
        snapshot_id = secrets.token_urlsafe(16)
        staging = self._root / f".tmp-{snapshot_id}"
        staging.mkdir()
        try:
            self._write_rows(staging / "rows.arrow", frozen.columns, frozen.context)
            size = _dir_size(staging)
            if size > estimate:
                self._make_room(size)
            now = self.clock()
            descriptor = self._descriptor(frozen, snapshot_id, instance_id, now, first_page_limit)
            (staging / "descriptor.json").write_text(json.dumps(descriptor), encoding="utf-8")
            staging.rename(self._root / snapshot_id)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return descriptor

    def _descriptor(
        self, frozen: FrozenSession, snapshot_id: str, instance_id: str, now: datetime, page_limit: int
    ) -> Descriptor:
        context, coverage = frozen.context, frozen.coverage
        epoch, seq = frozen.watermark
        watermark = {"epoch": epoch, "seq": seq}
        return {
            "snapshot_id": snapshot_id,
            "symbol": context.symbol,
            "provider_id": context.provider_id,
            "source_generation": context.source_generation,
            "exchange_timezone": context.exchange_timezone,
            "session_key": context.session_key,
            "volume_field": context.volume_field,
            "volume_unit": context.volume_unit,
            "session_from": iso_utc(frozen.session_from),
            "session_to": iso_utc(frozen.session_to),
            "frozen_watermark": watermark,
            "coverage": {
                "provider_id": context.provider_id,
                "symbol": context.symbol,
                "source_generation": context.source_generation,
                "volume_field": context.volume_field,
                "volume_unit": context.volume_unit,
                "covered_from": iso_utc(coverage.covered_from),
                "covered_to": iso_utc(coverage.covered_to),
                "coverage_state": coverage.state,
                "classification_coverage": "partial" if coverage.state != "unavailable" else "unavailable",
                "coverage_reason": coverage.reason,
                "last_trade_watermark": watermark,
            },
            "trade_count": frozen.trade_count,
            "invalid_trade_count": frozen.invalid_trade_count,
            "first_page_url": f"/api/v1/market/trades/history?snapshot_id={snapshot_id}&limit={page_limit}",
            "expires_at": iso_utc(now + self.ttl),
            "_instance_id": instance_id,
        }

    @staticmethod
    def _write_rows(path: Path, columns: Columns, context: TradeContext) -> None:
        schema = trades_schema(context.as_mapping())
        count = len(columns["time_msc"])
        with pa.OSFile(str(path), "wb") as sink, pa.ipc.new_file(sink, schema) as writer:
            for start in range(0, count, BATCH_ROWS):
                part = {name: values[start : start + BATCH_ROWS] for name, values in columns.items()}
                writer.write_batch(trades_record_batch(part, context.as_mapping()))

    def _make_room(self, needed: int) -> None:
        if needed > self.max_bytes:
            raise CacheResourceLimit("snapshot larger than the whole trade cache")
        tokens = self._tokens()
        used = sum(_dir_size(path) for path, _ in tokens)
        if used + needed <= self.max_bytes:
            return
        now = self.clock()
        expired = sorted(
            ((path, d) for path, d in tokens if (path / "rows.arrow").exists() and _parse_time(d["expires_at"]) <= now),
            key=lambda item: item[1]["expires_at"],
        )
        for path, _descriptor in expired:
            used -= _dir_size(path)
            self._evict(path)
            used += _dir_size(path)
            if used + needed <= self.max_bytes:
                return
        raise CacheResourceLimit("active trade snapshots leave no cache room")

    @staticmethod
    def _evict(path: Path) -> None:
        """Drop a token's rows but keep its descriptor, so late readers get 'expired', not 'unknown'."""
        (path / "rows.arrow").unlink(missing_ok=True)

    def sweep(self) -> int:
        """Evict expired tokens' rows and old tombstones; returns the tokens evicted."""
        now = self.clock()
        removed = 0
        for path, descriptor in self._tokens():
            expires = _parse_time(descriptor["expires_at"])
            if expires <= now and (path / "rows.arrow").exists():
                self._evict(path)
                removed += 1
            elif now - expires > TOMBSTONE_TTL:
                shutil.rmtree(path, ignore_errors=True)
        for path in self._root.glob(".tmp-*"):
            if time.time() - path.stat().st_mtime > 3600:
                shutil.rmtree(path, ignore_errors=True)
        return removed

    def expire_all(self, keep_instance: str | None = None) -> int:
        """Drop every token not issued by ``keep_instance`` (a restart invalidates them all)."""
        removed = 0
        for path, descriptor in self._tokens():
            if descriptor.get("_instance_id") != keep_instance and (path / "rows.arrow").exists():
                self._evict(path)
                removed += 1
        return removed

    def find_reusable(self, frozen: FrozenSession, instance_id: str) -> Descriptor | None:
        """An active token for exactly this prefix, watermark and coverage, if one exists."""
        now = self.clock()
        for _path, d in self._tokens():
            if (
                (_path / "rows.arrow").exists()
                and d.get("_instance_id") == instance_id
                and d["source_generation"] == frozen.context.source_generation
                and d["symbol"] == frozen.context.symbol
                and d["frozen_watermark"] == {"epoch": frozen.watermark[0], "seq": frozen.watermark[1]}
                and d["trade_count"] == frozen.trade_count
                and d["coverage"]["coverage_state"] == frozen.coverage.state
                and d["coverage"]["coverage_reason"] == frozen.coverage.reason
                and _parse_time(d["expires_at"]) - now >= MIN_REUSE_REMAINING
            ):
                return d
        return None

    # -- reading -------------------------------------------------------------------------
    def read_descriptor(self, snapshot_id: str) -> Descriptor:
        path = self._token_dir(snapshot_id)
        try:
            descriptor = json.loads((path / "descriptor.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError) as exc:
            raise SnapshotNotFound(snapshot_id) from exc
        if _parse_time(descriptor["expires_at"]) <= self.clock():
            raise SnapshotExpired("snapshot_expired")
        return descriptor

    def read_page(self, snapshot_id: str, cursor: str | None, limit: int) -> tuple[bytes, int, str | None, int]:
        """``(arrow stream bytes, rows, next cursor, page count)`` for a validated token."""
        descriptor = self.read_descriptor(snapshot_id)
        offset = decode_cursor(snapshot_id, cursor)
        total = int(descriptor["trade_count"])
        if offset > total:
            raise InvalidCursor("cursor is past the end of the snapshot")
        try:
            with pa.memory_map(str(self._token_dir(snapshot_id) / "rows.arrow"), "r") as source:
                table = pa.ipc.open_file(source).read_all()
        except (FileNotFoundError, pa.ArrowInvalid) as exc:
            raise SnapshotExpired("snapshot_expired") from exc
        page = table.slice(offset, limit)
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            if page.num_rows:
                writer.write_table(page)
        end = offset + page.num_rows
        next_cursor = encode_cursor(snapshot_id, end) if end < total else None
        page_count = -(-total // limit)
        return sink.getvalue().to_pybytes(), page.num_rows, next_cursor, page_count


def encode_cursor(snapshot_id: str, offset: int) -> str:
    return base64.urlsafe_b64encode(f"{snapshot_id}:{offset}".encode()).decode().rstrip("=")


def decode_cursor(snapshot_id: str, cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        token, offset = raw.rsplit(":", 1)
        value = int(offset)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursor("malformed cursor") from exc
    if token != snapshot_id or value < 0:
        raise InvalidCursor("cursor does not belong to this snapshot")
    return value


# -- API side ---------------------------------------------------------------------------------
@dataclass
class SnapshotOutcome:
    kind: str  # ready | pending | unknown_symbol | unavailable | resource_limit
    descriptor: Descriptor | None = None
    pending: Mapping[str, Any] | None = None
    message: str | None = None


def public_descriptor(descriptor: Descriptor) -> Descriptor:
    return {k: v for k, v in descriptor.items() if not k.startswith("_")}


class TradeSnapshotClient:
    """Requests snapshots from the publisher process; holds no ingestion state."""

    def __init__(self, client: redis.Redis, *, timeout_s: float = 5.0) -> None:
        self.client = client
        self.timeout_s = timeout_s

    def publisher_instance(self) -> str:
        raw = self.client.get(TRADE_INSTANCE_KEY)
        if raw is None:
            raise PublisherUnavailable("the market publisher is not running the trade service")
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    def request_snapshot(self, symbol: str) -> SnapshotOutcome:
        self.publisher_instance()
        request_id = secrets.token_urlsafe(12)
        request = {"id": request_id, "op": "snapshot", "symbol": symbol, "ts": time.time()}
        self.client.rpush(TRADE_REQUEST_QUEUE_KEY, json.dumps(request))
        reply = self.client.blpop([trade_result_key(request_id)], timeout=self.timeout_s)
        if reply is None:
            raise PublisherUnavailable("the market publisher did not answer the snapshot request")
        body = json.loads(reply[1])
        return SnapshotOutcome(
            kind=body["outcome"],
            descriptor=body.get("descriptor"),
            pending=body.get("pending"),
            message=body.get("message"),
        )


class TradeHistoryReader:
    """Validates a token against the publisher and serves its immutable pages."""

    def __init__(self, cache: TradeSnapshotCache, client: redis.Redis) -> None:
        self.cache = cache
        self.client = client

    def read(self, snapshot_id: str, cursor: str | None, limit: int) -> tuple[Descriptor, bytes, str | None, int]:
        try:
            descriptor = self.cache.read_descriptor(snapshot_id)
        except SnapshotExpired:
            bump(self.client, "snapshot_expired")
            raise
        self._validate_owner(descriptor)
        data, _rows, next_cursor, page_count = self.cache.read_page(snapshot_id, cursor, limit)
        return descriptor, data, next_cursor, page_count

    def _validate_owner(self, descriptor: Descriptor) -> None:
        instance = self.client.get(TRADE_INSTANCE_KEY)
        if instance is None:
            raise PublisherUnavailable("the market publisher is not running the trade service")
        if (instance.decode() if isinstance(instance, bytes) else instance) != descriptor.get("_instance_id"):
            bump(self.client, "snapshot_expired")
            raise SnapshotExpired("publisher_restarted")
        generation = self.client.get(trade_generation_key(descriptor["symbol"]))
        if (generation.decode() if isinstance(generation, bytes) else generation) != descriptor["source_generation"]:
            bump(self.client, "snapshot_expired")
            raise SnapshotExpired("source_generation_replaced")


# -- publisher side ----------------------------------------------------------------------------
@dataclass
class TradeService:
    """Runs every symbol's coordinator and answers API snapshot requests (publisher process)."""

    client: redis.Redis
    cache: TradeSnapshotCache
    coordinators: Mapping[str, TradeSessionCoordinator]
    max_backfills: int = 2
    instance_id: str = field(default_factory=lambda: secrets.token_hex(8))
    step_budget_s: float = 0.25
    clock: Callable[[], float] = time.monotonic
    diagnostics_interval_s: float = 5.0

    def __post_init__(self) -> None:
        self._published_generation: dict[str, str] = {}
        self._next_heartbeat = 0.0
        self._next_diagnostics = 0.0
        self._started = False

    def start(self) -> None:
        """A new publisher instance expires every token an earlier one issued."""
        removed = self.cache.expire_all(keep_instance=self.instance_id)
        if removed:
            bump(self.client, "snapshot_expired", removed)
        self._heartbeat(force=True)
        self._started = True

    def step(self) -> int:
        """One cooperative pass: heartbeat, requests, then ingestion work for each symbol."""
        if not self._started:
            self.start()
        self._heartbeat()
        served = self.serve_requests()
        active = [c for c in self.coordinators.values() if c.backfilling][: max(1, self.max_backfills)]
        for coordinator in self.coordinators.values():
            may = coordinator in active or not coordinator.backfilling
            coordinator.step(budget_s=self.step_budget_s, may_backfill=may)
            self._sync_generation(coordinator)
        self._publish_diagnostics()
        return served

    # -- requests ---------------------------------------------------------------------------
    def serve_requests(self, limit: int = 32) -> int:
        served = 0
        for _ in range(limit):
            raw = self.client.lpop(TRADE_REQUEST_QUEUE_KEY)
            if raw is None:
                break
            try:
                request = json.loads(raw)
                if time.time() - float(request["ts"]) > REQUEST_MAX_AGE_S:
                    continue
                reply = self._handle(request)
            except (ValueError, KeyError, TypeError):
                logger.warning("ignoring malformed trade request")
                continue
            key = trade_result_key(request["id"])
            self.client.rpush(key, json.dumps(reply))
            self.client.expire(key, RESULT_TTL_S)
            served += 1
        return served

    def _handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if request.get("op") != "snapshot":
            return {"outcome": "unavailable", "message": "unsupported request"}
        symbol = str(request["symbol"])
        coordinator = self.coordinators.get(symbol)
        if coordinator is None:
            return {"outcome": "unknown_symbol", "message": f"Symbol {symbol!r} is not published."}
        if coordinator.phase == "unavailable":
            reason = coordinator.coverage().reason
            return {"outcome": "unknown_symbol" if reason == "symbol_not_found" else "unavailable", "message": reason}
        frozen = coordinator.freeze()
        if frozen is None:
            progress = coordinator.progress()
            return {
                "outcome": "pending",
                "pending": {
                    "status": "backfill_pending",
                    "status_token": progress.status_token,
                    "covered_to": iso_utc(progress.covered_to),
                    "rows": progress.rows,
                },
            }
        try:
            descriptor = self.cache.find_reusable(frozen, self.instance_id)
            if descriptor is None:
                self.cache.sweep()
                descriptor = self.cache.write(frozen, instance_id=self.instance_id)
        except CacheResourceLimit as exc:
            bump(self.client, "snapshot_resource_limit")
            return {"outcome": "resource_limit", "message": str(exc)}
        bump(self.client, "snapshots_served")
        return {"outcome": "ready", "descriptor": descriptor}

    # -- shared state ------------------------------------------------------------------------
    def _heartbeat(self, force: bool = False) -> None:
        now = self.clock()
        if force or now >= self._next_heartbeat:
            self.client.set(TRADE_INSTANCE_KEY, self.instance_id, ex=INSTANCE_TTL_S)
            self._next_heartbeat = now + INSTANCE_TTL_S / 3

    def _sync_generation(self, coordinator: TradeSessionCoordinator) -> None:
        symbol = coordinator.symbol
        if self._published_generation.get(symbol) == coordinator.generation:
            return
        self.client.set(trade_generation_key(symbol), coordinator.generation)
        self._published_generation[symbol] = coordinator.generation

    def _publish_diagnostics(self) -> None:
        now = self.clock()
        if now < self._next_diagnostics:
            return
        self._next_diagnostics = now + self.diagnostics_interval_s
        for symbol, coordinator in self.coordinators.items():
            self.client.hset(
                f"{TRADE_DIAGNOSTICS_KEY}:{symbol}",
                mapping={k: str(v) for k, v in coordinator.diagnostics.as_mapping().items()},
            )


__all__ = [
    "CacheResourceLimit",
    "InvalidCursor",
    "PublisherUnavailable",
    "SnapshotExpired",
    "SnapshotNotFound",
    "SnapshotOutcome",
    "TradeHistoryReader",
    "TradeService",
    "TradeSnapshotCache",
    "TradeSnapshotClient",
    "public_descriptor",
    "valid_symbol",
]
