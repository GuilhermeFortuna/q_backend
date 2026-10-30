"""Calling ResearchStrategy decision hooks with the arguments their signatures accept."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

import pandas as pd

from q_backend.research.orders import TradeOrder
from q_backend.research.positions import ResearchPosition
from q_backend.research.strategy import ResearchStrategy
from q_backend.research.tick_data import TickData

_KEYWORD_KINDS = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


@dataclass(frozen=True)
class HookCall:
    """How a hook accepts ``positions`` and ``ticks``, detected from its signature."""

    positions: str
    ticks: str = "none"


def ticks_mode(hook: Any) -> str:
    """How a hook takes ``ticks``: ``"none"``, ``"optional"`` (defaults to None) or ``"required"``.

    Only a keyword-capable ``ticks`` parameter counts; ``**kwargs`` alone does not receive ticks.
    """
    parameter = inspect.signature(hook).parameters.get("ticks")
    if parameter is None or parameter.kind not in _KEYWORD_KINDS:
        return "none"
    return "optional" if parameter.default is not inspect.Parameter.empty else "required"


def detect_hook_call(strategy: ResearchStrategy, name: str) -> HookCall:
    """Detect how a hook takes ``positions`` and ``ticks``; only explicit parameters count."""
    hook = getattr(strategy, name)
    if name == "exit_strategy" and "phase" in inspect.signature(hook).parameters:
        raise TypeError(f"{type(strategy).__name__}.{name} parameter 'phase' was removed; remove it from the hook")
    try:
        signature = inspect.signature(hook)
        parameters = signature.parameters
        ticks = ticks_mode(hook)
        extra: dict[str, object] = {}
        if ticks != "none":
            extra["ticks"] = None
        positions_param = parameters.get("positions")
        if positions_param is None or positions_param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            signature.bind(object(), **extra)
            mode = "legacy"
        elif positions_param.kind == inspect.Parameter.POSITIONAL_ONLY:
            signature.bind(object(), (), **extra)
            mode = "positional"
        else:
            signature.bind(object(), positions=(), **extra)
            mode = "keyword"
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"{type(strategy).__name__}.{name} has an incompatible signature; "
            "expected (frame), (frame, positions), or either with a keyword-only ticks"
        ) from exc
    return HookCall(positions=mode, ticks=ticks)


def _permitted_actions(name: str) -> tuple[str, ...]:
    """Actions a hook may return."""
    if name == "entry_strategy":
        return ("buy", "sell", "cancel", "oco")
    return ("close", "amend")


def invoke_hook(
    strategy: ResearchStrategy,
    call: HookCall,
    name: str,
    prefix: pd.DataFrame,
    positions: tuple[ResearchPosition, ...],
    *,
    ticks: TickData | None = None,
) -> tuple[TradeOrder, ...]:
    """Call one decision hook and validate the orders it returns.

    A hook returns ``None``, one ``TradeOrder`` or a list of them; the result is always a tuple.
    """
    hook = getattr(strategy, name)
    timestamp = prefix.index[-1]
    label = f"{type(strategy).__name__}.{name} at bar {timestamp}"
    args: tuple[Any, ...] = (prefix,)
    kwargs: dict[str, Any] = {}
    if call.positions == "positional":
        args = (prefix, positions)
    elif call.positions == "keyword":
        kwargs["positions"] = positions
    if call.ticks != "none":
        kwargs["ticks"] = ticks
    try:
        decision = hook(*args, **kwargs)
    except Exception as exc:
        raise RuntimeError(f"Error in {label}: {exc}") from exc
    if decision is None:
        orders: tuple[TradeOrder, ...] = ()
    elif isinstance(decision, TradeOrder):
        orders = (decision,)
    elif isinstance(decision, (list, tuple)):
        orders = tuple(decision)
    else:
        raise TypeError(f"{label} returned {type(decision).__name__}, expected TradeOrder, a list of them, or None")
    allowed = _permitted_actions(name)
    for order in orders:
        if not isinstance(order, TradeOrder):
            raise TypeError(f"{label} returned a list holding {type(order).__name__}, expected only TradeOrder")
        if order.action not in allowed:
            raise ValueError(f"{label} returned illegal action {order.action!r}; permitted actions: {allowed}")
    _check_order_list(label, name, orders)
    return orders


def _check_order_list(label: str, name: str, orders: tuple[TradeOrder, ...]) -> None:
    """A list's orders must be ones the engine can resolve together on one bar."""
    if len(orders) <= 1:
        return
    if name == "exit_strategy":
        plain = sum(order.action == "close" and order.price is None and order.at is None for order in orders)
        timed = sum(order.action == "close" and order.at is not None for order in orders)
        amends = sum(order.action == "amend" for order in orders)
        if plain > 1 or timed > 1 or amends > 1:
            raise ValueError(
                f"{label} returned {orders!r}; an exit list holds "
                "at most one close without a price, at most one timed close and at most one amend"
            )
        return
    raise ValueError(f"{label} returned {orders!r}; use TradeOrder.oco for multiple resting entries")
