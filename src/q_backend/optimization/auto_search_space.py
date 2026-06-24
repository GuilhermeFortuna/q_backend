"""Derive Optuna search spaces from strategy registry parameter specs.

Each registered strategy declares editor bounds (``min``/``max``/``step``) for the
manual parameter form and optional optimizer-only bounds (``search_min``/``search_max``/
``search_step``/``search_scale``). This module is the single chokepoint that maps
specs to ``SearchSpaceConfig``: bounded searchable params become Optuna dimensions;
params with ``searchable=False`` or without usable bounds are pinned to registry
defaults via ``fixed_params`` (WO31 merges those into every trial).

When ``search_scale`` is ``"log"`` on a float param with a positive effective low,
the dimension becomes ``LogFloatParam`` (continuous log-uniform sampling; ``search_step``
is ignored).
"""

from __future__ import annotations

from typing import Any

from q_backend.backtesting.entry_models import EntryInstance, EntryManagerConfig
from q_backend.backtesting.strategy_registry import (
    StrategyParamSpec,
    get_registered_strategy,
)
from q_backend.backtesting.signal_managers.registry import get_manager
from q_backend.optimization.models import (
    CategoricalParam,
    FloatParam,
    IntParam,
    LogFloatParam,
    SearchParam,
    SearchSpaceConfig,
)


def _effective_search_bounds(
    spec: StrategyParamSpec,
) -> tuple[float | None, float | None, float | None]:
    lo = spec.search_min if spec.search_min is not None else spec.min
    hi = spec.search_max if spec.search_max is not None else spec.max
    st = spec.search_step if spec.search_step is not None else spec.step
    return lo, hi, st


def _search_param_from_spec(spec: StrategyParamSpec) -> SearchParam | None:
    if not spec.searchable:
        return None

    lo, hi, st = _effective_search_bounds(spec)

    if spec.type == "int":
        if lo is not None and hi is not None and lo < hi:
            step = int(st) if st else 1
            return IntParam(low=int(lo), high=int(hi), step=step)
        return None

    if spec.type == "float":
        if lo is not None and hi is not None and lo < hi:
            if spec.search_scale == "log" and lo > 0:
                return LogFloatParam(low=lo, high=hi)
            return FloatParam(low=lo, high=hi, step=st)
        return None

    if spec.type == "categorical":
        if spec.choices is not None and len(spec.choices) > 1:
            return CategoricalParam(choices=list(spec.choices))
        return None

    return None


def _derive_instance_search_space(
    strategy_name: str,
    slot_id: str,
) -> tuple[dict[str, SearchParam], dict[str, Any]]:
    entry = get_registered_strategy(strategy_name)
    strategy_params: dict[str, SearchParam] = {}
    fixed_params: dict[str, Any] = {}

    for spec in entry.info.params:
        if spec.exit_group is not None:
            continue
        search_param = _search_param_from_spec(spec)
        key = f"{slot_id}__{spec.name}"
        if search_param is not None:
            strategy_params[key] = search_param
        else:
            fixed_params[key] = spec.default

    return strategy_params, fixed_params


def derive_multi_entry_search_space(
    entries: list[EntryInstance],
    manager: EntryManagerConfig,
) -> tuple[SearchSpaceConfig, dict[str, Any]]:
    """Return ``(search_space, fixed_params)`` for a multi-entry composite setup."""
    strategy_params: dict[str, SearchParam] = {}
    fixed_params: dict[str, Any] = {}
    manager_params: dict[str, SearchParam] = {}

    for index, entry in enumerate(entries):
        slot_id = f"e{index}"
        instance_search, instance_fixed = _derive_instance_search_space(
            entry.strategy,
            slot_id,
        )
        strategy_params.update(instance_search)
        fixed_params.update(instance_fixed)
        for key, value in entry.params.items():
            namespaced = f"{slot_id}__{key}"
            if namespaced not in strategy_params:
                fixed_params[namespaced] = value

    signal_manager = get_manager(manager.kind, manager.params)
    for spec in signal_manager.param_specs():
        effective_spec = spec
        if spec.name == "vote_threshold":
            instance_count = max(len(entries), 1)
            updates: dict[str, Any] = {}
            if spec.max is None:
                updates["max"] = instance_count
            if spec.search_max is None:
                updates["search_max"] = instance_count
            if updates:
                effective_spec = spec.model_copy(update=updates)
        search_param = _search_param_from_spec(effective_spec)
        if search_param is not None:
            manager_params[spec.name] = search_param
        else:
            fixed_params[f"manager__{spec.name}"] = manager.params.get(
                spec.name,
                spec.default,
            )

    return (
        SearchSpaceConfig(
            strategy_params=strategy_params,
            risk_params={},
            manager_params=manager_params,
        ),
        fixed_params,
    )


def derive_strategy_search_space(strategy_name: str) -> tuple[SearchSpaceConfig, dict[str, Any]]:
    """Return ``(search_space, fixed_params)``.

    ``search_space.strategy_params`` holds params that will be searched;
    ``fixed_params`` holds ``{name: default}`` for params kept constant
    during the study. ``risk_params`` is empty — use ``default_risk_search_space``
    or ``auto_search_space`` for position-sizing dimensions.
    """
    entry = get_registered_strategy(strategy_name)
    strategy_params: dict[str, SearchParam] = {}
    fixed_params: dict[str, Any] = {}

    for spec in entry.info.params:
        search_param = _search_param_from_spec(spec)
        if search_param is not None:
            strategy_params[spec.name] = search_param
        else:
            fixed_params[spec.name] = spec.default

    return (
        SearchSpaceConfig(strategy_params=strategy_params, risk_params={}),
        fixed_params,
    )


def default_risk_search_space() -> SearchSpaceConfig:
    """Conservative default position-sizing search space.

    Uses ``fixed_safety_margin`` sizing and searches ``safety_margin_per_contract``
    over a modest log-scale range. Keys match ``build_position_sizing_config`` in
    ``search_space.py`` (``type``, ``safety_margin_per_contract``).
    """
    return SearchSpaceConfig(
        strategy_params={},
        risk_params={
            # Pin sizing mode; WO31 may override or disable risk search entirely.
            "type": CategoricalParam(choices=["fixed_safety_margin"]),
            # Capital buffer per contract (log scale suits wide dollar ranges).
            "safety_margin_per_contract": LogFloatParam(low=1000.0, high=10000.0),
        },
    )


def auto_search_space(strategy_name: str, *, include_risk: bool = True) -> SearchSpaceConfig:
    """Build a complete ``SearchSpaceConfig`` from registry metadata.

    Strategy params come from ``derive_strategy_search_space``; when
    ``include_risk`` is true, ``default_risk_search_space`` risk params are
    merged in.

    For ``fixed_params`` (unbounded registry params pinned to defaults), call
    ``derive_strategy_search_space`` directly — WO31 merges them into trials.
    """
    search_space, _ = derive_strategy_search_space(strategy_name)
    if not include_risk:
        return search_space

    risk_space = default_risk_search_space()
    return SearchSpaceConfig(
        strategy_params=search_space.strategy_params,
        risk_params=risk_space.risk_params,
    )


def auto_multi_entry_search_space(
    entries: list[EntryInstance],
    manager: EntryManagerConfig,
    *,
    include_risk: bool = True,
) -> tuple[SearchSpaceConfig, dict[str, Any]]:
    """Build a complete search space for a multi-entry composite configuration."""
    search_space, fixed_params = derive_multi_entry_search_space(entries, manager)
    if not include_risk:
        return search_space, fixed_params

    risk_space = default_risk_search_space()
    return (
        SearchSpaceConfig(
            strategy_params=search_space.strategy_params,
            risk_params=risk_space.risk_params,
            manager_params=search_space.manager_params,
        ),
        fixed_params,
    )
