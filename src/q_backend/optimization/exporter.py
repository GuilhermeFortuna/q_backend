import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import optuna

from q_backend.optimization.models import OptimizationConfig
from q_backend.optimization.runner import OptimizationResult


@dataclass
class ExportPaths:
    best_params: Path
    best_trial: Path
    trials_csv: Path
    summary: Path
    pareto_front: Path


def _serialize_trial(trial: optuna.trial.FrozenTrial) -> dict[str, Any]:
    return {
        "number": trial.number,
        "params": trial.params,
        "values": trial.values,
        "user_attrs": dict(trial.user_attrs),
        "state": trial.state.name,
    }


def export_results(
    result: OptimizationResult,
    config: OptimizationConfig,
    output_dir: Path,
) -> ExportPaths:
    output_dir.mkdir(parents=True, exist_ok=True)
    study = result.study

    best_params_path = output_dir / "best_params.json"
    best_trial_path = output_dir / "best_trial.json"
    trials_csv_path = output_dir / "trials.csv"
    summary_path = output_dir / "summary.json"
    pareto_path = output_dir / "pareto_front.json"

    best_params = result.best_params
    with best_params_path.open("w", encoding="utf-8") as handle:
        json.dump(best_params, handle, indent=2)

    best_trial_payload = (
        _serialize_trial(result.best_trial) if result.best_trial is not None else None
    )
    with best_trial_path.open("w", encoding="utf-8") as handle:
        json.dump(best_trial_payload, handle, indent=2, default=str)

    study.trials_dataframe().to_csv(trials_csv_path, index=False)

    pareto_trials = [_serialize_trial(trial) for trial in result.pareto_trials]
    with pareto_path.open("w", encoding="utf-8") as handle:
        json.dump(pareto_trials, handle, indent=2, default=str)

    summary = {
        "study_name": config.study.name,
        "n_trials": len(study.trials),
        "objective_mode": config.objective.mode.value,
        "best_values": (
            result.best_trial.values if result.best_trial is not None else None
        ),
        "failure_count": len(result.failures),
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, default=str)

    return ExportPaths(
        best_params=best_params_path,
        best_trial=best_trial_path,
        trials_csv=trials_csv_path,
        summary=summary_path,
        pareto_front=pareto_path,
    )
