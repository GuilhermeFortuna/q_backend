import optuna
import pytest

import q_backend.backtesting.strategies  # noqa: F401
from q_backend.backtesting.strategy_registry import (
    StrategyParamSpec,
    _STRATEGY_REGISTRY,
    get_registered_strategy,
    list_registered_strategies,
    register_strategy,
)
from q_backend.optimization.auto_search_space import (
    auto_search_space,
    default_risk_search_space,
    derive_strategy_search_space,
)
from q_backend.optimization.models import (
    CategoricalParam,
    FloatParam,
    IntParam,
    LogFloatParam,
)
from q_backend.optimization.search_space import build_position_sizing_config, suggest_params


def _is_searchable(spec: StrategyParamSpec) -> bool:
    if spec.type == "int":
        return spec.min is not None and spec.max is not None and spec.min < spec.max
    if spec.type == "float":
        return spec.min is not None and spec.max is not None and spec.min < spec.max
    if spec.type == "categorical":
        return spec.choices is not None and len(spec.choices) > 1
    return False


def _expected_search_param_type(spec: StrategyParamSpec) -> type:
    if spec.type == "int":
        return IntParam
    if spec.type == "float":
        return FloatParam
    return CategoricalParam


CANDLE_STRATEGIES = [
    info.name for info in list_registered_strategies() if info.engine == "candle"
]


@pytest.mark.parametrize("strategy_name", CANDLE_STRATEGIES)
def test_derive_search_space_matches_registry_bounds(strategy_name: str):
    info = get_registered_strategy(strategy_name).info
    search_space, fixed_params = derive_strategy_search_space(strategy_name)

    for spec in info.params:
        if _is_searchable(spec):
            assert spec.name in search_space.strategy_params
            assert spec.name not in fixed_params
            param = search_space.strategy_params[spec.name]
            assert isinstance(param, _expected_search_param_type(spec))

            if spec.type == "int":
                assert param.low == int(spec.min)
                assert param.high == int(spec.max)
                assert param.step == (int(spec.step) if spec.step else 1)
            elif spec.type == "float":
                assert param.low == spec.min
                assert param.high == spec.max
                assert param.step == spec.step
            else:
                assert param.choices == list(spec.choices)
        else:
            assert spec.name in fixed_params
            assert spec.name not in search_space.strategy_params
            assert fixed_params[spec.name] == spec.default


@pytest.fixture
def synthetic_strategy():
    class _DummyStrategy:
        pass

    name = "_AutoSearchSpaceSynthetic"
    _STRATEGY_REGISTRY.pop(name, None)
    register_strategy(
        name=name,
        label="Synthetic",
        description="Boundary-case params for auto search space tests.",
        params=[
            StrategyParamSpec(
                name="fixed_int",
                label="Fixed Int",
                type="int",
                default=5,
                min=10,
                max=10,
            ),
            StrategyParamSpec(
                name="single_cat",
                label="Single Cat",
                type="categorical",
                default="only",
                choices=["only"],
            ),
            StrategyParamSpec(
                name="float_no_step",
                label="Float No Step",
                type="float",
                default=1.0,
                min=0.0,
                max=2.0,
            ),
            StrategyParamSpec(
                name="int_no_step",
                label="Int No Step",
                type="int",
                default=3,
                min=1,
                max=5,
            ),
            StrategyParamSpec(
                name="unbounded",
                label="Unbounded",
                type="float",
                default=42.0,
            ),
        ],
        build=lambda _params, _symbol: _DummyStrategy(),
        strategy_class=_DummyStrategy,
    )
    yield name
    _STRATEGY_REGISTRY.pop(name, None)


def test_boundary_cases_on_synthetic_strategy(synthetic_strategy: str):
    search_space, fixed_params = derive_strategy_search_space(synthetic_strategy)

    assert fixed_params == {
        "fixed_int": 5,
        "single_cat": "only",
        "unbounded": 42.0,
    }

    float_param = search_space.strategy_params["float_no_step"]
    assert isinstance(float_param, FloatParam)
    assert float_param.step is None

    int_param = search_space.strategy_params["int_no_step"]
    assert isinstance(int_param, IntParam)
    assert int_param.step == 1


def test_default_risk_search_space_builds_valid_position_sizing():
    risk_space = default_risk_search_space()
    study = optuna.create_study()
    trial = study.ask()
    params = suggest_params(trial, risk_space)
    config = build_position_sizing_config(params.risk_params)
    assert config is not None
    assert config.type == "fixed_safety_margin"


def test_auto_search_space_merges_strategy_and_risk():
    space = auto_search_space("MACrossover", include_risk=True)
    assert "short_period" in space.strategy_params
    assert "type" in space.risk_params
    assert isinstance(
        space.risk_params["safety_margin_per_contract"], LogFloatParam
    )


def test_auto_search_space_without_risk():
    space = auto_search_space("MACrossover", include_risk=False)
    assert space.strategy_params
    assert space.risk_params == {}


def test_unknown_strategy_raises():
    with pytest.raises(ValueError, match="Unknown strategy"):
        derive_strategy_search_space("NotARealStrategy")
