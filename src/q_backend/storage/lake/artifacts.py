import logging
import shutil
from pathlib import Path
from typing import Literal

import pandas as pd

from q_backend.storage.settings import get_settings

logger = logging.getLogger(__name__)

ArtifactKind = Literal["trades", "equity"]


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
