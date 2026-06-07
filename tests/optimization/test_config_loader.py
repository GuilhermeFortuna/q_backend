from pathlib import Path

from q_backend.optimization.config_loader import load_optimization_config
from q_backend.optimization.models import ObjectiveMode

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "optimization"


def test_load_single_objective_fixture():
    config = load_optimization_config(FIXTURES_DIR / "single_objective.yaml")
    assert config.study.name == "test_single_objective"
    assert config.objective.mode == ObjectiveMode.MAXIMIZE_NET_PROFIT
    assert "short_period" in config.search_space.strategy_params


def test_load_multi_objective_fixture():
    config = load_optimization_config(FIXTURES_DIR / "multi_objective.yaml")
    assert config.is_multi_objective()
    assert config.study.direction is None
