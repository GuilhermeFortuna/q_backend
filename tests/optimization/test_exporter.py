import json
from pathlib import Path

import optuna

from q_backend.optimization.exporter import export_results
from q_backend.optimization.models import OptimizationConfig
from q_backend.optimization.runner import OptimizationResult


def test_export_results_writes_artifacts(tmp_path):
    study = optuna.create_study(direction="maximize")
    study.optimize(lambda trial: trial.suggest_float("x", 0, 1), n_trials=2)

    config = OptimizationConfig.model_validate(
        {
            "study": {"name": "export_test", "storage": {"type": "memory"}},
            "objective": {"mode": "maximize_net_profit"},
            "backtest": {
                "symbol": "X",
                "start": "2024-01-01T00:00:00",
                "end": "2024-06-01T00:00:00",
            },
            "search_space": {},
        }
    )
    result = OptimizationResult(
        study=study,
        best_params=study.best_params,
        best_trial=study.best_trial,
    )
    paths = export_results(result, config, tmp_path)

    assert paths.summary.exists()
    assert paths.trials_csv.exists()
    assert paths.best_params.exists()
    summary = json.loads(paths.summary.read_text(encoding="utf-8"))
    assert summary["study_name"] == "export_test"
