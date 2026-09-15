"""CompositeStrategy — vectorized genome DSL interpreter."""

from __future__ import annotations

from typing import Any, List

import numpy as np
import pandas as pd

from q_backend.backtesting.exit_strategy import ExitStrategy
from q_backend.backtesting.genome.compile import ExecutionPlan, compile_genome
from q_backend.backtesting.genome.feature_nodes import evaluate_feature_node
from q_backend.backtesting.genome.exit_rule_policy import (
    get_exit_rule_policy,
    resolve_exit_params_from_policy,
)
from q_backend.backtesting.genome.schema import Genome
from q_backend.backtesting.moving_averages import compute_ma, normalize_ma_type
from q_backend.backtesting.signal_columns import write_signal_columns
from q_backend.backtesting.strategies.lai_lau_common import (
    add_bar_index,
    compute_trb_channel_signals,
)
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy
from q_backend.backtesting.strategy_registry import register_strategy
from q_backend.backtesting.session_context import prepare_evaluation_frame
from q_backend.market_data.exogenous_columns import resolve_exog_column
from q_backend.storage.lake.artifacts import read_neural_model
from q_backend.backtesting.technical_indicators import (
    compute_atr,
    compute_bollinger_bands,
    compute_donchian_channels,
    compute_macd,
    compute_realized_vol,
    compute_rsi,
    compute_yang_zhang,
)
from q_backend.backtesting.transforms import (
    compute_clip,
    compute_pct_change,
    compute_rolling_rank,
    compute_rolling_zscore,
)

DEFAULT_MA_CROSSOVER_GENOME: dict[str, Any] = {
    "version": 1,
    "genome_id": "default-ma-crossover",
    "nodes": [
        {"id": "n1", "kind": "source.close", "params": {}, "inputs": []},
        {
            "id": "n2",
            "kind": "ind.ma",
            "params": {
                "period": {"param": "short_period"},
                "ma_type": {"param": "short_ma_type"},
            },
            "inputs": ["n1"],
        },
        {
            "id": "n3",
            "kind": "ind.ma",
            "params": {
                "period": {"param": "long_period"},
                "ma_type": {"param": "long_ma_type"},
            },
            "inputs": ["n1"],
        },
        {"id": "n4", "kind": "ind.diff", "params": {}, "inputs": ["n2", "n3"]},
        {
            "id": "n5",
            "kind": "cmp.cross_above",
            "params": {"threshold": {"param": "threshold"}},
            "inputs": ["n4"],
        },
        {
            "id": "n6",
            "kind": "cmp.cross_below",
            "params": {"threshold": {"param": "threshold", "negate": True}},
            "inputs": ["n4"],
        },
    ],
    "entry_long": {"ref": "n5"},
    "entry_short": {"ref": "n6"},
    "exit_long": {"ref": "n6"},
    "exit_short": {"ref": "n5"},
    "metadata": {"equivalent_registry": "MACrossover"},
}


class CompositeStrategy(TradingStrategy):
    """Interpret a validated genome under the standard strategy contract."""

    def __init__(
        self,
        genome: Genome | dict[str, Any],
        params: dict[str, Any] | None = None,
        symbol: str = "BTCUSDT",
        *,
        timeframe: str | None = None,
        latent_model_hash: str | None = None,
        **kwargs: Any,
    ) -> None:
        if isinstance(genome, dict):
            genome = Genome.model_validate(genome)
        self.genome = genome
        self.trial_params = dict(params or {})
        self.symbol = symbol
        self.timeframe = timeframe
        self._latent_model_hash = latent_model_hash
        self._exit_rule_policy = get_exit_rule_policy(genome)
        self._plan: ExecutionPlan | None = None
        self._series_cache: dict[str, dict[str, pd.Series]] = {}
        self._latent_frame_cache: pd.DataFrame | None = None
        self._latent_warmup: int | None = None
        self._latent_train_end: Any = None
        self._last_data_len: int | None = None
        self._compiled_params_key: tuple[tuple[str, Any], ...] | None = None
        super().__init__(genome=genome.model_dump(), params=self.trial_params, symbol=symbol, **kwargs)
        self._refresh_exit_strategy()

    def _refresh_exit_strategy(self) -> None:
        if self._exit_rule_policy is None:
            self.exit_strategy = ExitStrategy(self.trial_params)
            return
        runtime_params = resolve_exit_params_from_policy(
            self._exit_rule_policy,
            self.trial_params,
        )
        self.exit_strategy = ExitStrategy(runtime_params)

    @property
    def plan(self) -> ExecutionPlan:
        if self._plan is None:
            self._plan = compile_genome(self.genome, self.trial_params)
        return self._plan

    @property
    def holding_period_bars(self) -> int | None:
        return self.plan.fixed_holding_period

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        if any(node.kind.startswith("feature.") for node in self.genome.nodes):
            df = prepare_evaluation_frame(df, timeframe=self.timeframe)
        params_key = tuple(sorted(self.trial_params.items()))
        if params_key != self._compiled_params_key:
            self._plan = None
            self._compiled_params_key = params_key
            self._refresh_exit_strategy()

        data_len = len(df)
        if data_len != self._last_data_len:
            self._series_cache.clear()
            self._latent_frame_cache = None
            self._latent_warmup = None
            self._last_data_len = data_len

        plan = self.plan

        uses_bar_index = (
            any(compiled.node.kind in ("exit.fixed_holding", "ind.tsmom") for compiled in plan.sorted_nodes)
            or plan.fixed_holding_period is not None
        )
        if uses_bar_index:
            df = add_bar_index(df)

        for compiled in plan.sorted_nodes:
            if compiled.cache_key in self._series_cache:
                cached = self._series_cache[compiled.cache_key]
                for port, col in compiled.column_by_port.items():
                    df[col] = cached[port]
                continue

            self._evaluate_node(df, compiled)
            if compiled.node.kind.startswith("exit.") and compiled.node.kind != "exit.middle_band":
                continue
            self._series_cache[compiled.cache_key] = {
                port: df[col].copy() for port, col in compiled.column_by_port.items()
            }
        df["entry_long_signal"] = df[plan.entry_long_column].fillna(False).astype(bool)
        df["entry_short_signal"] = df[plan.entry_short_column].fillna(False).astype(bool)
        df["buy_signal"] = df["entry_long_signal"]
        df["sell_signal"] = df["entry_short_signal"]

        if plan.exit_long_policy == "middle_band" and plan.exit_long_middle_columns:
            df["exit_long_signal"] = df[plan.exit_long_middle_columns[0]].fillna(False).astype(bool)
            df["exit_short_signal"] = df[plan.exit_long_middle_columns[1]].fillna(False).astype(bool)
        else:
            if plan.exit_long_column:
                df["exit_long_signal"] = df[plan.exit_long_column].fillna(False).astype(bool)
            if plan.exit_short_column:
                df["exit_short_signal"] = df[plan.exit_short_column].fillna(False).astype(bool)

        if plan.fixed_holding_period is not None:
            exit_long: pd.Series | bool = False
            exit_short: pd.Series | bool = False
        else:
            if "exit_long_signal" in df.columns:
                exit_long = df["exit_long_signal"]
            else:
                exit_long = False
            if "exit_short_signal" in df.columns:
                exit_short = df["exit_short_signal"]
            else:
                exit_short = False

        return write_signal_columns(
            df,
            entry_long=df["entry_long_signal"],
            entry_short=df["entry_short_signal"],
            exit_long=exit_long,
            exit_short=exit_short,
            strategy_name=type(self).__name__,
        )

    def _binding_series(self, df: pd.DataFrame, compiled: Any, index: int) -> pd.Series:
        binding = compiled.input_bindings[index]
        return df[binding.column]

    def _bars_for_latent_compute(self, df: pd.DataFrame) -> pd.DataFrame:
        working = df.copy()
        if "time" not in working.columns:
            working = working.reset_index()
            if "time" not in working.columns:
                working = working.rename(columns={working.columns[0]: "time"})
        bars = working[["time", "open", "high", "low", "close", "volume"]].copy()
        return bars.sort_values("time").reset_index(drop=True)

    def _get_cached_latent_frame(self, bars: pd.DataFrame) -> pd.DataFrame:
        if self._latent_frame_cache is not None and len(self._latent_frame_cache) == len(bars):
            return self._latent_frame_cache
        assert self._latent_model_hash is not None
        from q_backend.features.compute import _get_or_compute_latent_frame

        frame = _get_or_compute_latent_frame(self._latent_model_hash, bars)
        self._latent_frame_cache = frame
        self._latent_warmup = None  # recompute warmup against the new window
        return frame

    def _get_latent_train_end(self) -> Any:
        """``train_end`` for the production encoder — read once per run, not per node."""
        if self._latent_train_end is None:
            assert self._latent_model_hash is not None
            self._latent_train_end = read_neural_model(self._latent_model_hash).config.train_end
        return self._latent_train_end

    def _evaluate_node(self, df: pd.DataFrame, compiled: Any) -> None:
        kind = compiled.node.kind
        params = compiled.resolved_params
        cols = compiled.column_by_port

        if kind == "source.close":
            df[cols["out"]] = df["close"]
        elif kind == "source.high":
            df[cols["out"]] = df["high"]
        elif kind == "source.low":
            df[cols["out"]] = df["low"]
        elif kind == "source.open":
            df[cols["out"]] = df["open"]
        elif kind == "source.volume":
            df[cols["out"]] = df["volume"]
        elif kind == "source.exog.close":
            df[cols["out"]] = resolve_exog_column(df, str(params["symbol"]), "close")
        elif kind == "source.exog.return":
            lookback = int(params["lookback_bars"])
            df[cols["out"]] = resolve_exog_column(df, str(params["symbol"]), f"return_{lookback}")
        elif kind == "source.exog.return_zscore":
            lookback = int(params["lookback_bars"])
            window = int(params["window"])
            df[cols["out"]] = resolve_exog_column(
                df,
                str(params["symbol"]),
                f"return_zscore_{lookback}_{window}",
            )
        elif kind == "source.exog.rolling_corr":
            window = int(params["window"])
            df[cols["out"]] = resolve_exog_column(df, str(params["symbol"]), f"rolling_corr_{window}")
        elif kind == "source.exog.relative_strength":
            lookback = int(params["lookback_bars"])
            df[cols["out"]] = resolve_exog_column(df, str(params["symbol"]), f"relative_strength_{lookback}")
        elif kind == "source.exog.vol_regime":
            vol_window = int(params["vol_window"])
            df[cols["out"]] = resolve_exog_column(df, str(params["symbol"]), f"vol_regime_{vol_window}").astype(bool)
        elif kind == "source.exog.direction_regime":
            lookback = int(params["lookback_bars"])
            df[cols["out"]] = resolve_exog_column(df, str(params["symbol"]), f"direction_regime_{lookback}").astype(
                bool
            )
        elif kind == "ind.ma":
            source = self._binding_series(df, compiled, 0)
            df[cols["out"]] = compute_ma(source, int(params["period"]), normalize_ma_type(str(params["ma_type"])))
        elif kind == "ind.ema":
            source = self._binding_series(df, compiled, 0)
            df[cols["out"]] = compute_ma(source, int(params["period"]), "ema")
        elif kind == "ind.rsi":
            source = self._binding_series(df, compiled, 0)
            df[cols["out"]] = compute_rsi(source, int(params["period"]))
        elif kind == "ind.atr":
            df[cols["out"]] = compute_atr(df["high"], df["low"], df["close"], int(params["period"]))
        elif kind == "ind.macd":
            source = self._binding_series(df, compiled, 0)
            macd_line, signal_line, histogram = compute_macd(
                source,
                int(params["fast_period"]),
                int(params["slow_period"]),
                int(params["signal_period"]),
            )
            df[cols["macd"]] = macd_line
            df[cols["macd_signal"]] = signal_line
            df[cols["macd_histogram"]] = histogram
        elif kind == "ind.bollinger":
            source = self._binding_series(df, compiled, 0)
            upper, middle, lower = compute_bollinger_bands(source, int(params["period"]), float(params["num_std"]))
            df[cols["bb_upper"]] = upper
            df[cols["bb_middle"]] = middle
            df[cols["bb_lower"]] = lower
        elif kind == "ind.donchian":
            upper, lower = compute_donchian_channels(df["high"], df["low"], int(params["period"]))
            df[cols["donchian_upper"]] = upper
            df[cols["donchian_lower"]] = lower
        elif kind == "ind.momentum":
            source = self._binding_series(df, compiled, 0)
            lookback = int(params["lookback_bars"])
            df[cols["out"]] = source / source.shift(lookback) - 1.0
        elif kind == "ind.realized_vol":
            source = self._binding_series(df, compiled, 0)
            window = int(params["window"])
            estimator = str(params.get("estimator", "close_to_close"))
            if estimator == "yang_zhang" and all(c in df.columns for c in ("open", "high", "low", "close")):
                df[cols["out"]] = compute_yang_zhang(df["open"], df["high"], df["low"], df["close"], window)
            else:
                df[cols["out"]] = compute_realized_vol(source, window)
        elif kind == "ind.diff":
            left = self._binding_series(df, compiled, 0)
            right = self._binding_series(df, compiled, 1)
            df[cols["out"]] = left - right
        elif kind == "ind.ratio":
            left = self._binding_series(df, compiled, 0)
            right = self._binding_series(df, compiled, 1)
            df[cols["out"]] = left / right - 1.0
        elif kind == "ind.trb_channel":
            close = self._binding_series(df, compiled, 0)
            tmp = pd.DataFrame({"close": close}, index=df.index)
            tmp = compute_trb_channel_signals(tmp, int(params["period"]), float(params["band_pct"]))
            for port in ("channel_high", "channel_low", "trb_upper", "trb_lower"):
                df[cols[port]] = tmp[port]
        elif kind == "ind.ma_band":
            ma = self._binding_series(df, compiled, 0)
            band_pct = float(params["band_pct"])
            upper = ma * (1.0 + band_pct / 100.0)
            lower = ma * (1.0 - band_pct / 100.0)
            df[cols["ma_band_upper"]] = upper
            df[cols["ma_band_lower"]] = lower
        elif kind == "ind.tsmom":
            self._evaluate_tsmom(df, compiled, params)
        elif kind == "ind.trend_blend":
            source = self._binding_series(df, compiled, 0)
            l1 = int(params["lookback_1"])
            l2 = int(params["lookback_2"])
            l3 = int(params["lookback_3"])
            vol_w = int(params["vol_window"])

            ret1 = source / source.shift(l1) - 1.0
            ret2 = source / source.shift(l2) - 1.0
            ret3 = source / source.shift(l3) - 1.0

            sig1 = np.where(ret1.isna(), np.nan, np.sign(ret1))
            sig2 = np.where(ret2.isna(), np.nan, np.sign(ret2))
            sig3 = np.where(ret3.isna(), np.nan, np.sign(ret3))

            df[cols["out"]] = (pd.Series(sig1, index=df.index) + sig2 + sig3) / 3.0
            df[cols["volatility"]] = compute_realized_vol(source, vol_w)
        elif kind == "ind.latent":
            if self._latent_model_hash is None:
                df[cols["out"]] = np.nan
            else:
                from q_backend.features.compute import _apply_warmup, _oos_warmup_bars

                bars = self._bars_for_latent_compute(df)
                latent_frame = self._get_cached_latent_frame(bars)
                latent_index = int(params.get("latent_index", 0))
                n_latents = len(latent_frame.columns)
                idx = min(max(0, latent_index), n_latents - 1)
                col_name = latent_frame.columns[idx]
                if self._latent_warmup is None:
                    self._latent_warmup = _oos_warmup_bars(bars["time"], self._get_latent_train_end())
                raw = latent_frame[col_name].reset_index(drop=True)
                values = _apply_warmup(raw, self._latent_warmup)
                by_time = pd.Series(values.to_numpy(), index=bars["time"])
                if "time" in df.columns:
                    df[cols["out"]] = df["time"].map(by_time).to_numpy()
                else:
                    df[cols["out"]] = pd.Series(df.index).map(by_time).to_numpy()
        elif kind == "transform.shift":
            source = self._binding_series(df, compiled, 0)
            df[cols["out"]] = source.shift(int(params.get("bars", 1)))
        elif kind == "transform.abs":
            source = self._binding_series(df, compiled, 0)
            df[cols["out"]] = source.abs()
        elif kind == "transform.scale":
            source = self._binding_series(df, compiled, 0)
            df[cols["out"]] = source * float(params["factor"])
        elif kind == "transform.zscore":
            source = self._binding_series(df, compiled, 0)
            df[cols["out"]] = compute_rolling_zscore(source, int(params["window"]))
        elif kind == "transform.rank":
            source = self._binding_series(df, compiled, 0)
            df[cols["out"]] = compute_rolling_rank(source, int(params["window"]))
        elif kind == "transform.pct_change":
            source = self._binding_series(df, compiled, 0)
            df[cols["out"]] = compute_pct_change(source, int(params["change_bars"]))
        elif kind == "transform.clip":
            source = self._binding_series(df, compiled, 0)
            df[cols["out"]] = compute_clip(
                source,
                float(params["clip_low"]),
                float(params["clip_high"]),
            )
        elif kind.startswith("feature."):
            evaluate_feature_node(
                df,
                kind=kind,
                params=params,
                cols=cols,
                binding_series=lambda frame, index: self._binding_series(frame, compiled, index),
            )
        elif kind.startswith("cmp.") or kind.startswith("logic."):
            self._evaluate_bool_node(df, compiled)
        elif kind == "exit.middle_band":
            close = self._binding_series(df, compiled, 0)
            middle = self._binding_series(df, compiled, 1)
            prev_close = close.shift(1)
            prev_middle = middle.shift(1)
            df[cols["exit_long"]] = (prev_close <= prev_middle) & (close > middle)
            df[cols["exit_short"]] = (prev_close >= prev_middle) & (close < middle)
        elif kind.startswith("exit."):
            return
        else:
            raise ValueError(f"Unsupported node kind '{kind}'.")

    def _evaluate_tsmom(self, df: pd.DataFrame, compiled: Any, params: dict[str, Any]) -> None:
        lookback = int(params["lookback_bars"])
        rebalance_bars = int(params["rebalance_bars"])
        vol_window = int(params["vol_window"])
        vol_estimator = str(params["vol_estimator"])

        momentum = df["close"] / df["close"].shift(lookback) - 1.0
        if vol_estimator == "yang_zhang" and all(c in df.columns for c in ("open", "high", "low", "close")):
            volatility = compute_yang_zhang(df["open"], df["high"], df["low"], df["close"], vol_window)
        else:
            volatility = compute_realized_vol(df["close"], vol_window)

        bar_index = np.arange(len(df))
        rebalance = bar_index % rebalance_bars == 0
        eval_sign = np.where(momentum.isna(), np.nan, np.sign(momentum.to_numpy()))
        rebalance_index = df.index[rebalance]
        rebalance_signs = pd.Series(eval_sign[rebalance], index=rebalance_index)
        prev_rebalance_sign = rebalance_signs.shift(1).fillna(0)
        prev_sign = pd.Series(0, index=df.index, dtype=float)
        prev_sign.loc[rebalance_index] = prev_rebalance_sign.to_numpy()

        buy = rebalance & (momentum > 0) & (prev_sign <= 0) & momentum.notna()
        sell = rebalance & (momentum < 0) & (prev_sign >= 0) & momentum.notna()

        cols = compiled.column_by_port
        df[cols["momentum"]] = momentum
        df[cols["volatility"]] = volatility
        df[cols["buy_signal"]] = buy
        df[cols["sell_signal"]] = sell

    def _evaluate_bool_node(self, df: pd.DataFrame, compiled: Any) -> None:
        kind = compiled.node.kind
        params = compiled.resolved_params
        out_col = compiled.column_by_port["out"]

        if kind == "logic.and":
            left = self._binding_series(df, compiled, 0)
            right = self._binding_series(df, compiled, 1)
            df[out_col] = left & right
        elif kind == "logic.or":
            left = self._binding_series(df, compiled, 0)
            right = self._binding_series(df, compiled, 1)
            df[out_col] = left | right
        elif kind == "logic.not":
            source = self._binding_series(df, compiled, 0)
            df[out_col] = ~source.fillna(False)
        elif kind == "cmp.gt":
            left = self._binding_series(df, compiled, 0)
            right = self._binding_series(df, compiled, 1)
            df[out_col] = left > right
        elif kind == "cmp.lt":
            left = self._binding_series(df, compiled, 0)
            right = self._binding_series(df, compiled, 1)
            df[out_col] = left < right
        elif kind == "cmp.gte":
            left = self._binding_series(df, compiled, 0)
            right = self._binding_series(df, compiled, 1)
            df[out_col] = left >= right
        elif kind == "cmp.lte":
            left = self._binding_series(df, compiled, 0)
            right = self._binding_series(df, compiled, 1)
            df[out_col] = left <= right
        elif kind == "cmp.cross_above":
            if len(compiled.input_bindings) == 1:
                series = self._binding_series(df, compiled, 0)
                threshold = float(params.get("threshold", 0.0))
                prev = series.shift(1)
                df[out_col] = (series > threshold) & (prev <= threshold)
            else:
                left = self._binding_series(df, compiled, 0)
                right = self._binding_series(df, compiled, 1)
                df[out_col] = (left > right) & (left.shift(1) <= right.shift(1))
        elif kind == "cmp.cross_below":
            if len(compiled.input_bindings) == 1:
                series = self._binding_series(df, compiled, 0)
                threshold = float(params.get("threshold", 0.0))
                prev = series.shift(1)
                df[out_col] = (series < threshold) & (prev >= threshold)
            else:
                left = self._binding_series(df, compiled, 0)
                right = self._binding_series(df, compiled, 1)
                df[out_col] = (left < right) & (left.shift(1) >= right.shift(1))
        elif kind == "cmp.touch_below":
            close = self._binding_series(df, compiled, 0)
            band = self._binding_series(df, compiled, 1)
            df[out_col] = (close.shift(1) >= band.shift(1)) & (close < band)
        elif kind == "cmp.touch_above":
            close = self._binding_series(df, compiled, 0)
            band = self._binding_series(df, compiled, 1)
            df[out_col] = (close.shift(1) <= band.shift(1)) & (close > band)
        elif kind == "cmp.trb_breakout_above":
            close = self._binding_series(df, compiled, 0)
            upper = self._binding_series(df, compiled, 1)
            channel_high = self._binding_series(df, compiled, 2)
            df[out_col] = (close.shift(1) <= channel_high) & (close > upper)
        elif kind == "cmp.trb_breakout_below":
            close = self._binding_series(df, compiled, 0)
            lower = self._binding_series(df, compiled, 1)
            channel_low = self._binding_series(df, compiled, 2)
            df[out_col] = (close.shift(1) >= channel_low) & (close < lower)

    def get_chart_indicators(self) -> List[ChartIndicatorSpec]:
        specs: List[ChartIndicatorSpec] = []
        for compiled in self.plan.sorted_nodes:
            kind = compiled.node.kind
            if kind.startswith("source.") or kind.startswith("exit."):
                continue
            for port, col in compiled.column_by_port.items():
                if port.endswith("_signal"):
                    continue
                pane = (
                    "oscillator"
                    if kind
                    in (
                        "ind.rsi",
                        "ind.macd",
                        "ind.momentum",
                        "ind.realized_vol",
                        "ind.tsmom",
                        "ind.latent",
                    )
                    else "price"
                )
                specs.append(
                    ChartIndicatorSpec(
                        key=col,
                        label=f"{compiled.node.id}:{port}",
                        pane=pane,
                    )
                )
        return specs


def _build_composite(params: dict[str, Any], symbol: str) -> CompositeStrategy:
    genome = params.get("genome", DEFAULT_MA_CROSSOVER_GENOME)
    reserved = frozenset({"genome", "latent_model_hash", "timeframe"})
    trial_params = {k: v for k, v in params.items() if k not in reserved}
    return CompositeStrategy(
        genome=genome,
        params=trial_params,
        symbol=symbol,
        timeframe=params.get("timeframe"),
        latent_model_hash=params.get("latent_model_hash"),
    )


register_strategy(
    name="CompositeStrategy",
    label="Evolved composite",
    description="Interpreted genome DSL (genetic search only).",
    params=[],
    build=_build_composite,
    strategy_class=CompositeStrategy,
    category="other",
)
