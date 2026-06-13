from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional, Protocol

import pandas as pd

from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.models import Trade
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.position_sizing import (
    PositionSizingConfig,
    build_position_sizer,
)
from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.market_data.clients.metatrader import _to_naive_local
from q_backend.optimization.metrics import build_equity_curve, compute_extended_metrics


@dataclass
class BacktestRunConfig:
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    initial_capital: float
    point_value: float
    strategy: str
    strategy_params: dict[str, Any]
    position_sizing: PositionSizingConfig | None
    costs: TransactionCostConfig | None = None
    parallel_mode: ParallelMode = ParallelMode.SEQUENTIAL
    day_trade: bool = False
    day_trade_start_time: str = "09:00"
    day_trade_end_time: str = "16:00"
    day_trade_close_time: str = "17:00"
    engine: Literal["candle", "tick"] = "candle"
    display_timeframe: str = "M1"
    tick_flags: Optional[str] = None


@dataclass
class BacktestRunResult:
    metrics: dict[str, Any]
    trial_user_attrs: dict[str, Any] = field(default_factory=dict)
    trades: list[Trade] | None = None


class BacktestRunner(Protocol):
    def run(self, config: BacktestRunConfig) -> BacktestRunResult: ...


class DefaultBacktestRunner:
    def __init__(
        self,
        data_provider: Callable[[BacktestRunConfig], pd.DataFrame] | None = None,
        market_data_service: Any | None = None,
    ):
        self._data_provider = data_provider
        self._market_data_service = market_data_service

    @classmethod
    def from_market_data(
        cls,
        market_data_service: Any,
        *,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> "DefaultBacktestRunner":
        """Fetch OHLCV once and reuse it for every trial in a study.

        MetaTrader5 must be used from the thread that initialized it. Optimization
        studies run on a worker thread, so market data is loaded here on the caller
        thread (typically the FastAPI request handler) and cached in memory.
        """
        ohlcv_data = market_data_service.get_ohlcv(symbol, timeframe, start, end)
        if not ohlcv_data:
            raise ValueError("No market data found for the given parameters.")

        df = pd.DataFrame([bar.model_dump() for bar in ohlcv_data])
        df.set_index("time", inplace=True)
        df.index = pd.to_datetime(df.index)

        def data_provider(_config: BacktestRunConfig) -> pd.DataFrame:
            return df

        return cls(data_provider=data_provider)

    @staticmethod
    def load_sliced_frame(
        market_data_service: Any,
        *,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Fetch OHLCV once on the caller thread and return a naive-local frame.

        MetaTrader5 must be used from the thread that initialized it, so loading
        happens here (typically the FastAPI request handler) and the resulting
        frame can be reused or shipped to worker processes.
        """
        ohlcv_data = market_data_service.get_ohlcv(symbol, timeframe, start, end)
        if not ohlcv_data:
            raise ValueError("No market data found for the given parameters.")

        df = pd.DataFrame([bar.model_dump() for bar in ohlcv_data])
        df.set_index("time", inplace=True)
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = pd.DatetimeIndex(
                [_to_naive_local(ts.to_pydatetime()) for ts in df.index]
            )
        return df

    @classmethod
    def from_frame_sliced(cls, df: pd.DataFrame) -> "DefaultBacktestRunner":
        """Serve walk-forward windows by slicing an in-memory frame per run."""

        def data_provider(config: BacktestRunConfig) -> pd.DataFrame:
            return df.loc[config.start : config.end]

        return cls(data_provider=data_provider)

    @classmethod
    def from_market_data_sliced(
        cls,
        market_data_service: Any,
        *,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> "DefaultBacktestRunner":
        """Fetch OHLCV once and slice by ``config.start``/``config.end`` per run."""
        df = cls.load_sliced_frame(
            market_data_service,
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
        )
        return cls.from_frame_sliced(df)

    def _fetch_data(self, config: BacktestRunConfig) -> pd.DataFrame:
        if self._data_provider is not None:
            return self._data_provider(config)

        if self._market_data_service is None:
            from q_backend.market_data.service import MarketDataService

            self._market_data_service = MarketDataService()

        ohlcv_data = self._market_data_service.get_ohlcv(
            config.symbol,
            config.timeframe,
            config.start,
            config.end,
        )
        if not ohlcv_data:
            raise ValueError("No market data found for the given parameters.")

        df = pd.DataFrame([bar.model_dump() for bar in ohlcv_data])
        df.set_index("time", inplace=True)
        df.index = pd.to_datetime(df.index)
        return df

    def run(self, config: BacktestRunConfig) -> BacktestRunResult:
        df = self._fetch_data(config)
        strategy = build_strategy(
            config.strategy, config.strategy_params, config.symbol
        )
        sizer = build_position_sizer(
            config.position_sizing, point_value=config.point_value
        )
        engine = BacktestEngine(
            strategy,
            sizer,
            initial_capital=config.initial_capital,
            point_values={config.symbol: config.point_value},
            day_trade=config.day_trade,
            day_trade_start_time=config.day_trade_start_time,
            day_trade_end_time=config.day_trade_end_time,
            day_trade_close_time=config.day_trade_close_time,
            costs=config.costs,
        )
        registry = engine.run(df, parallel_mode=config.parallel_mode)

        closed_trades = registry.get_closed_trades()
        base_metrics = registry.get_performance_metrics(config.initial_capital)
        equity_curve = build_equity_curve(
            closed_trades,
            config.initial_capital,
            config.start,
            config.end,
        )
        backtest_days = max((config.end - config.start).total_seconds() / 86400, 1.0)
        metrics = compute_extended_metrics(
            base_metrics,
            closed_trades,
            config.initial_capital,
            equity_curve,
            backtest_days,
        )

        return BacktestRunResult(
            metrics=metrics,
            trial_user_attrs={
                "total_trades": metrics.get("total_trades", 0),
            },
            trades=closed_trades,
        )
