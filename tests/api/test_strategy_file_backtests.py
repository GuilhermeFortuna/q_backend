"""Strategy-file backtests run from the Backtests workspace: versions, reruns and causality."""

from __future__ import annotations

import datetime as dt
import math
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from q_backend.api import backtest_jobs
from q_backend.api.backtest_jobs import BacktestJobRequest
from q_backend.api.routers import backtest as backtest_router
from q_backend.backtesting import run_service
from q_backend.backtesting.models import OrderAction
from q_backend.research import backtest
from q_backend.research.strategy_file_runs import _bars_frame
from q_backend.storage.db.models import BacktestRun, ResearchStrategyVersion
from q_backend.storage.settings import get_settings
from q_backend.tasks import worker_context

SYMBOL = "WIN$N"
START = dt.datetime(2024, 1, 2)
END = dt.datetime(2024, 4, 30)

SIMPLE_MA = '''"""Buy closes above a moving average; close when they fall below it."""

from __future__ import annotations

import datetime as dt

import pandas as pd

from q_backend.research import BoolParam, IntParam, ResearchStrategy, TimeParam, TradeOrder


class SimpleMa(ResearchStrategy):
    """Buys closes above a moving average and closes when they fall below it."""

    label = "Simple MA"
    parameters = {
        "length": IntParam(min=2, max=50, label="Length"),
        "use_filter": BoolParam(label="Use filter"),
        "entry_after": TimeParam(label="Entry after"),
    }

    def __init__(self, length: int = 5, use_filter: bool = False, entry_after: dt.time = dt.time(9, 0)):
        self.length = length
        self.use_filter = use_filter
        self.entry_after = entry_after

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

EDITED_MA = SIMPLE_MA.replace(
    'if frame["close"].iloc[-1] > frame["ma"].iloc[-1]:', 'if frame["close"].iloc[-1] < frame["ma"].iloc[-1]:'
)

LOOKAHEAD = '''from q_backend.research import ResearchStrategy, TradeOrder


class Lookahead(ResearchStrategy):
    """Reads the next bar's close, which no live strategy can see."""

    parameters = {}

    def __init__(self):
        pass

    def compute_indicators(self, frame):
        out = frame.copy()
        out["peek"] = frame["close"].shift(-1)
        return out

    def entry_strategy(self, frame, positions=()):
        if not positions and frame["peek"].iloc[-1] > frame["close"].iloc[-1]:
            return TradeOrder.buy()
        return None
'''


def _write(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")


@pytest.fixture
def strategy_env(run_jobs_sync, tmp_path, monkeypatch):
    """A strategy directory, a bundle root and an empty tick store, all under tmp_path."""
    root = tmp_path / "q-strategies"
    root.mkdir()
    monkeypatch.setenv("Q_STRATEGY_DIR", str(root))
    monkeypatch.setenv("Q_STRATEGY_BUNDLE_ROOT", str(tmp_path / "bundles"))
    monkeypatch.setenv("Q_RESEARCH_TICK_STORE", str(tmp_path / "ticks"))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


def _request(**overrides) -> BacktestJobRequest:
    body = {
        "symbol": SYMBOL,
        "timeframe": "D1",
        "start": START,
        "end": END,
        "strategy_file": "simple_ma",
        "strategy_params": {"length": 5},
    }
    body.update(overrides)
    return BacktestJobRequest(**body)


def _start(**overrides) -> str:
    return backtest_router.start_backtest(_request(**overrides))["run_id"]


def _row(run_jobs_sync, run_id: str) -> BacktestRun:
    with run_jobs_sync.harness_session_factory() as session:
        return session.get(BacktestRun, uuid.UUID(run_id))


def _version_count(run_jobs_sync) -> int:
    with run_jobs_sync.harness_session_factory() as session:
        return session.scalar(select(func.count()).select_from(ResearchStrategyVersion))


def _clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _direct_metrics(run_jobs_sync, strategy_class, **params):
    """The metrics of a direct research ``backtest()`` on the same bars and parameters."""
    frame = _bars_frame(
        worker_context.get_worker_market_data_service(), symbol=SYMBOL, timeframe="D1", start=START, end=END
    )
    return backtest(
        frame,
        strategy=strategy_class(**params),
        symbol=SYMBOL,
        quantity=1,
        point_value=1.0,
        initial_capital=100000.0,
        check_causality=True,
        progress=False,
    )


def test_a_strategy_file_run_matches_a_direct_backtest_and_records_its_version(run_jobs_sync, strategy_env):
    _write(strategy_env, "simple_ma.py", SIMPLE_MA)

    run_id = _start()

    row = _row(run_jobs_sync, run_id)
    assert row.status == "completed"
    assert row.strategy_version_id is not None
    assert "strategy" not in row.config
    assert row.config["strategy_version_id"] == str(row.strategy_version_id)
    assert row.config["strategy_params"] == {"length": 5, "use_filter": False, "entry_after": "09:00"}
    assert row.result_summary["ticks_used"] is False
    assert row.config["research_data_mode"] == "auto"
    assert run_service.backtest_run_detail(row).config["research_data_mode"] == "bars"
    assert row.result_summary["order_kinds"] == ["market_close", "market_entry"]

    from q_backend.research.strategy_files import load_strategy

    direct = _direct_metrics(
        run_jobs_sync,
        load_strategy("simple_ma", root=strategy_env, bundle_root=strategy_env.parent / "bundles"),
        length=5,
    )
    assert direct.metrics["total_trades"] > 0
    assert _clean(
        {k: v for k, v in row.result_summary.items() if k not in ("ticks_used", "order_kinds", "research_data_mode")}
    ) == _clean(direct.metrics)


def test_a_second_backtest_of_an_unchanged_file_reuses_its_version(run_jobs_sync, strategy_env):
    _write(strategy_env, "simple_ma.py", SIMPLE_MA)

    first = _start()
    second = _start()

    assert second == first
    assert _version_count(run_jobs_sync) == 1


def test_an_edit_creates_a_new_version_and_a_rerun_reproduces_the_first_run(run_jobs_sync, strategy_env):
    _write(strategy_env, "simple_ma.py", SIMPLE_MA)
    first = _row(run_jobs_sync, _start())
    first_version = first.strategy_version_id

    _write(strategy_env, "simple_ma.py", EDITED_MA)
    edited = _row(run_jobs_sync, _start())

    assert _version_count(run_jobs_sync) == 2
    assert edited.strategy_version_id != first_version
    assert _clean(edited.result_summary) != _clean(first.result_summary)

    rerun_id = _start(strategy_file=None, strategy_version_id=str(first_version), strategy_params={"length": 5})
    rerun = _row(run_jobs_sync, rerun_id)
    assert rerun.status == "completed"
    assert rerun.strategy_version_id == first_version
    assert _clean(rerun.result_summary) == _clean(first.result_summary)
    assert _version_count(run_jobs_sync) == 2


def test_a_lookahead_indicator_fails_the_run_with_the_causality_error(run_jobs_sync, strategy_env):
    _write(strategy_env, "lookahead.py", LOOKAHEAD)

    run_id = _start(strategy_file="lookahead", strategy_params={})

    row = _row(run_jobs_sync, run_id)
    assert row.status == "failed"
    assert "is not causal" in row.error_message
    assert "peek" in row.error_message


def test_a_run_of_an_edited_file_uses_the_frozen_version_not_the_live_file(run_jobs_sync, strategy_env):
    _write(strategy_env, "simple_ma.py", SIMPLE_MA)
    first_id = _start()
    first = _row(run_jobs_sync, first_id)

    # The live file now raises on import; the frozen version must still run.
    _write(strategy_env, "simple_ma.py", "raise RuntimeError('edited into a crash')\n")
    rerun_id = _start(
        strategy_file=None, strategy_version_id=str(first.strategy_version_id), strategy_params={"length": 5}
    )

    assert _row(run_jobs_sync, rerun_id).status == "completed"


def test_invalid_parameters_are_rejected_with_422(run_jobs_sync, strategy_env):
    _write(strategy_env, "simple_ma.py", SIMPLE_MA)

    for params in ({"lenght": 5}, {"length": "5"}, {"length": 99}, {"use_filter": "yes"}, {"entry_after": "25:00"}):
        with pytest.raises(HTTPException) as caught:
            _start(strategy_params=params)
        assert caught.value.status_code == 422, params


def test_an_invalid_strategy_file_is_rejected_with_422(run_jobs_sync, strategy_env):
    _write(strategy_env, "broken.py", "raise RuntimeError('no')\n")

    with pytest.raises(HTTPException) as caught:
        _start(strategy_file="broken", strategy_params={})

    assert caught.value.status_code == 422
    assert "RuntimeError: no" in caught.value.detail


def test_an_unknown_strategy_version_is_not_found(run_jobs_sync, strategy_env):
    with pytest.raises(HTTPException) as caught:
        _start(strategy_file=None, strategy_version_id=str(uuid.uuid4()), strategy_params={})

    assert caught.value.status_code == 404


def test_strategy_file_runs_refuse_the_tick_engine_and_ml_filters(run_jobs_sync, strategy_env):
    _write(strategy_env, "simple_ma.py", SIMPLE_MA)

    with pytest.raises(HTTPException) as caught:
        _start(engine="tick")

    assert caught.value.status_code == 422
    assert "candle engine" in caught.value.detail


def test_a_stored_strategy_file_config_reruns_unchanged(run_jobs_sync, strategy_env):
    """History re-runs post the stored config back as the request; it must be accepted as it is."""
    _write(strategy_env, "simple_ma.py", SIMPLE_MA)
    first = _row(run_jobs_sync, _start())

    rerun_id = backtest_jobs.start_job(BacktestJobRequest.model_validate(first.config))

    rerun = _row(run_jobs_sync, rerun_id)
    assert rerun.status == "completed"
    assert rerun.strategy_version_id == first.strategy_version_id
    assert _clean(rerun.result_summary) == _clean(first.result_summary)


def test_a_run_lists_its_version_and_bundle_hash(run_jobs_sync, strategy_env):
    _write(strategy_env, "simple_ma.py", SIMPLE_MA)
    run_id = _start()

    with run_jobs_sync.harness_session_factory() as session:
        detail = run_service.get_run(session, run_id)
        listing = run_service.list_runs(session, limit=10, offset=0)

    assert detail.strategy == "simple_ma"
    assert detail.strategy_version_id is not None
    assert len(detail.strategy_bundle_hash) == 64
    item = next(entry for entry in listing["items"] if entry.run_id == run_id)
    assert item.strategy_version_id == detail.strategy_version_id
    assert item.strategy_bundle_hash == detail.strategy_bundle_hash


@pytest.mark.parametrize("mode", ["ticks", "auto"])
def test_tick_mode_survives_dispatch_persistence_and_version_reruns(run_jobs_sync, strategy_env, monkeypatch, mode):
    from unittest.mock import Mock

    from q_backend.research.tick_store import TickStore, TickSyncReport
    from tests.research.test_stop_target_backtest import DAY, FLAT_0, _write_session

    _write(
        strategy_env,
        "simple_ma.py",
        """from q_backend.research import ResearchStrategy, TradeOrder

class Protective(ResearchStrategy):
    parameters = {}

    def entry_strategy(self, frame):
        if len(frame) == 1:
            return TradeOrder.buy(stop_loss=95.0)
""",
    )
    _write_session(
        strategy_env.parent / "ticks",
        FLAT_0 + [("10:10:00", 101.0), ("10:12:00", 102.0), ("10:20:00", 99.0)],
        symbol=SYMBOL,
    )
    sync = Mock(return_value=TickSyncReport([], [DAY], [], [], []))
    monkeypatch.setattr(TickStore, "sync", sync)
    first = _row(
        run_jobs_sync,
        _start(
            research_data_mode=mode,
            timeframe="M10",
            start=dt.datetime(2026, 10, 5),
            end=dt.datetime(2026, 10, 5, 23, 59, 59),
            strategy_params={},
        ),
    )
    assert first.status == "completed", first.error_message
    assert first.config["research_data_mode"] == mode
    assert first.result_summary["ticks_used"] is True
    sync.assert_called_once()
    detail = run_service.backtest_run_detail(first)
    assert detail.config["research_data_mode"] == "ticks"
    assert first.config["research_data_mode"] == mode
    rerun = _row(run_jobs_sync, _start(**{**detail.config, "strategy_file": None}))
    assert rerun.strategy_version_id == first.strategy_version_id
    assert rerun.result_summary == first.result_summary
