import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import joblib
import pandas as pd

from q_backend.neural.encoder import EncoderConfig, NeuralEncoder
from q_backend.neural.factory import load_encoder_from_artifact_payload
from q_backend.storage.settings import get_settings

logger = logging.getLogger(__name__)

ArtifactKind = Literal["trades", "equity"]
WalkForwardArtifactKind = Literal["oos_equity", "oos_trades", "windows"]
StrategySearchCandidateArtifactKind = Literal["oos_equity", "oos_trades"]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def lake_root() -> Path:
    settings = get_settings()
    root = Path(settings.data_lake_root)
    if not root.is_absolute():
        root = _project_root() / root
    root.mkdir(parents=True, exist_ok=True)
    return root


def _run_dir(run_id: str) -> Path:
    return lake_root() / "backtests" / run_id


def _artifact_relative_path(run_id: str, kind: ArtifactKind) -> str:
    filename = "trades.parquet" if kind == "trades" else "equity.parquet"
    return f"backtests/{run_id}/{filename}"


def _artifact_absolute_path(run_id: str, kind: ArtifactKind) -> Path:
    return lake_root() / _artifact_relative_path(run_id, kind)


def write_backtest_artifacts(
    run_id: str,
    trades: pd.DataFrame,
    equity_curve: pd.DataFrame,
) -> dict[str, str]:
    run_dir = _run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    trades.to_parquet(run_dir / "trades.parquet", index=False)
    equity_curve.to_parquet(run_dir / "equity.parquet", index=False)

    return {
        "trades": _artifact_relative_path(run_id, "trades"),
        "equity": _artifact_relative_path(run_id, "equity"),
    }


def read_backtest_artifact(run_id: str, kind: ArtifactKind) -> pd.DataFrame:
    path = _artifact_absolute_path(run_id, kind)
    if not path.is_file():
        raise FileNotFoundError(
            f"Backtest artifact '{kind}' not found for run '{run_id}'."
        )
    return pd.read_parquet(path)


def write_backtest_result(run_id: str, payload: dict[str, Any]) -> str:
    """Persist the full backtest response (metrics, trades, bars, indicators).

    Backtests are now async jobs, so the chart payload can't be returned inline —
    it's written here for the results endpoint to serve once the job finishes.
    """
    run_dir = _run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    return f"backtests/{run_id}/result.json"


def read_backtest_result(run_id: str) -> dict[str, Any]:
    path = _run_dir(run_id) / "result.json"
    if not path.is_file():
        raise FileNotFoundError(f"Backtest result not found for run '{run_id}'.")
    return json.loads(path.read_text(encoding="utf-8"))


def _encoder_ablation_dir(job_id: str) -> Path:
    return lake_root() / "experiments" / "encoder_ablation" / job_id


def write_encoder_ablation_result(job_id: str, payload: dict[str, Any]) -> str:
    """Persist a completed encoder ablation comparison table keyed by job_id."""
    run_dir = _encoder_ablation_dir(job_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    return f"experiments/encoder_ablation/{job_id}/result.json"


def read_encoder_ablation_result(job_id: str) -> dict[str, Any]:
    path = _encoder_ablation_dir(job_id) / "result.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"Encoder ablation result not found for job '{job_id}'."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _discovery_ab_dir(job_id: str) -> Path:
    return lake_root() / "experiments" / "discovery_ab" / job_id


def write_discovery_ab_report(job_id: str, payload: dict[str, Any]) -> str:
    """Persist a completed Discovery A/B verdict keyed by job_id."""
    run_dir = _discovery_ab_dir(job_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    return f"experiments/discovery_ab/{job_id}/result.json"


def read_discovery_ab_report(job_id: str) -> dict[str, Any]:
    path = _discovery_ab_dir(job_id) / "result.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"Discovery A/B result not found for job '{job_id}'."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def delete_backtest_artifacts(run_id: str) -> None:
    run_dir = _run_dir(run_id)
    if run_dir.is_dir():
        shutil.rmtree(run_dir)
        logger.info("Deleted backtest lake artifacts for run %s", run_id)


def _walkforward_run_dir(run_id: str) -> Path:
    return lake_root() / "walkforward" / run_id


def _walkforward_artifact_relative_path(
    run_id: str, kind: WalkForwardArtifactKind
) -> str:
    filenames = {
        "oos_equity": "oos_equity.parquet",
        "oos_trades": "oos_trades.parquet",
        "windows": "windows.parquet",
    }
    return f"walkforward/{run_id}/{filenames[kind]}"


def _walkforward_artifact_absolute_path(
    run_id: str, kind: WalkForwardArtifactKind
) -> Path:
    return lake_root() / _walkforward_artifact_relative_path(run_id, kind)


def write_walkforward_artifacts(
    run_id: str,
    oos_equity: pd.DataFrame,
    oos_trades: pd.DataFrame,
    windows: pd.DataFrame,
) -> dict[str, str]:
    run_dir = _walkforward_run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    oos_equity.to_parquet(run_dir / "oos_equity.parquet", index=False)
    oos_trades.to_parquet(run_dir / "oos_trades.parquet", index=False)
    windows.to_parquet(run_dir / "windows.parquet", index=False)

    return {
        "oos_equity": _walkforward_artifact_relative_path(run_id, "oos_equity"),
        "oos_trades": _walkforward_artifact_relative_path(run_id, "oos_trades"),
        "windows": _walkforward_artifact_relative_path(run_id, "windows"),
    }


def read_walkforward_artifact(
    run_id: str, kind: WalkForwardArtifactKind
) -> pd.DataFrame:
    path = _walkforward_artifact_absolute_path(run_id, kind)
    if not path.is_file():
        raise FileNotFoundError(
            f"Walk-forward artifact '{kind}' not found for run '{run_id}'."
        )
    return pd.read_parquet(path)


def delete_walkforward_artifacts(run_id: str) -> None:
    run_dir = _walkforward_run_dir(run_id)
    if run_dir.is_dir():
        shutil.rmtree(run_dir)
        logger.info("Deleted walk-forward lake artifacts for run %s", run_id)


def _strategy_search_run_dir(run_id: str) -> Path:
    return lake_root() / "strategy_search" / run_id


def _strategy_search_leaderboard_relative_path(run_id: str) -> str:
    return f"strategy_search/{run_id}/leaderboard.parquet"


def _strategy_search_candidate_artifact_relative_path(
    run_id: str,
    candidate_id: str,
    kind: StrategySearchCandidateArtifactKind,
) -> str:
    filename = "oos_equity.parquet" if kind == "oos_equity" else "oos_trades.parquet"
    return f"strategy_search/{run_id}/candidates/{candidate_id}/{filename}"


def _strategy_search_candidate_artifact_absolute_path(
    run_id: str,
    candidate_id: str,
    kind: StrategySearchCandidateArtifactKind,
) -> Path:
    return lake_root() / _strategy_search_candidate_artifact_relative_path(
        run_id, candidate_id, kind
    )


def write_strategy_search_artifacts(
    run_id: str,
    leaderboard: pd.DataFrame,
    candidate_equity: dict[str, pd.DataFrame],
    candidate_trades: dict[str, pd.DataFrame] | None = None,
    *,
    generation_leaderboards: dict[int, pd.DataFrame] | None = None,
    candidate_genomes: dict[str, dict[str, Any]] | None = None,
    lockbox_equity: pd.DataFrame | None = None,
    lockbox_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_dir = _strategy_search_run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    leaderboard.to_parquet(run_dir / "leaderboard.parquet", index=False)

    lake_paths: dict[str, Any] = {
        "leaderboard": _strategy_search_leaderboard_relative_path(run_id),
        "candidates": {},
    }

    trades_by_candidate = candidate_trades or {}
    for candidate_id, equity_df in candidate_equity.items():
        candidate_dir = run_dir / "candidates" / candidate_id
        candidate_dir.mkdir(parents=True, exist_ok=True)
        equity_df.to_parquet(candidate_dir / "oos_equity.parquet", index=False)
        candidate_paths: dict[str, str] = {
            "oos_equity": _strategy_search_candidate_artifact_relative_path(
                run_id, candidate_id, "oos_equity"
            ),
        }
        trades_df = trades_by_candidate.get(candidate_id)
        if trades_df is not None and not trades_df.empty:
            trades_df.to_parquet(candidate_dir / "oos_trades.parquet", index=False)
            candidate_paths["oos_trades"] = (
                _strategy_search_candidate_artifact_relative_path(
                    run_id, candidate_id, "oos_trades"
                )
            )
        lake_paths["candidates"][candidate_id] = candidate_paths

    if generation_leaderboards:
        lake_paths["generations"] = {}
        for generation, generation_df in generation_leaderboards.items():
            generation_dir = run_dir / "generations" / str(generation)
            generation_dir.mkdir(parents=True, exist_ok=True)
            rel = f"strategy_search/{run_id}/generations/{generation}/leaderboard.parquet"
            generation_df.to_parquet(generation_dir / "leaderboard.parquet", index=False)
            lake_paths["generations"][str(generation)] = rel

    if candidate_genomes:
        lake_paths["genomes"] = {}
        for candidate_id, genome in candidate_genomes.items():
            candidate_dir = run_dir / "candidates" / candidate_id
            candidate_dir.mkdir(parents=True, exist_ok=True)
            genome_path = candidate_dir / "genome.json"
            genome_path.write_text(json.dumps(genome), encoding="utf-8")
            rel = f"strategy_search/{run_id}/candidates/{candidate_id}/genome.json"
            lake_paths["genomes"][candidate_id] = rel

    if lockbox_equity is not None or lockbox_metrics is not None:
        lockbox_dir = run_dir / "lockbox"
        lockbox_dir.mkdir(parents=True, exist_ok=True)
        lockbox_paths: dict[str, str] = {}
        if lockbox_equity is not None:
            lockbox_equity.to_parquet(lockbox_dir / "equity.parquet", index=False)
            lockbox_paths["equity"] = f"strategy_search/{run_id}/lockbox/equity.parquet"
        if lockbox_metrics is not None:
            (lockbox_dir / "metrics.json").write_text(
                json.dumps(lockbox_metrics), encoding="utf-8"
            )
            lockbox_paths["metrics"] = f"strategy_search/{run_id}/lockbox/metrics.json"
        lake_paths["lockbox"] = lockbox_paths

    return lake_paths


def read_strategy_search_candidate_genome(run_id: str, candidate_id: str) -> dict[str, Any]:
    path = lake_root() / "strategy_search" / run_id / "candidates" / candidate_id / "genome.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"Strategy search genome not found for run '{run_id}' candidate '{candidate_id}'."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def read_strategy_search_artifact(run_id: str) -> pd.DataFrame:
    path = lake_root() / _strategy_search_leaderboard_relative_path(run_id)
    if not path.is_file():
        raise FileNotFoundError(
            f"Strategy search leaderboard not found for run '{run_id}'."
        )
    return pd.read_parquet(path)


def read_strategy_search_candidate_artifact(
    run_id: str,
    candidate_id: str,
    kind: StrategySearchCandidateArtifactKind,
) -> pd.DataFrame:
    path = _strategy_search_candidate_artifact_absolute_path(run_id, candidate_id, kind)
    if not path.is_file():
        raise FileNotFoundError(
            f"Strategy search candidate artifact '{kind}' not found for "
            f"run '{run_id}' candidate '{candidate_id}'."
        )
    return pd.read_parquet(path)


def delete_strategy_search_artifacts(run_id: str) -> None:
    run_dir = _strategy_search_run_dir(run_id)
    if run_dir.is_dir():
        shutil.rmtree(run_dir)
        logger.info("Deleted strategy search lake artifacts for run %s", run_id)


@dataclass(frozen=True)
class StoredFeatureMatrix:
    frame: pd.DataFrame
    manifest: dict[str, Any]


def _feature_matrix_dir(matrix_id: str) -> Path:
    return lake_root() / "features" / matrix_id


def feature_matrix_exists(matrix_id: str) -> bool:
    matrix_dir = _feature_matrix_dir(matrix_id)
    return (matrix_dir / "matrix.parquet").is_file() and (
        matrix_dir / "manifest.json"
    ).is_file()


def _frame_to_parquet(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["time", *sorted(frame.columns)])
    out = frame.copy()
    out.insert(0, "time", out.index)
    return out.reset_index(drop=True)


def _frame_from_parquet(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "time" not in df.columns:
        return pd.DataFrame()
    feature_cols = [col for col in df.columns if col != "time"]
    frame = df.set_index("time")
    frame.index = pd.to_datetime(frame.index)
    return frame[feature_cols].sort_index()


def write_feature_matrix(
    matrix_id: str,
    frame: pd.DataFrame,
    manifest: dict[str, Any],
) -> dict[str, str]:
    matrix_dir = _feature_matrix_dir(matrix_id)
    matrix_dir.mkdir(parents=True, exist_ok=True)

    ordered = frame.sort_index()
    if not ordered.empty:
        ordered = ordered[sorted(ordered.columns)]
    _frame_to_parquet(ordered).to_parquet(matrix_dir / "matrix.parquet", index=False)
    (matrix_dir / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )

    return {
        "matrix": f"features/{matrix_id}/matrix.parquet",
        "manifest": f"features/{matrix_id}/manifest.json",
    }


def read_feature_matrix(matrix_id: str) -> StoredFeatureMatrix:
    matrix_dir = _feature_matrix_dir(matrix_id)
    matrix_path = matrix_dir / "matrix.parquet"
    manifest_path = matrix_dir / "manifest.json"
    if not matrix_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError(
            f"Feature matrix artifacts not found for matrix '{matrix_id}'."
        )

    frame = _frame_from_parquet(pd.read_parquet(matrix_path))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return StoredFeatureMatrix(frame=frame, manifest=manifest)


def _neural_model_dir(model_hash: str) -> Path:
    return lake_root() / "neural_models" / model_hash


def _neural_model_artifact_relative_path(model_hash: str) -> str:
    return f"neural_models/{model_hash}/encoder.joblib"


def neural_model_exists(model_hash: str) -> bool:
    model_dir = _neural_model_dir(model_hash)
    return (model_dir / "encoder.joblib").is_file() and (
        model_dir / "manifest.json"
    ).is_file()


def write_neural_model(
    model_hash: str,
    encoder: NeuralEncoder,
    *,
    model_key: str | None = None,
) -> dict[str, str]:
    model_dir = _neural_model_dir(model_hash)
    if model_dir.exists() and (model_dir / "encoder.joblib").is_file():
        raise FileExistsError(
            f"Neural model artifact already exists for hash '{model_hash}'."
        )
    model_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "kind": encoder.config.kind,
        "config": encoder.config,
        "state": encoder.dump_artifact_state(),
        "model_id": encoder.model_id,
        "latent_names": encoder.latent_names,
        "val_metrics": encoder.val_metrics,
    }
    joblib.dump(payload, model_dir / "encoder.joblib")
    manifest = {
        "model_hash": model_hash,
        "model_id": encoder.model_id,
        "model_key": model_key,
        "kind": encoder.config.kind,
        "symbol": encoder.config.symbol,
        "timeframe": encoder.config.timeframe.upper(),
        "train_start": encoder.config.train_start.isoformat(),
        "train_end": encoder.config.train_end.isoformat(),
        "n_latents": encoder.config.n_latents,
        "input_features": list(encoder.config.input_features),
        "hyperparams": dict(encoder.config.hyperparams),
        "val_metrics": encoder.val_metrics,
        "latent_names": encoder.latent_names,
    }
    (model_dir / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )

    return {
        "encoder": _neural_model_artifact_relative_path(model_hash),
        "manifest": f"neural_models/{model_hash}/manifest.json",
    }


def read_neural_model(model_hash: str) -> NeuralEncoder:
    model_dir = _neural_model_dir(model_hash)
    artifact_path = model_dir / "encoder.joblib"
    if not artifact_path.is_file():
        raise FileNotFoundError(
            f"Neural model artifact not found for hash '{model_hash}'."
        )

    payload = joblib.load(artifact_path)
    if isinstance(payload, dict) and "state" in payload:
        encoder = load_encoder_from_artifact_payload(payload)
    else:
        encoder = _load_legacy_pca_artifact(payload)

    if encoder.model_id != model_hash:
        raise ValueError(
            f"Artifact model_id '{encoder.model_id}' does not match requested hash "
            f"'{model_hash}'."
        )
    return encoder


def _load_legacy_pca_artifact(payload: dict[str, Any]) -> NeuralEncoder:
    """Load WO142 artifacts written before kind-dispatched state envelopes."""
    from q_backend.neural.pca_encoder import PCAEncoder

    config = payload["config"]
    if not isinstance(config, EncoderConfig):
        raise TypeError("Neural model artifact has invalid EncoderConfig payload")
    state = {
        "scaler": payload.get("scaler"),
        "pca": payload.get("pca"),
        "feature_columns": list(payload.get("feature_columns", [])),
        "val_metrics": dict(payload.get("val_metrics", {})),
    }
    return PCAEncoder.load_from_artifact_state(config, state)


def list_neural_model_hashes() -> list[str]:
    root = lake_root() / "neural_models"
    if not root.is_dir():
        return []
    hashes: list[str] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and neural_model_exists(entry.name):
            hashes.append(entry.name)
    return hashes


def read_neural_model_manifest(model_hash: str) -> dict[str, Any]:
    manifest_path = _neural_model_dir(model_hash) / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"Neural model manifest not found for hash '{model_hash}'."
        )
    return json.loads(manifest_path.read_text(encoding="utf-8"))
