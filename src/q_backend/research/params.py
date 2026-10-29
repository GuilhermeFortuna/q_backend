"""Parameter declarations for ResearchStrategy files.

A strategy class declares each ``__init__`` argument in a class attribute ``parameters``. The
declaration gives the type, bounds and labels that the Research UI shows; the default comes from
the signature. :func:`declared_params` checks the two agree, and :func:`coerce_params` turns
submitted values into the Python values the constructor takes.
"""

from __future__ import annotations

import datetime as dt
import inspect
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Union

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ParamError(ValueError):
    """A parameter declaration or submitted value does not match the strategy's signature."""


@dataclass(frozen=True)
class IntParam:
    """An integer parameter with inclusive bounds."""

    min: int
    max: int
    step: int = 1
    label: str | None = None
    hint: str | None = None


@dataclass(frozen=True)
class FloatParam:
    """A float parameter with inclusive bounds."""

    min: float
    max: float
    step: float | None = None
    label: str | None = None
    hint: str | None = None


@dataclass(frozen=True)
class BoolParam:
    """A boolean parameter, shown as a checkbox."""

    label: str | None = None
    hint: str | None = None


@dataclass(frozen=True)
class ChoiceParam:
    """A string parameter restricted to ``choices``."""

    choices: tuple[str, ...]
    label: str | None = None
    hint: str | None = None


@dataclass(frozen=True)
class TimeParam:
    """A time-of-day parameter, shown as ``HH:MM``. Its default is a whole-minute ``datetime.time``."""

    label: str | None = None
    hint: str | None = None


Declaration = Union[IntParam, FloatParam, BoolParam, ChoiceParam, TimeParam]


@dataclass(frozen=True)
class DeclaredParam:
    """One ``__init__`` argument with its declaration and default, checked against the signature."""

    name: str
    kind: Literal["int", "float", "bool", "categorical", "time"]
    default: Any
    label: str
    hint: str | None
    min: float | None
    max: float | None
    step: float | None
    choices: tuple[str, ...] | None


def declared_params(strategy_cls: type) -> tuple[DeclaredParam, ...]:
    """Return the declared parameters of ``strategy_cls`` in ``__init__`` order.

    Raises :class:`ParamError` naming the argument when the signature and ``parameters`` disagree,
    when a default has the wrong type or falls outside its bounds, or when the constructor takes
    ``*args`` or ``**kwargs``.
    """
    name = strategy_cls.__name__
    arguments = _init_arguments(strategy_cls)
    declarations = getattr(strategy_cls, "parameters", {})
    if not isinstance(declarations, Mapping):
        raise ParamError(f"{name}.parameters must be a mapping of argument names to declarations")

    for argument in arguments:
        if argument.name not in declarations:
            raise ParamError(f"{name}.__init__ argument '{argument.name}' has no entry in parameters")
    init_names = {argument.name for argument in arguments}
    for declared_name in declarations:
        if declared_name not in init_names:
            raise ParamError(f"{name}.parameters declares '{declared_name}', which is not an __init__ argument")

    specs = []
    for argument in arguments:
        if argument.default is inspect.Parameter.empty:
            raise ParamError(f"{name}.__init__ argument '{argument.name}' must have a default")
        specs.append(_resolve(name, argument.name, argument.default, declarations[argument.name]))
    return tuple(specs)


def to_wire_spec(param: DeclaredParam) -> dict[str, Any]:
    """The parameter as the API's ``StrategyParamSpec`` payload."""
    return {
        "name": param.name,
        "label": param.label,
        "type": param.kind,
        "default": _to_wire_value(param),
        "min": param.min,
        "max": param.max,
        "step": param.step,
        "choices": list(param.choices) if param.choices is not None else None,
        "hint": param.hint,
    }


def coerce_params(params: Sequence[DeclaredParam], submitted: Mapping[str, Any]) -> dict[str, Any]:
    """Validate submitted values and return every constructor argument, defaults filled in.

    Time values may be ``"HH:MM"`` strings (the wire form) or ``datetime.time``. Unknown names,
    wrong types and out-of-bound values raise :class:`ParamError`.
    """
    by_name = {param.name: param for param in params}
    for name in submitted:
        if name not in by_name:
            raise ParamError(f"'{name}' is not a parameter of this strategy")

    values: dict[str, Any] = {}
    for param in params:
        if param.name in submitted:
            values[param.name] = _coerce_value(param, submitted[param.name])
        else:
            values[param.name] = param.default
    return values


def _init_arguments(strategy_cls: type) -> list[inspect.Parameter]:
    if strategy_cls.__init__ is object.__init__:
        return []
    arguments = list(inspect.signature(strategy_cls.__init__).parameters.values())[1:]
    for argument in arguments:
        if argument.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise ParamError(f"{strategy_cls.__name__}.__init__ must not take *args or **kwargs")
    return arguments


def _resolve(owner: str, name: str, default: Any, declaration: Any) -> DeclaredParam:
    label = getattr(declaration, "label", None) or name
    hint = getattr(declaration, "hint", None)
    where = f"{owner}.__init__ argument '{name}'"

    if isinstance(declaration, IntParam):
        _check_bounds(where, declaration.min, declaration.max)
        if declaration.step <= 0:
            raise ParamError(f"{where}: step must be positive")
        if not _is_int(default):
            raise ParamError(f"{where}: default {default!r} must be an integer")
        _check_within(where, default, declaration.min, declaration.max)
        return DeclaredParam(
            name, "int", default, label, hint, declaration.min, declaration.max, declaration.step, None
        )

    if isinstance(declaration, FloatParam):
        _check_bounds(where, declaration.min, declaration.max)
        if declaration.step is not None and declaration.step <= 0:
            raise ParamError(f"{where}: step must be positive")
        if not _is_number(default):
            raise ParamError(f"{where}: default {default!r} must be a number")
        _check_within(where, default, declaration.min, declaration.max)
        return DeclaredParam(
            name,
            "float",
            float(default),
            label,
            hint,
            float(declaration.min),
            float(declaration.max),
            None if declaration.step is None else float(declaration.step),
            None,
        )

    if isinstance(declaration, BoolParam):
        if not isinstance(default, bool):
            raise ParamError(f"{where}: default {default!r} must be a boolean")
        return DeclaredParam(name, "bool", default, label, hint, None, None, None, None)

    if isinstance(declaration, ChoiceParam):
        choices = tuple(declaration.choices)
        if not choices or not all(isinstance(choice, str) for choice in choices) or len(set(choices)) != len(choices):
            raise ParamError(f"{where}: choices must be a non-empty tuple of distinct strings")
        if default not in choices:
            raise ParamError(f"{where}: default {default!r} must be one of {list(choices)}")
        return DeclaredParam(name, "categorical", default, label, hint, None, None, None, choices)

    if isinstance(declaration, TimeParam):
        if not isinstance(default, dt.time):
            raise ParamError(f"{where}: default {default!r} must be a datetime.time")
        if default.second or default.microsecond or default.tzinfo is not None:
            raise ParamError(f"{where}: time default must be a naive time in whole minutes")
        return DeclaredParam(name, "time", default, label, hint, None, None, None, None)

    raise ParamError(f"{where}: declaration must be IntParam, FloatParam, BoolParam, ChoiceParam or TimeParam")


def _check_bounds(where: str, low: float, high: float) -> None:
    if not (_is_number(low) and _is_number(high)) or not low <= high:
        raise ParamError(f"{where}: bounds must be numbers with min <= max")


def _check_within(where: str, value: Any, low: float, high: float) -> None:
    if not low <= value <= high:
        raise ParamError(f"{where}: default {value!r} must be between {low} and {high}")


def _coerce_value(param: DeclaredParam, value: Any) -> Any:
    label = f"'{param.name}'"
    if param.kind == "bool":
        if not isinstance(value, bool):
            raise ParamError(f"{label} must be a boolean")
        return value
    if param.kind == "int":
        if not _is_int(value):
            raise ParamError(f"{label} must be an integer")
        _check_submitted_bounds(label, value, param)
        return value
    if param.kind == "float":
        if not _is_number(value) or not math.isfinite(value):
            raise ParamError(f"{label} must be a finite number")
        _check_submitted_bounds(label, value, param)
        return float(value)
    if param.kind == "categorical":
        if value not in (param.choices or ()):
            raise ParamError(f"{label} must be one of {list(param.choices or ())}")
        return value
    # time
    if isinstance(value, dt.time) and value.tzinfo is None and not (value.second or value.microsecond):
        return value.replace(second=0, microsecond=0)
    if isinstance(value, str) and _TIME_PATTERN.match(value):
        hours, minutes = value.split(":")
        return dt.time(int(hours), int(minutes))
    raise ParamError(f"{label} must be a time in HH:MM form")


def _check_submitted_bounds(label: str, value: float, param: DeclaredParam) -> None:
    if not param.min <= value <= param.max:
        raise ParamError(f"{label} must be between {param.min:g} and {param.max:g}")


def _to_wire_value(param: DeclaredParam) -> Any:
    if param.kind == "time":
        return param.default.strftime("%H:%M")
    return param.default


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
