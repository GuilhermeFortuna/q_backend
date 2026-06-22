import optuna
import pytest

import q_backend.backtesting.strategies  # noqa: F401
from q_backend.backtesting.exit_rules.presets import EXIT_PRESETS
from q_backend.backtesting.exit_rules.registry import list_exit_rules
from q_backend.backtesting.strategy_registry import get_registered_strategy, list_registered_strategies
from q_backend.optimization.auto_search_space import derive_strategy_search_space
from q_backend.optimization.exit_preset_search_space import (
    derive_exit_preset_search_space,
    preset_exit_param_names,
)
from q_backend.optimization.models import FloatParam, IntParam
from q_backend.optimization.search_space import suggest_params


def _preset(preset_id: str):
    return next(preset for preset in EXIT_PRESETS if preset.id == preset_id)


def test_fixed_bracket_preset_searches_stop_and_take_profit_with_low_zero():
    preset = _preset("fixed_pct_bracket")
    search_space, fixed_params = derive_exit_preset_search_space(
        "MACrossover",
        preset,
        include_risk=False,
    )

    stop = search_space.strategy_params["stop_loss_pct"]
    take = search_space.strategy_params["take_profit_pct"]
    assert isinstance(stop, FloatParam)
    assert isinstance(take, FloatParam)
    assert stop.low == 0.0
    assert take.low == 0.0
    assert "stop_loss_pct" not in fixed_params
    assert "take_profit_pct" not in fixed_params


def test_atr_chandelier_preset_searches_atr_period_and_rule_params():
    preset = _preset("atr_stop_chandelier")
    search_space, _fixed_params = derive_exit_preset_search_space(
        "MACrossover",
        preset,
        include_risk=False,
    )

    assert "atr_period" in search_space.strategy_params
    assert "stop_loss_atr" in search_space.strategy_params
    assert "chandelier_atr_mult" in search_space.strategy_params
    assert search_space.strategy_params["stop_loss_atr"].low == 0.0
    assert search_space.strategy_params["chandelier_atr_mult"].low == 0.0
    assert isinstance(search_space.strategy_params["atr_period"], IntParam)


def test_non_preset_exit_enable_params_are_pinned_off():
    preset = _preset("fixed_pct_bracket")
    _search_space, fixed_params = derive_exit_preset_search_space(
        "MACrossover",
        preset,
        include_risk=False,
        pin_non_preset_exits_off=True,
    )

    preset_rules = {
        rule.enable_param
        for rule in list_exit_rules()
        if rule.enable_param in preset.parameters
        or any(name in preset.parameters for name in rule.param_names)
    }
    for rule in list_exit_rules():
        if rule.enable_param in preset_rules:
            continue
        assert fixed_params.get(rule.enable_param) == 0


def test_baseline_derivation_unchanged():
    baseline_space, baseline_fixed = derive_strategy_search_space("MACrossover")
    preset = _preset("fixed_pct_bracket")
    preset_space, preset_fixed = derive_exit_preset_search_space(
        "MACrossover",
        preset,
        include_risk=False,
    )

    for name, param in baseline_space.strategy_params.items():
        info = get_registered_strategy("MACrossover").info
        spec = next(item for item in info.params if item.name == name)
        if spec.exit_group is None:
            assert preset_space.strategy_params[name] == param

    for name, value in baseline_fixed.items():
        info = get_registered_strategy("MACrossover").info
        spec = next(item for item in info.params if item.name == name)
        if spec.exit_group is None:
            assert preset_fixed[name] == value


def test_preset_exit_param_names_match_preset_family():
    preset = _preset("atr_stop_chandelier")
    assert preset_exit_param_names(preset) == [
        "atr_period",
        "chandelier_atr_mult",
        "stop_loss_atr",
    ]


def test_exit_preset_search_space_samples_without_metadata_keys():
    preset = _preset("fixed_pct_bracket")
    search_space, fixed_params = derive_exit_preset_search_space(
        "MACrossover",
        preset,
        include_risk=False,
    )
    study = optuna.create_study()
    trial = study.ask()
    params = suggest_params(trial, search_space)
    merged = {**fixed_params, **params.strategy_params}
    assert "_exit_preset_id" not in merged
    assert all(key in merged for key in ("short_period", "long_period"))
