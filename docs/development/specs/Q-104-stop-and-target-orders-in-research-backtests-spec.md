# Q-104: Stop, target and lazy intrabar exits in research backtests

**Status:** In Progress; core extension published as `v2026.10.08.3`; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 18 — Stop and target orders in research backtests
**Depends on:** Q-102, Q-103
**Implementation plan:** [Plan](../plans/Q-104-stop-and-target-orders-in-research-backtests-plan.md)

## Purpose

A `ResearchStrategy` can already inspect actual open positions through immutable
`ResearchPosition` snapshots. Its ordinary close requests and `exit_params` rules
still fill at the next bar's open. Q-102 supplies lazy tick-based protective fills
in the kernel, but the research API does not expose them yet.

After this task entry orders carry stop/target prices, and `exit_strategy()` can
also exit inside a candle using ticks from a `TickStore`. Evaluation is candle-first:
screen for a possible exit, load ticks only for candidate candles, then confirm
the first executable exit in tick order. This fill model is for research only.

## Behaviour

### Orders

```python
class Breakout(ResearchStrategy):
    def entry_strategy(self, frame):
        last = frame.iloc[-1]
        if last["close"] > last["upper"]:
            return TradeOrder.buy(
                stop_loss=last["close"] - 2 * last["atr"],
                take_profit=last["close"] + 4 * last["atr"],
            )
        return None
```

- `TradeOrder.buy` and `TradeOrder.sell` accept keyword-only `stop_loss` and `take_profit`, both optional price levels. `TradeOrder.close()` takes none.
- Construction raises `ValueError` for a level that is not a finite positive number, for a buy whose stop is not below its target, for a sell whose stop is not above its target, and for a close with a level.
- An order without levels behaves exactly as today.

### Lazy intrabar exits from `exit_strategy()`

An exit override explicitly declaring a keyword-capable `phase` parameter opts
into intrabar evaluation. `positions` remains independently optional. Hooks without
`phase` keep their existing closed-bar calls and next-open close execution;
`**kwargs` alone does not opt in. All phases return `TradeOrder.close()` or `None`.

```python
def exit_strategy(self, frame, positions, *, phase="bar"):
    if not positions:
        return None
    position = positions[0]
    level = position.entry_price + (5.0 if position.side == "long" else -5.0)
    if phase == "screen":
        reached = (
            frame["high"].iloc[-1] >= level if position.side == "long"
            else frame["low"].iloc[-1] <= level
        )
    elif phase == "tick":
        reached = (
            frame["close"].iloc[-1] >= level if position.side == "long"
            else frame["close"].iloc[-1] <= level
        )
    else:
        return None
    return TradeOrder.close() if reached else None
```

- **`screen`:** evaluate the full candidate candle after queued fills and before
  intrabar processing. A close means "inspect ticks", not an executed order. The
  screen receives an immutable snapshot of actual open positions. Flat candles
  require no screen or tick replay.
- **`tick`:** for a screened candidate, evaluate in stored tick order with earlier
  completed bars and a causal partial current candle. The first confirmed close
  fills at that tick's actual trade price and timestamp, which can differ from
  the next open. Stop replay once no positions remain open.
- **`bar`:** retain the ordinary close request after intrabar execution, before
  the entry hook. A close here still fills at the next open. These two closed-bar
  hooks receive the same post-intrabar position snapshot.
- Screening must conservatively cover possible tick triggers, including a range
  crossing that reverses before the final close. False-positive candidates are
  allowed and do not fill without tick confirmation. Where OHLC cannot rule out
  a condition, screen every candle with an open position. A final-close-only
  screen is insufficient for conditions that may trigger earlier in the candle.
- Replay's current-row `open` is the first trade price, `high`/`low` are extrema
  observed so far, `close` is the current tick, and `tick_volume` is the observed
  trade count. Its index remains the bar-open timestamp. Other current-row source
  columns, including volume/quote aggregates and caller-derived columns, are
  unavailable (`NaN`), never copied from the completed candle.
- Recompute user indicators on an isolated raw prefix with this partial candle
  before each tick evaluation. Never reuse final-candle indicators. Normal
  preparation remains once per run; extra computations occur only during replay.
  Preserve prefix/index ownership, validation and stripped attrs.
- Hooks must be deterministic and transfer no evolving state from screen to tick
  calls. Full-candle screening identifies candidates only; later information must
  never justify an earlier tick fill. Invalid actions/types and failures abort
  with strategy, phase, bar and tick context as applicable.
- Entry hooks remain closed-bar hooks. Both static and position-aware runtime
  entry decisions must carry their actual stop/target levels to the kernel.

### Running

```python
store = TickStore("WDO$N")
frame = store.bars("M10", start="2025-10-01")
result = backtest(frame, strategy=Breakout(), symbol="WDO$N", ticks=store, point_value=10.0)
```

- `backtest()` gains `ticks: TickStore | None = None`.
- The first entry order that carries a level without `ticks` raises `ValueError` naming the strategy, the bar and the `ticks` argument. `ticks.symbol` must equal `symbol`.
- A phase-aware exit also requires `ticks`; missing it raises `ValueError` naming
  the strategy and argument. A strategy with neither levels nor a phase-aware
  exit produces identical results with/without a store and makes no tick reads.
- Load a candle's prices only when a custom exit screen qualifies or Q-102's
  directional protective screen qualifies, including gaps beyond a level. Load
  each candidate interval at most once even when both mechanisms qualify. A
  cached session-file read and scanning a candle's ticks are distinct operations.
- Tick intervals end at the next bar within the same session, or the exchange
  calendar day's end for its last bar; they never include the next day's session.
- The frame must be built from the same store with `TickStore.bars`. When the engine loads a bar's prices it checks that their first, highest, lowest and last equal the bar's open, high, low and close, and raises `ValueError` naming the bar and `TickStore.bars` when they do not.
- A bar whose prices are needed but not stored raises `NoMarketDataError` naming the session day and `TickStore.sync`. There is no bar-only fallback.
- An empty needed interval also fails with bar context; there is no fallback to
  an assumed OHLC fill or next-open execution for a requested intrabar exit.

### Fills

The rule is the candle kernel's (Q-102) and is restated in the guide:

- The entry fills at the next bar's open, as today. If a level is already on the wrong side of that fill price, the entry is not taken.
- A stop triggers on the first traded price at or beyond its level and fills at that price, so a gap through the level fills at the gap price.
- A target triggers on the first traded price strictly beyond its level and fills at the level.
- When both levels lie inside one bar, the order in which prices traded decides.
- Levels are fixed at entry. Legacy/bar-phase `exit_strategy` requests,
  `exit_params` rules, day-trade closes and `force_close_at_end` retain their
  existing open/close execution alongside the new intrabar mode.
- Protective orders and custom tick exits resolve in one chronological scan;
  the first executable event wins. At the same tick a protective exit wins over
  a custom exit, with existing stop-before-target precedence preserved. Do not
  scan all protective fills first and miss an earlier custom exit.
- A newly opened trade cannot close on its own entry tick; that tick still
  updates the replay frame. Duplicate timestamps retain stored tick order.
- A custom tick exit fills at the confirming trade price. It cannot invent an
  untraded price inside the OHLC range. A target's level-price fill retains the
  separate resting-order model already specified by Q-102.
- Tick snapshots reflect execution so far; closed-bar snapshots exclude positions
  closed intrabar. Session closes at the open precede replay; final-bar closes
  follow replay, as today.
- Costs are charged per side as configured, on protective fills too.

### Results

- `trades["exit_reason"]` is `STOP_LOSS` or `TAKE_PROFIT` for a protective fill.
- `trades` gains `exit_tick_time` for both protective and custom intrabar fills,
  and `NaT` for other exits. Custom tick closes use the existing signal-exit reason.
  `exit_time` stays the bar timestamp; realized equity includes either intrabar
  exit on that bar, with the configured per-side costs.
- `trades` gains `stop_loss` and `take_profit`, the levels the trade was opened with, `NaN` when unset.
- `BacktestResult` gains `rejected_entries`, a frame with the bar time, side, fill price and both levels of each entry that was not taken. It is empty when there are none.
- Empty frames return the complete new schemas without hooks or tick reads.

### Research only

- Only `q_backend.research.backtest` passes levels and a price source to the kernel. Stack backtest jobs, optimisation, walk-forward and the forward evaluator call it as before and their results do not change.
- The previous pin `v2026.10.08.2` includes protective execution and
  position-aware callbacks, but its callback runs after whole-bar protective
  execution. Q-104's additive `q_core`/PyO3 extension is published as
  `v2026.10.08.3`, providing lazy screen/tick callbacks and runtime entry-level
  transport. Backend adoption uses that published tag. Preserve existing
  callback signatures and golden results. Q-102 remains Done; Q-103's existing
  `trade_prices` accessor is sufficient for replay.

## Documentation

`docs/research-library.md`:

- "Strategy hooks and causality": entry level arguments and phase-aware exit
  screening/replay, using actual positions; causal partial-candle indicators and
  the responsibility to screen conservatively.
- "Execution model": a "Stop and target orders" subsection with the fill rule, the wrong-side rule, the requirement to build the frame from the store, the history limit that follows from it, and the statement that live trading still decides on completed bars, so these fills are not reproduced by a deployed strategy.
- "Transaction costs": protective fills are traded prices and carry the configured per-side cost; the half-spread term stays in the cost, and a target, which is a resting order, is charged it as well.
- "Backtest results": the new columns and `rejected_entries`.
- Explain lazy candidate-only reads, chronological protective/custom precedence,
  tick-confirmed prices and next-open legacy/bar-phase closes.

`examples/research/stop_target_backtest.py` demonstrates both entry levels and a
phase-aware exit over a store, printing metrics, tick exit times, reason counts and
rejected entries. It includes candles excluded by the exit screen.

## Focused acceptance

Each case uses a `TickStore` written under `tmp_path` from handwritten ticks.

1. A long whose stop trades first inside a bar closes at the traded price with `STOP_LOSS`, and `exit_tick_time` is that tick's time; the mirrored short does the same.
2. A bar containing both levels resolves by trade order, in both orders.
3. A price that only touches the target does not fill; a gap through the stop fills at the bar's open.
4. An entry whose stop is on the wrong side of its fill is absent from `trades` and present in `rejected_entries`.
5. A level without `ticks`, a store for another symbol, a frame that does not match the store and a bar without stored ticks each raise the error the spec names.
6. A strategy without levels or a phase-aware exit returns identical `trades`,
   `metrics`, `equity` and `data` with/without `ticks`, and the store reads no file.
7. A counting store shows that only protective or custom candidate bars are read.
8. `equity` includes the profit of a protective exit on the bar in which it closed.
9. Invalid `TradeOrder` levels raise at construction.
10. Existing research, candle golden and evaluator tests pass with unchanged bodies.
11. Long and short custom exits fill within the candidate candle at the first
    confirmed tick price, different from the next open, with `exit_tick_time` set.
12. A counting store/hook proves no replay for screened-out/flat candles and one
    interval request when custom and protective candidates overlap. False-positive
    screens do not fill; an earlier crossing followed by reversal still qualifies.
13. Replay OHLC, tick counts, indicators and unavailable fields reveal no future
    data; a later-tick condition never fills earlier. Include an indicator crossing.
14. Custom-before-protective, protective-before-custom and same-tick ties resolve
    by the specified ordering. The first entry tick is not reused as an exit tick.
15. Position-aware entry levels travel through runtime callbacks; tick and final
    closed-bar snapshots reflect actual fills. Entry/exit bar hooks share a tuple.
16. Phase-aware missing-store errors, contextual replay failures, same-time tick
    ordering, per-session bounds and complete empty schemas behave as specified.
17. Existing frame-only and position-aware exits retain next-open behavior without
    `phase`; opting into screening does not change entry capacity/session gates.

Verification uses synthetic data. No gateway, database, Docker, GPU, Wine or desktop run.

## Delivery boundary

- No stop or limit entry orders or mutation of entry-attached protective levels.
- Actual immutable position context is supported; no assumed-fill state in hooks.
- No change to `exit_params` rules or their next-open fill, the forward evaluator, execution orders or any contract.
- `publish()` keeps sending the fields it sends today; showing the new columns in the Research desktop is not part of this task.
- Registered strategies run by name do not gain levels.
