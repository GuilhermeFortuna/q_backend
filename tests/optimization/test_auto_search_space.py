import optuna
import pytest

import q_backend.backtesting.strategies  # noqa: F401
from q_backend.backtesting.exit_rules.registry import all_param_specs
from q_backend.backtesting.strategy_registry import (
    StrategyParamSpec,
    _STRATEGY_REGISTRY,
    get_registered_strategy,
    list_registered_strategies,
    register_strategy,
)
from q_backend.optimization.auto_search_space import (
    _effective_search_bounds,
    _search_param_from_spec,
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
    if not spec.searchable:
        return False
    lo, hi, _st = _effective_search_bounds(spec)
    if spec.type == "int":
        return lo is not None and hi is not None and lo < hi
    if spec.type == "float":
        return lo is not None and hi is not None and lo < hi
    if spec.type == "categorical":
        return spec.choices is not None and len(spec.choices) > 1
    return False


def _expected_search_param_type(spec: StrategyParamSpec) -> type:
    if spec.type == "int":
        return IntParam
    if spec.type == "float":
        lo, _hi, _st = _effective_search_bounds(spec)
        if spec.search_scale == "log" and lo is not None and lo > 0:
            return LogFloatParam
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

            lo, hi, st = _effective_search_bounds(spec)
            if spec.type == "int":
                assert param.low == int(lo)
                assert param.high == int(hi)
                assert param.step == (int(st) if st else 1)
            elif spec.type == "float":
                assert param.low == lo
                assert param.high == hi
                if isinstance(param, FloatParam):
                    assert param.step == st
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


def test_search_overrides_honored_over_editor_bounds():
    spec = StrategyParamSpec(
        name="stop_loss_pct",
        label="Stop Loss",
        type="float",
        default=0.0,
        min=0.0,
        max=0.50,
        step=0.001,
        search_min=0.002,
        search_max=0.05,
        search_scale="log",
    )
    param = _search_param_from_spec(spec)
    assert isinstance(param, LogFloatParam)
    assert param.low == 0.002
    assert param.high == 0.05


def test_fallback_to_editor_bounds_when_search_absent():
    spec = StrategyParamSpec(
        name="period",
        label="Period",
        type="int",
        default=20,
        min=2,
        max=400,
        step=1,
    )
    param = _search_param_from_spec(spec)
    assert isinstance(param, IntParam)
    assert param.low == 2
    assert param.high == 400
    assert param.step == 1


def test_search_scale_log_ignores_search_step():
    spec = StrategyParamSpec(
        name="take_profit_pct",
        label="Take Profit",
        type="float",
        default=0.0,
        min=0.0,
        max=1.0,
        step=0.001,
        search_min=0.003,
        search_max=0.10,
        search_step=0.01,
        search_scale="log",
    )
    param = _search_param_from_spec(spec)
    assert isinstance(param, LogFloatParam)


def test_searchable_false_pins_to_fixed_params():
    search_space, fixed_params = derive_strategy_search_space("HurstTrendBlend")
    assert "risk_free_rate_annual" in fixed_params
    assert "signal_lag_bars" in fixed_params
    assert "risk_free_rate_annual" not in search_space.strategy_params
    assert "signal_lag_bars" not in search_space.strategy_params


def test_validator_rejects_invalid_search_bounds():
    with pytest.raises(ValueError, match="search_min must be < search_max"):
        StrategyParamSpec(
            name="bad",
            label="Bad",
            type="float",
            default=1.0,
            search_min=5.0,
            search_max=1.0,
        )

    with pytest.raises(ValueError, match="search_scale='log' requires positive"):
        StrategyParamSpec(
            name="bad_log",
            label="Bad Log",
            type="float",
            default=0.0,
            min=0.0,
            max=1.0,
            search_scale="log",
        )


def test_curated_rsi_mean_reversion_search_bounds():
    info = get_registered_strategy("RSIMeanReversion").info
    period = next(p for p in info.params if p.name == "period")
    assert period.search_min == 7
    assert period.search_max == 21
    assert period.search_step == 7


def test_curated_tsmom_search_bounds():
    info = get_registered_strategy("TSMOM").info
    lookback = next(p for p in info.params if p.name == "lookback_bars")
    assert lookback.search_min == 63
    assert lookback.search_max == 252
    assert lookback.search_step == 63


def test_exit_rule_specs_have_log_pct_search_bounds():
    specs = {s.name: s for s in all_param_specs()}
    sl = specs["stop_loss_pct"]
    assert sl.search_scale == "log"
    assert sl.search_min == 0.002
    assert sl.search_max == 0.05
    assert sl.min == 0.0
    assert sl.max == 0.50
    assert sl.step == 0.001


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
