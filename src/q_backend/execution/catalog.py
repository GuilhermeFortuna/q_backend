"""Deployable strategy catalog: built-in candle strategies and eligible custom wrappers.

A catalog entry is deployable to forward paper execution only when it (or, for a
custom wrapper, its base) is a registered *candle* strategy other than the
``CompositeStrategy`` genome interpreter. Tick strategies and genomes never appear
here; the research catalog (``GET /api/v1/strategies``) is unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel

from q_backend.backtesting.custom_strategy_store import load_custom_strategies
from q_backend.backtesting.strategy_registry import (
    StrategyParamSpec,
    get_registered_strategy,
    list_registered_strategies,
    load_and_register_custom_strategies,
    merge_strategy_params,
)

EXCLUDED_STRATEGIES = frozenset({"CompositeStrategy"})


class CatalogUnavailableError(ValueError):
    """Raised when a requested catalog strategy is unknown or not deployable."""


class CatalogEntry(BaseModel):
    name: str
    label: str
    description: str
    source_kind: str
    base_strategy_name: str
    params: list[StrategyParamSpec]


class CatalogResponse(BaseModel):
    strategies: list[CatalogEntry]


def _custom_base_names() -> dict[str, str]:
    return {item["name"]: item["base_strategy"] for item in load_custom_strategies()}


def _is_deployable(name: str, base_name: str) -> bool:
    if name in EXCLUDED_STRATEGIES or base_name in EXCLUDED_STRATEGIES:
        return False
    try:
        info = get_registered_strategy(name).info
    except ValueError:
        return False
    return info.engine == "candle"


def build_catalog() -> list[CatalogEntry]:
    """List built-in candle strategies plus custom wrappers with a deployable base."""
    load_and_register_custom_strategies()
    customs = _custom_base_names()
    entries: list[CatalogEntry] = []
    for info in list_registered_strategies():
        base_name = customs.get(info.name, info.name)
        if not _is_deployable(info.name, base_name):
            continue
        entries.append(
            CatalogEntry(
                name=info.name,
                label=info.label,
                description=info.description,
                source_kind="custom" if info.name in customs else "builtin",
                base_strategy_name=base_name,
                params=info.params,
            )
        )
    return entries


@dataclass(frozen=True)
class CompiledCatalogSelection:
    """Server-computed, worker-buildable strategy configuration for a catalog input."""

    base_strategy_name: str
    resolved_params: dict[str, Any]
    compiled_config: dict[str, Any]
    source_kind: str
    source_strategy_name: Optional[str]


def compile_catalog_selection(
    *,
    catalog_name: str,
    strategy_params: Optional[dict[str, Any]],
    exit_params: Optional[dict[str, Any]],
    symbol: str,
    timeframe: str,
) -> CompiledCatalogSelection:
    """Resolve a catalog selection to an immutable, worker-buildable compiled config.

    For a custom wrapper, ``catalog_name`` is kept only as provenance
    (``source_strategy_name``); the compiled config always names the expanded
    built-in base strategy so a later wrapper edit or deletion cannot change an
    already-deployed configuration.
    """
    load_and_register_custom_strategies()
    customs = _custom_base_names()
    base_name = customs.get(catalog_name, catalog_name)
    if not _is_deployable(catalog_name, base_name):
        raise CatalogUnavailableError(f"strategy '{catalog_name}' is not deployable to forward execution")

    submitted = {**(strategy_params or {}), **(exit_params or {})}
    known_names = {spec.name for spec in get_registered_strategy(catalog_name).info.params}
    unknown = sorted(set(submitted) - known_names)
    if unknown:
        raise CatalogUnavailableError(f"unknown parameter(s) for '{catalog_name}': {', '.join(unknown)}")
    _validate_param_bounds(catalog_name, submitted)

    resolved_params = merge_strategy_params(catalog_name, submitted)
    compiled_config = {
        "strategy": base_name,
        "strategy_params": resolved_params,
        "symbol": symbol,
        "timeframe": timeframe,
    }
    return CompiledCatalogSelection(
        base_strategy_name=base_name,
        resolved_params=resolved_params,
        compiled_config=compiled_config,
        source_kind="custom" if catalog_name in customs else "builtin",
        source_strategy_name=catalog_name if catalog_name in customs else None,
    )


def _validate_param_bounds(catalog_name: str, submitted: dict[str, Any]) -> None:
    specs = {spec.name: spec for spec in get_registered_strategy(catalog_name).info.params}
    for name, raw in submitted.items():
        spec = specs.get(name)
        if spec is None:
            continue
        if spec.type == "categorical":
            if spec.choices is not None and str(raw) not in spec.choices:
                raise CatalogUnavailableError(f"'{name}' must be one of {spec.choices}")
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise CatalogUnavailableError(f"'{name}' must be numeric") from exc
        if spec.min is not None and value < spec.min:
            raise CatalogUnavailableError(f"'{name}' must be >= {spec.min}")
        if spec.max is not None and value > spec.max:
            raise CatalogUnavailableError(f"'{name}' must be <= {spec.max}")
        if spec.step is not None and spec.step > 0:
            steps = (value - (spec.min or 0.0)) / spec.step
            if abs(steps - round(steps)) > 1e-6:
                raise CatalogUnavailableError(f"'{name}' must align to step {spec.step}")
