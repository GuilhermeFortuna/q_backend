import optuna

from q_backend.backtesting.position_sizing import FixedQuantityPositionSizing
from q_backend.optimization.models import (
    CategoricalParam,
    FloatParam,
    IntParam,
    SearchSpaceConfig,
)
from q_backend.optimization.search_space import (
    build_position_sizing_config,
    suggest_params,
)


def test_suggest_params_prefixes_names():
    search_space = SearchSpaceConfig(
        strategy_params={
            "short_period": IntParam(type="int", low=2, high=4),
        },
        risk_params={
            "quantity": FloatParam(type="float", low=1.0, high=2.0),
        },
    )
    study = optuna.create_study()
    trial = study.ask()
    params = suggest_params(trial, search_space)
    assert "short_period" in params.strategy_params
    assert "quantity" in params.risk_params


def test_build_position_sizing_fixed_quantity():
    config = build_position_sizing_config({"type": "fixed_quantity", "quantity": 2.5})
    assert isinstance(config, FixedQuantityPositionSizing)
    assert config.quantity == 2.5


def test_build_position_sizing_categorical_type():
    search_space = SearchSpaceConfig(
        risk_params={
            "type": CategoricalParam(type="categorical", choices=["fixed_quantity"]),
            "quantity": FloatParam(type="float", low=1.0, high=3.0),
        }
    )
    study = optuna.create_study()
    trial = study.ask()
    params = suggest_params(trial, search_space)
    config = build_position_sizing_config(params.risk_params)
    assert config is not None
    assert config.type == "fixed_quantity"
