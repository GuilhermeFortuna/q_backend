from q_backend.api.routers.strategies import list_exit_rules_catalog, list_strategies
from q_backend.backtesting.strategy_registry import StrategyCategory


def test_list_strategies_returns_all_registered():
    response = list_strategies()
    names = [item.name for item in response["strategies"]]
    assert names == sorted([
        "BollingerReversion",
        "CompositeStrategy",
        "DonchianBreakout",
        "FMA",
        "GatevPairs",
        "HurstTrendBlend",
        "MACD",
        "MACrossover",
        "RSIMeanReversion",
        "TRB",
        "TickMaBreakout",
        "TSMOM",
        "VMA",
    ])


def test_list_strategies_macrossover_schema():
    response = list_strategies()
    ma = next(item for item in response["strategies"] if item.name == "MACrossover")

    assert ma.label == "MA Crossover"
    assert ma.description == "Short/long moving-average crossover."

    param_names = [spec.name for spec in ma.params]
    assert param_names[:5] == [
        "short_period",
        "long_period",
        "short_ma_type",
        "long_ma_type",
        "threshold",
    ]

    short_period = next(spec for spec in ma.params if spec.name == "short_period")
    assert short_period.type == "int"
    assert short_period.default == 50
    assert short_period.min == 2
    assert short_period.max == 400

    short_ma_type = next(spec for spec in ma.params if spec.name == "short_ma_type")
    assert short_ma_type.type == "categorical"
    assert short_ma_type.default == "sma"
    assert short_ma_type.choices == sorted(["sma", "ema", "wma", "smma", "hma"])

    threshold = next(spec for spec in ma.params if spec.name == "threshold")
    assert threshold.type == "float"
    assert threshold.default == 0.0


def test_list_strategies_tick_ma_breakout_schema():
    response = list_strategies()
    tick = next(item for item in response["strategies"] if item.name == "TickMaBreakout")

    assert tick.label == "Tick MA Breakout"
    assert tick.engine == "tick"
    param_names = [spec.name for spec in tick.params]
    assert param_names == [
        "short_period",
        "long_period",
        "threshold",
        "sl_points",
        "tp_points",
    ]


def test_list_strategies_each_has_valid_param_schema():
    response = list_strategies()
    for strategy in response["strategies"]:
        assert strategy.name
        assert strategy.label
        assert strategy.description
        if strategy.name == "CompositeStrategy":
            continue
        assert len(strategy.params) >= 1
        for spec in strategy.params:
            assert spec.name
            assert spec.label
            assert spec.type in {"int", "float", "categorical"}
            assert spec.default is not None
            if spec.type == "categorical":
                assert spec.choices
            if spec.type in {"int", "float"}:
                assert spec.min is not None
                assert spec.max is not None


VALID_CATEGORIES: set[StrategyCategory] = {
    "trend",
    "mean_reversion",
    "breakout",
    "momentum",
    "other",
}


def test_list_strategies_presentation_metadata():
    response = list_strategies()
    for strategy in response["strategies"]:
        assert strategy.category in VALID_CATEGORIES
        if strategy.name == "CompositeStrategy":
            continue
        assert strategy.thesis
        assert strategy.strong_in
        assert strategy.weak_in
        for spec in strategy.params:
            assert spec.hint


def test_list_strategies_macrossover_presentation_metadata():
    response = list_strategies()
    ma = next(item for item in response["strategies"] if item.name == "MACrossover")

    assert ma.category == "trend"
    assert ma.strong_in
    assert ma.weak_in

    short_period = next(spec for spec in ma.params if spec.name == "short_period")
    assert short_period.hint


def test_exit_rules_catalog_endpoint():
    response = list_exit_rules_catalog()

    assert set(response.keys()) == {"exit_rules", "shared_exit_params", "exit_presets"}
    assert len(response["exit_rules"]) == 11
    assert response["shared_exit_params"] == ["atr_period"]
    assert len(response["exit_presets"]) == 6

    chandelier = next(item for item in response["exit_rules"] if item.id == "chandelier")
    assert chandelier.label == "Chandelier Exit"
    assert chandelier.enable_param == "chandelier_atr_mult"
    assert chandelier.param_names == ["chandelier_atr_mult"]
    assert chandelier.required_param_names == ["atr_period"]

    preset = next(item for item in response["exit_presets"] if item.id == "atr_stop_chandelier")
    assert preset.parameters["stop_loss_atr"] == 2.0
    assert preset.parameters["chandelier_atr_mult"] == 3.0


def test_strategies_endpoint_unchanged_after_exit_rules_catalog():
    response = list_strategies()
    ma = next(item for item in response["strategies"] if item.name == "MACrossover")

    assert ma.label == "MA Crossover"
    assert [spec.name for spec in ma.params[:5]] == [
        "short_period",
        "long_period",
        "short_ma_type",
        "long_ma_type",
        "threshold",
    ]
    assert any(spec.name == "stop_loss_pct" for spec in ma.params)
    assert any(spec.name == "atr_period" for spec in ma.params)
