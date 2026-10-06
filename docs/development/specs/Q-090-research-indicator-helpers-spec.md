# Q-090: Research indicator helpers

**Status:** written spec and plan awaiting human review; the Q project board is the status of record.
**Batch:** 15 — Python research library
**Depends on:** Q-089, Q-023
**Implementation plan:** [Plan](../plans/Q-090-research-indicator-helpers-plan.md)

## Purpose

Provide discoverable indicator functions that scripts and ResearchStrategy implementations can use with ordinary pandas frames. Reuse Q's existing calculations, defaults where already defined, warm-up behavior and pinned q_core kernels. Users add columns with normal pandas assignments; no special DataFrame subclass or expression language.

## Public interface

```python
from q_backend.research import indicators

bars["rsi"] = indicators.rsi(bars["close"], period=14)
bars["ema_21"] = indicators.ma(bars["close"], period=21, kind="ema")
bars["atr"] = indicators.atr(bars, period=14)
upper, middle, lower = indicators.bollinger(bars["close"], period=20, num_std=2.0)
bars = bars.assign(bb_upper=upper, bb_middle=middle, bb_lower=lower)
```

`research.indicators` exports these exact functions:

| Function | Return | Delegates to |
| --- | --- | --- |
| ma(close: Series, period: int, kind: str = "sma") | Series | moving_averages.compute_ma |
| rsi(close: Series, period: int) | Series | technical_indicators.compute_rsi |
| atr(frame: DataFrame, period: int) | Series | compute_atr(high, low, close, period) |
| bollinger(close: Series, period: int, num_std: float = 2.0) | tuple[Series, Series, Series] | compute_bollinger_bands, upper/middle/lower |
| macd(close: Series, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) | tuple[Series, Series, Series] | compute_macd, line/signal/histogram |
| donchian(frame: DataFrame, period: int) | tuple[Series, Series] | compute_donchian_channels, upper/lower |
| realized_vol(close: Series, window: int, periods_per_year: int = 252) | Series | compute_realized_vol |
| yang_zhang(frame: DataFrame, window: int, periods_per_year: int = 252) | Series | compute_yang_zhang(open, high, low, close, window, periods_per_year) |

## Required behavior

1. Outputs preserve the input index, including timezone and index name. Input frames/Series are never mutated. Preserve the existing kernel output and NaN warm-up behavior; do not backfill, truncate the frame or replace unavailable values with zero. Empty input returns empty outputs with the same index.
2. ma kind accepts case-normalized sma/ema/wma/smma/hma and rejects unknown kinds rather than silently falling back. Integer periods/windows must exclude booleans, be >= 1, except Yang-Zhang >= 2; periods_per_year must be a positive integer. num_std must be finite and > 0. MACD periods must be positive; do not introduce a fast<slow restriction absent from the backend calculation.
3. Require numeric Series, required frame columns, and finite values or NaN; reject infinity and nonnumeric/object data with readable ValueError/TypeError naming the input. Permit NaN because indicator warm-up and user transformations may legitimately contain it. Missing columns report all required missing names; validate before entering native kernels. Do not require OHLCV volume columns for ATR/Donchian/Yang-Zhang.
4. Each formula is called through existing backend bridge modules; no direct q_core imports outside existing bridges and no duplicate pandas/Rust formula. Unsupported/missing installed kernels preserve the actionable ImportError from the bridge. Pure indicator operations require the installed q_core extension, not Postgres, Redis, an API process or a GPU.
5. The research package only loads the indicator bridges when indicators are requested. Importing or running helpers does not initialize provider configuration, storage, ASGI, tasks, torch or a terminal.
6. Document simple column addition, tuple order, parameter validation, NaN warm-up, units and annualization assumptions. The default 252 is an explicit periods-per-year argument, not automatic correction for M5 bars. Include a script that reads a caller-supplied historical Parquet frame and adds RSI/EMA/ATR without service access.

## Files and acceptance

Create src/q_backend/research/indicators.py, examples/research/add_indicators.py, tests/research/test_indicators.py. Extend lazy exports and docs/research-library.md. Use existing moving_averages.py, technical_indicators.py and indicator_kernels.py unchanged unless a small factoring is essential.

- Every exported helper matches its underlying backend output on a frozen small frame, including values, NaNs, index and tuple order. No new mathematical goldens are needed.
- Original input remains equal after each call; empty, timezone-aware, integer-valued and NaN-containing numeric inputs work as specified.
- Unknown MA kind, bool/fractional periods, insufficient Yang-Zhang window, infinity, invalid num_std, nonnumeric data and missing columns fail clearly.
- Offline example runs against a temporary fixture and preserves row count/timezone; no native MT5, network, DB, Redis or torch initialization.
- Focused tests and relevant existing MA/technical-indicator regressions pass. No live feeds, GPU or full platform CI requirement for wrappers.

## Delivery boundary

Written spec and plan await human review. This session authors documentation and board tasks only. Integrate/publish the documentation before implementation; the human owns initial Status and Todo approval. Launch through `./work start Q-090 --agent <agent> --worktree` from the workspace root after dependencies are Done. Implement natively using superpowers:executing-plans when available; delegation requires separate authorization. No push, merge, protected-branch checkout, or manual board-status changes by implementation agents.

Use the existing backend environment and pinned q_core package. This batch introduces a Python interface inside q_backend, not a separate distribution or a lightweight dependency extra. No HTTP API, generated wire contract, desktop UI, live order submission, tick strategy, optimizer/discovery integration, or q_core release is required. Existing consumers retain their behavior.
