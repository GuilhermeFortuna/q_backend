# q_backend findings

Existing behaviour observed while writing the Batch 04 specs and plans
(Q-020 to Q-025). None of these is fixed by Batch 04. Several of those tasks
promise byte-identical goldens and unchanged parity, so each item here is
recorded as today's behaviour and preserved. Each needs triage: a later task, an
accepted deviation, or no action. Record the decision beside the item.

## Backtesting and execution semantics

1. **A genome with a `rebalance` exit never closes its trade.** Found while
   planning Q-024 (`backtesting/genome/composite_strategy.py`,
   `genome/exit_rule_policy.py`).
2. **A genome's middle-band long exit overrides its short exit** when both fire
   on the same bar. Found while planning Q-024.
3. **Live holding-period exits almost never fire.** FMA, TRB and fixed-holding
   genomes find the entry bar by matching the position's open time exactly to
   a bar timestamp, which a live fill rarely equals. Found while planning Q-024
   (`strategies/lai_lau_common.py`).
4. **The evaluator labels every strategy exit `exit_rule`**, so it cannot tell
   them apart from `ExitStrategy` rule exits. Found while planning Q-024.
5. **RSI mean reversion can raise both entry triggers on one bar.** Found while
   planning Q-024 (`strategies/rsi_mean_reversion.py`).
6. **The evaluator and the engine size differently.** The evaluator sizes at the
   bar close with `initial_capital`. The engine sizes at the next bar's open
   with compounded capital (`execution/evaluator.py:222`, `engine.py:226`).
7. **SEQUENTIAL backtests leave open trades open at the end.** Only DAY_TRADE
   mode force-closes (`engine.py:306`).
8. **The tick kernel's `block_reentry` flag does nothing.** It is set on exit and
   cleared at the top of the next iteration, before it is read
   (`backtesting/tick/kernel.py`).
9. **Candle and tick backtests split days differently.** Candle backtests group
   by the index's local date (`engine.py:105`), tick backtests by UTC day
   (`tick/engine.py:18`).
10. **The momentum, trend-blend and TSMOM math exists twice.** One copy is in
    `features/compute.py`, the other in `genome/composite_strategy.py`, and the
    first says it mirrors the second. Invariant 1 requires a single copy. Q-023
    leaves both in Python.
11. **`gatev_pairs` overwrites the price columns and writes a float `spread`**,
    which collides with the contract's int64 `spread` bar column. Found while
    planning Q-025. Its resolution is expected in Q-028.

## Market data and the lake

12. **Timezone-aware range bounds are converted inconsistently.** `read_ohlcv`
    compares them as naive UTC against Brasília wall-clock values.
    `files_for_range` then treats the same values as Brasília. Tick reads
    convert them to Brasília. Found while planning Q-020
    (`market_data/local_store.py`).
13. **A file the catalog lists but that is missing on disk is skipped
    silently**, with no error. Found while planning Q-020.
14. **`scripts/generate_lake_fixture.py` no longer runs as written.** Since
    Q-017, `write_ohlcv` needs the catalog database and writes checksum-named
    files. Found while planning Q-020.
15. **`ohlcv_list_to_frame` keeps a `volume` column the `OHLCV` model does not
    have**, so the live rolling window carries only open, high, low and close.
    Found while planning Q-025.
16. **`seed_window` sorts and trims but does not dedupe**, unlike
    `ingest_completed_bars` (`execution/evaluator.py`). Found while planning
    Q-025.

## Documentation

17. **`transforms.compute_rolling_zscore` says it uses population standard
    deviation**, but pandas' `rolling.std()` uses ddof=1. Found while planning
    Q-021.
