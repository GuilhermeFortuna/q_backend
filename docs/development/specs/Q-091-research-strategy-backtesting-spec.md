# Q-091: Research strategy classes and local backtesting

> Position context is extended by [research-position-context-spec.md](research-position-context-spec.md). Its optional hook signatures and runtime evaluation supersede this initial spec's no-position limitation.

**Status:** written spec and plan awaiting human review; the Q project board is the status of record.
**Batch:** 15 — Python research library
**Depends on:** Q-089, Q-090, Q-028
**Implementation plan:** [Plan](../plans/Q-091-research-strategy-backtesting-plan.md)

## Purpose

Let an experiment define a class with exactly three strategy hooks and run it synchronously against a pandas frame. Entry and exit hooks return None or one TradeOrder for the current closed bar. Translate decisions into existing Q candle signals and run the current BacktestEngine/q_core kernel; do not create another simulator or require API/worker/storage services.

## Strategy and TradeOrder interface

```python
from q_backend.research import load_bars, ResearchStrategy, TradeOrder, indicators, backtest

class RSIReversion(ResearchStrategy):
    def __init__(self, period=14):
        self.period = period

    def compute_indicators(self, frame):
        frame = frame.copy()
        frame["rsi"] = indicators.rsi(frame["close"], self.period)
        return frame

    def entry_strategy(self, frame):
        if len(frame) < 2:
            return None
        previous, current = frame["rsi"].iloc[-2:]
        if previous >= 30 and current < 30:
            return TradeOrder.buy()
        return None

    def exit_strategy(self, frame):
        if frame["rsi"].iloc[-1] > 50:
            return TradeOrder.close()
        return None

bars = load_bars("WIN$", timeframe="M5", start="2026-09-01")
result = backtest(bars, strategy=RSIReversion(), symbol="WIN$",
                  quantity=1, point_value=0.20, initial_capital=10_000)
print(result.metrics)
print(result.trades)
```

- `ResearchStrategy` is an ABC separate from backend TradingStrategy. Its only user strategy hooks are `compute_indicators(frame: DataFrame) -> DataFrame`, `entry_strategy(frame: DataFrame) -> TradeOrder | None`, `exit_strategy(frame: DataFrame) -> TradeOrder | None`. entry_strategy is abstract. compute_indicators defaults to returning the frame; exit_strategy defaults to None. Users can override all three. No mandatory base constructor, symbol attribute, chart metadata or registry entry.
- `TradeOrder` is an immutable dataclass with one field `action: Literal["buy", "sell", "close"]`, validated at construction. Classmethods buy(), sell(), close() return instances. It is a research decision request, not the broker order/fill model. buy/sell are legal only from entry_strategy; close only from exit_strategy. Reject wrong object types/actions; never silently coerce.
- Initial orders have no quantity, fill price, limit price, reason or broker id. Quantity and costs belong to backtest configuration. Entry orders choose direction; close requests apply to whichever position exists (encode both exit-long and exit-short). No order lists, partial exits, limit/stop entry orders, per-order sizing or custom exit-reason promises in this batch.

## Evaluation and causality

1. Validate the supplied historical frame, then call compute_indicators once on an owned copy of the entire frame. It must return a DataFrame with exactly the same index and unchanged original market columns; added/replaced user indicator columns are allowed. Reject changed row count/order, duplicate columns, reserved q_signal_* columns and bar_index from user frames/hooks.
2. For bar i, pass a separate owned prefix of the augmented frame through i to exit_strategy, then entry_strategy, each exactly once. Each hook receives its own copy; mutation cannot affect another hook, later bars, the result frame or the caller. No future rows or frame attrs containing whole-range/returned-end metadata are passed to hooks. Empty input calls no strategy hooks.
3. Both decision hooks run regardless of position, including on the last bar. They are functions of closed-bar history, not fill/position callbacks. Strategy parameters may live on the instance; hooks must not keep evolving state or infer actual positions from previous returned orders. Hooks cannot observe whether the kernel accepted an order. Document this limitation prominently.
4. Signals on i execute at i+1 open using existing engine semantics. Last-bar decisions have no next-open execution. None means no action for this bar. Repeated buy/sell conditions produce repeated requests; existing sizing/exposure caps decide fills. Opposite orders and simultaneous close+entry follow current kernel reversal/priority rules. Stop/target/day-trade exits keep existing precedence.
5. Batch indicator computation is not automatically causal just because hooks see prefixes. All transformations must depend only on preceding/current rows. Document centered windows, shift(-1), whole-frame statistics and evolving hook state as invalid. Verify the shipped RSI example on a few fixed prefixes; no public causality checker or automatic expensive re-evaluation is part of this batch.
6. Hook exceptions fail the run, preserving cause with method name/bar timestamp; no skipped errors or fallback strategy. Each backtest recompiles decisions; no registry registration, persistent caches or reuse of stale signal arrays.

## Backtest interface and configuration

`backtest(frame: DataFrame, *, strategy: ResearchStrategy | str, symbol: str, strategy_params: Mapping[str, object] | None = None, quantity: int = 1, point_value: float = 1.0, initial_capital: float = 100000.0, costs: TransactionCostConfig | None = None, exit_params: Mapping[str, object] | None = None, day_trade: bool = False, day_trade_start_time: str = "09:00", day_trade_end_time: str = "16:00", day_trade_close_time: str = "17:00", force_close_at_end: bool = False) -> BacktestResult`

- str uses the existing built-in candle factory/registry and its parameter validation. Reject tick names and research-only ML variants requiring service/model hydration; do not silently run them without their filters. Custom instances reject strategy_params. Validate exit_params with existing exit rule registry; conflicting built-in strategy_params/exit_params keys fail explicitly. No silent unknown parameters.
- quantity is a positive integer excluding bool. point_value and initial_capital are finite and > 0; costs fields finite and >= 0. Use FixedQuantitySizer, quantity as exposure cap, no signal-strength scaling. Default point_value=1.0 is never inferred from symbol; document futures must supply their multiplier. costs=None explicitly means zero modeled costs, not real-world cost-free trading.
- Input requires unique ascending aware DatetimeIndex and valid open/high/low/close, following Q-089 price validation. Normalize aware indices to America/Sao_Paulo before the kernel, so day-trade wall-clock rules match exchange time. Reject naive indices with instructions to tz_localize (no guess). Volume columns are optional for custom strategies and validated when present; built-ins retain their specific requirements. Empty frames with correct schema yield empty results.
- Day-trade settings configure the existing sequential kernel session gates; preserve indicators/history across sessions. Do not use ParallelMode.DAY_TRADE to reset callback histories or add worker processes. Validate clock strings and chronological start <= end <= close with existing parsers.
- Stops/targets/trailing rules are configured via exit_params; do not attach engine ExitStrategy onto the user's instance, where it would shadow exit_strategy(frame). Use a private TradingStrategy adapter with prepared signals, empty chart metadata and a separate engine ExitStrategy. Indicator augmentation may fill required exit columns using existing logic. No new order/fill/exit math or q_core changes.

## Results

BacktestResult exposes `metrics: dict`, `trades: DataFrame`, `equity: DataFrame`, `data: DataFrame`. data contains the actual prepared indicators and exit-required columns, with internal q_signal_* and bar_index omitted; it does not recompute the user hooks. Return independent owned frames.

- trades includes all registry trades, sorted by entry time, with stable columns: trade_id, symbol, side (long/short), status (open/closed), entry_time, entry_price, exit_time, exit_price, pnl, quantity, commission, point_value, exit_reason. Nullable exit fields preserve open positions; timezone-aware timestamps use America/Sao_Paulo. Random trade ids are not numerical reproducibility keys.
- metrics delegates to TradeRegistry.get_performance_metrics(initial_capital), including its existing empty/undefined conventions. Document that these are closed-trade metrics; do not invent Sharpe or mark-to-market returns.
- equity has aware index time matching data, and one float64 `realized_equity` column: initial capital plus cumulative closed-trade net PnL booked at exit timestamps. Open trades are not marked to market; label this accurately and preserve commissions from the registry. Empty input has empty equity. No separate PnL calculator.
- force_close_at_end=False preserves open positions; True delegates the terminal close to the kernel. No database/lake/Redis run persistence or hidden saving.

## Ownership and acceptance

Create research/strategy.py, research/orders.py, research/adapter.py, research/backtest.py, research/results.py and focused tests under tests/research. Extend lazy exports and docs/research-library.md. Add examples/research/rsi_reversion.py (caller-supplied Parquet, no services) and examples/research/mt5_backtest.py (load_bars -> indicators -> built-in/custom strategy). Remove/update the stale README reference to the missing scripts/backtests/run_ccm_backtest.py in favor of the real examples. No sys.path hacks.

- Long/short/close/None decisions and callback order/prefix isolation compile to the exact signal arrays expected by the existing engine. Wrong returns, actions, mutations, exceptions and changed market rows fail clearly.
- Frozen OHLC fixtures match direct engine runs for next-open fills, repeated requests, reversal, simultaneous close+entry, stop/target precedence, day-trade gating, costs, last-bar signals and forced/unforced terminal close. Compare numerical results, not UUIDs.
- Built-in MACrossover output equals direct factory/engine output under matching config; unsupported tick/ML names and parameter conflicts fail.
- The shipped RSI example makes identical decisions for the same bar when evaluated from full-prepared and independently prepared prefix data on a few fixed checkpoints.
- Result metrics/trades/realized equity agree with registry PnL and include open/empty cases; non-Brasília aware input yields correct exchange-local session gates.
- Offline example and pure DataFrame backtesting run without API/worker/DB/Redis/GPU/Wine. The MT5 example requires an already-running terminal/gateway for load_bars only; it never reads a catalog or starts services. Tests use small in-memory historical frames. Run focused research tests plus existing candle engine/exit/signal/strategy regressions; focused existing regressions are sufficient while the adapter leaves shared engine behavior unchanged. If implementation changes shared engine behavior, identify and run the corresponding additional canonical check. No live market or desktop acceptance requirement.

## Delivery boundary

Written spec and plan await human review. This session authors documentation and board tasks only. Integrate/publish the documentation before implementation; the human owns initial Status and Todo approval. Launch through `./work start Q-091 --agent <agent> --worktree` from the workspace root after dependencies are Done. Implement natively using superpowers:executing-plans when available; delegation requires separate authorization. No push, merge, protected-branch checkout, or manual board-status changes by implementation agents.

Use the existing backend environment and pinned q_core package. This batch introduces a Python interface inside q_backend, not a separate distribution or a lightweight dependency extra. No HTTP API, generated wire contract, desktop UI, live order submission, tick strategy, optimizer/discovery integration, or q_core release is required. Existing consumers retain their behavior.
