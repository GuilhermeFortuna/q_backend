# Q-100: Publish research backtests to the Research stack

**Status:** written spec awaiting human review; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 17 — Research script runs in the Research stack
**Depends on:** Q-098, Q-099
**Implementation plan:** [Plan](../plans/Q-100-publish-research-backtests-to-the-research-stack-plan.md)

## Purpose

A research script ends at `print(result.metrics)`. `experiments/ccm_test.py` runs a custom strategy over six years of `CCM$` H1 bars and has no chart, no trade list and no monthly breakdown to look at, while the Research desktop has all of them for runs the stack executed.

After this task a script publishes its finished backtest with one call, and the run opens in Backtests history with the Trade Chart, Performance, Monthly and Trade List views filled:

```python
result = backtest(bars, strategy=SmartMaCrossover(), symbol="CCM$", quantity=1, point_value=450.0)
run_id = result.publish()
```

## Behaviour

### What a result remembers

`backtest()` records what publishing needs, so the script does not restate it:

- `BacktestResult.config`: a read-only mapping of the run's arguments — `symbol`, `strategy` (the strategy's class name, or the registered name), `strategy_params`, `quantity`, `point_value`, `initial_capital`, `costs`, `exit_params`, the day-trade settings, `force_close_at_end`, and `timeframe`.
- `timeframe` comes from `frame.attrs["q_research"]["timeframe"]`, which `load_bars` and `resample_ticks` set. It is `None` for a frame without that metadata.
- For a custom strategy, `strategy_params` holds the instance's public attributes whose values are JSON scalars (`str`, `int`, `float`, `bool`, `None`). Other attributes are omitted, not stringified.
- The engine's closed trade records are kept privately on the result so the published trades carry the same fields as a stack run's.

Existing fields, their columns and their values are unchanged.

### `BacktestResult.publish(*, name=None, timeframe=None, api_url=None) -> str`

Sends the run to `POST /api/v1/backtests/import` (Q-097, Q-099) and returns the new `run_id`.

- `name` is the strategy name shown in history; it defaults to `config["strategy"]`.
- `timeframe` overrides the recorded one. With neither, `publish` raises `ValueError` saying to pass `timeframe=`.
- `api_url` overrides `Q_API_URL`, which defaults to `http://127.0.0.1:8000`, the address `./dev research` serves.

The request is built as follows:

- `config`: a `BacktestRequest` with `strategy` set to the name, `engine` `candle`, `start` and `end` from the first and last bar, `position_sizing` as fixed quantity, and the recorded parameters, costs, exits and day-trade settings.
- `result.bars` from `result.data`; `result.indicators` from `result.indicators` (Q-098), one value per bar with warm-up `NaN` sent as null; `result.trades` from the closed trade records; `result.metrics` from `result.metrics`. Open trades are not sent, as for stack runs.
- `provenance`: the path of the running script, the strategy's qualified class name, its source text when `inspect.getsource` can read it, and the commit and dirty state of the git repository containing the script when there is one. Each unavailable item is null; none is an error.

### Failures

- An unreachable API raises `ConnectionError` naming the URL and saying to start the stack with `./dev research`.
- A `422` raises `ValueError` carrying the server's message. Any other error status raises `RuntimeError` with the status and body.
- A result with no bars raises `ValueError` before any request is made.
- Publishing never changes the result and never retries on its own. Calling it twice creates two runs.

### Dependencies

`backtest()` and every other library function still work with no database, Redis, control API or worker. Only `publish()` needs the control API.

## Documentation

- `docs/research-library.md`: a "Publish a backtest to the Research stack" section covering the call, `Q_API_URL`, what appears in Backtests history, that chart series come from `chart_indicators()`, that a published run is a record for review and cannot be re-run or optimised from the desktop, and that each call creates a new run. Add `config` to "Backtest results".
- `examples/research/mt5_backtest.py` gains a `--publish` flag.

## Focused acceptance

1. `result.config` reports the arguments of the run, the timeframe of a `load_bars`-style frame, and the JSON-scalar public attributes of a custom strategy.
2. With mocked HTTP, `publish()` posts one request whose body validates against the vendored Q-097 models: bars and indicator series of equal length, null for warm-up values, closed trades only, and metrics equal to `result.metrics`.
3. `name`, `timeframe` and `api_url` override their defaults; a frame without timeframe metadata and no `timeframe=` raises `ValueError`.
4. Provenance carries the script path, class name and source, and the git commit and dirty flag when the script is in a repository; each is null when unavailable.
5. Connection failure, `422` and another error status raise the documented exceptions.
6. One test posts a published request to the Q-099 endpoint through the API test client and reads the same bars, indicators, trades and metrics back from `GET /api/v1/backtest/{run_id}/result`.
7. `tests/research/test_imports.py` still passes: importing `q_backend.research` starts no service.

Verification uses synthetic frames, mocked HTTP and the API test client. No gateway, worker, Docker, GPU or desktop run is required by automated checks. One manual step follows merge: run `experiments/ccm_test.py` with `result.publish()` against `./dev research` and open the run in Backtests history, once Q-101 is merged.

## Delivery boundary

- No upload or server-side execution of strategy code.
- No publishing of tick backtests, optimisation results or walk-forward results.
- No local report or chart in the library.
- `experiments/` lives on the `research/experiments` branch, not on `development`; adding `publish()` to those scripts is done on that branch, outside this task.
