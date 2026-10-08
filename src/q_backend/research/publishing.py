"""Publish finished backtest results to the Research stack."""

from __future__ import annotations

import inspect
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pandas as pd

from q_backend.api.schemas.backtest import (
    BacktestImportBar,
    BacktestImportRequest,
    BacktestImportResult,
    BacktestProvenance,
    BacktestRequest,
    ChartIndicatorSeries,
)
from q_backend.backtesting.chart_data import serialize_chart_data
from q_backend.backtesting.position_sizing import FixedQuantityPositionSizing
from q_backend.backtesting.strategy import ChartIndicatorSpec

if TYPE_CHECKING:
    from q_backend.research.charting import ChartIndicator
    from q_backend.research.results import BacktestResult

Q_API_URL: str = os.getenv("Q_API_URL", "http://127.0.0.1:8000")


class _IndicatorAdapter:
    """Lightweight adapter exposing get_chart_indicators() for serialize_chart_data."""

    def __init__(self, indicators: tuple[ChartIndicator, ...]) -> None:
        self._indicators = indicators

    def get_chart_indicators(self) -> list[ChartIndicatorSpec]:
        return [
            ChartIndicatorSpec(
                key=ind.column,
                label=ind.label,
                pane=ind.pane,
                color=ind.color,
            )
            for ind in self._indicators
        ]


def _clean_for_json(val: Any) -> Any:
    """Recursively clean floats so NaN and infinite values become None."""
    if isinstance(val, float):
        if not math.isfinite(val):
            return None
        return val
    if isinstance(val, dict):
        return {k: _clean_for_json(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_clean_for_json(v) for v in val]
    return val


def _find_git_repo(path: Path) -> tuple[Path | None, str | None, bool | None]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None, None, None
        git_root = Path(proc.stdout.strip()).resolve()

        rev_proc = subprocess.run(
            ["git", "-C", str(git_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        rev = rev_proc.stdout.strip() if rev_proc.returncode == 0 and rev_proc.stdout.strip() else None

        status_proc = subprocess.run(
            ["git", "-C", str(git_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        dirty = bool(status_proc.stdout.strip()) if status_proc.returncode == 0 else None
        return git_root, rev, dirty
    except Exception:  # noqa: BLE001
        return None, None, None


def _get_script_path() -> Path | None:
    try:
        main_mod = sys.modules.get("__main__")
        main_file = getattr(main_mod, "__file__", None)
        if main_file:
            p = Path(main_file)
            if p.name != "pytest" and not p.name.startswith("pytest-") and p.is_file():
                return p.resolve()
        if sys.argv and sys.argv[0]:
            p = Path(sys.argv[0])
            if p.name != "pytest" and not p.name.startswith("pytest-") and p.is_file():
                return p.resolve()
    except Exception:  # noqa: BLE001, S110 - script path resolution is best-effort and degrades to None
        pass
    return None


def _collect_provenance(strategy: Any) -> BacktestProvenance:
    script_str: str | None = None
    git_rev: str | None = None
    git_dirty: bool | None = None

    script_path = _get_script_path()
    if script_path is not None:
        git_root, rev, dirty = _find_git_repo(script_path.parent)
        if git_root is not None:
            try:
                script_str = str(script_path.relative_to(git_root))
            except ValueError:
                script_str = str(script_path)
            git_rev = rev
            git_dirty = dirty
        else:
            script_str = str(script_path)

    strat_class_name: str | None = None
    strat_source: str | None = None
    try:
        strat_cls = None
        if isinstance(strategy, str):
            try:
                from q_backend.backtesting.strategy_registry import get_registered_strategy

                reg = get_registered_strategy(strategy)
                strat_cls = reg.cls
            except Exception:  # noqa: BLE001 - optional registered strategy lookup
                strat_cls = None
        elif strategy is not None:
            strat_cls = getattr(strategy, "__class__", None)

        if strat_cls is not None:
            mod = getattr(strat_cls, "__module__", "")
            qual = getattr(strat_cls, "__qualname__", strat_cls.__name__)
            strat_class_name = f"{mod}.{qual}" if mod else qual
            try:
                strat_source = inspect.getsource(strat_cls)
            except Exception:  # noqa: BLE001 - source lookup degrades to None
                strat_source = None
    except Exception:  # noqa: BLE001, S110 - strategy class resolution is best-effort and degrades to None
        pass

    return BacktestProvenance(
        script=script_str,
        strategy_class=strat_class_name,
        strategy_source=strat_source,
        git_revision=git_rev,
        git_dirty=git_dirty,
    )


def publish_backtest_result(
    result: BacktestResult,
    *,
    name: str | None = None,
    timeframe: str | None = None,
    api_url: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Build and send a backtest import request to the Research stack API."""
    if result.data.empty:
        raise ValueError("Cannot publish backtest result with no bars.")

    effective_timeframe = timeframe or result.config.get("timeframe")
    if not effective_timeframe:
        raise ValueError("No timeframe metadata found in data; pass timeframe= to publish().")

    effective_name = name or result.config.get("strategy")
    if not effective_name:
        effective_name = "CustomStrategy"

    base_url = api_url or os.getenv("Q_API_URL") or Q_API_URL
    target_url = f"{base_url.rstrip('/')}/api/v1/backtests/import"

    # 1. Chart series & bars via serialize_chart_data
    adapter = _IndicatorAdapter(result.indicators)
    chart_data = serialize_chart_data(result.data, adapter)

    # Sanitize warm-up NaNs / Infs in indicators
    clean_indicators = []
    for ind in chart_data["indicators"]:
        clean_values = [None if (v is None or pd.isna(v) or not math.isfinite(v)) else float(v) for v in ind["values"]]
        clean_indicators.append(
            ChartIndicatorSeries(
                key=ind["key"],
                label=ind["label"],
                pane=ind["pane"],
                color=ind["color"],
                values=clean_values,
            )
        )

    # 2. Closed trades only via Trade model_dump
    closed_trades_dump = [t.model_dump(mode="json") for t in result._closed_trades]

    # 3. Clean metrics
    clean_metrics = _clean_for_json(result.metrics)

    # 4. Config
    first_ts = result.data.index[0]
    last_ts = result.data.index[-1]
    start_dt = (
        first_ts.to_pydatetime() if hasattr(first_ts, "to_pydatetime") else pd.Timestamp(first_ts).to_pydatetime()
    )
    end_dt = last_ts.to_pydatetime() if hasattr(last_ts, "to_pydatetime") else pd.Timestamp(last_ts).to_pydatetime()

    quantity = result.config.get("quantity", 1)
    position_sizing = FixedQuantityPositionSizing(quantity=float(quantity))

    config_model = BacktestRequest(
        symbol=str(result.config["symbol"]),
        timeframe=str(effective_timeframe),
        start=start_dt,
        end=end_dt,
        initial_capital=float(result.config.get("initial_capital", 100000.0)),
        point_value=float(result.config.get("point_value", 1.0)),
        strategy=str(effective_name),
        strategy_params=dict(result.config.get("strategy_params") or {}),
        exit_params=dict(result.config.get("exit_params") or {}),
        position_sizing=position_sizing,
        costs=result.config.get("costs"),
        engine="candle",
        day_trade=bool(result.config.get("day_trade", False)),
        day_trade_start_time=str(result.config.get("day_trade_start_time", "09:00")),
        day_trade_end_time=str(result.config.get("day_trade_end_time", "16:00")),
        day_trade_close_time=str(result.config.get("day_trade_close_time", "17:00")),
    )

    import_result = BacktestImportResult(
        metrics=clean_metrics,
        trades=closed_trades_dump,
        bars=[BacktestImportBar.model_validate(b) for b in chart_data["bars"]],
        indicators=clean_indicators,
    )

    provenance = _collect_provenance(getattr(result, "_strategy", None))

    req_model = BacktestImportRequest(
        config=config_model,
        result=import_result,
        provenance=provenance,
    )

    payload = _clean_for_json(req_model.model_dump(mode="json"))

    try:
        http_client = client if client is not None else httpx.Client()
        response = http_client.post(target_url, json=payload)
    except httpx.RequestError as exc:
        raise ConnectionError(
            f"Cannot connect to Research API at {base_url}. Start the stack with './dev research'."
        ) from exc

    if response.status_code == 422:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:  # noqa: BLE001
            detail = response.text
        raise ValueError(f"Server rejected backtest import (422): {detail}")

    if response.is_error:
        raise RuntimeError(f"Backtest import failed with status {response.status_code}: {response.text}")

    try:
        res_data = response.json()
        return str(res_data["run_id"])
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Unexpected response format from import endpoint: {response.text}") from exc
