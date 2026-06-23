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
    assert short.low == 5
    assert short.high == 60
    assert short.step == 5

    threshold = search_space.strategy_params["threshold"]
    assert isinstance(threshold, FloatParam)
    assert threshold.low == -3.0
    assert threshold.high == 3.0
    assert threshold.step == 0.5

    short_type = search_space.strategy_params["short_ma_type"]
    assert isinstance(short_type, CategoricalParam)
    assert short_type.choices == sorted(["sma", "ema", "wma", "smma", "hma"])

    assert search_space.risk_params == {}


def test_discovery_exit_bounds_are_curated():
    """WO87 Task 6: discovery must inherit the curated exit search bounds, not the
    wide editor bounds (stop loss was 0-0.5 step 0.001 ~= 500 grid points)."""
    from q_backend.backtesting.genome.param_bounds import GENOME_PARAM_BOUNDS
    from q_backend.backtesting.genome.search_space import _search_param_from_spec

    sl = GENOME_PARAM_BOUNDS["exit_stop_loss_pct"]
    assert sl.max == 0.05  # curated search_max, not the 0.50 editor bound

    param = _search_param_from_spec(sl)
    assert isinstance(param, FloatParam)
    assert param.low == 0.0  # enable param keeps 0 so the GA can switch it off
    assert param.high == 0.05
    grid = round((param.high - param.low) / param.step) + 1
    assert grid <= 15  # was ~500 before the fix

    atr_period = GENOME_PARAM_BOUNDS["exit_atr_period"]
    assert atr_period.min == 7
    assert atr_period.max == 28


def test_genome_search_space_includes_tightened_exit_dimension():
    from q_backend.backtesting.genome.exit_rule_policy import (
        EXIT_RULE_POLICY_METADATA_KEY,
        build_single_stop_exit_policy,
    )
    from q_backend.backtesting.genome.schema import Genome

    genome = Genome.model_validate(MA_CROSSOVER_GENOME)
    metadata = dict(genome.metadata or {})
    metadata[EXIT_RULE_POLICY_METADATA_KEY] = build_single_stop_exit_policy(atr=False)
    genome.metadata = metadata

    search_space, _ = derive_genome_search_space(genome)
    sl = search_space.strategy_params["exit_stop_loss_pct"]
    assert isinstance(sl, FloatParam)
    assert sl.low == 0.0
    assert sl.high == 0.05
