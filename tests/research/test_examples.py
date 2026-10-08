"""Tests exercising research examples: offline Parquet RSI and mocked MT5 backtest."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from examples.research.mt5_backtest import (
    build_parser as mt5_build_parser,
    main as mt5_main,
    run_mt5_workflow,
)
from examples.research.rsi_reversion import (
    RSIReversion,
    build_parser as rsi_build_parser,
    main as rsi_main,
    run_rsi_backtest,
)
from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import indicators


@pytest.fixture
def synthetic_rsi_parquet(tmp_path: Path) -> Path:
    # 40 bars with a clear dip below 30 and recovery above 50
    idx = pd.date_range("2026-09-01 09:00", periods=40, freq="5min", tz=BRASILIA_TZ, name="time")
    # Base price path: start 100, drop sharply, then rise
    closes = np.full(40, 100.0)
    for i in range(15, 25):
        closes[i] = 100.0 - (i - 14) * 3.0  # drop to 70
    for i in range(25, 40):
        closes[i] = 70.0 + (i - 24) * 2.5  # rise to 107.5

    opens = closes - 0.2
    highs = closes + 0.5
    lows = closes - 0.5
    tick_volumes = np.full(40, 100, dtype=np.int64)

    df = pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "tick_volume": tick_volumes,
        },
        index=idx,
    )
    file_path = tmp_path / "bars.parquet"
    df.to_parquet(file_path)
    return file_path


def test_example_scripts_default_to_unadjusted_win_symbol() -> None:
    assert rsi_build_parser().parse_args(["--input", "data.parquet"]).symbol == "WIN$N"
    assert mt5_build_parser().parse_args(["--start", "2026-09-01"]).symbol == "WIN$N"


def test_offline_rsi_example_execution(synthetic_rsi_parquet: Path) -> None:
    result = run_rsi_backtest(
        synthetic_rsi_parquet,
        symbol="WIN$",
        period=14,
        quantity=1,
        point_value=0.20,
        capital=10000.0,
    )
    assert "total_trades" in result.metrics
    assert "realized_equity" in result.equity.columns
    assert "rsi" in result.data.columns
    assert "q_signal_entry" not in result.data.columns


def test_offline_rsi_example_declares_rsi_in_oscillator_pane(synthetic_rsi_parquet: Path) -> None:
    result = run_rsi_backtest(synthetic_rsi_parquet, symbol="WIN$", capital=10000.0)
    assert [(indicator.column, indicator.pane) for indicator in result.indicators] == [("rsi", "oscillator")]


def test_rsi_prefix_stability_checkpoints(synthetic_rsi_parquet: Path) -> None:
    """Verify that RSIReversion decisions at fixed bar checkpoints match when evaluated on prefixes."""
    df = pd.read_parquet(synthetic_rsi_parquet)
    strat = RSIReversion(period=14)

    # Check bar 20, 25, 30
    checkpoints = [20, 25, 30]
    for cp in checkpoints:
        prefix = df.iloc[:cp].copy()
        prefix_aug = strat.compute_indicators(prefix)
        entry_dec = strat.entry_strategy(prefix_aug)
        exit_dec = strat.exit_strategy(prefix_aug)

        # Full frame evaluation
        full_aug = strat.compute_indicators(df.copy())
        full_prefix = full_aug.iloc[:cp].copy()
        entry_full = strat.entry_strategy(full_prefix)
        exit_full = strat.exit_strategy(full_prefix)

        # Verify rsi value at checkpoint matches exactly
        assert prefix_aug["rsi"].iloc[-1] == pytest.approx(full_aug["rsi"].iloc[cp - 1])
        assert entry_dec == entry_full
        assert exit_dec == exit_full


def test_mt5_example_mocked_gateway(synthetic_rsi_parquet: Path) -> None:
    mock_bars = pd.read_parquet(synthetic_rsi_parquet)

    with patch("examples.research.mt5_backtest.load_bars", return_value=mock_bars) as mock_load:
        # 1. Custom strategy
        res_custom = run_mt5_workflow(
            symbol="WIN$",
            timeframe="M5",
            start="2026-09-01",
            strategy_mode="custom",
        )
        assert mock_load.called
        assert "total_trades" in res_custom.metrics

        # 2. Builtin strategy
        res_builtin = run_mt5_workflow(
            symbol="WIN$",
            timeframe="M5",
            start="2026-09-01",
            strategy_mode="builtin",
        )
        assert "total_trades" in res_builtin.metrics


def test_examples_cost_per_contract_parser_flag() -> None:
    """Both example scripts accept --cost-per-contract, defaulting to 0.0."""
    rsi_args_default = rsi_build_parser().parse_args(["--input", "dummy.parquet"])
    assert rsi_args_default.cost_per_contract == 0.0

    rsi_args_custom = rsi_build_parser().parse_args(["--input", "dummy.parquet", "--cost-per-contract", "3.75"])
    assert rsi_args_custom.cost_per_contract == 3.75

    mt5_args_default = mt5_build_parser().parse_args(["--start", "2026-09-01"])
    assert mt5_args_default.cost_per_contract == 0.0

    mt5_args_custom = mt5_build_parser().parse_args(["--start", "2026-09-01", "--cost-per-contract", "3.75"])
    assert mt5_args_custom.cost_per_contract == 3.75


def test_rsi_example_cost_forwarding_and_output(
    synthetic_rsi_parquet: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """RSI example forwards cost_per_contract to backtest and prints total commission and zero-cost line."""
    # 1. Forwarding to backtest()
    with patch("examples.research.rsi_reversion.backtest", wraps=None) as mock_backtest:
        mock_backtest.return_value = MagicMock(
            metrics={"total_trades": 1, "total_pnl": 100.0, "total_commission": 7.50, "win_rate": 1.0},
            trades=pd.DataFrame(),
            equity=pd.DataFrame({"realized_equity": [10000.0]}),
        )
        run_rsi_backtest(synthetic_rsi_parquet, cost_per_contract=3.75)
        assert mock_backtest.called
        _, kwargs = mock_backtest.call_args
        assert isinstance(kwargs.get("costs"), TransactionCostConfig)
        assert kwargs["costs"].cost_per_contract == 3.75

    # 2. main() with zero cost (default)
    with patch("examples.research.rsi_reversion.run_rsi_backtest") as mock_run:
        mock_run.return_value = MagicMock(
            metrics={"total_trades": 1, "total_pnl": 100.0, "total_commission": 0.0, "win_rate": 1.0},
            trades=pd.DataFrame(),
            equity=pd.DataFrame({"realized_equity": [10000.0]}),
        )
        capsys.readouterr()
        ret_zero = rsi_main(["--input", str(synthetic_rsi_parquet), "--cost-per-contract", "0.0"])
        assert ret_zero == 0
        out_zero = capsys.readouterr().out
        assert "Total commission: 0.00" in out_zero
        assert "No transaction costs were applied." in out_zero
        mock_run.assert_called_with(
            Path(synthetic_rsi_parquet),
            symbol="WIN$N",
            period=14,
            quantity=1,
            point_value=0.20,
            capital=10000.0,
            cost_per_contract=0.0,
        )

    # 3. main() with non-zero cost
    with patch("examples.research.rsi_reversion.run_rsi_backtest") as mock_run:
        mock_run.return_value = MagicMock(
            metrics={"total_trades": 1, "total_pnl": 100.0, "total_commission": 7.50, "win_rate": 1.0},
            trades=pd.DataFrame(),
            equity=pd.DataFrame({"realized_equity": [10000.0]}),
        )
        capsys.readouterr()
        ret_cost = rsi_main(["--input", str(synthetic_rsi_parquet), "--cost-per-contract", "3.75"])
        assert ret_cost == 0
        out_cost = capsys.readouterr().out
        assert "Total commission: 7.50" in out_cost
        assert "No transaction costs were applied." not in out_cost
        mock_run.assert_called_with(
            Path(synthetic_rsi_parquet),
            symbol="WIN$N",
            period=14,
            quantity=1,
            point_value=0.20,
            capital=10000.0,
            cost_per_contract=3.75,
        )


def test_mt5_example_cost_forwarding_and_output(
    synthetic_rsi_parquet: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """MT5 example forwards cost_per_contract to backtest and prints total commission and zero-cost line."""
    mock_bars = pd.read_parquet(synthetic_rsi_parquet)

    # 1. Forwarding to backtest()
    with patch("examples.research.mt5_backtest.load_bars", return_value=mock_bars):
        with patch("examples.research.mt5_backtest.backtest", wraps=None) as mock_backtest:
            mock_backtest.return_value = MagicMock(
                metrics={"total_trades": 1, "total_pnl": 100.0, "total_commission": 7.50, "win_rate": 1.0},
                trades=pd.DataFrame(),
                equity=pd.DataFrame({"realized_equity": [10000.0]}),
            )
            run_mt5_workflow(
                symbol="WIN$",
                timeframe="M5",
                start="2026-09-01",
                cost_per_contract=3.75,
            )
            assert mock_backtest.called
            _, kwargs = mock_backtest.call_args
            assert isinstance(kwargs.get("costs"), TransactionCostConfig)
            assert kwargs["costs"].cost_per_contract == 3.75

    # 2. main() with zero cost (default)
    with patch("examples.research.mt5_backtest.run_mt5_workflow") as mock_run:
        mock_run.return_value = MagicMock(
            metrics={"total_trades": 1, "total_pnl": 100.0, "total_commission": 0.0, "win_rate": 1.0},
            trades=pd.DataFrame(),
            equity=pd.DataFrame({"realized_equity": [10000.0]}),
        )
        capsys.readouterr()
        ret_zero = mt5_main(["--start", "2026-09-01", "--cost-per-contract", "0.0"])
        assert ret_zero == 0
        out_zero = capsys.readouterr().out
        assert "Total commission: 0.00" in out_zero
        assert "No transaction costs were applied." in out_zero
        mock_run.assert_called_with(
            symbol="WIN$N",
            timeframe="M5",
            start="2026-09-01",
            end=None,
            strategy_mode="custom",
            quantity=1,
            point_value=0.20,
            capital=10000.0,
            cost_per_contract=0.0,
            gateway_url=None,
            gateway_token=None,
        )

    # 3. main() with non-zero cost
    with patch("examples.research.mt5_backtest.run_mt5_workflow") as mock_run:
        mock_run.return_value = MagicMock(
            metrics={"total_trades": 1, "total_pnl": 100.0, "total_commission": 7.50, "win_rate": 1.0},
            trades=pd.DataFrame(),
            equity=pd.DataFrame({"realized_equity": [10000.0]}),
        )
        capsys.readouterr()
        ret_cost = mt5_main(["--start", "2026-09-01", "--cost-per-contract", "3.75"])
        assert ret_cost == 0
        out_cost = capsys.readouterr().out
        assert "Total commission: 7.50" in out_cost
        assert "No transaction costs were applied." not in out_cost
        mock_run.assert_called_with(
            symbol="WIN$N",
            timeframe="M5",
            start="2026-09-01",
            end=None,
            strategy_mode="custom",
            quantity=1,
            point_value=0.20,
            capital=10000.0,
            cost_per_contract=3.75,
            gateway_url=None,
            gateway_token=None,
        )
