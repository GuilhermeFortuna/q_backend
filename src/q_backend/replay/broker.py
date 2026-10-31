"""Simulated demo broker for the replay edge: orders, netting, protections and visibility.

The broker is driven only by the timeline. Every state change is a scheduled event processed in
``(market time, priority, sequence)`` order inside one store transaction, so an interrupted
advance leaves no partial effect. Client-visible state is event-sourced with visibility times:
an effect that happens at market instant ``t`` becomes readable at ``t + broker_response_ms``, and
a read captured at ingress reports only effects visible at that ingress instant.

Numerical rules come from ``q_core.execution`` (quote-model matching, fees); this module owns the
scheduling, order identity, netting ledger and visibility. Decimal arithmetic keeps money exact.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any

from q_core import execution as core

from q_backend.replay.config import ReplayRunConfig
from q_backend.replay.journal import QueuedEvent, ReplayRunStore, payload_hash
from q_backend.replay.market import ReplayMarket
from q_backend.replay.timeline import ReplayTimeline

# Event priorities at equal market time: expiry, market ticks (per symbol order), executions,
# command arrival and acceptance. Response visibility is a read-time property, not an event.
PRI_EXPIRY = 0
PRI_MARKET_BASE = 1000
PRI_EXECUTION = 4000
PRI_COMMAND = 5000

# MT5 return codes used in outcomes (the subset the edge contract needs).
RETCODE_DONE = 10009
RETCODE_REJECT = 10006
RETCODE_INVALID_VOLUME = 10014
RETCODE_INVALID_STOPS = 10016
RETCODE_NO_MONEY = 10019
RETCODE_NO_QUOTES = 10021
RETCODE_ORDER_NOT_FOUND = 10035
RETCODE_POSITION_NOT_FOUND = 10036

# A quote older than this market interval cannot price a fill (model assumption, documented).
QUOTE_MAX_AGE_US = 60_000_000
TICKET_START = 100_000
_MONEY = Decimal("0.01")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_intents (
    intent_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    received_us INTEGER NOT NULL,
    outcome TEXT,
    outcome_visible_us INTEGER,
    order_ticket INTEGER,
    closes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS broker_orders (
    ticket INTEGER PRIMARY KEY,
    intent_id TEXT,
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    side TEXT NOT NULL,
    role TEXT NOT NULL,
    volume_steps INTEGER NOT NULL,
    level_ticks INTEGER,
    sl_ticks INTEGER,
    tp_ticks INTEGER,
    expiration_us INTEGER,
    status TEXT NOT NULL,
    placed_visible_us INTEGER NOT NULL,
    closed_visible_us INTEGER,
    reason TEXT,
    comment TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_deals (
    ticket INTEGER PRIMARY KEY,
    order_ticket INTEGER NOT NULL,
    intent_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    volume_steps INTEGER NOT NULL,
    price_ticks INTEGER NOT NULL,
    entry INTEGER NOT NULL,
    position_ticket INTEGER NOT NULL,
    commission TEXT NOT NULL,
    profit TEXT NOT NULL,
    reason TEXT NOT NULL,
    comment TEXT NOT NULL,
    time_us INTEGER NOT NULL,
    visible_us INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_positions (
    symbol TEXT PRIMARY KEY,
    ticket INTEGER NOT NULL,
    net_steps INTEGER NOT NULL,
    basis TEXT NOT NULL,
    sl_ticks INTEGER,
    tp_ticks INTEGER,
    protection TEXT NOT NULL,
    opened_us INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_position_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    visible_us INTEGER NOT NULL,
    ticket INTEGER NOT NULL,
    net_steps INTEGER NOT NULL,
    basis TEXT NOT NULL,
    sl_ticks INTEGER,
    tp_ticks INTEGER,
    opened_us INTEGER NOT NULL
);
"""


class DuplicateIntentError(ValueError):
    """The intent id was already received; the broker never submits it again."""


class InvalidRequestError(ValueError):
    """A request that cannot be formed into an order (malformed numbers, unknown action)."""


def _decimal(value: float | int | str) -> Decimal:
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(str(value))


def _money(value: Decimal) -> str:
    return str(value.quantize(_MONEY))


class SimulatedBroker:
    """Order, position and deal state for one replay run. Single owner; callers hold the lock."""

    def __init__(
        self,
        *,
        store: ReplayRunStore,
        market: ReplayMarket,
        config: ReplayRunConfig,
        timeline: ReplayTimeline,
    ) -> None:
        self._store = store
        self._market = market
        self._config = config
        self._timeline = timeline
        self._install_schema()
        self._latency = config.latency
        self._symbols = {symbol: index for index, symbol in enumerate(config.symbols)}
        self._tick_size = {symbol: _decimal(market.spec(symbol).point) for symbol in config.symbols}
        self._volume_step = {symbol: _decimal(market.spec(symbol).volume_step) for symbol in config.symbols}
        self._contract = {symbol: _decimal(market.spec(symbol).trade_contract_size) for symbol in config.symbols}
        self._margin = {symbol: _decimal(market.spec(symbol).margin_per_contract) for symbol in config.symbols}

    def _install_schema(self) -> None:
        self._store.install_schema(_SCHEMA)

    # ------------------------------------------------------------------
    # feed and scheduling
    # ------------------------------------------------------------------
    def install_feed(self, conn: sqlite3.Connection, *, start_us: int, end_us: int, max_queued: int) -> None:
        """Schedule the first in-interval tick of every symbol. Warmup ticks are never replayed."""
        for symbol in self._config.symbols:
            index = self._market.first_index_at_or_after(symbol, start_us // 1000)
            if index < self._market.tick_count(symbol):
                self._schedule_tick(conn, symbol, index, end_us, max_queued)

    def _schedule_tick(self, conn, symbol: str, index: int, end_us: int, max_queued: int) -> None:
        if index >= self._market.tick_count(symbol):
            return
        tick_msc, _, _ = self._market.tick_at(symbol, index)
        at_us = tick_msc * 1000
        if at_us >= end_us:
            return
        self._timeline.schedule(
            conn,
            at_us=at_us,
            priority=PRI_MARKET_BASE + self._symbols[symbol],
            kind="market_tick",
            payload={"symbol": symbol, "index": index},
            max_queued_events=max_queued,
        )

    def _schedule(self, conn, *, at_us: int, priority: int, kind: str, payload: dict[str, Any]) -> None:
        self._timeline.schedule(
            conn,
            at_us=at_us,
            priority=priority,
            kind=kind,
            payload=payload,
            max_queued_events=self._config.limits.max_queued_events,
        )

    # ------------------------------------------------------------------
    # ingress
    # ------------------------------------------------------------------
    def receive(
        self, conn: sqlite3.Connection, *, intent_id: str, action: str, body: dict[str, Any], ingress_us: int
    ) -> None:
        """Record an intent at ingress and schedule its arrival at the broker."""
        state = conn.execute("SELECT state FROM run WHERE id = 1").fetchone()[0]
        if state in ("completed", "stopped", "failed"):
            raise ValueError("replay run is not active")
        digest = payload_hash({"action": action, **body})
        row = conn.execute("SELECT payload_hash FROM broker_intents WHERE intent_id = ?", (intent_id,)).fetchone()
        if row is not None:
            raise DuplicateIntentError(f"intent {intent_id} was already received")
        conn.execute(
            "INSERT INTO broker_intents (intent_id, action, payload_hash, received_us) VALUES (?, ?, ?, ?)",
            (intent_id, action, digest, ingress_us),
        )
        self._schedule(
            conn,
            at_us=ingress_us + self._latency.order_transport_ms * 1000,
            priority=PRI_COMMAND,
            kind="arrive",
            payload={"intent_id": intent_id, "action": action, "body": body},
        )

    # ------------------------------------------------------------------
    # event dispatch
    # ------------------------------------------------------------------
    def handle(self, event: QueuedEvent, conn: sqlite3.Connection) -> None:
        handlers = {
            "market_tick": self._on_tick,
            "arrive": self._on_arrive,
            "accept": self._on_accept,
            "execute": self._on_execute,
            "expire": self._on_expire,
        }
        handler = handlers.get(event.kind)
        if handler is None:
            raise ValueError(f"unknown broker event {event.kind!r}")
        handler(event, conn)

    def _now(self, event: QueuedEvent) -> int:
        return event.at_us

    def _visible(self, now_us: int) -> int:
        return now_us + self._latency.broker_response_ms * 1000

    def _on_tick(self, event: QueuedEvent, conn) -> None:
        symbol, index = event.payload["symbol"], int(event.payload["index"])
        tick_msc, bid, ask = self._market.tick_at(symbol, index)
        now = tick_msc * 1000
        quote = self._quote_ticks(symbol, now)
        if quote is not None:
            self._check_triggers(conn, symbol, now, quote)
        self._schedule_tick(conn, symbol, index + 1, self._config.end_us, self._config.limits.max_queued_events)

    def _quote_ticks(self, symbol: str, now_us: int) -> tuple[int, int] | None:
        """(bid, ask) in ticks from the latest priced quote, or None when missing or stale."""
        quote = self._market.quote_at(symbol, now_us)
        if quote is None:
            return None
        bid, ask, tick_msc = quote
        if now_us - tick_msc * 1000 > QUOTE_MAX_AGE_US:
            return None
        try:
            return self._to_ticks(symbol, bid), self._to_ticks(symbol, ask)
        except ValueError:
            return None

    def _to_ticks(self, symbol: str, price: float | Decimal) -> int:
        return int(
            core.price_to_ticks(price=format(_decimal(price), "f"), tick_size=format(self._tick_size[symbol], "f"))
        )

    def _price(self, symbol: str, ticks: int) -> Decimal:
        return Decimal(ticks) * self._tick_size[symbol]

    def _check_triggers(self, conn, symbol: str, now: int, quote: tuple[int, int]) -> None:
        bid, ask = quote
        slippage = self._market.spec(symbol).slippage_ticks
        for row in conn.execute(
            "SELECT ticket, kind, side, level_ticks FROM broker_orders"
            " WHERE symbol = ? AND status = 'working' AND kind IN ('stop', 'limit')"
            " ORDER BY ticket",
            (symbol,),
        ).fetchall():
            ticket, kind, side, level = row
            fill = core.match_quote(
                side=side, kind=kind, level_ticks=level, bid_ticks=bid, ask_ticks=ask, slippage_ticks=slippage
            )
            if fill is None:
                continue
            status = "triggered" if kind == "stop" else "scheduled"
            conn.execute("UPDATE broker_orders SET status = ? WHERE ticket = ?", (status, ticket))
            self._schedule(
                conn,
                at_us=now + self._latency.broker_execution_ms * 1000,
                priority=PRI_EXECUTION,
                kind="execute",
                payload={"ticket": ticket},
            )
        self._check_protections(conn, symbol, now, quote)

    def _check_protections(self, conn, symbol: str, now: int, quote: tuple[int, int]) -> None:
        position = conn.execute(
            "SELECT ticket, net_steps, sl_ticks, tp_ticks, protection FROM broker_positions"
            " WHERE symbol = ? AND net_steps != 0",
            (symbol,),
        ).fetchone()
        if position is None or position[4] != "idle":
            return
        ticket, net, sl, tp, _ = position
        bid, ask = quote
        closing = "sell" if net > 0 else "buy"
        slippage = self._market.spec(symbol).slippage_ticks
        for role, kind, level in (("sl", "stop", sl), ("tp", "limit", tp)):
            if level is None:
                continue
            fill = core.match_quote(
                side=closing, kind=kind, level_ticks=level, bid_ticks=bid, ask_ticks=ask, slippage_ticks=slippage
            )
            if fill is None:
                continue
            order = self._insert_order(
                conn,
                intent_id=None,
                symbol=symbol,
                kind=kind,
                side=closing,
                role=f"protection_{role}",
                volume_steps=abs(net),
                level_ticks=level,
                sl_ticks=None,
                tp_ticks=None,
                expiration=None,
                placed_us=now,
                comment=role,
                status="triggered" if kind == "stop" else "scheduled",
            )
            conn.execute("UPDATE broker_positions SET protection = 'scheduled' WHERE symbol = ?", (symbol,))
            self._schedule(
                conn,
                at_us=now + self._latency.broker_execution_ms * 1000,
                priority=PRI_EXECUTION,
                kind="execute",
                payload={"ticket": order},
            )
            return

    # ------------------------------------------------------------------
    # arrival and acceptance
    # ------------------------------------------------------------------
    def _on_arrive(self, event: QueuedEvent, conn) -> None:
        now = self._now(event)
        self._schedule(
            conn,
            at_us=now + self._latency.broker_accept_ms * 1000,
            priority=PRI_COMMAND,
            kind="accept",
            payload=dict(event.payload),
        )

    def _on_accept(self, event: QueuedEvent, conn) -> None:
        now = self._now(event)
        intent_id, action, body = event.payload["intent_id"], event.payload["action"], event.payload["body"]
        try:
            if action == "deal":
                self._accept_deal(conn, intent_id, body, now)
            elif action == "pending":
                self._accept_pending(conn, intent_id, body, now)
            elif action == "sltp":
                self._accept_sltp(conn, intent_id, body, now)
            elif action == "remove":
                self._accept_remove(conn, intent_id, body, now)
            elif action == "check":
                self._accept_check(conn, intent_id, body, now)
            else:
                raise InvalidRequestError(f"unknown action {action!r}")
        except InvalidRequestError as exc:
            self._reject(conn, intent_id, now, RETCODE_INVALID_VOLUME, str(exc))

    def _parse_volume(self, symbol: str, volume: float) -> int:
        spec = self._market.spec(symbol)
        if not (spec.volume_min <= volume <= spec.volume_max):
            raise InvalidRequestError(f"volume {volume} outside [{spec.volume_min}, {spec.volume_max}]")
        steps = _decimal(volume) / self._volume_step[symbol]
        if steps != steps.to_integral_value():
            raise InvalidRequestError(f"volume {volume} is not a multiple of {spec.volume_step}")
        return int(steps)

    def _free_margin(self, conn) -> Decimal:
        return self._balance(conn) - self._used_margin(conn)

    def _used_margin(self, conn) -> Decimal:
        total = Decimal(0)
        for symbol, net in conn.execute("SELECT symbol, net_steps FROM broker_positions").fetchall():
            total += abs(Decimal(net)) * self._volume_step[symbol] * self._margin[symbol]
        return total

    def _balance(self, conn) -> Decimal:
        total = _decimal(self._config.initial_balance)
        for profit, commission in conn.execute("SELECT profit, commission FROM broker_deals").fetchall():
            total += Decimal(profit) + Decimal(commission)
        return total

    def _accept_deal(self, conn, intent_id: str, body: dict, now: int) -> None:
        symbol = body["symbol"]
        volume = self._parse_volume(symbol, float(body["volume"]))
        side = body.get("side")
        if side not in ("buy", "sell"):
            raise InvalidRequestError("deal orders need side buy or sell")
        sl_ticks = self._optional_ticks(symbol, body.get("sl"))
        tp_ticks = self._optional_ticks(symbol, body.get("tp"))
        if self._quote_ticks(symbol, now) is None:
            self._reject(conn, intent_id, now, RETCODE_NO_QUOTES, "no fresh quote for the symbol")
            return
        required = self._required_margin(conn, symbol, side, volume)
        if required > self._free_margin(conn):
            self._reject(conn, intent_id, now, RETCODE_NO_MONEY, "insufficient funds for the margin")
            return
        ticket = self._insert_order(
            conn,
            intent_id=intent_id,
            symbol=symbol,
            kind="market",
            side=side,
            role="entry",
            volume_steps=volume,
            level_ticks=None,
            sl_ticks=sl_ticks,
            tp_ticks=tp_ticks,
            expiration=None,
            placed_us=now,
            comment=str(body.get("comment") or ""),
            status="accepted",
        )
        conn.execute("UPDATE broker_intents SET order_ticket = ? WHERE intent_id = ?", (ticket, intent_id))
        self._schedule(
            conn,
            at_us=now + self._latency.broker_execution_ms * 1000,
            priority=PRI_EXECUTION,
            kind="execute",
            payload={"ticket": ticket},
        )

    def _required_margin(self, conn, symbol: str, side: str, volume_steps: int) -> Decimal:
        row = conn.execute("SELECT net_steps FROM broker_positions WHERE symbol = ?", (symbol,)).fetchone()
        net = row[0] if row else 0
        signed = volume_steps if side == "buy" else -volume_steps
        # Margin is charged only for exposure that increases the absolute position.
        increase = max(0, abs(net + signed) - abs(net))
        return Decimal(increase) * self._volume_step[symbol] * self._margin[symbol]

    def _accept_pending(self, conn, intent_id: str, body: dict, now: int) -> None:
        symbol = body["symbol"]
        volume = self._parse_volume(symbol, float(body["volume"]))
        order_type = int(body["type"])
        kinds = {2: ("limit", "buy"), 3: ("limit", "sell"), 4: ("stop", "buy"), 5: ("stop", "sell")}
        if order_type not in kinds:
            raise InvalidRequestError("pending orders use type 2, 3, 4 or 5")
        kind, side = kinds[order_type]
        quote = self._quote_ticks(symbol, now)
        if quote is None:
            self._reject(conn, intent_id, now, RETCODE_NO_QUOTES, "no fresh quote for the symbol")
            return
        try:
            level = self._to_ticks(symbol, _decimal(body["price"]))
        except (KeyError, ValueError) as exc:
            raise InvalidRequestError("pending orders need a price on the tick grid") from exc
        bid, ask = quote
        valid = {
            ("limit", "buy"): level < ask,
            ("limit", "sell"): level > bid,
            ("stop", "buy"): level > ask,
            ("stop", "sell"): level < bid,
        }[(kind, side)]
        if not valid:
            self._reject(conn, intent_id, now, RETCODE_INVALID_STOPS, "pending price is on the wrong side of the quote")
            return
        expiration = body.get("expiration")
        expiration_us = int(expiration) * 1_000_000 if expiration else None
        if expiration_us is not None and expiration_us <= now:
            self._reject(conn, intent_id, now, RETCODE_INVALID_STOPS, "pending order expires before it is placed")
            return
        ticket = self._insert_order(
            conn,
            intent_id=intent_id,
            symbol=symbol,
            kind=kind,
            side=side,
            role="entry",
            volume_steps=volume,
            level_ticks=level,
            sl_ticks=self._optional_ticks(symbol, body.get("sl")),
            tp_ticks=self._optional_ticks(symbol, body.get("tp")),
            expiration=expiration_us,
            placed_us=now,
            comment=str(body.get("comment") or ""),
            status="working",
        )
        if expiration_us is not None:
            self._schedule(conn, at_us=expiration_us, priority=PRI_EXPIRY, kind="expire", payload={"ticket": ticket})
        self._decide(
            conn,
            intent_id,
            now,
            {"outcome": "accepted", "order_ticket": ticket, "retcode": RETCODE_DONE},
            close=False,
            order_ticket=ticket,
        )

    def _accept_sltp(self, conn, intent_id: str, body: dict, now: int) -> None:
        symbol = body["symbol"]
        position = conn.execute("SELECT ticket, net_steps FROM broker_positions WHERE symbol = ?", (symbol,)).fetchone()
        if position is None or position[1] == 0 or position[0] != int(body["position"]):
            self._reject(conn, intent_id, now, RETCODE_POSITION_NOT_FOUND, "position not found")
            return
        sl = self._optional_ticks(symbol, body.get("sl"))
        tp = self._optional_ticks(symbol, body.get("tp"))
        conn.execute("UPDATE broker_positions SET sl_ticks = ?, tp_ticks = ? WHERE symbol = ?", (sl, tp, symbol))
        self._record_position(conn, symbol, self._visible(now))
        self._decide(
            conn,
            intent_id,
            now,
            {"outcome": "accepted", "order_ticket": position[0], "retcode": RETCODE_DONE},
            close=True,
        )

    def _optional_ticks(self, symbol: str, value) -> int | None:
        if value is None or value == 0:
            return None
        try:
            return self._to_ticks(symbol, _decimal(value))
        except ValueError as exc:
            raise InvalidRequestError("protective level is off the tick grid") from exc

    def _accept_remove(self, conn, intent_id: str, body: dict, now: int) -> None:
        ticket = int(body["order_ticket"])
        row = conn.execute("SELECT status, kind, role FROM broker_orders WHERE ticket = ?", (ticket,)).fetchone()
        if row is None or row[2] != "entry" or row[1] == "market":
            self._reject(conn, intent_id, now, RETCODE_ORDER_NOT_FOUND, "pending order not found")
            return
        if row[0] not in ("working", "scheduled"):
            self._reject(conn, intent_id, now, RETCODE_REJECT, f"order is already {row[0]} and cannot be cancelled")
            return
        visible = self._visible(now)
        conn.execute(
            "UPDATE broker_orders SET status = 'cancelled', closed_visible_us = ?, reason = 'client'"
            " WHERE ticket = ?",
            (visible, ticket),
        )
        self._decide(
            conn, intent_id, now, {"outcome": "accepted", "order_ticket": ticket, "retcode": RETCODE_DONE}, close=True
        )

    def _accept_check(self, conn, intent_id: str, body: dict, now: int) -> None:
        symbol = body["symbol"]
        volume = self._parse_volume(symbol, float(body["volume"]))
        side = body.get("side", "buy")
        margin = self._required_margin(conn, symbol, side, volume)
        allowed = self._quote_ticks(symbol, now) is not None and margin <= self._free_margin(conn)
        result = {"allowed": allowed, "retcode": RETCODE_DONE if allowed else RETCODE_NO_MONEY, "margin": float(margin)}
        if not allowed:
            result["reason"] = "no fresh quote or insufficient funds"
        self._decide(conn, intent_id, now, result, close=True)

    def _reject(self, conn, intent_id: str, now: int, retcode: int, reason: str) -> None:
        self._decide(conn, intent_id, now, {"outcome": "rejected", "retcode": retcode, "reason": reason}, close=True)

    def _decide(
        self, conn, intent_id: str, now: int, outcome: dict, *, close: bool, order_ticket: int | None = None
    ) -> None:
        visible = self._visible(now)
        conn.execute(
            "UPDATE broker_intents SET outcome = ?, outcome_visible_us = ?, closes = ?,"
            " order_ticket = COALESCE(?, order_ticket) WHERE intent_id = ?",
            (json.dumps(outcome, sort_keys=True), visible, int(close), order_ticket, intent_id),
        )

    # ------------------------------------------------------------------
    # orders, fills and positions
    # ------------------------------------------------------------------
    def _next_ticket(self, conn) -> int:
        row = conn.execute(
            "SELECT MAX(t) FROM (SELECT MAX(ticket) AS t FROM broker_orders"
            " UNION ALL SELECT MAX(ticket) FROM broker_deals"
            " UNION ALL SELECT MAX(ticket) FROM broker_positions)"
        ).fetchone()
        return max(TICKET_START, int(row[0] or 0) + 1)

    def _insert_order(
        self,
        conn,
        *,
        intent_id,
        symbol,
        kind,
        side,
        role,
        volume_steps,
        level_ticks,
        sl_ticks,
        tp_ticks,
        expiration,
        placed_us,
        comment,
        status,
    ) -> int:
        ticket = self._next_ticket(conn)
        conn.execute(
            "INSERT INTO broker_orders (ticket, intent_id, symbol, kind, side, role, volume_steps,"
            " level_ticks, sl_ticks, tp_ticks, expiration_us, status, placed_visible_us, comment)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticket,
                intent_id,
                symbol,
                kind,
                side,
                role,
                volume_steps,
                level_ticks,
                sl_ticks,
                tp_ticks,
                expiration,
                status,
                self._visible(placed_us),
                comment,
            ),
        )
        return ticket

    def _on_execute(self, event: QueuedEvent, conn) -> None:
        now = self._now(event)
        ticket = int(event.payload["ticket"])
        row = conn.execute(
            "SELECT symbol, kind, side, volume_steps, level_ticks, status, intent_id, sl_ticks, tp_ticks, role,"
            " comment FROM broker_orders WHERE ticket = ?",
            (ticket,),
        ).fetchone()
        if row is None:
            return
        symbol, kind, side, volume, level, status, intent_id, sl, tp, role, comment = row
        if status not in ("accepted", "triggered", "scheduled"):
            return  # cancelled, expired or already filled: the scheduled execution is void
        quote = self._quote_ticks(symbol, now)
        if quote is None:
            self._finish_unfilled(conn, ticket, intent_id, now, "rejected", RETCODE_NO_QUOTES, "no fresh quote")
            return
        bid, ask = quote
        slippage = self._market.spec(symbol).slippage_ticks
        if kind == "limit":
            fill = core.match_quote(
                side=side, kind="limit", level_ticks=level, bid_ticks=bid, ask_ticks=ask, slippage_ticks=slippage
            )
            if fill is None:
                if role.startswith("protection"):
                    # Position-level protection creates a fresh order on the next touch.
                    self._finish_unfilled(conn, ticket, intent_id, now, "cancelled", RETCODE_REJECT, "price retreated")
                else:
                    conn.execute("UPDATE broker_orders SET status = 'working' WHERE ticket = ?", (ticket,))
                return
        else:
            fill = core.match_quote(
                side=side, kind="market", level_ticks=None, bid_ticks=bid, ask_ticks=ask, slippage_ticks=slippage
            )
        if fill is None:  # defensive: market fills always price from a valid quote
            return
        self._fill(
            conn,
            ticket=ticket,
            symbol=symbol,
            side=side,
            volume_steps=volume,
            price_ticks=int(fill),
            now=now,
            intent_id=intent_id,
            sl=sl,
            tp=tp,
            role=role,
            comment=comment,
            kind=kind,
        )

    def _finish_unfilled(self, conn, ticket: int, intent_id, now: int, status: str, retcode: int, reason: str):
        visible = self._visible(now)
        conn.execute(
            "UPDATE broker_orders SET status = ?, closed_visible_us = ?, reason = ? WHERE ticket = ?",
            (status, visible, reason, ticket),
        )
        if intent_id is not None:
            self._decide(
                conn, intent_id, now, {"outcome": "rejected", "retcode": retcode, "reason": reason}, close=True
            )
        # A failed protective exit re-arms its position so the level can trigger again.
        conn.execute(
            "UPDATE broker_positions SET protection = 'idle' WHERE protection = 'scheduled' AND symbol ="
            " (SELECT symbol FROM broker_orders WHERE ticket = ?)",
            (ticket,),
        )

    def _fill(
        self, conn, *, ticket, symbol, side, volume_steps, price_ticks, now, intent_id, sl, tp, role, comment, kind
    ) -> None:
        signed = volume_steps if side == "buy" else -volume_steps
        visible = self._visible(now)
        row = conn.execute(
            "SELECT ticket, net_steps, basis, sl_ticks, tp_ticks, opened_us FROM broker_positions" " WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if row is None:
            position_ticket, net, basis, opened = None, 0, Decimal(0), now
            cur_sl = cur_tp = None
        else:
            position_ticket, net, basis, cur_sl, cur_tp, opened = (
                row[0],
                row[1],
                Decimal(row[2]),
                row[3],
                row[4],
                row[5],
            )
        price = self._price(symbol, price_ticks)
        step, contract = self._volume_step[symbol], self._contract[symbol]
        remaining = signed
        reduces = net != 0 and (net > 0) != (signed > 0)
        if reduces:
            closed = min(abs(net), abs(signed))
            profit = (price - basis) * Decimal(closed) * step * contract * (1 if net > 0 else -1)
            self._insert_deal(
                conn,
                ticket=ticket,
                order=ticket,
                intent_id=intent_id,
                symbol=symbol,
                side=side,
                steps=closed,
                price_ticks=price_ticks,
                entry=1,
                position=position_ticket,
                profit=profit,
                now=now,
                visible=visible,
                reason=("client" if role == "entry" else role.replace("protection_", "")),
                comment=comment,
            )
            net += closed if net < 0 else -closed
            remaining = signed + (closed if signed < 0 else -closed)
            if net == 0:
                position_ticket, basis, cur_sl, cur_tp = None, Decimal(0), None, None
        if remaining != 0 and (net == 0 or (net > 0) == (remaining > 0)):
            # Opening (or adding to) exposure in the direction of the remaining quantity.
            new_steps = abs(net) + abs(remaining)
            if net == 0:
                position_ticket = ticket
                basis = price
                opened = now
                if role == "entry":
                    cur_sl, cur_tp = sl, tp
            else:
                basis = (basis * abs(net) + price * abs(remaining)) / Decimal(new_steps)
            net = net + remaining
            self._insert_deal(
                conn,
                ticket=ticket,
                order=ticket,
                intent_id=intent_id,
                symbol=symbol,
                side=side,
                steps=abs(remaining),
                price_ticks=price_ticks,
                entry=0,
                position=position_ticket,
                profit=Decimal(0),
                now=now,
                visible=visible,
                reason="client" if role == "entry" else role.replace("protection_", ""),
                comment=comment,
            )
        if net == 0:
            conn.execute("DELETE FROM broker_positions WHERE symbol = ?", (symbol,))
        else:
            sl_value = cur_sl if cur_sl is not None else None
            tp_value = cur_tp if cur_tp is not None else None
            conn.execute(
                "INSERT INTO broker_positions (symbol, ticket, net_steps, basis, sl_ticks, tp_ticks,"
                " protection, opened_us) VALUES (?, ?, ?, ?, ?, ?, 'idle', ?)"
                " ON CONFLICT(symbol) DO UPDATE SET ticket = excluded.ticket, net_steps = excluded.net_steps,"
                " basis = excluded.basis, sl_ticks = excluded.sl_ticks, tp_ticks = excluded.tp_ticks,"
                " opened_us = excluded.opened_us, protection = 'idle'",
                (symbol, position_ticket, net, format(basis, "f"), sl_value, tp_value, opened),
            )
        conn.execute(
            "UPDATE broker_orders SET status = 'filled', closed_visible_us = ? WHERE ticket = ?",
            (visible, ticket),
        )
        if intent_id is not None:
            self._decide(
                conn,
                intent_id,
                now,
                {"outcome": "accepted", "order_ticket": ticket, "retcode": RETCODE_DONE},
                close=True,
                order_ticket=ticket,
            )
        self._record_position(conn, symbol, visible)

    def _insert_deal(
        self,
        conn,
        *,
        ticket,
        order,
        intent_id,
        symbol,
        side,
        steps,
        price_ticks,
        entry,
        position,
        profit,
        now,
        visible,
        reason,
        comment,
    ) -> None:
        deal_ticket = self._next_ticket(conn)
        commission = core.fill_cost(
            quantity_steps=int(steps),
            volume_step=format(self._volume_step[symbol], "f"),
            price_ticks=int(price_ticks),
            tick_size=format(self._tick_size[symbol], "f"),
            point_value=format(self._contract[symbol], "f"),
            cost_per_contract=self._market.spec(symbol).commission_per_contract,
            cost_bps="0",
            currency_decimals=2,
        )
        conn.execute(
            "INSERT INTO broker_deals (ticket, order_ticket, intent_id, symbol, side, volume_steps,"
            " price_ticks, entry, position_ticket, commission, profit, reason, comment, time_us, visible_us)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                deal_ticket,
                order,
                intent_id,
                symbol,
                side,
                int(steps),
                int(price_ticks),
                entry,
                position or deal_ticket,
                _money(-Decimal(commission)) if Decimal(commission) else "0.00",
                _money(profit),
                reason,
                comment,
                now,
                visible,
            ),
        )

    def _on_expire(self, event: QueuedEvent, conn) -> None:
        ticket = int(event.payload["ticket"])
        row = conn.execute("SELECT status, intent_id FROM broker_orders WHERE ticket = ?", (ticket,)).fetchone()
        if row is None or row[0] not in ("working", "scheduled"):
            return  # exclusive expiry: a fill or cancel that came first wins
        visible = self._visible(event.at_us)
        conn.execute(
            "UPDATE broker_orders SET status = 'expired', closed_visible_us = ?, reason = 'expired'"
            " WHERE ticket = ?",
            (visible, ticket),
        )

    def _record_position(self, conn, symbol: str, visible: int) -> None:
        row = conn.execute(
            "SELECT ticket, net_steps, basis, sl_ticks, tp_ticks, opened_us FROM broker_positions" " WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO broker_position_events (symbol, visible_us, ticket, net_steps, basis,"
                " sl_ticks, tp_ticks, opened_us) VALUES (?, ?, 0, 0, '0', NULL, NULL, 0)",
                (symbol, visible),
            )
            return
        conn.execute(
            "INSERT INTO broker_position_events (symbol, visible_us, ticket, net_steps, basis, sl_ticks,"
            " tp_ticks, opened_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, visible, row[0], row[1], row[2], row[3], row[4], row[5]),
        )

    # ------------------------------------------------------------------
    # end of data
    # ------------------------------------------------------------------
    def finish(self, conn: sqlite3.Connection, end_us: int) -> None:
        """End of data or stop: cancel untriggered orders, reject unexecuted requests, never flatten."""
        visible = self._visible(end_us)
        conn.execute(
            "UPDATE broker_orders SET status = 'cancelled', closed_visible_us = ?, reason = 'end_of_data'"
            " WHERE status = 'working' AND role = 'entry'",
            (visible,),
        )
        for ticket, intent_id in conn.execute(
            "SELECT ticket, intent_id FROM broker_orders WHERE status IN ('accepted', 'triggered', 'scheduled')"
        ).fetchall():
            conn.execute(
                "UPDATE broker_orders SET status = 'rejected', closed_visible_us = ?,"
                " reason = 'end_of_data' WHERE ticket = ?",
                (visible, ticket),
            )
            if intent_id is not None:
                self._decide(
                    conn,
                    intent_id,
                    end_us,
                    {"outcome": "rejected", "retcode": RETCODE_REJECT, "reason": "end_of_data"},
                    close=True,
                )
        conn.execute("UPDATE broker_positions SET protection = 'idle' WHERE protection = 'scheduled'")
        for (intent_id,) in conn.execute("SELECT intent_id FROM broker_intents WHERE outcome IS NULL").fetchall():
            self._decide(
                conn,
                intent_id,
                end_us,
                {"outcome": "rejected", "retcode": RETCODE_REJECT, "reason": "end_of_data"},
                close=True,
            )

    # ------------------------------------------------------------------
    # reads: every answer is the state visible at the ingress instant
    # ------------------------------------------------------------------
    def intent_outcome(self, intent_id: str, ingress_us: int) -> dict[str, Any] | None:
        """Decided outcome visible at ``ingress_us``, or None while still in flight or unknown."""
        row = self._store._conn.execute(  # noqa: SLF001
            "SELECT outcome, outcome_visible_us, closes FROM broker_intents WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        if row is None or row[0] is None or row[1] > ingress_us:
            return None
        outcome = json.loads(row[0])
        outcome["closes"] = bool(row[2])
        return outcome

    def intent_known(self, intent_id: str) -> bool:
        row = self._store._conn.execute(  # noqa: SLF001
            "SELECT 1 FROM broker_intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        return row is not None

    def positions(self, ingress_us: int, symbol: str | None = None) -> list[dict[str, Any]]:
        conn = self._store._conn  # noqa: SLF001
        rows = conn.execute(
            "SELECT e.symbol, e.ticket, e.net_steps, e.basis, e.sl_ticks, e.tp_ticks, e.opened_us"
            " FROM broker_position_events e WHERE e.visible_us <= ? AND e.seq IN ("
            " SELECT MAX(seq) FROM broker_position_events WHERE visible_us <= ? GROUP BY symbol)"
            " AND e.net_steps != 0 ORDER BY e.symbol",
            (ingress_us, ingress_us),
        ).fetchall()
        result = []
        for sym, ticket, net, basis, sl, tp, opened in rows:
            if symbol and sym != symbol:
                continue
            step = self._volume_step[sym]
            result.append(
                {
                    "ticket": ticket,
                    "symbol": sym,
                    "type": 0 if net > 0 else 1,
                    "volume": float(abs(net) * step),
                    "price_open": float(Decimal(basis)),
                    "sl": float(self._price(sym, sl)) if sl is not None else 0.0,
                    "tp": float(self._price(sym, tp)) if tp is not None else 0.0,
                    "magic": 0,
                    "comment": "",
                    "time": opened // 1_000_000,
                }
            )
        return result

    def orders(self, ingress_us: int, symbol: str | None = None) -> list[dict[str, Any]]:
        conn = self._store._conn  # noqa: SLF001
        rows = conn.execute(
            "SELECT ticket, symbol, kind, side, volume_steps, level_ticks, sl_ticks, tp_ticks, expiration_us,"
            " placed_visible_us FROM broker_orders WHERE role = 'entry' AND kind IN ('stop', 'limit')"
            " AND placed_visible_us <= ? AND (closed_visible_us IS NULL OR closed_visible_us > ?)"
            " ORDER BY ticket",
            (ingress_us, ingress_us),
        ).fetchall()
        result = []
        for ticket, sym, kind, side, volume, level, sl, tp, expiration, _ in rows:
            if symbol and sym != symbol:
                continue
            order_type = {("limit", "buy"): 2, ("limit", "sell"): 3, ("stop", "buy"): 4, ("stop", "sell"): 5}[
                (kind, side)
            ]
            result.append(
                {
                    "ticket": ticket,
                    "symbol": sym,
                    "type": order_type,
                    "volume": float(volume * self._volume_step[sym]),
                    "price": float(self._price(sym, level)),
                    "sl": float(self._price(sym, sl)) if sl is not None else 0.0,
                    "tp": float(self._price(sym, tp)) if tp is not None else 0.0,
                    "expiration": expiration // 1_000_000 if expiration else None,
                    "magic": 0,
                    "comment": "",
                    "time_setup": 0,
                }
            )
        return result

    def deals(self, ingress_us: int, start_us: int, end_us: int, symbol: str | None = None) -> list[dict[str, Any]]:
        conn = self._store._conn  # noqa: SLF001
        rows = conn.execute(
            "SELECT ticket, order_ticket, symbol, side, volume_steps, price_ticks, entry, position_ticket,"
            " commission, profit, reason, comment, time_us FROM broker_deals"
            " WHERE visible_us <= ? AND time_us >= ? AND time_us < ? ORDER BY time_us, ticket",
            (ingress_us, start_us, end_us),
        ).fetchall()
        result = []
        for (
            ticket,
            order,
            sym,
            side,
            steps,
            price,
            entry,
            position,
            commission,
            profit,
            reason,
            comment,
            time_us,
        ) in rows:
            if symbol and sym != symbol:
                continue
            result.append(
                {
                    "ticket": ticket,
                    "order_ticket": order,
                    "symbol": sym,
                    "type": 0 if side == "buy" else 1,
                    "entry": entry,
                    "volume": float(steps * self._volume_step[sym]),
                    "price": float(self._price(sym, price)),
                    "commission": float(commission),
                    "swap": 0.0,
                    "profit": float(profit),
                    "fee": 0.0,
                    "magic": 0,
                    "comment": comment,
                    "time_msc": time_us // 1000,
                    "position_id": position,
                    "reason": reason if reason in ("client", "expert", "sl", "tp", "so", "other") else "other",
                }
            )
        return result

    def account(self, ingress_us: int) -> dict[str, Any]:
        conn = self._store._conn  # noqa: SLF001
        balance = _decimal(self._config.initial_balance)
        for profit, commission in conn.execute(
            "SELECT profit, commission FROM broker_deals WHERE visible_us <= ?", (ingress_us,)
        ).fetchall():
            balance += Decimal(profit) + Decimal(commission)
        used = Decimal(0)
        floating = Decimal(0)
        for sym, net, basis in conn.execute(
            "SELECT symbol, net_steps, basis FROM broker_position_events e WHERE visible_us <= ? AND seq IN"
            " (SELECT MAX(seq) FROM broker_position_events WHERE visible_us <= ? GROUP BY symbol)",
            (ingress_us, ingress_us),
        ).fetchall():
            used += abs(Decimal(net)) * self._volume_step[sym] * self._margin[sym]
            quote = self._market.quote_at(sym, self._quote_cursor(ingress_us))
            if net and quote is not None:
                mark = _decimal(quote[0] if net > 0 else quote[1])
                floating += (mark - Decimal(basis)) * Decimal(net) * self._volume_step[sym] * self._contract[sym]
        return {
            "login": self._config.account.login,
            "server": "q-replay-demo",
            "currency": self._config.account.currency,
            "trade_allowed": True,
            "terminal_trade_allowed": True,
            "balance": float(balance),
            "equity": float(balance + floating),
            "margin_free": float(balance + floating - used),
            "trade_mode": "demo",
        }

    def quote(self, symbol: str, now_us: int) -> dict[str, Any] | None:
        now_us = self._quote_cursor(now_us)
        quote = self._market.quote_at(symbol, now_us)
        if quote is None:
            return None
        bid, ask, tick_msc = quote
        return {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "last": bid,
            "time_msc": tick_msc,
            "age_ms": max(0, now_us // 1000 - tick_msc),
        }

    def _quote_cursor(self, visible_us: int) -> int:
        """Delivery drain exposes final effects, never market ticks beyond the trading cutoff."""
        run = self._store.load_run()
        cutoff = self._config.end_us - 1
        if run is not None and run.state == "stopped":
            cutoff = min(cutoff, run.cursor_us - self._latency.broker_response_ms * 1000)
        return min(visible_us, cutoff)

    def lookup(self, intent_id: str, kind: str, ingress_us: int) -> dict[str, Any]:
        """Lookup answer that never reports not_found for an intent still in flight."""
        if not self.intent_known(intent_id):
            return {"outcome": "not_found", "closes_intent": True}
        outcome = self.intent_outcome(intent_id, ingress_us)
        if outcome is None:
            return {"outcome": "unavailable", "reason": "intent still in flight", "closes_intent": False}
        conn = self._store._conn  # noqa: SLF001
        ticket = outcome.get("order_ticket")
        if kind == "pending":
            if outcome["outcome"] == "rejected":
                return {
                    "outcome": "rejected",
                    "retcode": outcome["retcode"],
                    "reason": outcome["reason"],
                    "closes_intent": True,
                }
            if ticket is None:
                return {"outcome": "not_found", "closes_intent": True}
            row = conn.execute("SELECT status FROM broker_orders WHERE ticket = ?", (ticket,)).fetchone()
            status = row[0] if row else "expired"
            if status in ("working", "scheduled"):
                return {"outcome": "placed", "order_ticket": ticket, "closes_intent": False}
            if status == "cancelled":
                return {"outcome": "cancelled", "closes_intent": True}
            if status == "expired":
                return {"outcome": "expired", "closes_intent": True}
            if status == "filled":
                return {"outcome": "filled", "deals": self._deals_of(ticket, ingress_us), "closes_intent": True}
            return {"outcome": "rejected", "retcode": RETCODE_REJECT, "reason": status, "closes_intent": True}
        if outcome["outcome"] == "rejected":
            return {
                "outcome": "rejected",
                "retcode": outcome["retcode"],
                "reason": outcome["reason"],
                "closes_intent": True,
            }
        if ticket is not None:
            deals = self._deals_of(ticket, ingress_us)
            if deals:
                return {"outcome": "filled", "deals": deals, "closes_intent": True}
        return {"outcome": "not_found", "closes_intent": True}

    def _deals_of(self, order_ticket: int, ingress_us: int) -> list[dict[str, Any]]:
        conn = self._store._conn  # noqa: SLF001
        return [deal for deal in self.deals(ingress_us, 0, 2**62) if deal["order_ticket"] == order_ticket]


__all__ = [
    "DuplicateIntentError",
    "InvalidRequestError",
    "SimulatedBroker",
    "PRI_COMMAND",
    "PRI_EXECUTION",
    "PRI_EXPIRY",
    "PRI_MARKET_BASE",
    "QUOTE_MAX_AGE_US",
]
