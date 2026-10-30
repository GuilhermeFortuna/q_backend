"""compare_results: trade-by-trade comparison of two backtests (Q-139)."""

from __future__ import annotations

import pandas as pd
import pytest

from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import compare_results
from q_backend.research.results import BacktestResult

BASE = pd.Timestamp("2026-10-05 10:00", tz=BRASILIA_TZ)


def _trade(minutes: int, **fields) -> dict:
    trade = {
        "entry_time": BASE + pd.Timedelta(minutes=minutes),
        "side": "long",
        "entry_price": 100.0,
        "exit_time": BASE + pd.Timedelta(minutes=minutes + 30),
        "exit_price": 103.0,
        "quantity": 1.0,
        "entry_reason": "go",
        "exit_reason": "stop",
        "pnl": 3.0,
    }
    trade.update(fields)
    return trade


def _result(*trades: dict, total_pnl: float = 0.0, profit_factor: float = 0.0, drawdown: float = 0.0):
    return BacktestResult(
        metrics={"total_pnl": total_pnl, "profit_factor": profit_factor, "max_drawdown_value": drawdown},
        trades=pd.DataFrame(list(trades)),
        equity=pd.DataFrame(),
        data=pd.DataFrame(),
    )


def test_identical_results_match_every_trade():
    a = _result(_trade(0), _trade(60, side="short", pnl=-1.0), total_pnl=2.0, profit_factor=1.5, drawdown=4.0)
    b = _result(_trade(0), _trade(60, side="short", pnl=-1.0), total_pnl=2.0, profit_factor=1.5, drawdown=4.0)
    report = compare_results(a, b)
    assert report.identical
    assert report.matched == 2
    assert report.differing == ()
    assert report.left_only == () and report.right_only == ()
    assert report.left.total_pnl == report.right.total_pnl == 2.0
    assert report.left.profit_factor == 1.5
    assert report.left.max_drawdown == 4.0


def test_a_differing_pair_names_its_first_differing_field():
    a = _result(_trade(0, exit_price=103.0, pnl=3.0))
    b = _result(_trade(0, exit_price=104.0, pnl=4.0))
    report = compare_results(a, b)
    assert not report.identical
    assert report.matched == 0
    [diff] = report.differing
    assert diff.field == "exit_price"
    assert (diff.left_value, diff.right_value) == (103.0, 104.0)


def test_the_first_field_in_comparison_order_is_the_one_reported():
    # side differs as well as the exit price; side comes first in the comparison order.
    a = _result(_trade(0, side="long", exit_price=103.0))
    b = _result(_trade(0, side="short", exit_price=104.0))
    [diff] = compare_results(a, b).differing
    assert diff.field == "side"


def test_a_pair_differing_only_in_reasons_is_matched_and_listed_as_a_label_difference():
    a = _result(_trade(0, exit_reason="stop"))
    b = _result(_trade(0, exit_reason="Hold time"))
    report = compare_results(a, b)
    assert report.identical
    assert report.matched == 1
    assert report.differing == ()
    [label] = report.labels
    assert label.field == "exit_reason"
    assert (label.left_value, label.right_value) == ("stop", "Hold time")


def test_an_entry_reason_difference_is_a_label_difference_too():
    a = _result(_trade(0, entry_reason="long breakout"))
    b = _result(_trade(0, entry_reason="breakout"))
    report = compare_results(a, b)
    assert report.identical
    [label] = report.labels
    assert label.field == "entry_reason"


def test_a_value_difference_hides_no_label_difference_it_reports_only_the_value():
    a = _result(_trade(0, exit_price=103.0, exit_reason="stop"))
    b = _result(_trade(0, exit_price=104.0, exit_reason="target"))
    report = compare_results(a, b)
    assert not report.identical
    assert report.matched == 0
    assert [diff.field for diff in report.differing] == ["exit_price"]
    assert report.labels == ()


def test_trades_present_in_only_one_result_are_one_sided():
    a = _result(_trade(0), _trade(60))
    b = _result(_trade(0), _trade(120, side="short"))
    report = compare_results(a, b)
    assert report.matched == 1
    assert report.differing == ()
    assert [trade["entry_time"] for trade in report.left_only] == [BASE + pd.Timedelta(minutes=60)]
    assert [trade["entry_time"] for trade in report.right_only] == [BASE + pd.Timedelta(minutes=120)]
    assert not report.identical


def test_tolerance_accepts_small_numeric_differences_only():
    a = _result(_trade(0, exit_price=103.0))
    b = _result(_trade(0, exit_price=103.004))
    assert not compare_results(a, b).identical
    report = compare_results(a, b, tolerance=0.01)
    assert report.identical
    assert report.matched == 1


def test_tolerance_does_not_relax_text_or_time_fields():
    a = _result(_trade(0, side="long"))
    b = _result(_trade(0, side="short"))
    assert compare_results(a, b, tolerance=1e9).differing[0].field == "side"


def test_two_trades_opening_at_one_time_pair_in_order():
    a = _result(_trade(0, exit_price=101.0), _trade(0, exit_price=102.0))
    b = _result(_trade(0, exit_price=102.0), _trade(0, exit_price=101.0))
    report = compare_results(a, b)
    assert report.matched == 0
    assert [diff.field for diff in report.differing] == ["exit_price", "exit_price"]


def test_empty_results_are_identical():
    assert compare_results(_result(), _result()).identical


def test_report_reads_as_a_summary():
    a = _result(_trade(0, exit_price=103.0), total_pnl=3.0)
    b = _result(_trade(0, exit_price=104.0), total_pnl=4.0)
    text = str(compare_results(a, b))
    assert "matched 0, differing 1, label only 0" in text
    assert "differs at" in text and "exit_price" in text
    assert "total PnL: 3.00 | 4.00" in text


def test_report_lists_label_differences_separately():
    a = _result(_trade(0, exit_reason="stop"))
    b = _result(_trade(0, exit_reason="target"))
    text = str(compare_results(a, b))
    assert "matched 1, differing 0, label only 1" in text
    assert "label differs at" in text and "exit_reason" in text


def test_only_backtest_results_are_compared():
    with pytest.raises(TypeError, match="BacktestResult"):
        compare_results(_result(), [])


def test_negative_tolerance_is_rejected():
    with pytest.raises(ValueError, match="tolerance"):
        compare_results(_result(), _result(), tolerance=-1.0)
