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
    """How a hook accepts ``positions``, ``phase`` and ``ticks``, detected from its signature."""

    positions: str
    phase: bool
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
    """Detect how a hook takes ``positions``, ``phase`` and ``ticks``; only explicit parameters count."""
    hook = getattr(strategy, name)
    try:
        signature = inspect.signature(hook)
        parameters = signature.parameters
        phase_param = parameters.get("phase")
        phase = name == "exit_strategy" and phase_param is not None and phase_param.kind in _KEYWORD_KINDS
        ticks = ticks_mode(hook)
        extra: dict[str, object] = {"phase": "bar"} if phase else {}
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
            "expected (frame), (frame, positions), or either with a keyword-only phase or ticks"
        ) from exc
    return HookCall(positions=mode, phase=phase, ticks=ticks)


def _permitted_actions(name: str, phase: str | None) -> tuple[str, ...]:
    """Actions a hook may return: ``amend`` is limited to the bar phase and hooks without a phase."""
    if name == "entry_strategy":
        return ("buy", "sell", "cancel")
    if phase in (None, "bar"):
        return ("close", "amend")
    return ("close",)


def invoke_hook(
    strategy: ResearchStrategy,
    call: HookCall,
    name: str,
    prefix: pd.DataFrame,
    positions: tuple[ResearchPosition, ...],
    *,
    phase: str | None = None,
    detail: str = "",
    ticks: TickData | None = None,
) -> TradeOrder | None:
    """Call one decision hook and validate the order it returns."""
    hook = getattr(strategy, name)
    timestamp = prefix.index[-1]
    suffix = f" (phase {phase}{detail})" if phase is not None else ""
    label = f"{type(strategy).__name__}.{name} at bar {timestamp}{suffix}"
    args: tuple[Any, ...] = (prefix,)
    kwargs: dict[str, Any] = {}
    if call.positions == "positional":
        args = (prefix, positions)
    elif call.positions == "keyword":
        kwargs["positions"] = positions
    if call.phase and phase is not None:
        kwargs["phase"] = phase
    if call.ticks != "none":
        kwargs["ticks"] = ticks
    try:
        decision = hook(*args, **kwargs)
    except Exception as exc:
        raise RuntimeError(f"Error in {label}: {exc}") from exc
    if decision is not None:
        if not isinstance(decision, TradeOrder):
            raise TypeError(f"{label} returned {type(decision).__name__}, expected TradeOrder or None")
        allowed = _permitted_actions(name, phase)
        if decision.action not in allowed:
            raise ValueError(f"{label} returned illegal action {decision.action!r}; permitted actions: {allowed}")
    return decision
