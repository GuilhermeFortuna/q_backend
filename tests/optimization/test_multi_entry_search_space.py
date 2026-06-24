from __future__ import annotations

from datetime import datetime, timezone

import optuna
import pytest
from optuna.trial import FixedTrial

import q_backend.backtesting.strategies  # noqa: F401
from q_backend.backtesting.entry_models import EntryInstance, EntryManagerConfig
from q_backend.backtesting.composite_entry import CompositeEntryStrategy
from q_backend.backtesting.factory import build_composite_entry
from q_backend.backtesting.strategy_registry import get_registered_strategy
from q_backend.optimization.auto_search_space import (
    _search_param_from_spec,
    auto_multi_entry_search_space,
    derive_multi_entry_search_space,
    derive_strategy_search_space,
)
from q_backend.optimization.backtest_runner import BacktestRunConfig, DefaultBacktestRunner
from q_backend.optimization.models import (
    ObjectiveConfig,
    ObjectiveMode,
    OptimizationConfig,
    StorageConfig,
    StudyConfig,
    BacktestConfig,
)
from q_backend.optimization.runner import OptimizationRunner
from q_backend.optimization.search_space import (
    build_position_sizing_config,
    entries_from_trial_params,
    manager_params_from_trial,
    suggest_params,
)


def _entry_searchable_keys(strategy_name: str) -> set[str]:
    entry = get_registered_strategy(strategy_name)
    keys: set[str] = set()
    for spec in entry.info.params:
        if spec.exit_group is not None:
            continue
        if _search_param_from_spec(spec) is not None:
            keys.add(spec.name)
    return keys


def _fixed_trial_values(search_space) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, spec in search_space.strategy_params.items():
        optuna_name = f"strategy__{key}"
        if spec.type == "int":
            values[optuna_name] = spec.low
        elif spec.type == "float":
            values[optuna_name] = spec.low
        elif spec.type == "log-float":
            values[optuna_name] = spec.low
        else:
            values[optuna_name] = spec.choices[0]
    for key, spec in search_space.manager_params.items():
        optuna_name = f"manager__{key}"
        if spec.type == "int":
            values[optuna_name] = spec.low
        elif spec.type == "float":
            values[optuna_name] = spec.low
        else:
            values[optuna_name] = spec.choices[0]
    return values


def test_two_instances_derive_disjoint_namespaced_keys():
    entries = [
        EntryInstance(strategy="MACrossover", params={"short_period": 5, "long_period": 20}),
        EntryInstance(strategy="MACrossover", params={"short_period": 10, "long_period": 40}),
    ]
    manager = EntryManagerConfig(kind="or")

    search_space, _fixed = derive_multi_entry_search_space(entries, manager)
    strategy_keys = set(search_space.strategy_params)

    assert "e0__short_period" in strategy_keys
    assert "e0__long_period" in strategy_keys
    assert "e1__short_period" in strategy_keys
    assert "e1__long_period" in strategy_keys
    e0_keys = {key for key in strategy_keys if key.startswith("e0__")}
    e1_keys = {key for key in strategy_keys if key.startswith("e1__")}
    assert e0_keys.isdisjoint(e1_keys)
    assert {key.removeprefix("e0__") for key in e0_keys} == {
        key.removeprefix("e1__") for key in e1_keys
    }


def test_majority_manager_exposes_vote_threshold_only():
    entries = [
        EntryInstance(strategy="MACrossover", params={}),
        EntryInstance(strategy="RSIMeanReversion", params={}),
    ]
    majority_space, _fixed = derive_multi_entry_search_space(
        entries,
        EntryManagerConfig(kind="majority", params={"vote_threshold": 2}),
    )
    or_space, _ = derive_multi_entry_search_space(
        entries,
        EntryManagerConfig(kind="or"),
    )

    assert set(majority_space.manager_params) == {"vote_threshold"}
    assert or_space.manager_params == {}


def test_entries_from_trial_params_round_trip():
    entries = [
        EntryInstance(strategy="MACrossover", params={"short_period": 5}),
        EntryInstance(strategy="RSIMeanReversion", params={"period": 14}),
    ]
    manager = EntryManagerConfig(kind="majority", params={"vote_threshold": 2})
    search_space, fixed_params = derive_multi_entry_search_space(entries, manager)

    trial = FixedTrial(_fixed_trial_values(search_space))
    trial_params = suggest_params(trial, search_space)
    rebuilt = entries_from_trial_params(
        trial_params.strategy_params,
        entries,
        fixed_strategy_params=fixed_params,
    )
    manager_params = manager_params_from_trial(
        trial_params.manager_params,
        manager,
        fixed_params=fixed_params,
    )

    assert [entry.strategy for entry in rebuilt] == ["MACrossover", "RSIMeanReversion"]
    assert rebuilt[0].params["short_period"] == trial_params.strategy_params["e0__short_period"]
    assert rebuilt[1].params["period"] == trial_params.strategy_params["e1__period"]
    assert manager_params["vote_threshold"] == trial_params.manager_params["vote_threshold"]


def test_single_entry_parity_after_stripping_prefix():
    strategy_name = "MACrossover"
    entries = [EntryInstance(strategy=strategy_name, params={})]
    multi_space, _multi_fixed = derive_multi_entry_search_space(
        entries,
        EntryManagerConfig(kind="or"),
    )
    single_space, _single_fixed = derive_strategy_search_space(strategy_name)
    entry_only = _entry_searchable_keys(strategy_name)

    stripped = {
        key.split("__", 1)[1] for key in multi_space.strategy_params if key.startswith("e0__")
    }
    assert stripped == entry_only
    assert stripped == set(single_space.strategy_params.keys()) & entry_only


def test_multi_entry_optimization_rebuilds_composite_strategy(sample_ohlcv_df):
    entries = [
        EntryInstance(strategy="MACrossover", params={}),
        EntryInstance(strategy="RSIMeanReversion", params={}),
    ]
    manager = EntryManagerConfig(kind="or")
    search_space, fixed_params = auto_multi_entry_search_space(
        entries,
        manager,
        include_risk=False,
    )

    start = sample_ohlcv_df.index[0].to_pydatetime()
    end = sample_ohlcv_df.index[-1].to_pydatetime()
    frame = sample_ohlcv_df.copy()
    if frame.index.tz is not None:
        frame.index = frame.index.tz_localize(None)
        start = start.replace(tzinfo=None)
        end = end.replace(tzinfo=None)

    config = OptimizationConfig(
        study=StudyConfig(
            name="multi_entry_opt",
            n_trials=5,
            seed=42,
            storage=StorageConfig(type="memory"),
        ),
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
        backtest=BacktestConfig(
            symbol="TEST",
            timeframe="H4",
            start=start,
            end=end,
            strategy="MACrossover",
            entries=entries,
            entry_manager=manager,
        ),
        search_space=search_space,
        fixed_params=fixed_params,
    )

    runner = DefaultBacktestRunner.from_frame_sliced(frame)
    result = OptimizationRunner(config, runner).run()

    assert result.best_trial is not None
    strategy_params = result.best_trial.user_attrs["strategy_params"]
    manager_params = result.best_trial.user_attrs.get("manager_params", {})

    rebuilt_entries = entries_from_trial_params(
        strategy_params,
        entries,
        fixed_strategy_params=fixed_params,
    )
    resolved_manager = manager_params_from_trial(
        manager_params,
        manager,
        fixed_params=fixed_params,
    )
    strategy = build_composite_entry(
        [
            {"strategy": entry.strategy, "params": entry.params}
            for entry in rebuilt_entries
        ],
        manager.kind,
        resolved_manager,
        {},
        "TEST",
    )
    assert isinstance(strategy, CompositeEntryStrategy)

    run_config = BacktestRunConfig(
        symbol="TEST",
        timeframe="H4",
        start=start,
        end=end,
        initial_capital=100_000.0,
        point_value=1.0,
        strategy="MACrossover",
        strategy_params=strategy_params,
        position_sizing=build_position_sizing_config({}),
        entries=entries,
        entry_manager=manager,
        manager_params=manager_params,
        fixed_params=fixed_params,
    )
    backtest_result = runner.run(run_config)
    assert backtest_result.metrics.get("total_trades", 0) > 0


def test_multi_entry_ma_validation_prunes_namespaced_periods():
    from q_backend.optimization.models import TrialParams
    from q_backend.optimization.validators import validate_trial_params

    entries = [
        EntryInstance(strategy="MACrossover", params={}),
        EntryInstance(strategy="MACrossover", params={}),
    ]
    with pytest.raises(optuna.TrialPruned):
        validate_trial_params(
            TrialParams(
                strategy_params={
                    "e0__short_period": 30,
                    "e0__long_period": 10,
                    "e1__short_period": 5,
                    "e1__long_period": 20,
                }
            ),
            "MACrossover",
            entries=entries,
        )
