# Q-098: Chart indicator declarations for research strategies

**Status:** written spec awaiting human review; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 17 — Research script runs in the Research stack
**Depends on:** none
**Implementation plan:** [Plan](../plans/Q-098-chart-indicator-declarations-for-research-strategies-plan.md)

## Purpose

The Trade Chart draws the indicator series a strategy declares through `get_chart_indicators()`. `ResearchStrategyAdapter.get_chart_indicators()` returns an empty list, so a custom `ResearchStrategy` has no way to say which of the columns it computed should be drawn, or whether a column belongs on the price pane or in the oscillator pane. A published script run (Q-100) would show candles and trade markers and nothing else.

After this task a research strategy declares its chart series, and `BacktestResult` carries them in the shape the stack already uses.

## Behaviour

### Declaring series

- `q_backend.research` exports `ChartIndicator`, a frozen value with `column` (required), `pane` (`"price"` or `"oscillator"`, default `"price"`), `label` (default: the column name) and `color` (optional CSS colour string).
- `ResearchStrategy` gains the optional hook `chart_indicators() -> Sequence[ChartIndicator]`, which returns an empty sequence by default. It is not abstract: existing strategies keep working unchanged.

```python
class SmartMaCrossover(ResearchStrategy):
    def chart_indicators(self):
        return [
            ChartIndicator("short_ma", label="EMA 9"),
            ChartIndicator("long_ma", label="WMA 20"),
            ChartIndicator("ma_delta", pane="oscillator"),
        ]
```

### Validation

`backtest()` validates the declaration after `compute_indicators` has run, and raises `ValueError` naming the strategy class and the column when:

- an entry is not a `ChartIndicator`;
- a column is missing from the frame `compute_indicators` returned, is a market column (`open`, `high`, `low`, `close`), or is not numeric;
- a column is declared twice.

The hook is called once per backtest. It does not receive the frame and cannot change results.

### Results

- `ResearchStrategyAdapter.get_chart_indicators()` returns the declaration as `ChartIndicatorSpec` values, so `serialize_chart_data` produces the same series shape for a research strategy as for a registered one.
- `BacktestResult` gains `indicators`, a tuple of `ChartIndicator` in declaration order. For a registered strategy run by name it holds that strategy's own chart indicators. An undeclared custom strategy yields an empty tuple.
- `result.data` is unchanged: it keeps every column the strategy computed, declared or not.

No pane is inferred. A column the strategy does not declare is not drawn; guessing a pane from value ranges would misplace series silently.

## Documentation

`docs/research-library.md`: add the hook to "Strategy hooks and causality" and `indicators` to "Backtest results", with the example above. `examples/research/rsi_reversion.py` declares its RSI in the oscillator pane.

## Focused acceptance

1. A strategy declaring a price series and an oscillator series returns both in `result.indicators`, in order, with default labels equal to the column names.
2. Each invalid declaration listed above raises `ValueError` naming the class and column.
3. A strategy without the hook returns an empty tuple and produces the same trades, metrics, equity and data as before this task.
4. A registered strategy run by name reports its existing chart indicators.
5. `serialize_chart_data` over the adapter's augmented frame yields one series per declared column with one value per bar.

Verification uses synthetic frames. No gateway, database, Redis, Docker, GPU or desktop run is required.

## Delivery boundary

- No change to hook causality rules, signal compilation, the candle engine or any registered strategy.
- No chart rendering in the library; drawing belongs to the Research desktop.
- Publishing the series is Q-100.
