from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Type, Union

from pydantic import BaseModel, model_validator

from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.tick.strategy import TickStrategy

StrategyEngine = Literal["candle", "tick"]
StrategyCategory = Literal["trend", "mean_reversion", "breakout", "momentum", "other"]
StrategyBase = Union[TradingStrategy, TickStrategy]


ExitGroup = Literal["stop_loss", "trailing", "target", "time", "general"]


class StrategyParamSpec(BaseModel):
    name: str
    label: str
    type: Literal["int", "float", "categorical"]
    default: int | float | str
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: list[str] | None = None
    hint: str | None = None
    exit_group: ExitGroup | None = None
    search_min: float | None = None
    search_max: float | None = None
    search_step: float | None = None
    search_scale: Literal["linear", "log"] | None = None
    searchable: bool = True

    @model_validator(mode="after")
    def validate_search_bounds(self) -> StrategyParamSpec:
        if self.search_min is not None and self.search_max is not None:
            if self.search_min >= self.search_max:
                raise ValueError(
                    f"search_min must be < search_max for param '{self.name}'"
                )
        if self.search_scale == "log":
            if self.type != "float":
                raise ValueError(
                    f"search_scale='log' only valid for float params: '{self.name}'"
                )
            effective_low = (
                self.search_min if self.search_min is not None else self.min
            )
            if effective_low is None or effective_low <= 0:
                raise ValueError(
                    f"search_scale='log' requires positive effective low for '{self.name}'"
                )
        return self


class StrategyInfo(BaseModel):
    name: str
    label: str
    description: str
    params: list[StrategyParamSpec]
    engine: StrategyEngine = "candle"
    category: StrategyCategory = "other"
    thesis: str = ""
    strong_in: str = ""
    weak_in: str = ""


class StrategiesResponse(BaseModel):
    strategies: list[StrategyInfo]


class ExitRuleInfo(BaseModel):
    id: str
    label: str
    description: str
    exit_group: ExitGroup
    enable_param: str
    enable_value: float | int
    param_names: list[str]
    required_param_names: list[str]


class SignalManagerInfo(BaseModel):
    id: str
    label: str
    description: str
    param_names: list[str]
    params: list[StrategyParamSpec] = []


class SignalManagerCatalogResponse(BaseModel):
    managers: list[SignalManagerInfo]


class ExitPreset(BaseModel):
    id: str
    label: str
    description: str
    parameters: dict[str, float | int]


class ExitRuleCatalogResponse(BaseModel):
    exit_rules: list[ExitRuleInfo]
    shared_exit_params: list[str]
    exit_presets: list[ExitPreset]


@dataclass(frozen=True)
class RegisteredStrategy:
    strategy_class: Type[StrategyBase]
    info: StrategyInfo
    build: Callable[[dict[str, Any], str], StrategyBase]


_STRATEGY_REGISTRY: dict[str, RegisteredStrategy] = {}


def register_strategy(
    *,
    name: str,
    label: str,
    description: str,
    params: list[StrategyParamSpec],
    build: Callable[[dict[str, Any], str], StrategyBase],
    strategy_class: Type[StrategyBase],
    engine: StrategyEngine = "candle",
    category: StrategyCategory = "other",
    thesis: str = "",
    strong_in: str = "",
    weak_in: str = "",
) -> Type[StrategyBase]:
    if name in _STRATEGY_REGISTRY:
        raise ValueError(f"Strategy '{name}' is already registered.")

    # Automatically append exit strategy parameters to candle strategies
    if engine == "candle" and name != "CompositeStrategy":
        from q_backend.backtesting.exit_strategy import get_exit_strategy_params
        existing_names = {p.name for p in params}
        for exit_param in get_exit_strategy_params():
            if exit_param.name not in existing_names:
                params.append(exit_param)

    _STRATEGY_REGISTRY[name] = RegisteredStrategy(
        strategy_class=strategy_class,
        info=StrategyInfo(
            name=name,
            label=label,
            description=description,
            params=params,
            engine=engine,
            category=category,
            thesis=thesis,
            strong_in=strong_in,
            weak_in=weak_in,
        ),
        build=build,
    )
    return strategy_class


_CUSTOM_STRATEGIES: set[str] = set()


def load_and_register_custom_strategies() -> None:
    from q_backend.backtesting.custom_strategy_store import load_custom_strategies

    # Remove old custom strategies
    for name in list(_CUSTOM_STRATEGIES):
        _STRATEGY_REGISTRY.pop(name, None)
    _CUSTOM_STRATEGIES.clear()

    customs = load_custom_strategies()
    for item in customs:
        name = item["name"]
        base_name = item["base_strategy"]
        description = item.get("description", "")
        saved_params = item.get("parameters", {})

        if base_name not in _STRATEGY_REGISTRY:
            continue

        base_entry = _STRATEGY_REGISTRY[base_name]

        # Override parameter defaults
        custom_params = []
        for param in base_entry.info.params:
            spec = param.model_copy()
            if spec.name in saved_params:
                spec.default = saved_params[spec.name]
            custom_params.append(spec)

        # Create a build function
        def make_build(base_build, default_params):
            def custom_build(params: dict[str, Any], symbol: str) -> StrategyBase:
                merged = default_params.copy()
                merged.update(params)
                return base_build(merged, symbol)
            return custom_build

        # Register the custom strategy
        _STRATEGY_REGISTRY[name] = RegisteredStrategy(
            strategy_class=base_entry.strategy_class,
            info=StrategyInfo(
                name=name,
                label=name,
                description=description,
                params=custom_params,
                engine=base_entry.info.engine,
                category=base_entry.info.category,
                thesis=base_entry.info.thesis,
                strong_in=base_entry.info.strong_in,
                weak_in=base_entry.info.weak_in,
            ),
            build=make_build(base_entry.build, saved_params),
        )
        _CUSTOM_STRATEGIES.add(name)


def get_registered_strategy(name: str) -> RegisteredStrategy:
    load_and_register_custom_strategies()
    try:
        return _STRATEGY_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"Unknown strategy: {name}") from exc


def list_registered_strategies() -> list[StrategyInfo]:
    load_and_register_custom_strategies()
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
    if name == "CompositeStrategy":
        return params.copy()
    entry = get_registered_strategy(name)
    merged: dict[str, Any] = {}
    for spec in entry.info.params:
        if spec.name in params:
            merged[spec.name] = _coerce_param_value(spec, params[spec.name])
        else:
            merged[spec.name] = spec.default
    return merged


import q_backend.backtesting.tick.strategies  # noqa: F401 — register tick strategies
