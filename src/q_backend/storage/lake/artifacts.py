import json
import logging
import shutil
from pathlib import Path
from typing import Any, Literal

import pandas as pd

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

    return lake_paths


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
