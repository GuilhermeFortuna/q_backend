"""Controllable fake MetaTrader5 module for gateway and execution-edge HTTP tests."""

from __future__ import annotations

import threading
import types
from collections import namedtuple
from collections.abc import Callable
from datetime import datetime
from typing import Any

import numpy as np

_RATE_DTYPE = [
    ("time", "i8"),
    ("open", "f8"),
    ("high", "f8"),
    ("low", "f8"),
    ("close", "f8"),
    ("tick_volume", "i8"),
    ("spread", "i4"),
    ("real_volume", "i8"),
]

_TICK_DTYPE = [
    ("time", "i8"),
    ("bid", "f8"),
    ("ask", "f8"),
    ("last", "f8"),
    ("volume", "f8"),
    ("time_msc", "i8"),
    ("flags", "i4"),
]

_SymbolInfo = namedtuple(
    "SymbolInfo",
    [
        "name",
        "description",
        "digits",
        "point",
        "trade_mode",
        "volume_step",
        "volume_min",
        "volume_max",
        "filling_mode",
    ],
)
_SymbolSearch = namedtuple("SymbolSearch", ["name", "description", "path", "custom"])
_TerminalInfo = namedtuple("TerminalInfo", ["build", "trade_allowed"])
_AccountInfo = namedtuple(
    "AccountInfo",
    [
        "login",
        "server",
        "currency",
        "trade_allowed",
        "trade_expert",
        "balance",
        "equity",
        "margin_free",
        "trade_mode",
    ],
)
_TickInfo = namedtuple("TickInfo", ["bid", "ask", "last", "time", "time_msc"])
_CheckResult = namedtuple("CheckResult", ["retcode", "balance", "equity", "margin", "margin_free"])
_SendResult = namedtuple("SendResult", ["retcode", "order", "deal", "comment"])
_Position = namedtuple(
    "Position",
    [
        "ticket",
        "symbol",
        "type",
        "volume",
        "price_open",
        "sl",
        "tp",
        "price_current",
        "profit",
        "magic",
        "comment",
        "time",
    ],
)
_Deal = namedtuple(
    "Deal",
    [
        "ticket",
        "order",
        "symbol",
        "type",
        "entry",
        "volume",
        "price",
        "commission",
        "swap",
        "profit",
        "fee",
        "magic",
        "comment",
        "time",
        "time_msc",
        "position_id",
        "reason",
    ],
    defaults=(0, 0),
)
_HistoryOrder = namedtuple(
    "HistoryOrder",
    ["ticket", "magic", "comment", "state", "symbol", "volume_initial", "type", "time"],
)
# Mirrors MetaTrader5's TradeOrder fields for working (pending) orders.
_TradeOrder = namedtuple(
    "TradeOrder",
    [
        "ticket",
        "symbol",
        "type",
        "volume_current",
        "price_open",
        "sl",
        "tp",
        "time_setup",
        "time_expiration",
        "magic",
        "comment",
    ],
)


def make_fake_mt5() -> types.ModuleType:
    """Build a controllable fake MetaTrader5 module."""
    m = types.ModuleType("MetaTrader5")

    m.TIMEFRAME_M1 = 1
    m.TIMEFRAME_M2 = 2
    m.TIMEFRAME_M3 = 3
    m.TIMEFRAME_M4 = 4
    m.TIMEFRAME_M5 = 5
    m.TIMEFRAME_M6 = 6
    m.TIMEFRAME_M10 = 10
    m.TIMEFRAME_M12 = 12
    m.TIMEFRAME_M15 = 15
    m.TIMEFRAME_M20 = 20
    m.TIMEFRAME_M30 = 30
    m.TIMEFRAME_H1 = 16385
    m.TIMEFRAME_H2 = 16386
    m.TIMEFRAME_H3 = 16387
    m.TIMEFRAME_H4 = 16388
    m.TIMEFRAME_H6 = 16390
    m.TIMEFRAME_H8 = 16392
    m.TIMEFRAME_H12 = 16396
    m.TIMEFRAME_D1 = 16408
    m.TIMEFRAME_W1 = 32769
    m.TIMEFRAME_MN1 = 49153
    m.COPY_TICKS_ALL = 1
    m.COPY_TICKS_TRADE = 2

    m.TRADE_ACTION_DEAL = 1
    m.TRADE_ACTION_PENDING = 5
    m.TRADE_ACTION_SLTP = 6
    m.TRADE_ACTION_REMOVE = 8
    m.ORDER_TYPE_BUY = 0
    m.ORDER_TYPE_SELL = 1
    m.ORDER_TYPE_BUY_LIMIT = 2
    m.ORDER_TYPE_SELL_LIMIT = 3
    m.ORDER_TYPE_BUY_STOP = 4
    m.ORDER_TYPE_SELL_STOP = 5
    m.ORDER_TIME_GTC = 0
    m.ORDER_TIME_SPECIFIED = 2
    m.DEAL_REASON_CLIENT = 0
    m.DEAL_REASON_MOBILE = 1
    m.DEAL_REASON_WEB = 2
    m.DEAL_REASON_EXPERT = 3
    m.DEAL_REASON_SL = 4
    m.DEAL_REASON_TP = 5
    m.DEAL_REASON_SO = 6
    m.ORDER_FILLING_FOK = 0
    m.ORDER_FILLING_IOC = 1
    m.ORDER_FILLING_RETURN = 2
    m.SYMBOL_FILLING_FOK = 1
    m.SYMBOL_FILLING_IOC = 2
    m.SYMBOL_FILLING_RETURN = 4
    m.TRADE_RETCODE_DONE = 10009
    m.TRADE_RETCODE_DONE_PARTIAL = 10010
    m.TRADE_RETCODE_TIMEOUT = 10012
    m.TRADE_RETCODE_NO_CONNECTION = 10031
    m.TRADE_RETCODE_REJECT = 10006
    m.ORDER_STATE_CANCELED = 2
    m.ORDER_STATE_REJECTED = 5
    m.ORDER_STATE_EXPIRED = 6

    state: dict[str, Any] = {
        "init_ok": True,
        "known_symbols": {"WIN$"},
        "symbol_info": {
            "WIN$": _SymbolInfo(
                "WIN$",
                "Mini Ibovespa",
                0,
                1.0,
                4,
                1.0,
                1.0,
                100.0,
                m.SYMBOL_FILLING_IOC,
            ),
        },
        "search": [
            _SymbolSearch("WIN$", "Mini Ibovespa", "Futures\\WIN$", False),
            _SymbolSearch("WINQ26", "Mini Ibovespa Aug26", "Futures\\WINQ26", False),
        ],
        "rates_queue": [],
        "rates_default": np.empty(0, dtype=_RATE_DTYPE),
        "rates_from_pos": None,
        "last_rates_from_pos": None,
        "rates_from": np.zeros(1, dtype=_RATE_DTYPE),
        "last_error": (-1, "fake error"),
        "ticks_queue": [],
        "ticks_default": np.empty(0, dtype=_TICK_DTYPE),
        "ticks_source": None,
        "tick_range_calls": [],
        "last_flags": None,
        "build": 4200,
        "terminal_trade_allowed": True,
        "account": _AccountInfo(
            login=10987654,
            server="MetaQuotes-Demo",
            currency="BRL",
            trade_allowed=True,
            trade_expert=True,
            balance=100000.0,
            equity=100000.0,
            margin_free=100000.0,
            trade_mode=0,
        ),
        "ticks": {
            "WIN$": _TickInfo(
                bid=130000.0, ask=130010.0, last=130005.0, time=1_710_000_000, time_msc=1_710_000_000_000
            ),
        },
        "order_check_retcode": m.TRADE_RETCODE_DONE,
        "order_check_margin": 500.0,
        "order_check_raises": None,
        "order_send_queue": [],
        "order_send_default": None,
        "order_send_raises": None,
        "order_send_barrier": None,
        "order_send_calls": 0,
        "positions": [],
        "history_orders": [],
        "history_deals": [],
        "history_deals_returns_none": False,
        "active_orders": [],
        "order_send_requests": [],
    }
    m._state = state

    def initialize(**_kwargs):
        return state["init_ok"]

    def shutdown():
        return None

    def last_error():
        return state["last_error"]

    def symbol_select(symbol, _enable):
        return symbol in state["known_symbols"]

    def symbol_info(symbol):
        return state["symbol_info"].get(symbol)

    def symbols_get(_pattern=None):
        return state["search"]

    def terminal_info():
        return _TerminalInfo(build=state["build"], trade_allowed=state["terminal_trade_allowed"])

    def account_info():
        if not state["init_ok"]:
            return None
        return state["account"]

    def symbol_info_tick(symbol):
        return state["ticks"].get(symbol)

    def order_check(_request):
        if state["order_check_raises"] is not None:
            raise state["order_check_raises"]
        return _CheckResult(
            retcode=state["order_check_retcode"],
            balance=state["account"].balance,
            equity=state["account"].equity,
            margin=state["order_check_margin"],
            margin_free=state["account"].margin_free,
        )

    def order_send(request):
        state["order_send_calls"] += 1
        state["order_send_requests"].append(dict(request))
        barrier: threading.Event | None = state["order_send_barrier"]
        if barrier is not None:
            barrier.wait(timeout=5)
        if state["order_send_raises"] is not None:
            raise state["order_send_raises"]
        if state["order_send_queue"]:
            result = state["order_send_queue"].pop(0)
            if result is None:
                return None
            return result
        if state["order_send_default"] is not None:
            return state["order_send_default"]
        return _SendResult(
            retcode=m.TRADE_RETCODE_DONE,
            order=12345678,
            deal=98765,
            comment=str(request.get("comment", "")),
        )

    def orders_get(symbol=None, group="", ticket=None):
        del group
        orders = state["active_orders"]
        if symbol is not None:
            orders = [o for o in orders if o.symbol == symbol]
        if ticket is not None:
            orders = [o for o in orders if int(getattr(o, "ticket", 0)) == ticket]
        return list(orders)

    def positions_get(symbol=None):
        positions = state["positions"]
        if symbol is None:
            return list(positions)
        return [p for p in positions if p.symbol == symbol]

    def history_orders_get(date_from, date_to, group=""):
        del group
        start = _to_epoch(date_from)
        end = _to_epoch(date_to)
        return [o for o in state["history_orders"] if start <= int(getattr(o, "time", start)) <= end]

    def history_deals_get(date_from, date_to, group=""):
        del group
        if state["history_deals_returns_none"]:
            return None
        start = _to_epoch(date_from)
        end = _to_epoch(date_to)
        return [d for d in state["history_deals"] if start <= int(getattr(d, "time", start)) <= end]

    def copy_rates_range(_symbol, _timeframe, _date_from, _date_to):
        if state["rates_queue"]:
            return state["rates_queue"].pop(0)
        return state["rates_default"]

    def copy_rates_from_pos(symbol, timeframe, pos, count):
        state["last_rates_from_pos"] = (symbol, timeframe, pos, count)
        return state["rates_from_pos"]

    def copy_rates_from(_symbol, _timeframe, _date_from, _count):
        return state["rates_from"]

    def copy_ticks_range(_symbol, _date_from, _date_to, flags):
        state["last_flags"] = flags
        state["tick_range_calls"].append((_date_from, _date_to, flags))
        if state["ticks_queue"]:
            return state["ticks_queue"].pop(0)
        source = state["ticks_source"]
        if source is not None:
            # Inclusive on both ends, like the real terminal; times are epoch milliseconds.
            start_ms = int(_date_from.timestamp() * 1000)
            end_ms = int(_date_to.timestamp() * 1000)
            return source[(source["time_msc"] >= start_ms) & (source["time_msc"] <= end_ms)]
        return state["ticks_default"]

    m.initialize = initialize
    m.shutdown = shutdown
    m.last_error = last_error
    m.symbol_select = symbol_select
    m.symbol_info = symbol_info
    m.symbols_get = symbols_get
    m.terminal_info = terminal_info
    m.account_info = account_info
    m.symbol_info_tick = symbol_info_tick
    m.order_check = order_check
    m.order_send = order_send
    m.orders_get = orders_get
    m.positions_get = positions_get
    m.history_orders_get = history_orders_get
    m.history_deals_get = history_deals_get
    m.copy_rates_range = copy_rates_range
    m.copy_rates_from_pos = copy_rates_from_pos
    m.copy_rates_from = copy_rates_from
    m.copy_ticks_range = copy_ticks_range
    return m


def _to_epoch(value: datetime | int | float) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(value)


def reset_order_send(fake: types.ModuleType) -> None:
    fake._state["order_send_calls"] = 0
    fake._state["order_send_queue"] = []
    fake._state["order_send_default"] = None
    fake._state["order_send_raises"] = None
    fake._state["order_send_barrier"] = None


def set_order_send_barrier(fake: types.ModuleType) -> threading.Event:
    barrier = threading.Event()
    fake._state["order_send_barrier"] = barrier
    return barrier


def make_deal(
    *,
    ticket: int = 98765,
    order: int = 12345678,
    symbol: str = "WIN$",
    volume: float = 1.0,
    price: float = 130005.0,
    magic: int = 0,
    comment: str = "q:test",
    time_msc: int = 1_710_000_000_000,
    position_id: int = 0,
    reason: int = 0,
) -> Any:
    return _Deal(
        ticket=ticket,
        order=order,
        symbol=symbol,
        type=0,
        entry=0,
        volume=volume,
        price=price,
        commission=-2.5,
        swap=0.0,
        profit=0.0,
        fee=0.0,
        magic=magic,
        comment=comment,
        time=time_msc // 1000,
        time_msc=time_msc,
        position_id=position_id,
        reason=reason,
    )


def make_position(
    *,
    ticket: int = 42,
    symbol: str = "WIN$",
    volume: float = 1.0,
    price_open: float = 130000.0,
    magic: int = 1,
    comment: str = "q:test",
) -> Any:
    return _Position(
        ticket=ticket,
        symbol=symbol,
        type=0,
        volume=volume,
        price_open=price_open,
        sl=0.0,
        tp=0.0,
        price_current=130010.0,
        profit=10.0,
        magic=magic,
        comment=comment,
        time=1_710_000_000,
    )


def make_send_result(
    *,
    retcode: int,
    order: int = 0,
    deal: int = 0,
    comment: str = "",
) -> Any:
    return _SendResult(retcode=retcode, order=order, deal=deal, comment=comment)


def make_history_order(
    *,
    ticket: int = 555,
    magic: int = 0,
    comment: str = "q:test",
    state: int = 2,
    symbol: str = "WIN$",
    volume: float = 1.0,
    time: int = 1_710_000_000,
) -> Any:
    return _HistoryOrder(
        ticket=ticket,
        magic=magic,
        comment=comment,
        state=state,
        symbol=symbol,
        volume_initial=volume,
        type=0,
        time=time,
    )


def make_working_order(
    *,
    ticket: int = 223344,
    symbol: str = "WIN$",
    order_type: int = 4,
    volume: float = 1.0,
    price: float = 130100.0,
    sl: float = 0.0,
    tp: float = 0.0,
    time_expiration: int = 0,
    magic: int = 0,
    comment: str = "q:test",
) -> Any:
    return _TradeOrder(
        ticket=ticket,
        symbol=symbol,
        type=order_type,
        volume_current=volume,
        price_open=price,
        sl=sl,
        tp=tp,
        time_setup=1_710_000_000,
        time_expiration=time_expiration,
        magic=magic,
        comment=comment,
    )
