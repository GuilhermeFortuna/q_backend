"""Genome metadata representation for composable exit-rule policies (WO80)."""

from __future__ import annotations

import math
from typing import Any

from q_backend.backtesting.exit_rules.presets import EXIT_PRESETS
from q_backend.backtesting.exit_rules.registry import all_param_specs, list_exit_rules
from q_backend.backtesting.genome.param_bounds import GENOME_PARAM_BOUNDS
from q_backend.backtesting.genome.schema import Genome
from q_backend.backtesting.strategy_registry import (
    ExitPreset,
    ExitRuleInfo,
    StrategyParamSpec,
)

EXIT_RULE_POLICY_METADATA_KEY = "exit_rule_policy"


def genome_exit_param_ref(exit_param_name: str) -> str:
    return f"exit_{exit_param_name}"


def _rules_for_preset(preset: ExitPreset) -> list[ExitRuleInfo]:
    preset_params = set(preset.parameters.keys())
    rules: list[ExitRuleInfo] = []
    for info in list_exit_rules():
        if info.enable_param in preset_params:
            rules.append(info)
            continue
        if any(name in preset_params for name in info.param_names):
            rules.append(info)
    return rules


def preset_exit_param_names(preset: ExitPreset) -> list[str]:
    preset_rules = _rules_for_preset(preset)
    names: set[str] = set(preset.parameters.keys())
    for rule in preset_rules:
        names.add(rule.enable_param)
        names.update(rule.param_names)
        names.update(rule.required_param_names)
    return sorted(names)


def preset_by_id(preset_id: str) -> ExitPreset:
    for preset in EXIT_PRESETS:
        if preset.id == preset_id:
            return preset
    raise KeyError(preset_id)


def selected_exit_policy_presets(preset_ids: list[str] | None) -> list[ExitPreset]:
    if preset_ids is None:
        return list(EXIT_PRESETS)
    return [preset_by_id(preset_id) for preset_id in preset_ids]


def build_exit_rule_policy(preset: ExitPreset) -> dict[str, Any]:
    params = {name: {"param": genome_exit_param_ref(name)} for name in preset_exit_param_names(preset)}
    return {
        "preset_id": preset.id,
        "params": params,
    }


def build_single_stop_exit_policy(*, atr: bool = False) -> dict[str, Any]:
    if atr:
        return {
            "preset_id": "atr_stop_only",
            "params": {
                "stop_loss_atr": {"param": genome_exit_param_ref("stop_loss_atr")},
                "atr_period": {"param": genome_exit_param_ref("atr_period")},
            },
        }
    return {
        "preset_id": "fixed_stop_only",
        "params": {
            "stop_loss_pct": {"param": genome_exit_param_ref("stop_loss_pct")},
        },
    }


def get_exit_rule_policy(genome: Genome) -> dict[str, Any] | None:
    metadata = genome.metadata or {}
    policy = metadata.get(EXIT_RULE_POLICY_METADATA_KEY)
    if not isinstance(policy, dict):
        return None
    if "params" not in policy:
        return None
    return policy


def exit_policy_genome_param_names(genome: Genome) -> list[str]:
    policy = get_exit_rule_policy(genome)
    if policy is None:
        return []
    names: list[str] = []
    for value in policy.get("params", {}).values():
        if isinstance(value, dict) and "param" in value:
            names.append(str(value["param"]))
    return sorted(names)


def exit_policy_enable_param_names(genome: Genome) -> set[str]:
    policy = get_exit_rule_policy(genome)
    if policy is None:
        return set()
    preset_id = policy.get("preset_id")
    if not isinstance(preset_id, str):
        return set()
    try:
        preset = preset_by_id(preset_id)
    except KeyError:
        if preset_id == "fixed_stop_only":
            return {"stop_loss_pct"}
        if preset_id == "atr_stop_only":
            return {"stop_loss_atr"}
        return set()
    return {rule.enable_param for rule in _rules_for_preset(preset)}


def attach_exit_rule_policy(genome: Genome, preset: ExitPreset) -> Genome:
    child = genome.model_copy(deep=True)
    metadata = dict(child.metadata or {})
    metadata[EXIT_RULE_POLICY_METADATA_KEY] = build_exit_rule_policy(preset)
    child.metadata = metadata
    return child


def drop_exit_rule_policy(genome: Genome) -> Genome:
    child = genome.model_copy(deep=True)
    metadata = dict(child.metadata or {})
    metadata.pop(EXIT_RULE_POLICY_METADATA_KEY, None)
    child.metadata = metadata
    return child


def _is_param_ref(value: Any) -> bool:
    return isinstance(value, dict) and "param" in value and len(value) <= 2


def resolve_exit_params_from_policy(
    policy: dict[str, Any],
    trial_params: dict[str, Any],
) -> dict[str, Any]:
    runtime: dict[str, Any] = {}
    for info in list_exit_rules():
        runtime[info.enable_param] = 0

    for spec in all_param_specs():
        if spec.exit_group is not None and spec.name not in runtime:
            runtime[spec.name] = spec.default

    for exit_name, binding in policy.get("params", {}).items():
        if _is_param_ref(binding):
            genome_key = str(binding["param"])
            spec = GENOME_PARAM_BOUNDS.get(genome_key)
            runtime[exit_name] = (
                trial_params[genome_key] if genome_key in trial_params else (spec.default if spec is not None else 0)
            )
        else:
            runtime[exit_name] = binding
    return runtime


def exit_policy_metadata_for_genome(genome: Genome) -> dict[str, Any] | None:
    policy = get_exit_rule_policy(genome)
    if policy is None:
        return None
    preset_id = policy.get("preset_id")
    label = preset_id
    if isinstance(preset_id, str):
        try:
            label = preset_by_id(preset_id).label
        except KeyError:
            if preset_id == "fixed_stop_only":
                label = "Fixed stop only"
            elif preset_id == "atr_stop_only":
                label = "ATR stop only"
    metadata: dict[str, Any] = {
        "exit_policy_id": preset_id,
        "exit_policy_label": label,
        "exit_param_names": exit_policy_genome_param_names(genome),
    }
    last_exit_op = (genome.metadata or {}).get("last_exit_mutation_op")
    if last_exit_op is not None:
        metadata["last_exit_mutation_op"] = last_exit_op
    return metadata


def _round_to_sig(value: float, sig: int = 1) -> float:
    """Round ``value`` to ``sig`` significant figures (keeps synthesized steps clean)."""
    if value == 0:
        return 0.0
    digits = sig - 1 - math.floor(math.log10(abs(value)))
    return round(value, digits)


def _genome_exit_bounds(spec: StrategyParamSpec, *, is_enable: bool) -> tuple[float, float | None, float | None]:
    """Translate a curated exit spec into ``(min, max, step)`` for ``GENOME_PARAM_BOUNDS``.

    WO87 Task 6: discovery samples exit magnitudes from these bounds via the genome
    search space, which reads only ``min``/``max``/``step`` (it ignores ``search_*``).
    So the curated optimizer bounds must be baked in here. Enable params keep ``min=0``
    so the GA can switch the family off. The genome sampler has no log mode, so a
    ``log`` scale is approximated by a coarse linear step targeting ~12 grid points.
    """
    search_max = spec.search_max if spec.search_max is not None else spec.max
    search_min = spec.search_min if spec.search_min is not None else spec.min

    if is_enable:
        low: float = 0.0 if spec.type == "float" else 0
    else:
        low = search_min if search_min is not None else (spec.min or 0.0)

    if spec.search_step is not None:
        step: float | None = spec.search_step
    elif spec.search_scale == "log" and search_min is not None and search_max is not None:
        step = _round_to_sig((search_max - search_min) / 12.0, sig=1)
    else:
        step = spec.step

    return low, search_max, step


def register_exit_param_bounds() -> None:
    """Populate ``GENOME_PARAM_BOUNDS`` with ``exit_*`` keys from the exit catalog."""
    enable_params = {info.enable_param for info in list_exit_rules()}
    for spec in all_param_specs():
        key = genome_exit_param_ref(spec.name)
        if key in GENOME_PARAM_BOUNDS:
            continue
        is_enable = spec.name in enable_params
        min_val, max_val, step_val = _genome_exit_bounds(spec, is_enable=is_enable)
        GENOME_PARAM_BOUNDS[key] = spec.model_copy(
            update={
                "name": key,
                "min": min_val,
                "max": max_val,
                "step": step_val,
                "default": 0 if is_enable else spec.default,
                # search_* are baked into min/max/step above; clear them so the genome
                # path (which reads only min/max/step) can't double-apply.
                "search_min": None,
                "search_max": None,
                "search_step": None,
                "search_scale": None,
            }
        )


register_exit_param_bounds()
