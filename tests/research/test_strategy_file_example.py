"""The documented strategy-file example validates, and its helper import resolves."""

from __future__ import annotations

import shutil
from pathlib import Path

from q_backend.research.strategy_files import list_strategy_files, load_strategy

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "research" / "strategy_files"


def test_the_documented_example_validates_and_loads(tmp_path: Path) -> None:
    root = tmp_path / "q-strategies"
    shutil.copytree(EXAMPLE_DIR, root, ignore=shutil.ignore_patterns("__pycache__"))
    bundles = tmp_path / "bundles"

    (info,) = list_strategy_files(root, bundle_root=bundles)

    assert info.valid is True, info.error
    assert info.id == "session_breakout"
    assert info.label == "Session breakout"
    assert info.class_name == "SessionBreakout"
    assert info.description == ("Buy when a bar closes above the session's opening range, at most once per session.")
    assert {param.name: param.kind for param in info.parameters} == {
        "range_bars": "int",
        "enter_after": "time",
    }
    cls = load_strategy("session_breakout", root=root, bundle_root=bundles)
    assert cls.__name__ == "SessionBreakout"


def test_the_documented_example_runs_with_the_causality_check(tmp_path: Path) -> None:
    import numpy as np
    import pandas as pd

    from q_backend.market_data.timezone import BRASILIA_TZ
    from q_backend.research import backtest

    cls = load_strategy("session_breakout", root=EXAMPLE_DIR, bundle_root=tmp_path / "bundles")
    days = pd.bdate_range("2026-09-01", periods=5, tz=BRASILIA_TZ)
    frames = []
    rng = np.random.default_rng(7)
    for day in days:
        index = pd.date_range(day + pd.Timedelta(hours=9), day + pd.Timedelta(hours=17), freq="5min", tz=BRASILIA_TZ)
        close = 100_000 + np.cumsum(rng.normal(0, 25, len(index)))
        frames.append(
            pd.DataFrame(
                {
                    "open": close,
                    "high": close + rng.uniform(5, 40, len(index)),
                    "low": close - rng.uniform(5, 40, len(index)),
                    "close": close,
                    "tick_volume": np.full(len(index), 100, dtype="int64"),
                },
                index=index.rename("time"),
            )
        )
    frame = pd.concat(frames)

    result = backtest(frame, strategy=cls(), symbol="WIN$N", day_trade=True, check_causality=True, progress=False)

    assert "total_trades" in result.metrics
