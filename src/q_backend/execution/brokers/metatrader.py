"""Locked MT5 live broker adapter (implemented, not production-validated)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from q_backend.execution.brokers.base import (
    BrokerHealth,
    BrokerRejection,
    BrokerRejectionCode,
    BrokerSubmissionOutcome,
    BrokerSubmissionResult,
    Clock,
    MarketOrderRequest,
    PaperCostConfig,
)
from q_backend.execution.brokers.live_gates import LiveExecutionGates, evaluate_live_gates
from q_backend.execution.brokers.mt5_constants import (
    Mt5OrderFilling,
    Mt5OrderType,
    Mt5OrderTime,
    Mt5Retcode,
    Mt5SymbolFilling,
    Mt5TradeAction,
    is_success_retcode,
    is_unknown_retcode,
    retcode_label,
)
from q_backend.execution.brokers.mt5_runtime import DefaultMt5Runtime, Mt5Runtime
from q_backend.execution.brokers.paper import validate_quote
from q_backend.execution.domain import BrokerMode, ExecutionSide, FillRecord

logger = logging.getLogger(__name__)

_SENSITIVE_REQUEST_KEYS = frozenset({"password"})


def intent_magic(order_id: UUID, *, base: int = 0) -> int:
    """Stable MT5 magic number derived from Q order identity."""
    return int((base ^ (order_id.int & 0x7FFFFFFF)) & 0x7FFFFFFF)


def intent_comment(order_id: UUID) -> str:
    """MT5-safe comment carrying Q intent identity (<=31 chars)."""
    compact = str(order_id).replace("-", "")[:24]
    return f"q:{compact}"


def sanitize_request_for_storage(request: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in request.items() if k not in _SENSITIVE_REQUEST_KEYS}


def normalize_volume(quantity: Decimal, *, step: float, vmin: float, vmax: float) -> float:
    step_d = Decimal(str(step))
    qty = quantity
    if step_d > 0:
        steps = (qty / step_d).quantize(Decimal("1"))
        qty = steps * step_d
    value = float(qty)
    if value < vmin:
        raise ValueError("volume below symbol minimum")
    if vmax > 0 and value > vmax:
        raise ValueError("volume above symbol maximum")
    return value


def normalize_price(price: float, digits: int) -> float:
    return round(price, digits)


def select_filling_mode(symbol_filling_mode: int) -> int:
    """Pick a symbol-supported filling policy (never hard-code RETURN)."""
    mode = int(symbol_filling_mode)
    if mode & int(Mt5SymbolFilling.FOK):
        return int(Mt5OrderFilling.FOK)
    if mode & int(Mt5SymbolFilling.IOC):
        return int(Mt5OrderFilling.IOC)
    if mode & int(Mt5SymbolFilling.RETURN):
        return int(Mt5OrderFilling.RETURN)
    raise ValueError("symbol does not advertise a supported filling mode")


def map_side_to_mt5(side: ExecutionSide) -> int:
    return int(Mt5OrderType.BUY if side == ExecutionSide.BUY else Mt5OrderType.SELL)


class MetaTraderBroker:
    """Translate domain market orders to MT5 requests; reconcile real fills.

    Capability is **live_locked** by default. Submission requires all activation
    gates, a controlled-account validation record, and ``dry_run=False``.
    """

    def __init__(
        self,
        *,
        runtime: Optional[Mt5Runtime] = None,
        clock: Clock,
        gates: LiveExecutionGates,
        dry_run: bool = True,
        slippage_deviation: int = 20,
        max_quote_age_seconds: float = 30.0,
        magic_base: int = 0,
        reconcile_lookback: timedelta = timedelta(hours=24),
    ) -> None:
        self._runtime = runtime or DefaultMt5Runtime()
        self._clock = clock
        self._gates = gates
        self._dry_run = dry_run
        self._slippage_deviation = slippage_deviation
        self._max_quote_age_seconds = max_quote_age_seconds
        self._magic_base = magic_base
        self._reconcile_lookback = reconcile_lookback

    def health(self) -> BrokerHealth:
        now = self._clock.now()
        if self._runtime.is_stub():
            return BrokerHealth(
                broker_mode=BrokerMode.MT5_LIVE,
                is_available=False,
                message="MT5 live adapter unavailable (stub platform)",
                checked_at=now,
            )
        account = self._runtime.account_info()
        available = account is not None
        message = (
            "MT5 live adapter connected"
            if available
            else "MT5 live adapter cannot read account_info"
        )
        return BrokerHealth(
            broker_mode=BrokerMode.MT5_LIVE,
            is_available=available,
            message=message,
            checked_at=now,
        )

    def submit_market_order(
        self,
        request: MarketOrderRequest,
        *,
        cost_config: PaperCostConfig,
    ) -> BrokerSubmissionResult:
        now = self._clock.now()
        account = self._runtime.account_info()
        gate_rejection = evaluate_live_gates(
            self._gates,
            account_login=getattr(account, "login", None),
        )
        if gate_rejection is not None:
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=gate_rejection,
                cost_config=cost_config,
            )

        preflight = self._preflight(request, now=now, account=account)
        if preflight is not None:
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=preflight,
                cost_config=cost_config,
            )

        symbol_info = self._runtime.symbol_info(request.symbol)
        tick = self._runtime.symbol_info_tick(request.symbol)
        assert symbol_info is not None and tick is not None

        try:
            volume = normalize_volume(
                request.quantity,
                step=float(symbol_info.volume_step),
                vmin=float(symbol_info.volume_min),
                vmax=float(symbol_info.volume_max),
            )
            filling = select_filling_mode(int(symbol_info.filling_mode))
        except ValueError as exc:
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=BrokerRejection(
                    code=BrokerRejectionCode.INVALID_QUANTITY
                    if "volume" in str(exc)
                    else BrokerRejectionCode.UNSUPPORTED_FILL_POLICY,
                    message=str(exc),
                ),
                cost_config=cost_config,
            )

        side_price = float(tick.ask if request.side == ExecutionSide.BUY else tick.bid)
        price = normalize_price(side_price, int(symbol_info.digits))
        magic = intent_magic(request.order_id, base=self._magic_base)
        comment = intent_comment(request.order_id)
        mt5_request: dict[str, Any] = {
            "action": int(Mt5TradeAction.DEAL),
            "symbol": request.symbol,
            "volume": volume,
            "type": map_side_to_mt5(request.side),
            "price": price,
            "deviation": self._slippage_deviation,
            "magic": magic,
            "comment": comment,
            "type_time": int(Mt5OrderTime.GTC),
            "type_filling": filling,
        }
        stored_request = sanitize_request_for_storage(mt5_request)

        check = self._runtime.order_check(mt5_request)
        if check is None:
            code, desc = self._runtime.last_error()
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=BrokerRejection(
                    code=BrokerRejectionCode.ORDER_CHECK_FAILED,
                    message=f"order_check failed: {desc}",
                    context={"mt5_error_code": code},
                ),
                raw_request=stored_request,
                cost_config=cost_config,
            )
        check_retcode = int(getattr(check, "retcode", 0))
        if not is_success_retcode(check_retcode):
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=BrokerRejection(
                    code=BrokerRejectionCode.ORDER_CHECK_FAILED,
                    message=f"order_check rejected: {retcode_label(check_retcode)}",
                    context={"retcode": check_retcode},
                ),
                raw_request=stored_request,
                raw_response={"check_retcode": check_retcode},
                cost_config=cost_config,
            )

        if self._dry_run:
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=BrokerRejection(
                    code=BrokerRejectionCode.LIVE_LOCKED,
                    message=(
                        "live adapter is implemented and order_check passed, "
                        "but dry_run prevents order_send in this environment"
                    ),
                    context={"capability": "live_locked"},
                ),
                raw_request=stored_request,
                raw_response={"check_retcode": check_retcode},
                cost_config=cost_config,
            )

        send = self._runtime.order_send(mt5_request)
        if send is None:
            recovered = self.recover_unknown(request, now=now)
            if recovered.outcome == BrokerSubmissionOutcome.FILLED:
                return recovered
            code, desc = self._runtime.last_error()
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.UNKNOWN,
                rejection=BrokerRejection(
                    code=BrokerRejectionCode.ORDER_SEND_FAILED,
                    message=f"order_send returned None: {desc}",
                    context={"mt5_error_code": code},
                ),
                raw_request=stored_request,
                metadata={"magic": magic, "comment": comment},
                cost_config=cost_config,
            )

        send_retcode = int(getattr(send, "retcode", 0))
        raw_response = {
            "retcode": send_retcode,
            "order": int(getattr(send, "order", 0) or 0),
            "deal": int(getattr(send, "deal", 0) or 0),
        }
        external_order_id = str(raw_response["order"]) if raw_response["order"] else None

        if is_unknown_retcode(send_retcode):
            recovered = self.recover_unknown(
                request,
                now=now,
                external_order_id=external_order_id,
            )
            if recovered.outcome != BrokerSubmissionOutcome.UNKNOWN:
                return recovered
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.UNKNOWN,
                rejection=BrokerRejection(
                    code=BrokerRejectionCode.ORDER_SEND_FAILED,
                    message=f"ambiguous send outcome: {retcode_label(send_retcode)}",
                    context={"retcode": send_retcode},
                ),
                external_order_id=external_order_id,
                raw_request=stored_request,
                raw_response=raw_response,
                metadata={"magic": magic, "comment": comment},
                cost_config=cost_config,
            )

        if not is_success_retcode(send_retcode):
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=BrokerRejection(
                    code=BrokerRejectionCode.ORDER_SEND_FAILED,
                    message=f"order_send rejected: {retcode_label(send_retcode)}",
                    context={"retcode": send_retcode},
                ),
                external_order_id=external_order_id,
                raw_request=stored_request,
                raw_response=raw_response,
                cost_config=cost_config,
            )

        reconciled = self._reconcile_fill(
            request,
            now=now,
            magic=magic,
            comment=comment,
            external_order_id=external_order_id,
            fallback_deal_id=str(raw_response["deal"]) if raw_response["deal"] else None,
        )
        if reconciled is None:
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.UNKNOWN,
                external_order_id=external_order_id,
                raw_request=stored_request,
                raw_response=raw_response,
                metadata={"magic": magic, "comment": comment},
                cost_config=cost_config,
            )

        fill, deal_ids = reconciled
        return BrokerSubmissionResult(
            outcome=BrokerSubmissionOutcome.FILLED,
            fill=fill,
            external_order_id=external_order_id,
            external_deal_ids=deal_ids,
            raw_request=stored_request,
            raw_response=raw_response,
            metadata={"magic": magic, "comment": comment},
            cost_config=cost_config,
        )

    def recover_unknown(
        self,
        request: MarketOrderRequest,
        *,
        now: Optional[datetime] = None,
        external_order_id: Optional[str] = None,
    ) -> BrokerSubmissionResult:
        """Search existing MT5 state by intent metadata; never resend."""
        now = now or self._clock.now()
        magic = intent_magic(request.order_id, base=self._magic_base)
        comment = intent_comment(request.order_id)

        if external_order_id:
            ticket = int(external_order_id)
            orders = self._runtime.orders_get(ticket=ticket) or []
            if orders:
                reconciled = self._reconcile_fill(
                    request,
                    now=now,
                    magic=magic,
                    comment=comment,
                    external_order_id=external_order_id,
                )
                if reconciled is not None:
                    fill, deal_ids = reconciled
                    return BrokerSubmissionResult(
                        outcome=BrokerSubmissionOutcome.FILLED,
                        fill=fill,
                        external_order_id=external_order_id,
                        external_deal_ids=deal_ids,
                        metadata={"recovered": True, "magic": magic},
                    )

        reconciled = self._reconcile_fill(
            request,
            now=now,
            magic=magic,
            comment=comment,
            external_order_id=external_order_id,
        )
        if reconciled is not None:
            fill, deal_ids = reconciled
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.FILLED,
                fill=fill,
                external_order_id=external_order_id,
                external_deal_ids=deal_ids,
                metadata={"recovered": True, "magic": magic},
            )

        return BrokerSubmissionResult(
            outcome=BrokerSubmissionOutcome.UNKNOWN,
            external_order_id=external_order_id,
            metadata={"magic": magic, "comment": comment, "recovered": False},
        )

    def _preflight(
        self,
        request: MarketOrderRequest,
        *,
        now: datetime,
        account: Any,
    ) -> Optional[BrokerRejection]:
        if account is None:
            return BrokerRejection(
                code=BrokerRejectionCode.BROKER_UNAVAILABLE,
                message="MT5 account_info is unavailable",
            )
        if not bool(getattr(account, "trade_allowed", False)):
            return BrokerRejection(
                code=BrokerRejectionCode.TRADING_DISABLED,
                message="terminal trading is disabled for this account",
            )
        if not self._runtime.symbol_select(request.symbol, True):
            return BrokerRejection(
                code=BrokerRejectionCode.SYMBOL_UNAVAILABLE,
                message=f"symbol {request.symbol} is not visible in MarketWatch",
            )
        info = self._runtime.symbol_info(request.symbol)
        if info is None:
            return BrokerRejection(
                code=BrokerRejectionCode.SYMBOL_UNAVAILABLE,
                message=f"symbol {request.symbol} is unavailable",
            )
        tick = self._runtime.symbol_info_tick(request.symbol)
        if tick is None:
            return BrokerRejection(
                code=BrokerRejectionCode.QUOTE_UNAVAILABLE,
                message=f"quote for {request.symbol} is unavailable",
            )
        from q_backend.execution.brokers.base import ExecutableQuote

        quote = ExecutableQuote(
            symbol=request.symbol,
            bid=Decimal(str(tick.bid)),
            ask=Decimal(str(tick.ask)),
            timestamp=datetime.fromtimestamp(int(tick.time), tz=timezone.utc),
            source="mt5",
        )
        quote_rejection = validate_quote(
            quote,
            now=now,
            max_age_seconds=self._max_quote_age_seconds,
        )
        if quote_rejection is not None:
            return quote_rejection
        trade_mode = int(getattr(info, "trade_mode", 0))
        if trade_mode == 0:
            return BrokerRejection(
                code=BrokerRejectionCode.SYMBOL_UNAVAILABLE,
                message=f"symbol {request.symbol} is not tradable",
            )
        return None

    def _reconcile_fill(
        self,
        request: MarketOrderRequest,
        *,
        now: datetime,
        magic: int,
        comment: str,
        external_order_id: Optional[str],
        fallback_deal_id: Optional[str] = None,
    ) -> Optional[tuple[FillRecord, tuple[str, ...]]]:
        start = now - self._reconcile_lookback
        deals = self._runtime.history_deals_get(start, now) or []
        matches = [
            deal
            for deal in deals
            if int(getattr(deal, "magic", 0)) == magic
            or str(getattr(deal, "comment", "")).startswith(comment)
        ]
        if fallback_deal_id:
            matches = [
                deal
                for deal in matches
                if str(int(getattr(deal, "ticket", 0))) == fallback_deal_id
            ] or matches

        if not matches:
            return None

        # Deduplicate by deal ticket while preserving order.
        seen: set[str] = set()
        deal_ids: list[str] = []
        total_qty = Decimal("0")
        notional = Decimal("0")
        latest_time = now
        for deal in sorted(matches, key=lambda d: int(getattr(d, "time", 0))):
            ticket = str(int(getattr(deal, "ticket", 0)))
            if ticket in seen or ticket == "0":
                continue
            seen.add(ticket)
            deal_ids.append(ticket)
            qty = Decimal(str(getattr(deal, "volume", 0)))
            price = Decimal(str(getattr(deal, "price", 0)))
            total_qty += qty
            notional += qty * price
            deal_time = datetime.fromtimestamp(
                int(getattr(deal, "time", int(now.timestamp()))),
                tz=timezone.utc,
            )
            latest_time = max(latest_time, deal_time)

        if total_qty <= 0:
            return None

        avg_price = notional / total_qty
        fill = FillRecord(
            broker_mode=BrokerMode.MT5_LIVE,
            external_fill_id=deal_ids[-1],
            side=request.side,
            quantity=total_qty,
            price=avg_price,
            fee=Decimal("0"),
            slippage=Decimal("0"),
            filled_at=latest_time,
            metadata={
                "symbol": request.symbol,
                "deployment_id": str(request.deployment_id),
                "order_id": str(request.order_id),
                "external_order_id": external_order_id,
                "deal_tickets": deal_ids,
            },
        )
        return fill, tuple(deal_ids)
