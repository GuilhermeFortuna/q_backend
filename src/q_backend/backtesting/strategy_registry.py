from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Type

from pydantic import BaseModel

from q_backend.backtesting.strategy import TradingStrategy


class StrategyParamSpec(BaseModel):
    name: str
    label: str
    type: Literal["int", "float", "categorical"]
    default: int | float | str
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: list[str] | None = None


class StrategyInfo(BaseModel):
    name: str
    label: str
    description: str
    params: list[StrategyParamSpec]


class StrategiesResponse(BaseModel):
    strategies: list[StrategyInfo]


@dataclass(frozen=True)
class RegisteredStrategy:
    strategy_class: Type[TradingStrategy]
    info: StrategyInfo
    build: Callable[[dict[str, Any], str], TradingStrategy]


_STRATEGY_REGISTRY: dict[str, RegisteredStrategy] = {}


def register_strategy(
    *,
    name: str,
    label: str,
    description: str,
    params: list[StrategyParamSpec],
    build: Callable[[dict[str, Any], str], TradingStrategy],
    strategy_class: Type[TradingStrategy],
) -> Type[TradingStrategy]:
    if name in _STRATEGY_REGISTRY:
        raise ValueError(f"Strategy '{name}' is already registered.")

    _STRATEGY_REGISTRY[name] = RegisteredStrategy(
        strategy_class=strategy_class,
        info=StrategyInfo(
            name=name,
            label=label,
            description=description,
            params=params,
        ),
        build=build,
    )
    return strategy_class


def get_registered_strategy(name: str) -> RegisteredStrategy:
    try:
        return _STRATEGY_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"Unknown strategy: {name}") from exc


def list_registered_strategies() -> list[StrategyInfo]:
    return sorted(
        (entry.info for entry in _STRATEGY_REGISTRY.values()),
        key=lambda info: info.name,
    )


def default_params_for(name: str) -> dict[str, Any]:
    entry = get_registered_strategy(name)
    return {spec.name: spec.default for spec in entry.info.params}


def _coerce_param_value(spec: StrategyParamSpec, raw: Any) -> Any:
    if spec.type == "int":
        return int(raw)
    if spec.type == "float":
        return float(raw)
    return str(raw)


def merge_strategy_params(name: str, params: dict[str, Any]) -> dict[str, Any]:
    entry = get_registered_strategy(name)
    merged: dict[str, Any] = {}
    for spec in entry.info.params:
        if spec.name in params:
            merged[spec.name] = _coerce_param_value(spec, params[spec.name])
        else:
            merged[spec.name] = spec.default
    return merged
