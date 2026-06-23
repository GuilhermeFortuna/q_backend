from q_backend.backtesting.genome.param_bounds import GENOME_PARAM_BOUNDS


def test_threshold_within_curated_genome_range():
    spec = GENOME_PARAM_BOUNDS["threshold"]
    assert spec.min == -3.0
    assert spec.max == 3.0
    assert spec.step == 0.5


def test_period_within_curated_genome_range():
    spec = GENOME_PARAM_BOUNDS["period"]
    assert spec.min == 10
    assert spec.max == 100
    assert spec.step == 10
