"""Golden backtest regressions and backtest↔live parity lock-down (WO180).

Why this exists
---------------
The backtest engine's numbers are the foundation every other system
(optimization, discovery, research acceptance, paper trading) builds on. A subtle
change to indicator warm-up, exit-evaluation order, sizing, or cost handling would
pass the rest of the unit suite while silently shifting every result. This module
pins current behavior so drift becomes a loud, reviewable diff instead of silent
corruption.

What is pinned
--------------
For each canonical case a strategy config is run on committed-deterministic
synthetic OHLCV (fixed seed, generated in-test — no data files, no network) and the
*complete* output — every trade's entry/exit time, price, direction, size, pnl,
commission plus the summary metrics — is normalized to canonical JSON and compared
field-for-field against ``tests/backtesting/goldens/<case>.json``.

Regenerating goldens
--------------------
``uv run pytest tests/backtesting/test_goldens.py --regen-goldens`` rewrites the
files. Regeneration is a deliberate act: the diff shows up in git and must be
justified in the commit/WO message that regenerates it. Golden files are reviewed
like code.
"""

from __future__ import annotations

import copy
import difflib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
import pytest

import q_backend.backtesting.strategies  # noqa: F401 — register built-in strategies
from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.factory import build_composite_entry, build_strategy
from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.backtesting.models import OrderAction, Signal, Trade
from q_backend.backtesting.position_sizing import (
    FixedQuantityPositionSizing,
    FixedSafetyMarginPositionSizing,
    PositionSizer,
    build_position_sizer,
)
from q_backend.backtesting.registry import TradeRegistry
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.strategy_registry import default_params_for
from q_backend.backtesting.tick.strategy import TickArrays
from q_backend.execution.bars import bar_close_time
from q_backend.execution.domain import StrategyIdentity
from q_backend.execution.evaluator import StrategyEvaluator
from q_backend.execution.parity import reference_queued_signals_by_close, signals_equal
from q_backend.optimization.backtest_runner import BacktestRunConfig
from q_backend.optimization.tick_backtest_runner import TickBacktestRunner

GOLDENS_DIR = Path(__file__).parent / "goldens"

SYMBOL = "SYNTH"
TIMEFRAME = "H1"
INITIAL_CAPITAL = 100_000.0
POINT_VALUE = 1.0
FLOAT_DECIMALS = 10

# A fast MA crossover reused across many cases so crossovers (and therefore
# trades) occur reliably on the synthetic series.
BASE_MA_PARAMS: dict[str, Any] = {
    "short_period": 5,
    "long_period": 20,
    "threshold": 0.0,
}


# --------------------------------------------------------------------------- #
# Deterministic synthetic data (committed via the generator, not as a file).
# --------------------------------------------------------------------------- #
def synthetic_ohlcv(
    n: int = 400,
    *,
    seed: int = 20240609,
    freq: str = "h",
    start: str = "2023-01-02",
) -> pd.DataFrame:
    """A fixed-seed OHLCV frame. Identical bytes on every machine and run."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 1.0, size=n)
    close = 100.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.5, size=n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, size=n))
    volume = rng.integers(1_000, 5_000, size=n).astype(float)
    index = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def synthetic_ticks(
    n: int = 2_000,
    *,
    seed: int = 20240609,
    base_msc: int = 1_700_000_000_000,
    msc_per_tick: int = 40_000,
) -> TickArrays:
    """A fixed-seed intraday tick stream (one trading day) for the tick engine."""
    rng = np.random.default_rng(seed)
    trend = 100.0 + 8.0 * np.sin(np.linspace(0.0, 6.0 * np.pi, n))
    noise = np.cumsum(rng.normal(0.0, 0.02, size=n))
    last = (trend + noise).astype(np.float64)
    spread = 0.02
    time_msc = base_msc + np.arange(n, dtype=np.int64) * msc_per_tick
    return TickArrays(
        time_msc=time_msc,
        bid=(last - spread / 2).astype(np.float64),
        ask=(last + spread / 2).astype(np.float64),
        last=last,
        volume=np.ones(n, dtype=np.float64),
    )


# --------------------------------------------------------------------------- #
# Canonical-JSON normalization: sorted keys, ISO timestamps, rounded floats.
# --------------------------------------------------------------------------- #
def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, bool) or isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        rounded = round(float(value), FLOAT_DECIMALS)
        # Collapse -0.0 so equivalent runs never differ on sign of zero.
        return 0.0 if rounded == 0 else rounded
    if isinstance(value, (pd.Timestamp, datetime)):
        return _isoformat(value)
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"Unserializable golden value of type {type(value)!r}: {value!r}")


def _isoformat(value: datetime | pd.Timestamp) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(timezone.utc)
    return ts.tz_convert("UTC").isoformat()


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(_normalize(payload), indent=2, sort_keys=True) + "\n"


def _serialize_trade(trade: Trade) -> dict[str, Any]:
    """Stable, deterministic trade fields only.

    ``id``/``order_id`` are random UUIDs and ``created_at`` is wall-clock, so they
    are excluded — they carry no engine behavior and would break reproducibility.
    """
    return {
        "action": trade.action.value,
        "quantity": trade.quantity,
        "entry_time": trade.entry_time,
        "entry_price": trade.entry_price,
        "exit_time": trade.exit_time,
        "exit_price": trade.exit_price,
        "pnl": trade.pnl,
        "commission": trade.commission,
        "point_value": trade.point_value,
        "exit_reason": trade.exit_reason,
        "status": trade.status.value,
    }


def _serialize_registry(registry: TradeRegistry) -> list[dict[str, Any]]:
    trades = registry.get_all_trades()
    serialized = [_serialize_trade(t) for t in trades]
    # One entry per bar per symbol, so entry_time is a stable total order.
    serialized.sort(key=lambda t: (_isoformat(t["entry_time"]), t["action"], t["entry_price"]))
    return serialized


# --------------------------------------------------------------------------- #
# Case definitions.
# --------------------------------------------------------------------------- #
@dataclass
class CandleCase:
    """One canonical candle-engine golden case."""

    name: str
    surface: str
    make_strategy: Callable[[], TradingStrategy]
    make_sizer: Callable[[], PositionSizer] = field(
        default=lambda: build_position_sizer(FixedQuantityPositionSizing(quantity=1.0), point_value=POINT_VALUE)
    )
    n_bars: int = 400
    seed: int = 20240609
    parallel_mode: ParallelMode = ParallelMode.SEQUENTIAL
    day_trade: bool = False
    day_trade_start_time: str = "09:00"
    day_trade_end_time: str = "16:00"
    day_trade_close_time: str = "17:00"
    data_override: Callable[[pd.DataFrame], pd.DataFrame] | None = None

    def data(self) -> pd.DataFrame:
        frame = synthetic_ohlcv(self.n_bars, seed=self.seed)
        if self.data_override is not None:
            return self.data_override(frame)
        return frame


def _ma(params: dict[str, Any]) -> Callable[[], TradingStrategy]:
    return lambda: build_strategy("MACrossover", params, SYMBOL)


# A CompositeStrategy genome that gates a fast MA crossover with a session-window
# context feature (feature.session_window + logic.and). Exercises the genome
# interpreter and the context-feature path end-to-end.
CTX_GENOME: dict[str, Any] = {
    "version": 1,
    "genome_id": "golden-ctx-ma-session",
    "nodes": [
        {"id": "src", "kind": "source.close", "params": {}, "inputs": []},
        {"id": "ma_s", "kind": "ind.ma", "params": {"period": 5, "ma_type": "sma"}, "inputs": ["src"]},
        {"id": "ma_l", "kind": "ind.ma", "params": {"period": 20, "ma_type": "sma"}, "inputs": ["src"]},
        {"id": "diff", "kind": "ind.diff", "params": {}, "inputs": ["ma_s", "ma_l"]},
        {"id": "xup", "kind": "cmp.cross_above", "params": {"threshold": 0.0}, "inputs": ["diff"]},
        {"id": "xdn", "kind": "cmp.cross_below", "params": {"threshold": 0.0}, "inputs": ["diff"]},
        {
            "id": "win",
            "kind": "feature.session_window",
            "params": {"window_from": "08:00", "window_to": "20:00"},
            "inputs": [],
        },
        {"id": "long_gate", "kind": "logic.and", "params": {}, "inputs": ["xup", "win"]},
        {"id": "short_gate", "kind": "logic.and", "params": {}, "inputs": ["xdn", "win"]},
    ],
    "entry_long": {"ref": "long_gate"},
    "entry_short": {"ref": "short_gate"},
    "exit_long": {"ref": "xdn"},
    "exit_short": {"ref": "xup"},
    "metadata": {},
}


def _composite_entries_or_and() -> list[dict[str, Any]]:
    return [
        {"strategy": "MACrossover", "params": dict(BASE_MA_PARAMS)},
        {"strategy": "MACD", "params": default_params_for("MACD")},
    ]


CANDLE_CASES: dict[str, CandleCase] = {
    case.name: case
    for case in [
        CandleCase(
            name="ma_crossover_baseline",
            surface="Classic MA-crossover entry/exit, long+short, no exit rules — "
            "the engine's next-bar-open fill timing and cross-based exits.",
            make_strategy=_ma(dict(BASE_MA_PARAMS)),
        ),
        CandleCase(
            name="ma_crossover_fixed_stops",
            surface="Stop family (fixed_sl) + target family (fixed_tp): percent "
            "stop-loss and take-profit against the entry price.",
            make_strategy=_ma({**BASE_MA_PARAMS, "stop_loss_pct": 0.02, "take_profit_pct": 0.04}),
        ),
        CandleCase(
            name="ma_crossover_atr_exits",
            surface="Indicator-family exits (atr_sl + atr_tp): engine auto-computes "
            "the atr_N column the exit strategy requires.",
            make_strategy=_ma({**BASE_MA_PARAMS, "stop_loss_atr": 2.0, "take_profit_atr": 3.0, "atr_period": 14}),
        ),
        CandleCase(
            name="ma_crossover_trailing",
            surface="Trailing family (percent trailing stop): stateful in-trade " "extreme tracking across bars.",
            make_strategy=_ma({**BASE_MA_PARAMS, "trailing_stop_pct": 0.03}),
        ),
        CandleCase(
            name="ma_crossover_donchian_stop",
            surface="Trailing/indicator family (donchian channel stop): engine "
            "auto-computes donchian_high/low_N channel columns.",
            make_strategy=_ma({**BASE_MA_PARAMS, "donchian_exit_period": 20}),
        ),
        CandleCase(
            name="ma_crossover_time_stop",
            surface="Time family (max bars in trade): stateful bar counter forces " "the exit after N bars.",
            make_strategy=_ma({**BASE_MA_PARAMS, "max_bars_in_trade": 10}),
        ),
        CandleCase(
            name="rsi_mean_reversion_baseline",
            surface="A second registered strategy category (mean reversion) to pin "
            "the RSI entry/exit surface, long+short.",
            make_strategy=lambda: build_strategy(
                "RSIMeanReversion",
                {"period": 14, "oversold": 30.0, "overbought": 70.0},
                SYMBOL,
            ),
        ),
        CandleCase(
            name="composite_or_macd_macrossover",
            surface="Multi-entry OR composition (MACrossover OR MACD) via the signal " "manager.",
            make_strategy=lambda: build_composite_entry(_composite_entries_or_and(), "or", {}, {}, SYMBOL),
        ),
        CandleCase(
            name="composite_and_macd_macrossover",
            surface="Multi-entry AND composition (MACrossover AND MACD) via the " "signal manager.",
            make_strategy=lambda: build_composite_entry(_composite_entries_or_and(), "and", {}, {}, SYMBOL),
        ),
        CandleCase(
            name="composite_majority_three",
            surface="Multi-entry Majority composition (3 strategies, vote_threshold=2) " "via the signal manager.",
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
        CandleCase(
            name="genome_ma_session_gate",
            surface="CompositeStrategy genome with a context feature "
            "(feature.session_window gating an MA crossover via logic.and).",
            make_strategy=lambda: CompositeStrategy(genome=copy.deepcopy(CTX_GENOME), params={}, symbol=SYMBOL),
        ),
        CandleCase(
            name="ma_crossover_safety_margin_sizing",
            surface="Position-sizing variant: FixedSafetyMargin derives contract "
            "count from capital instead of a fixed quantity.",
            make_strategy=_ma(dict(BASE_MA_PARAMS)),
            make_sizer=lambda: build_position_sizer(
                FixedSafetyMarginPositionSizing(safety_margin_per_contract=5_000.0, min_contracts=1),
                point_value=POINT_VALUE,
            ),
        ),
        CandleCase(
            name="trb_fixed_holding",
            surface="TRB with a fixed holding-period exit (bars since entry), not "
            "exit-rule or flip-triggered closes.",
            make_strategy=lambda: build_strategy(
                "TRB",
                {"period": 20, "band_pct": 0.0, "holding_period": 10},
                SYMBOL,
            ),
        ),
        CandleCase(
            name="bollinger_band_exits",
            surface="Bollinger mean-reversion entries with band-cross exits "
            "(exit_long_signal / exit_short_signal columns).",
            make_strategy=lambda: build_strategy(
                "BollingerReversion",
                default_params_for("BollingerReversion"),
                SYMBOL,
            ),
        ),
        CandleCase(
            name="tsmom_trend_rebalance_every_bar_strength",
            surface="TSMOM trend rule with rebalance_on_every_bar and "
            "strength-scaled fixed quantity (signal_strength into size).",
            make_strategy=lambda: build_strategy(
                "TSMOM",
                {
                    "lookback_bars": 48,
                    "rebalance_bars": 12,
                    "vol_window": 20,
                    "vol_estimator": "close_to_close",
                    "trading_rule": "trend",
                    "trend_signal_cap": 2.0,
                    "nw_lags": 4,
                    "rebalance_on_every_bar": "true",
                },
                SYMBOL,
            ),
            make_sizer=lambda: build_position_sizer(
                FixedQuantityPositionSizing(quantity=10.0, scale_by_signal_strength=True),
                point_value=POINT_VALUE,
            ),
        ),
        CandleCase(
            name="hurst_rebalance_every_bar_strength",
            surface="HurstTrendBlend with rebalance_on_every_bar and " "strength-scaled fixed quantity.",
            make_strategy=lambda: build_strategy(
                "HurstTrendBlend",
                {
                    "lookback_1": 10,
                    "lookback_2": 21,
                    "lookback_3": 48,
                    "rebalance_bars": 12,
                    "vol_window": 20,
                    "vol_estimator": "close_to_close",
                    "risk_free_rate_annual": 0.0,
                    "signal_lag_bars": 0,
                    "rebalance_on_every_bar": "true",
                },
                SYMBOL,
            ),
            make_sizer=lambda: build_position_sizer(
                FixedQuantityPositionSizing(quantity=10.0, scale_by_signal_strength=True),
                point_value=POINT_VALUE,
            ),
        ),
        CandleCase(
            name="gatev_pairs",
            surface="GatevPairs distance strategy over synthetic pair columns "
            "(close_a/b, open_a/b) on the golden OHLCV frame.",
            make_strategy=lambda: build_strategy(
                "GatevPairs",
                {
                    "col_a": "close_a",
                    "col_b": "close_b",
                    "open_col_a": "open_a",
                    "open_col_b": "open_b",
                    "formation_bars": 40,
                    "trading_bars": 20,
                    "open_threshold_sd": 1.0,
                    "vol_window": 20,
                    "vol_estimator": "close_to_close",
                },
                SYMBOL,
            ),
            data_override=lambda data: data.assign(
                close_a=data["close"],
                # Independent walk so the spread crosses the formation SD threshold.
                close_b=data["close"].to_numpy()
                + np.cumsum(np.random.default_rng(99).normal(0.0, 0.5, size=len(data))),
                open_a=data["open"],
                open_b=data["open"].to_numpy() + np.cumsum(np.random.default_rng(100).normal(0.0, 0.5, size=len(data))),
            ),
        ),
        CandleCase(
            name="ma_crossover_day_trade",
            surface="MA-crossover through the day-trade path (ParallelMode.DAY_TRADE " "with end-of-day force closes).",
            make_strategy=_ma(dict(BASE_MA_PARAMS)),
            parallel_mode=ParallelMode.DAY_TRADE,
            day_trade=True,
            # Hourly synthetic bars span 00–23 UTC; widen the session so entries fire.
            day_trade_start_time="00:00",
            day_trade_end_time="23:00",
            day_trade_close_time="23:30",
        ),
    ]
}

TICK_CASE_NAME = "tick_ma_breakout"
ALL_CASE_NAMES = list(CANDLE_CASES) + [TICK_CASE_NAME]


# --------------------------------------------------------------------------- #
# Running a case → normalized output payload.
# --------------------------------------------------------------------------- #
def run_candle_case(case: CandleCase) -> dict[str, Any]:
    strategy = case.make_strategy()
    sizer = case.make_sizer()
    engine = BacktestEngine(
        strategy,
        sizer,
        initial_capital=INITIAL_CAPITAL,
        point_values={SYMBOL: POINT_VALUE},
        day_trade=case.day_trade,
        day_trade_start_time=case.day_trade_start_time,
        day_trade_end_time=case.day_trade_end_time,
        day_trade_close_time=case.day_trade_close_time,
    )
    registry = engine.run(case.data(), parallel_mode=case.parallel_mode)
    trades = _serialize_registry(registry)
    return {
        "case": case.name,
        "engine": "candle",
        "surface": case.surface,
        "trade_count": len(trades),
        "trades": trades,
        "metrics": registry.get_performance_metrics(INITIAL_CAPITAL),
    }


def run_tick_case() -> dict[str, Any]:
    ticks = synthetic_ticks()
    start = datetime(2023, 11, 14, tzinfo=timezone.utc)
    end = datetime(2023, 11, 15, tzinfo=timezone.utc)
    config = BacktestRunConfig(
        symbol=SYMBOL,
        timeframe="TICK",
        start=start,
        end=end,
        initial_capital=INITIAL_CAPITAL,
        point_value=POINT_VALUE,
        strategy="TickMaBreakout",
        strategy_params={
            "short_period": 20,
            "long_period": 50,
            "threshold": 0.0,
            "sl_points": 0.0,
            "tp_points": 0.0,
        },
        position_sizing=FixedQuantityPositionSizing(quantity=1.0),
        engine="tick",
    )
    result = TickBacktestRunner(ticks).run(config)
    return {
        "case": TICK_CASE_NAME,
        "engine": "tick",
        "surface": "Tick engine through tick_backtest_runner: TickMaBreakout over a "
        "fixed synthetic tick stream. Pins the tick pipeline's summary metrics.",
        "metrics": result.metrics,
    }


def run_named_case(name: str) -> dict[str, Any]:
    if name == TICK_CASE_NAME:
        return run_tick_case()
    return run_candle_case(CANDLE_CASES[name])


# --------------------------------------------------------------------------- #
# Golden read/compare/write.
# --------------------------------------------------------------------------- #
def _golden_path(name: str) -> Path:
    return GOLDENS_DIR / f"{name}.json"


def _diff(name: str, expected: str, actual: str) -> str:
    lines = difflib.unified_diff(
        expected.splitlines(),
        actual.splitlines(),
        fromfile=f"{name}.json (committed)",
        tofile=f"{name}.json (current)",
        lineterm="",
    )
    return "\n".join(lines)


def _assert_or_regen(name: str, payload: dict[str, Any], regen: bool) -> None:
    actual = _canonical_json(payload)
    path = _golden_path(name)
    if regen:
        GOLDENS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        return
    assert path.exists(), (
        f"Missing golden {path}. Generate it with:\n"
        f"  uv run pytest tests/backtesting/test_goldens.py --regen-goldens"
    )
    expected = path.read_text()
    if expected != actual:
        raise AssertionError(
            f"Golden mismatch for {name!r}. The engine output no longer matches the "
            f"committed golden. If this change is intended, regenerate with "
            f"--regen-goldens and justify the diff in your commit/WO message.\n\n" + _diff(name, expected, actual)
        )


# --------------------------------------------------------------------------- #
# Tests.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ALL_CASE_NAMES)
def test_golden(name: str, regen_goldens: bool) -> None:
    _assert_or_regen(name, run_named_case(name), regen_goldens)


@pytest.mark.parametrize("name", ALL_CASE_NAMES)
def test_determinism_double_run(name: str) -> None:
    """Same case run twice in-process yields byte-identical normalized output."""
    first = _canonical_json(run_named_case(name))
    second = _canonical_json(run_named_case(name))
    assert first == second, f"Nondeterministic output for {name!r}"


def test_tamper_produces_readable_diff() -> None:
    """Mutating one trade field must make the golden comparison fail with a diff."""
    name = "ma_crossover_baseline"
    payload = run_named_case(name)
    assert payload["trade_count"] > 0, "tamper test needs a case that trades"

    tampered = copy.deepcopy(payload)
    tampered["trades"][0]["pnl"] = (tampered["trades"][0]["pnl"] or 0.0) + 1.0

    with pytest.raises(AssertionError) as excinfo:
        _assert_or_regen(name, tampered, regen=False)
    message = str(excinfo.value)
    assert "Golden mismatch" in message
    assert '"pnl"' in message  # the diff points at the mutated field


@pytest.mark.parametrize("name", list(CANDLE_CASES))
def test_candle_cases_actually_trade(name: str) -> None:
    """Guard against a silently empty golden: every candle case must trade."""
    payload = run_candle_case(CANDLE_CASES[name])
    assert payload["trade_count"] > 0, f"{name!r} produced no trades — golden is vacuous"


# --------------------------------------------------------------------------- #
# Backtest ↔ live parity, extended from hand-picked cases to the golden registry.
#
# For every golden candle strategy we drive the same synthetic frame bar-by-bar
# through the forward ``StrategyEvaluator`` and assert its queued entry/exit signals
# match the backtest engine's section-D reference extractor at every bar close —
# with and without an open position (the latter exercises the stateful exit rules).
# --------------------------------------------------------------------------- #
def _parity_identity(name: str) -> StrategyIdentity:
    return StrategyIdentity(
        strategy_name=name,
        strategy_version=1,
        compiled_config={},
        config_hash="golden-parity",
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
    )


def _signals_from_result(result) -> tuple[list[Signal], list[Signal]]:
    exits = [Signal.model_validate(s) for s in result.queued_exit_signals]
    entries = [Signal.model_validate(s) for s in result.queued_entry_signals]
    return exits, entries


@pytest.mark.parametrize("name", list(CANDLE_CASES))
@pytest.mark.parametrize("with_open_trade", [False, True])
def test_backtest_live_parity(name: str, with_open_trade: bool) -> None:
    case = CANDLE_CASES[name]
    data = case.data()

    open_trade: Trade | None = None
    if with_open_trade:
        entry_idx = 40
        open_trade = Trade(
            id="golden-parity-trade",
            order_id="golden-parity-order",
            symbol=SYMBOL,
            action=OrderAction.BUY,
            quantity=1.0,
            entry_time=data.index[entry_idx].to_pydatetime(),
            entry_price=float(data.iloc[entry_idx]["close"]),
        )

    reference = reference_queued_signals_by_close(
        case.make_strategy(),
        data,
        timeframe=TIMEFRAME,
        open_trade=copy.deepcopy(open_trade) if open_trade else None,
    )
    ref_by_close = {close: (exits, entries) for close, exits, entries in reference}

    evaluator = StrategyEvaluator(
        deployment_id="golden-parity",
        identity=_parity_identity(name),
        strategy=case.make_strategy(),
        window_bound=len(data),
    )
    if open_trade is not None:
        evaluator.set_open_trade(copy.deepcopy(open_trade))
    results = evaluator.ingest_completed_bars(data)

    assert len(results) == len(data)
    for result in results:
        assert result.bar_close_time in ref_by_close, f"{name}: no reference row for close {result.bar_close_time}"
        ref_exits, ref_entries = ref_by_close[result.bar_close_time]
        fwd_exits, fwd_entries = _signals_from_result(result)
        assert signals_equal(ref_exits, fwd_exits), (
            f"{name}: exit-signal parity mismatch at {result.bar_close_time} " f"(open_trade={with_open_trade})"
        )
        assert signals_equal(ref_entries, fwd_entries), (
            f"{name}: entry-signal parity mismatch at {result.bar_close_time} " f"(open_trade={with_open_trade})"
        )
