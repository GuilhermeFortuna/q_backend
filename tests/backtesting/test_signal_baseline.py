"""Queued-signal baseline for Q-024 columnar migration.

Pins today's per-bar ``evaluate_queued_signals`` output for every strategy class
and decision branch *before* any strategy source changes. Goldens live under
``tests/backtesting/goldens/signals/`` and are regenerated only with
``--regen-goldens``.
"""

from __future__ import annotations

import copy
import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import pytest

import q_backend.backtesting.strategies  # noqa: F401 — register built-ins
from q_backend.backtesting.factory import build_composite_entry, build_strategy
from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.backtesting.genome.registry_fixtures import (
    MA_CROSSOVER_GENOME,
    REGISTRY_DEFAULT_PARAMS,
    REGISTRY_GENOME_FIXTURES,
)
from q_backend.backtesting.indicator_frame import augment_indicator_frame
from q_backend.backtesting.models import OrderAction, Signal, Trade
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.strategy_registry import default_params_for
from q_backend.execution.signal_eval import evaluate_queued_signals, signal_arrays

from backtesting.test_goldens import (
    BASE_MA_PARAMS,
    CTX_GENOME,
    SYMBOL,
    _normalize,
    synthetic_ohlcv,
)

SIGNALS_DIR = Path(__file__).parent / "goldens" / "signals"

LEGACY_BOOL_TRIGGER_COLUMNS: tuple[str, ...] = (
    "buy_signal",
    "sell_signal",
    "exit_long_signal",
    "exit_short_signal",
    "exit_signal",
    "rebalance",
    "net_long_signal",
    "net_short_signal",
    "entry_long_signal",
    "entry_short_signal",
)

OPEN_BAR = 40
HOLDING_PERIOD_CASES = frozenset({"fma", "trb", "genome_fma", "genome_trb"})


@dataclass
class BaselineCase:
    name: str
    make_strategy: Callable[[], TradingStrategy]
    data_override: Callable[[pd.DataFrame], pd.DataFrame] | None = None
    extra_scenarios: tuple[str, ...] = field(default_factory=tuple)

    def data(self) -> pd.DataFrame:
        frame = synthetic_ohlcv(400)
        if self.data_override is not None:
            return self.data_override(frame)
        return frame


def _composite_entries_or_and() -> list[dict[str, Any]]:
    return [
        {"strategy": "MACrossover", "params": dict(BASE_MA_PARAMS)},
        {"strategy": "MACD", "params": default_params_for("MACD")},
    ]


def _ma_genome_with_exit_kind(kind: str) -> dict[str, Any]:
    genome = copy.deepcopy(MA_CROSSOVER_GENOME)
    genome["nodes"] = list(genome["nodes"]) + [
        {"id": "ex_long", "kind": kind, "params": {}, "inputs": []},
        {"id": "ex_short", "kind": kind, "params": {}, "inputs": []},
    ]
    genome["exit_long"] = {"ref": "ex_long"}
    genome["exit_short"] = {"ref": "ex_short"}
    return genome


def _tsmom_params(*, trading_rule: str, rebalance_on_every_bar: str, trend_signal_cap: float = 1.0) -> dict[str, Any]:
    return {
        "lookback_bars": 48,
        "rebalance_bars": 12,
        "vol_window": 20,
        "vol_estimator": "close_to_close",
        "trading_rule": trading_rule,
        "trend_signal_cap": trend_signal_cap,
        "nw_lags": 4,
        "rebalance_on_every_bar": rebalance_on_every_bar,
    }


def _hurst_params(*, rebalance_on_every_bar: str, signal_lag_bars: int) -> dict[str, Any]:
    return {
        "lookback_1": 10,
        "lookback_2": 21,
        "lookback_3": 48,
        "rebalance_bars": 12,
        "vol_window": 20,
        "vol_estimator": "close_to_close",
        "risk_free_rate_annual": 0.0,
        "signal_lag_bars": signal_lag_bars,
        "rebalance_on_every_bar": rebalance_on_every_bar,
    }


def _gatev_data(data: pd.DataFrame) -> pd.DataFrame:
    return data.assign(
        close_a=data["close"],
        close_b=data["close"] * 0.99,
        open_a=data["open"],
        open_b=data["open"] * 0.99,
    )


def _build_baseline_cases() -> dict[str, BaselineCase]:
    cases: list[BaselineCase] = [
        BaselineCase(
            name="macrossover",
            make_strategy=lambda: build_strategy("MACrossover", dict(BASE_MA_PARAMS), SYMBOL),
        ),
        BaselineCase(
            name="macrossover_trailing",
            make_strategy=lambda: build_strategy(
                "MACrossover",
                {**BASE_MA_PARAMS, "trailing_stop_pct": 0.03},
                SYMBOL,
            ),
        ),
        BaselineCase(
            name="macd",
            make_strategy=lambda: build_strategy("MACD", default_params_for("MACD"), SYMBOL),
        ),
        BaselineCase(
            name="rsi_mean_reversion",
            make_strategy=lambda: build_strategy(
                "RSIMeanReversion",
                default_params_for("RSIMeanReversion"),
                SYMBOL,
            ),
        ),
        BaselineCase(
            name="bollinger_reversion",
            make_strategy=lambda: build_strategy(
                "BollingerReversion",
                default_params_for("BollingerReversion"),
                SYMBOL,
            ),
        ),
        BaselineCase(
            name="donchian_breakout",
            make_strategy=lambda: build_strategy(
                "DonchianBreakout",
                default_params_for("DonchianBreakout"),
                SYMBOL,
            ),
        ),
        BaselineCase(
            name="vma",
            make_strategy=lambda: build_strategy("VMA", default_params_for("VMA"), SYMBOL),
        ),
        BaselineCase(
            name="fma",
            make_strategy=lambda: build_strategy(
                "FMA",
                {"period": 20, "band_pct": 0.5, "ma_type": "ema", "holding_period": 10},
                SYMBOL,
            ),
            extra_scenarios=("long_offgrid",),
        ),
        BaselineCase(
            name="trb",
            make_strategy=lambda: build_strategy(
                "TRB",
                {"period": 20, "band_pct": 0.0, "holding_period": 10},
                SYMBOL,
            ),
            extra_scenarios=("long_offgrid",),
        ),
        BaselineCase(
            name="tsmom_sign_flip",
            make_strategy=lambda: build_strategy(
                "TSMOM",
                _tsmom_params(trading_rule="sign", rebalance_on_every_bar="false"),
                SYMBOL,
            ),
        ),
        BaselineCase(
            name="tsmom_sign_rebalance",
            make_strategy=lambda: build_strategy(
                "TSMOM",
                _tsmom_params(trading_rule="sign", rebalance_on_every_bar="true"),
                SYMBOL,
            ),
        ),
        BaselineCase(
            name="tsmom_trend",
            make_strategy=lambda: build_strategy(
                "TSMOM",
                _tsmom_params(
                    trading_rule="trend",
                    rebalance_on_every_bar="false",
                    trend_signal_cap=2.0,
                ),
                SYMBOL,
            ),
        ),
        BaselineCase(
            name="hurst_flip",
            make_strategy=lambda: build_strategy(
                "HurstTrendBlend",
                _hurst_params(rebalance_on_every_bar="false", signal_lag_bars=2),
                SYMBOL,
            ),
        ),
        BaselineCase(
            name="hurst_rebalance",
            make_strategy=lambda: build_strategy(
                "HurstTrendBlend",
                _hurst_params(rebalance_on_every_bar="true", signal_lag_bars=0),
                SYMBOL,
            ),
        ),
        BaselineCase(
            name="gatev_pairs",
            make_strategy=lambda: build_strategy(
                "GatevPairs",
                {
                    "col_a": "close_a",
                    "col_b": "close_b",
                    "open_col_a": "open_a",
                    "open_col_b": "open_b",
                    "formation_bars": 100,
                    "trading_bars": 50,
                    "open_threshold_sd": 1.0,
                    "vol_window": 20,
                    "vol_estimator": "close_to_close",
                },
                SYMBOL,
            ),
            data_override=_gatev_data,
        ),
        BaselineCase(
            name="genome_ctx",
            make_strategy=lambda: CompositeStrategy(
                genome=copy.deepcopy(CTX_GENOME),
                params={},
                symbol=SYMBOL,
            ),
        ),
        BaselineCase(
            name="genome_ma_rebalance_exit",
            make_strategy=lambda: CompositeStrategy(
                genome=_ma_genome_with_exit_kind("exit.rebalance"),
                params=dict(REGISTRY_DEFAULT_PARAMS["MACrossover"]),
                symbol=SYMBOL,
            ),
        ),
        BaselineCase(
            name="genome_ma_opposite_exit",
            make_strategy=lambda: CompositeStrategy(
                genome=_ma_genome_with_exit_kind("exit.opposite_signal"),
                params=dict(REGISTRY_DEFAULT_PARAMS["MACrossover"]),
                symbol=SYMBOL,
            ),
        ),
        BaselineCase(
            name="composite_or",
            make_strategy=lambda: build_composite_entry(
                _composite_entries_or_and(),
                "or",
                {},
                {},
                SYMBOL,
            ),
        ),
        BaselineCase(
            name="composite_and",
            make_strategy=lambda: build_composite_entry(
                _composite_entries_or_and(),
                "and",
                {},
                {},
                SYMBOL,
            ),
        ),
        BaselineCase(
            name="composite_majority",
            make_strategy=lambda: build_composite_entry(
                [
                    {"strategy": "MACrossover", "params": dict(BASE_MA_PARAMS)},
                    {"strategy": "MACD", "params": default_params_for("MACD")},
                    {
                        "strategy": "RSIMeanReversion",
                        "params": {"period": 14, "oversold": 30.0, "overbought": 70.0},
                    },
                ],
                "majority",
                {"vote_threshold": 2},
                {},
                SYMBOL,
            ),
        ),
    ]

    for strategy_name, genome in REGISTRY_GENOME_FIXTURES.items():
        case_name = f"genome_{strategy_name.lower()}"
        extra = ("long_offgrid",) if case_name in HOLDING_PERIOD_CASES else ()
        cases.append(
            BaselineCase(
                name=case_name,
                make_strategy=lambda g=genome, n=strategy_name: CompositeStrategy(
                    genome=copy.deepcopy(g),
                    params=dict(REGISTRY_DEFAULT_PARAMS[n]),
                    symbol=SYMBOL,
                ),
                extra_scenarios=extra,
            )
        )

    return {case.name: case for case in cases}


BASELINE_CASES = _build_baseline_cases()
BASELINE_CASE_NAMES = sorted(BASELINE_CASES)


def _open_trade(frame: pd.DataFrame, *, action: OrderAction, entry_time: Any) -> Trade:
    return Trade(
        id="signal-baseline-trade",
        order_id="signal-baseline-order",
        symbol=SYMBOL,
        action=action,
        quantity=1.0,
        entry_time=pd.Timestamp(entry_time).to_pydatetime(),
        entry_price=float(frame.iloc[OPEN_BAR]["close"]),
    )


def _scenario_trades(name: str, frame: pd.DataFrame) -> list[Trade]:
    if name == "none":
        return []
    if name == "long":
        return [_open_trade(frame, action=OrderAction.BUY, entry_time=frame.index[OPEN_BAR])]
    if name == "short":
        return [_open_trade(frame, action=OrderAction.SELL, entry_time=frame.index[OPEN_BAR])]
    if name == "long_offgrid":
        entry = frame.index[OPEN_BAR] + pd.Timedelta(minutes=30)
        return [_open_trade(frame, action=OrderAction.BUY, entry_time=entry)]
    raise KeyError(f"unknown scenario {name!r}")


def _scenario_names(case: BaselineCase) -> tuple[str, ...]:
    names: list[str] = ["none", "long", "short"]
    names.extend(case.extra_scenarios)
    return tuple(names)


def _assert_legacy_bool_dtypes(frame: pd.DataFrame, *, case_name: str) -> None:
    for col in LEGACY_BOOL_TRIGGER_COLUMNS:
        if col not in frame.columns:
            continue
        assert pd.api.types.is_bool_dtype(frame[col]), (
            f"{case_name}: legacy trigger column {col!r} must be bool dtype, " f"got {frame[col].dtype!r}"
        )


def _serialize_exit(signal: Signal) -> list[Any]:
    return [signal.action.value, signal.symbol, signal.exit_reason]


def _serialize_entry(signal: Signal) -> list[Any]:
    return [signal.action.value, signal.symbol, signal.strength]


def _run_scenario(strategy: TradingStrategy, frame: pd.DataFrame, open_trades: list[Trade]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    signals = signal_arrays(strategy, frame)
    for i in range(len(frame)):
        row = frame.iloc[i]
        exits, entries = evaluate_queued_signals(strategy, signals, i, row, open_trades)
        if not exits and not entries:
            continue
        records.append(
            {
                "i": i,
                "exits": [_serialize_exit(sig) for sig in exits],
                "entries": [_serialize_entry(sig) for sig in entries],
            }
        )
    return records


def run_baseline_case(case: BaselineCase) -> dict[str, Any]:
    data = case.data()
    scenarios: dict[str, list[dict[str, Any]]] = {}
    for scenario in _scenario_names(case):
        strategy = case.make_strategy()
        frame = augment_indicator_frame(strategy, data)
        _assert_legacy_bool_dtypes(frame, case_name=case.name)
        open_trades = _scenario_trades(scenario, frame)
        scenarios[scenario] = _run_scenario(strategy, frame, open_trades)
    return {"case": case.name, "scenarios": scenarios}


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(_normalize(payload), indent=2, sort_keys=True) + "\n"


def _golden_path(name: str) -> Path:
    return SIGNALS_DIR / f"{name}.json"


def _diff(name: str, expected: str, actual: str) -> str:
    lines = difflib.unified_diff(
        expected.splitlines(),
        actual.splitlines(),
        fromfile=f"signals/{name}.json (committed)",
        tofile=f"signals/{name}.json (current)",
        lineterm="",
    )
    return "\n".join(lines)


def _assert_or_regen(name: str, payload: dict[str, Any], regen: bool) -> None:
    actual = _canonical_json(payload)
    path = _golden_path(name)
    if regen:
        SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        return
    assert path.exists(), (
        f"Missing signal baseline golden {path}. Generate it with:\n"
        f"  uv run pytest tests/backtesting/test_signal_baseline.py --regen-goldens"
    )
    expected = path.read_text()
    if expected != actual:
        raise AssertionError(
            f"Signal baseline mismatch for {name!r}. If this change is intended, "
            f"regenerate with --regen-goldens and justify the diff.\n\n" + _diff(name, expected, actual)
        )


@pytest.mark.parametrize("name", BASELINE_CASE_NAMES)
def test_signal_baseline(name: str, regen_goldens: bool) -> None:
    _assert_or_regen(name, run_baseline_case(BASELINE_CASES[name]), regen_goldens)


def test_signal_baseline_tamper_produces_readable_diff() -> None:
    """Mutating one entry strength must fail the golden comparison with a diff."""
    name = "macrossover"
    payload = run_baseline_case(BASELINE_CASES[name])
    found = False
    for records in payload["scenarios"].values():
        for record in records:
            if record["entries"]:
                record["entries"][0][2] = float(record["entries"][0][2]) + 0.01
                found = True
                break
        if found:
            break
    assert found, "tamper test needs a case with at least one entry signal"

    with pytest.raises(AssertionError) as excinfo:
        _assert_or_regen(name, payload, regen=False)
    message = str(excinfo.value)
    assert "Signal baseline mismatch" in message
    assert "0.01" in message or "1.01" in message or "0.99" in message
