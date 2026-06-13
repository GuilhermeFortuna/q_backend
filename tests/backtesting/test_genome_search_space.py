"""derive_genome_search_space tests."""

from q_backend.optimization.models import CategoricalParam, FloatParam, IntParam
from q_backend.backtesting.genome.search_space import derive_genome_search_space
from q_backend.backtesting.genome.registry_fixtures import MA_CROSSOVER_GENOME


def test_ma_genome_search_space_matches_registry_bounds():
    search_space, fixed_params = derive_genome_search_space(MA_CROSSOVER_GENOME)

    assert fixed_params == {}
    names = set(search_space.strategy_params.keys())
    assert names == {
        "short_period",
        "long_period",
        "short_ma_type",
        "long_ma_type",
        "threshold",
    }

    short = search_space.strategy_params["short_period"]
    assert isinstance(short, IntParam)
    assert short.low == 2
    assert short.high == 400

    threshold = search_space.strategy_params["threshold"]
    assert isinstance(threshold, FloatParam)
    assert threshold.low == 0.0
    assert threshold.high == 100.0

    short_type = search_space.strategy_params["short_ma_type"]
    assert isinstance(short_type, CategoricalParam)
    assert short_type.choices == sorted(["sma", "ema", "wma", "smma", "hma"])

    assert search_space.risk_params == {}
