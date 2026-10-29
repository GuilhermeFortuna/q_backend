# Q-140: Finer decision timeframes for research strategies

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 39 — Live-placeable research orders
**Depends on:** Q-136
**Implementation plan:** [Plan](../plans/Q-140-finer-decision-timeframes-for-research-strategies-plan.md)

## Purpose

Some intrabar logic is a decision, not a fill: "after the breakout, leave if a one-minute bar closes back inside
the range". Today a strategy can only express it with same-candle priced orders, or not at all. This task lets a
strategy decide on a finer timeframe than its signals: it runs on M1 bars while reading M5 or M15 indicators
built only from completed bars. No look-ahead is possible, and the same strategy deploys with its decisions on
M1.

## Current system

- `backtest()` runs hooks once per bar of the frame it is given. Multi-timeframe logic means resampling by hand,
  and nothing guarantees that a coarse bar is used only once it has closed.
- `check_causality=True` recomputes `compute_indicators` on history prefixes and catches columns that read later
  bars.
- Q-136 supports forward timeframes of M5 and slower.

## Behaviour

### Closed-bar context

- `q_backend.research.closed_bars(frame, timeframe)` resamples the frame to a coarser timeframe on B3 session
  boundaries, as `TickStore.bars` does. It returns the coarse OHLCV aligned to the frame's index. Each row holds
  the last coarse bar that closed at or before that row's close, and a row inside a forming coarse bar sees the
  previous one. The function refuses a timeframe that is not a multiple of the frame's.
- Indicators computed on `closed_bars(...)` and joined back to the frame pass `check_causality`.

### Running on the decision timeframe

- A strategy decides on the timeframe of the frame it receives, so a strategy that uses `closed_bars` on an M1
  frame decides every M1 close. Nothing else changes for strategies that do not use it.
- The class attribute `decision_timeframe` documents the intended frame timeframe. A stack run, and a
  deployment, refuse a frame of a different timeframe.

### Forward execution

- Strategy-version deployments accept M1 when the version declares `decision_timeframe = "M1"`. Registry
  strategies keep Q-136's M5 floor. The worker's M1 bar handling uses the existing bar coordinator, and its
  decision timing is reported in the existing timing fields.

## Acceptance criteria

1. On fixture M1 bars, `closed_bars(frame, "M5")` matches M5 bars built independently from the same data, shifted
   to completed bars. Rows inside a forming M5 bar see the previous M5 bar.
2. **Before/after match.** An M5 fixture strategy run on M5 bars, and its M1 version (M5 indicators from
   `closed_bars`, deciding only at M5 boundaries), produce `compare_results(...).identical` trades.
3. A fixture strategy that reads the forming coarse bar instead of `closed_bars` fails `check_causality`.
4. A strategy version with `decision_timeframe = "M1"` deploys at M1, and its forward decisions match its
   backtest on fixture bars. A registry strategy at M1 is still refused.
5. Existing research and execution tests pass unchanged.

## Out of scope

- Decisions on ticks (tier B), and sub-minute bars.
- Changing the bar coordinator or stream timeframes.
