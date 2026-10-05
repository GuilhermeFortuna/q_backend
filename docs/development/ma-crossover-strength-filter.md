# MA Crossover — Strength Filter

Choose **MA Crossover — Strength Filter** (`MACrossoverStrengthFilter`) as the
single entry strategy in Research Backtests. The catalog supplies its parameter
form; no saved ML model or training job is needed.

Defaults reproduce the entry recipe explored on CCM$ H1:

| Parameter | Default |
|---|---|
| Short MA | EMA, period 9 |
| Long MA | WMA, period 20 |
| Crossover gap threshold | 0 |
| ATR period | 14 |
| Minimum crossover strength | 0.1616 |

At the signal candle's close, strength is:

```text
side × (current MA delta − previous MA delta) / current ATR
MA delta = short MA − long MA
side = +1 for BUY, −1 for SELL
```

An entry is accepted only when strength is finite and **strictly greater** than
the configured minimum. Missing/warmup/zero ATR rejects the entry. The existing
engine fills accepted entries at the next exported bar's open.

Opposite crossovers still close an existing trade even when the opposite entry
is rejected. Shared stop, target, trailing and other exit settings remain
available. The original MA Crossover is unchanged.

The research composite layer derives its stance from the original crossover
edges and applies the strength gate to final entry columns afterward. This keeps
rejected entries from changing exit timing. Multiple entry instances are rejected
for this variant; combining entry filters and voting rules needs separately
defined semantics. Existing multi-entry strategies retain their behavior.

The chart includes crossover strength in an oscillator pane. CSV exports include
ATR, crossover strength and acceptance diagnostics, with the usual instance
prefix when run through a composite entry configuration.

The default cutoff is an **exploratory CCM$ H1 setting**, not an automatically
retrained quantile or a validated universal threshold. In the previously analyzed
123-trade later period, the recipe retained 34 trades, R$6,481 net PnL and a 3.03
profit factor, versus R$11,013 and 1.76 for the original strategy. Better average
trade quality came with lower total profit. The literal 0.1616 default rounds the
original earlier-data quantile (0.161618...), so exact historical acceptance can
differ for signals immediately adjacent to that boundary.

Verification covers direct and research-composite entries, raw stance/exit
preservation, next-bar engine execution, rejected reversals closing without
re-entry, strict cutoff equality, invalid parameters, unavailable ATR and
prefix causality. Original/composite/ML-filter regression tests run alongside
these checks. Indicators reuse the shared Q MA and ATR kernels.
