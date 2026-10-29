"""Placement latency in research backtests (Q-139)."""

from __future__ import annotations

import pandas as pd
import pytest

from q_backend.research import ResearchStrategy, TradeOrder, compare_results
from tests.research.test_resting_orders_backtest import _m10_frame, _run, _write_session


class PlaceOnce(ResearchStrategy):
    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        return TradeOrder.buy_stop(105.0, expire_bars=1) if len(frame) == 1 else None


# The order is decided at the 10:10 bar's open-to-close; a trade at 106 prints at 10:10:06 and 107 at 10:10:12.
TRADES = [("10:00:00", 100.0), ("10:10:00", 100.0), ("10:10:06", 106.0), ("10:10:12", 107.0), ("10:20:00", 100.0)]


def test_a_placement_latency_skips_trades_before_the_decision_plus_latency(tmp_path):
    store = _write_session(tmp_path, TRADES)
    early = _run(_m10_frame(store), PlaceOnce(), store, latency_ms=0)
    late = _run(_m10_frame(store), PlaceOnce(), store, latency_ms=10_000)
    assert early.trades.iloc[0]["entry_price"] == 106.0
    assert late.trades.iloc[0]["entry_price"] == 107.0


def test_the_default_latency_leaves_results_unchanged(tmp_path):
    store = _write_session(tmp_path, TRADES)
    omitted = _run(_m10_frame(store), PlaceOnce(), store)
    explicit = _run(_m10_frame(store), PlaceOnce(), store, latency_ms=0)
    assert compare_results(omitted, explicit).identical
    assert "latency_ms" not in explicit.config


def test_a_nonzero_latency_is_recorded_in_the_config(tmp_path):
    store = _write_session(tmp_path, TRADES)
    result = _run(_m10_frame(store), PlaceOnce(), store, latency_ms=250)
    assert result.config["latency_ms"] == 250


@pytest.mark.parametrize("latency", [-1, 1.5, True, "10"])
def test_latency_must_be_a_non_negative_whole_number_of_milliseconds(tmp_path, latency):
    store = _write_session(tmp_path, TRADES)
    with pytest.raises(ValueError, match="latency_ms must be a non-negative integer"):
        _run(_m10_frame(store), PlaceOnce(), store, latency_ms=latency)
