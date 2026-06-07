from pathlib import Path

import yaml

from q_backend.optimization.models import OptimizationConfig


def load_optimization_config(path: str | Path) -> OptimizationConfig:
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return OptimizationConfig.model_validate(raw)
