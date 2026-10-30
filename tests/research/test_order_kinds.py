"""Order kinds classification and persistence across backtest runs."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import uuid

import numpy as np
import pandas as pd
import pytest

from q_backend.api import backtest_jobs
from q_backend.api.backtest_jobs import BacktestJobRequest
from q_backend.api.routers import backtest as backtest_router
from q_backend.backtesting import run_service
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import (
    BacktestResult,
    IntParam,
    ResearchStrategy,
    TradeOrder,
    backtest,
)
from q_backend.research.adapter import classify_order
from q_backend.research.data import MT5_SOURCE
from q_backend.research.frame import attach_metadata
from q_backend.storage.db.models import BacktestRun
from q_backend.storage.settings import get_settings


def _sample_frame() -> pd.DataFrame:
    times = pd.date_range("2024-01-02 09:00", periods=50, freq="15min", tz=BRASILIA_TZ)
    np.random.seed(42)
    close = 100.0 + np.cumsum(np.random.randn(len(times)) * 0.5)
    high = close + np.random.uniform(0.1, 0.5, len(times))
    low = close - np.random.uniform(0.1, 0.5, len(times))
    open_ = (high + low) / 2.0
    frame = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000.0,
        },
        index=times,
    )
    return attach_metadata(
        frame,
        symbol="WIN$N",
        timeframe="M15",
        source=MT5_SOURCE,
        requested_start=times[0],
        requested_end=times[-1],
    )


class MarketOnlyStrategy(ResearchStrategy):
    parameters = {}

    def compute_indicators(self, frame):
        return frame

    def entry_strategy(self, frame, positions=()):
        if not positions and len(frame) == 10:
            return TradeOrder.buy()
        return None

    def exit_strategy(self, frame, positions=()):
        if positions and len(frame) == 20:
            return TradeOrder.close()
        return None


class ProtectiveLevelsStrategy(ResearchStrategy):
    parameters = {}

    def compute_indicators(self, frame):
        return frame

    def entry_strategy(self, frame, positions=()):
        if not positions and len(frame) == 11:
            price = frame["close"].iloc[-1]
            return TradeOrder.buy(stop_loss=price - 5.0, take_profit=price + 10.0)
        return None

    def exit_strategy(self, frame, positions=()):
        if positions and len(frame) == 21:
            return TradeOrder.close()
        return None


def test_classify_order_kinds():
    assert classify_order(TradeOrder.buy()) == {"market_entry"}
    assert classify_order(TradeOrder.sell()) == {"market_entry"}
    assert classify_order(TradeOrder.close()) == {"market_close"}

    assert classify_order(TradeOrder.buy(stop_loss=90.0)) == {"market_entry", "protective_levels"}
    assert classify_order(TradeOrder.sell(take_profit=80.0)) == {"market_entry", "protective_levels"}

    assert classify_order(TradeOrder.buy_stop(price=105.0)) == {"resting_stop"}
    assert classify_order(TradeOrder.buy_stop(price=105.0, stop_loss=95.0)) == {"resting_stop", "protective_levels"}

    assert classify_order(TradeOrder.buy_limit(price=95.0)) == {"resting_limit"}
    assert classify_order(TradeOrder.buy_limit(price=95.0, take_profit=110.0)) == {"resting_limit", "protective_levels"}

    assert classify_order(TradeOrder.amend(stop_loss=98.0)) == {"amend"}
    assert classify_order(TradeOrder.cancel()) == {"cancel"}


def test_backtest_records_order_kinds_market_only():
    frame = _sample_frame()
    result = backtest(
        frame,
        strategy=MarketOnlyStrategy(),
        symbol="WIN$N",
        quantity=1,
        progress=False,
    )
    assert result.order_kinds == frozenset({"market_entry", "market_close"})


def test_backtest_records_order_kinds_protective_levels():
    frame = _sample_frame()
    # Note: TradeOrder with stop_loss without ticks will raise tick error in backtest unless ticks is provided or mocked.
    # Here we check that adapter records the order kind when the hook returns it.
    from q_backend.research.adapter import ResearchStrategyAdapter

    adapter = ResearchStrategyAdapter(ProtectiveLevelsStrategy(), symbol="WIN$N")
    adapter.compute_indicators(frame)
    hooks = adapter.runtime_callback(frame)
    # Trigger entry hook (raises because ticks are required for protective levels in backtest)
    with pytest.raises(ValueError, match="carries stop or take-profit levels"):
        hooks.entry(10, ())
    hooks.strategy(20, (("pos1", 1, 10, 100.0, 1, 95.0, 110.0),))
    assert adapter.order_kinds == {"market_entry", "protective_levels", "market_close"}


@pytest.fixture
def strategy_env(run_jobs_sync, tmp_path, monkeypatch):
    root = tmp_path / "q-strategies"
    root.mkdir()
    monkeypatch.setenv("Q_STRATEGY_DIR", str(root))
    monkeypatch.setenv("Q_STRATEGY_BUNDLE_ROOT", str(tmp_path / "bundles"))
    monkeypatch.setenv("Q_RESEARCH_TICK_STORE", str(tmp_path / "ticks"))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


SIMPLE_MA = '''"""Simple MA strategy."""
from __future__ import annotations
import pandas as pd
from q_backend.research import ResearchStrategy, TradeOrder, IntParam

class SimpleMa(ResearchStrategy):
    parameters = {"length": IntParam(min=2, max=50, label="Length")}

    def __init__(self, length: int = 5):
        self.length = length

    def compute_indicators(self, frame):
        out = frame.copy()
        out["ma"] = frame["close"].rolling(self.length).mean()
        return out

    def entry_strategy(self, frame, positions=()):
        if pd.isna(frame["ma"].iloc[-1]) or positions:
            return None
        if frame["close"].iloc[-1] > frame["ma"].iloc[-1]:
            return TradeOrder.buy()
        return None

    def exit_strategy(self, frame, positions=()):
        if positions and frame["close"].iloc[-1] < frame["ma"].iloc[-1]:
            return TradeOrder.close()
        return None
'''

TICK_HOOK_STRATEGY = '''"""Strategy with tick hook."""
from __future__ import annotations
import pandas as pd
from q_backend.research import ResearchStrategy, TradeOrder, IntParam

class TickStrategy(ResearchStrategy):
    parameters = {"length": IntParam(min=2, max=50, label="Length")}

    def __init__(self, length: int = 5):
        self.length = length

    def compute_indicators(self, frame, ticks=None):
        out = frame.copy()
        out["ma"] = frame["close"].rolling(self.length).mean()
        return out

    def entry_strategy(self, frame, positions=()):
        if pd.isna(frame["ma"].iloc[-1]) or positions:
            return None
        if frame["close"].iloc[-1] > frame["ma"].iloc[-1]:
            return TradeOrder.buy()
        return None

    def exit_strategy(self, frame, positions=()):
        if positions and frame["close"].iloc[-1] < frame["ma"].iloc[-1]:
            return TradeOrder.close()
        return None
'''


def test_strategy_file_run_records_order_kinds_in_summary_and_detail(run_jobs_sync, strategy_env):
    (strategy_env / "simple_ma.py").write_text(SIMPLE_MA, encoding="utf-8")
    req = BacktestJobRequest(
        symbol="WIN$N",
        timeframe="D1",
        start=dt.datetime(2024, 1, 2),
        end=dt.datetime(2024, 4, 30),
        strategy_file="simple_ma",
        strategy_params={"length": 5},
    )
    run_id = backtest_router.start_backtest(req)["run_id"]
    with run_jobs_sync.harness_session_factory() as session:
        row = session.get(BacktestRun, uuid.UUID(run_id))
        assert row is not None
        assert row.status == "completed"
        assert row.result_summary is not None
        assert "order_kinds" in row.result_summary
        assert row.result_summary["order_kinds"] == ["market_close", "market_entry"]

        detail = run_service.backtest_run_detail(row, session)
        assert detail.order_kinds == ["market_close", "market_entry"]


def test_strategy_file_with_tick_hooks_adds_tick_hooks_kind(run_jobs_sync, strategy_env):
    (strategy_env / "tick_strat.py").write_text(TICK_HOOK_STRATEGY, encoding="utf-8")
    req = BacktestJobRequest(
        symbol="WIN$N",
        timeframe="D1",
        start=dt.datetime(2024, 1, 2),
        end=dt.datetime(2024, 4, 30),
        strategy_file="tick_strat",
        strategy_params={"length": 5},
    )
    run_id = backtest_router.start_backtest(req)["run_id"]
    with run_jobs_sync.harness_session_factory() as session:
        row = session.get(BacktestRun, uuid.UUID(run_id))
        assert row is not None
        assert row.status == "completed"
        assert row.result_summary is not None
        assert "tick_hooks" in row.result_summary["order_kinds"]
        assert row.result_summary["order_kinds"] == ["market_close", "market_entry", "tick_hooks"]
