"""UI runs choose a coherent bar/tick source, independent of local caches."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from q_backend.api.schemas.backtest import BacktestRequest
from q_backend.research import ResearchStrategy, TradeOrder, backtest
from q_backend.research import strategy_file_runs as runs
from q_backend.research.publishing import build_import_request
from q_backend.research.tick_store import TickSyncReport
from tests.research.test_stop_target_backtest import DAY, FLAT_0, SYMBOL, _write_session


class Orders(ResearchStrategy):
    def entry_strategy(self, frame):
        if len(frame) == 1:
            return TradeOrder.buy(stop_loss=95.0)


class RequiredTicks(Orders):
    def compute_indicators(self, frame, *, ticks):
        assert ticks is not None
        return frame.copy()


class NoOrders(ResearchStrategy):
    def entry_strategy(self, frame):
        return None


class OptionalTicks(NoOrders):
    def compute_indicators(self, frame, *, ticks=None):
        return frame.copy()


@pytest.fixture
def setup_run(tmp_path, monkeypatch):
    store = _write_session(tmp_path / "ticks", FLAT_0 + [("10:10:00", 100.0), ("10:11:00", 96.0), ("10:12:00", 94.0)])
    frame = store.bars("M10", start=DAY.isoformat(), end=f"{DAY} 23:59:59")
    provider = frame.copy()
    provider.iloc[1, provider.columns.get_loc("close")] = 94.5
    monkeypatch.setattr(runs, "_bars_frame", Mock(return_value=provider))
    monkeypatch.setattr(runs, "TickStore", Mock(return_value=store))
    monkeypatch.setattr(runs, "load_bundle", Mock(return_value=Orders))
    monkeypatch.setattr(runs, "validate_bundle", Mock(return_value=SimpleNamespace(uses_tick_hooks=False)))
    sync = Mock(return_value=TickSyncReport([], [DAY], [], [], []))
    monkeypatch.setattr(store, "sync", sync)
    kwargs = dict(
        frozen=runs.FrozenStrategy("orders", Mock(), []),
        bundle_root=tmp_path,
        symbol=SYMBOL,
        timeframe="M10",
        start=datetime(2026, 10, 5),
        end=datetime(2026, 10, 5, 23, 59, 59),
        strategy_params={},
        quantity=1,
        point_value=1.0,
        initial_capital=100000.0,
        costs=None,
        exit_params={},
        day_trade=False,
        day_trade_start_time="09:00",
        day_trade_end_time="16:00",
        day_trade_close_time="17:00",
        post_exit_bars=10,
        market_data_service=Mock(),
        checkpoint=Mock(),
        on_phase=Mock(),
    )
    return kwargs, store, frame, sync


def test_tick_mode_matches_script_using_tick_bars(setup_run):
    kwargs, store, frame, sync = setup_run
    outcome = runs.run_strategy(**kwargs, research_data_mode="ticks")
    expected = build_import_request(
        backtest(frame, strategy=Orders(), symbol=SYMBOL, ticks=store, check_causality=True, progress=False),
        name="orders",
        timeframe="M10",
    )
    exclude = {"trades": {"__all__": {"id", "order_id"}}}
    assert outcome.request.result.model_dump(exclude=exclude) == expected.result.model_dump(exclude=exclude)
    assert outcome.ticks_used
    sync.assert_called_once()
    runs._bars_frame.assert_not_called()


def test_bars_never_attaches_or_syncs_cached_ticks(setup_run, monkeypatch):
    kwargs, _, _, sync = setup_run
    monkeypatch.setattr(runs, "load_bundle", Mock(return_value=NoOrders))
    assert not runs.run_strategy(**kwargs).ticks_used
    runs.TickStore.assert_not_called()
    sync.assert_not_called()


@pytest.mark.parametrize("mode", ["bars", "ticks"])
def test_required_hooks_need_ticks(setup_run, monkeypatch, mode):
    kwargs, _, _, sync = setup_run
    monkeypatch.setattr(runs, "load_bundle", Mock(return_value=RequiredTicks))
    if mode == "bars":
        with pytest.raises(ValueError, match="requires tick data"):
            runs.run_strategy(**kwargs, research_data_mode=mode)
        sync.assert_not_called()
        runs._bars_frame.assert_not_called()
    else:
        assert runs.run_strategy(**kwargs, research_data_mode=mode).ticks_used


def test_optional_hooks_allow_bars(setup_run, monkeypatch):
    kwargs, _, _, sync = setup_run
    monkeypatch.setattr(runs, "load_bundle", Mock(return_value=OptionalTicks))
    assert not runs.run_strategy(**kwargs, research_data_mode="bars").ticks_used
    sync.assert_not_called()


def test_sync_failure_aborts_partial_history(setup_run, monkeypatch):
    kwargs, _, _, sync = setup_run
    sync.return_value = TickSyncReport([], [], [], [], [DAY], errors={DAY: "gateway disconnected"})
    simulate = Mock()
    monkeypatch.setattr(runs, "backtest", simulate)
    with pytest.raises(ValueError, match="2026-10-05.*gateway disconnected"):
        runs.run_strategy(**kwargs, research_data_mode="ticks")
    simulate.assert_not_called()


@pytest.mark.parametrize("timeframe", ["W1", "MN1"])
def test_unsupported_tick_timeframe_rejected_before_sync(setup_run, timeframe):
    kwargs, _, _, sync = setup_run
    kwargs["timeframe"] = timeframe
    with pytest.raises(ValueError, match="Unsupported timeframe"):
        runs.run_strategy(**kwargs, research_data_mode="ticks")
    sync.assert_not_called()


def test_cancel_after_sync_prevents_simulation(setup_run, monkeypatch):
    kwargs, _, _, sync = setup_run

    def checkpoint():
        if sync.called:
            raise RuntimeError("cancelled")

    kwargs["checkpoint"] = checkpoint
    simulate = Mock()
    monkeypatch.setattr(runs, "backtest", simulate)
    with pytest.raises(RuntimeError, match="cancelled"):
        runs.run_strategy(**kwargs, research_data_mode="ticks")
    simulate.assert_not_called()


def test_mode_only_applies_to_strategy_files():
    assert BacktestRequest(symbol=SYMBOL).research_data_mode == "auto"
    for mode in ["auto", "bars", "ticks"]:
        with pytest.raises(ValidationError, match="strategy file"):
            BacktestRequest(symbol=SYMBOL, research_data_mode=mode)
        assert (
            BacktestRequest(symbol=SYMBOL, strategy_file="orders", research_data_mode=mode).research_data_mode == mode
        )


def test_successful_empty_days_do_not_prevent_other_sessions(setup_run):
    from datetime import date

    kwargs, _, _, sync = setup_run
    sync.return_value.empty = [date(2026, 10, 6)]
    assert runs.run_strategy(**kwargs, research_data_mode="ticks").ticks_used


def test_empty_tick_range_fails_without_provider_fallback(setup_run, monkeypatch):
    from q_backend.research.errors import NoMarketDataError

    kwargs, store, _, _ = setup_run
    monkeypatch.setattr(
        store,
        "bars",
        Mock(
            side_effect=NoMarketDataError(
                symbol=SYMBOL, timeframe="M10", source="tick_store", start="2026-10-05", end="2026-10-05"
            )
        ),
    )
    with pytest.raises(NoMarketDataError):
        runs.run_strategy(**kwargs, research_data_mode="ticks")
    runs._bars_frame.assert_not_called()


def test_auto_restarts_with_fresh_strategy_and_tick_bars(setup_run, monkeypatch):
    kwargs, _, _, sync = setup_run
    instances = []

    class Stateful(Orders):
        def __init__(self):
            self.calls = 0
            instances.append(self)

        def entry_strategy(self, frame):
            self.calls += 1
            return super().entry_strategy(frame)

    monkeypatch.setattr(runs, "load_bundle", Mock(return_value=Stateful))
    outcome = runs.run_strategy(**kwargs, research_data_mode="auto")
    assert outcome.ticks_used
    assert outcome.request.result.trades[0]["exit_price"] == 94.0
    assert len(instances) == 2
    assert instances[0] is not instances[1]
    assert instances[0].calls == 1
    sync.assert_called_once()
    runs._bars_frame.assert_called_once()


@pytest.mark.parametrize("strategy", [RequiredTicks, OptionalTicks])
def test_auto_uses_ticks_upfront_for_any_tick_hook(setup_run, monkeypatch, strategy):
    kwargs, _, _, sync = setup_run
    monkeypatch.setattr(runs, "load_bundle", Mock(return_value=strategy))
    assert runs.run_strategy(**kwargs, research_data_mode="auto").ticks_used
    runs._bars_frame.assert_not_called()
    sync.assert_called_once()


def test_auto_stays_on_bars_for_candle_only_strategies(setup_run, monkeypatch):
    kwargs, _, _, sync = setup_run
    monkeypatch.setattr(runs, "load_bundle", Mock(return_value=NoOrders))
    assert not runs.run_strategy(**kwargs, research_data_mode="auto").ticks_used
    sync.assert_not_called()


def test_auto_does_not_retry_unrelated_strategy_errors(setup_run, monkeypatch):
    kwargs, _, _, sync = setup_run
    simulate = Mock(side_effect=ValueError("pass ticks= to backtest() is just user text"))
    monkeypatch.setattr(runs, "backtest", simulate)
    with pytest.raises(ValueError, match="just user text"):
        runs.run_strategy(**kwargs, research_data_mode="auto")
    assert simulate.call_count == 1
    sync.assert_not_called()


def test_auto_retries_only_once(setup_run, monkeypatch):
    from q_backend.research.errors import TickDataRequiredError

    kwargs, _, _, sync = setup_run
    simulate = Mock(side_effect=TickDataRequiredError("ticks needed"))
    monkeypatch.setattr(runs, "backtest", simulate)
    with pytest.raises(TickDataRequiredError):
        runs.run_strategy(**kwargs, research_data_mode="auto")
    assert simulate.call_count == 2
    sync.assert_called_once()


def test_auto_cancel_before_retry_does_not_download_ticks(setup_run, monkeypatch):
    from q_backend.research.errors import TickDataRequiredError

    kwargs, _, _, sync = setup_run
    simulate = Mock(side_effect=TickDataRequiredError("ticks needed"))
    monkeypatch.setattr(runs, "backtest", simulate)

    def checkpoint():
        if simulate.called:
            raise RuntimeError("cancelled")

    kwargs["checkpoint"] = checkpoint
    with pytest.raises(RuntimeError, match="cancelled"):
        runs.run_strategy(**kwargs, research_data_mode="auto")
    sync.assert_not_called()


@pytest.mark.parametrize("action", ["amend", "resting", "timed_exit"])
def test_auto_handles_runtime_tick_orders(setup_run, monkeypatch, action):
    kwargs, _, _, sync = setup_run

    class RuntimeOrders(ResearchStrategy):
        def entry_strategy(self, frame):
            if len(frame) == 1:
                return TradeOrder.buy_limit(99.0) if action == "resting" else TradeOrder.buy()

        def exit_strategy(self, frame, positions=()):
            if positions:
                if action == "amend":
                    return TradeOrder.amend(stop_loss=95.0)
                if action == "timed_exit":
                    from datetime import time

                    return TradeOrder.close(at=time(10, 25))

    monkeypatch.setattr(runs, "load_bundle", Mock(return_value=RuntimeOrders))
    assert runs.run_strategy(**kwargs, research_data_mode="auto").ticks_used
    sync.assert_called_once()
    runs._bars_frame.assert_called_once()
