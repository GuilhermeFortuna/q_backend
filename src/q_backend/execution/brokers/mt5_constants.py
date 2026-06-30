"""MT5 trade constants isolated from domain types (WO172)."""

from __future__ import annotations

from enum import IntEnum


class Mt5OrderType(IntEnum):
    BUY = 0
    SELL = 1


class Mt5TradeAction(IntEnum):
    DEAL = 1


class Mt5OrderTime(IntEnum):
    GTC = 0


class Mt5OrderFilling(IntEnum):
    FOK = 0
    IOC = 1
    RETURN = 2


class Mt5SymbolFilling(IntEnum):
    FOK = 1
    IOC = 2
    RETURN = 4


class Mt5Retcode(IntEnum):
    REQUOTE = 10004
    REJECT = 10006
    INVALID_VOLUME = 10014
    MARKET_CLOSED = 10018
    DONE = 10009
    DONE_PARTIAL = 10010
    TIMEOUT = 10012
    PRICE_CHANGED = 10020
    PRICE_OFF = 10021
    INVALID_STOPS = 10016
    TRADE_DISABLED = 10017
    NO_CONNECTION = 10031


RETCODE_LABELS: dict[int, str] = {
    int(Mt5Retcode.REQUOTE): "requote",
    int(Mt5Retcode.REJECT): "reject",
    int(Mt5Retcode.INVALID_VOLUME): "invalid_volume",
    int(Mt5Retcode.MARKET_CLOSED): "market_closed",
    int(Mt5Retcode.DONE): "done",
    int(Mt5Retcode.DONE_PARTIAL): "done_partial",
    int(Mt5Retcode.TIMEOUT): "timeout",
    int(Mt5Retcode.PRICE_CHANGED): "price_changed",
    int(Mt5Retcode.PRICE_OFF): "price_off",
    int(Mt5Retcode.INVALID_STOPS): "invalid_stops",
    int(Mt5Retcode.TRADE_DISABLED): "trade_disabled",
    int(Mt5Retcode.NO_CONNECTION): "no_connection",
}


def retcode_label(retcode: int) -> str:
    return RETCODE_LABELS.get(int(retcode), f"retcode_{retcode}")


def is_success_retcode(retcode: int) -> bool:
    return int(retcode) in {
        int(Mt5Retcode.DONE),
        int(Mt5Retcode.DONE_PARTIAL),
    }


def is_unknown_retcode(retcode: int) -> bool:
    return int(retcode) in {
        int(Mt5Retcode.TIMEOUT),
        int(Mt5Retcode.NO_CONNECTION),
    }
