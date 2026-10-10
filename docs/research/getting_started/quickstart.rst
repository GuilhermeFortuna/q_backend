========================
10 Minutes to Q Research
========================

This quickstart guides you through loading market data, enriching it with indicators, implementing a custom trading strategy, running a local backtest, and analyzing results.

1. Fetching Historical Bars
---------------------------

Fetch 5-minute bars for the unadjusted Mini-Ibovespa future (``WIN$N``):

.. code-block:: python

   from q_backend.research import load_bars

   bars = load_bars("WIN$N", timeframe="M5", start="2026-09-01")
   print(bars.tail())

The returned object is a standard pandas :class:`pandas.DataFrame` indexed by timezone-aware Brasília timestamps (``America/Sao_Paulo``).

2. Adding Technical Indicators
------------------------------

Enrich the dataframe with Rust-backed technical indicators:

.. code-block:: python

   from q_backend.research import indicators

   # Calculate 14-period RSI and 21-period EMA
   bars["rsi"] = indicators.rsi(bars["close"], period=14)
   bars["ema_21"] = indicators.ma(bars["close"], period=21, kind="ema")

   # Calculate Bollinger Bands
   upper, middle, lower = indicators.bollinger(bars["close"], period=20, num_std=2.0)
   bars = bars.assign(bb_upper=upper, bb_middle=middle, bb_lower=lower)

All indicators guarantee exact index preservation, immutability of input objects, and proper NaN warm-up periods.

3. Defining a Strategy
----------------------

Subclass :class:`~q_backend.research.ResearchStrategy` to define decision rules:

.. code-block:: python

   from q_backend.research import ResearchStrategy, TradeOrder, indicators

   class RSIReversion(ResearchStrategy):
       def __init__(self, period: int = 14):
           self.period = period

       def compute_indicators(self, frame):
           frame = frame.copy()
           frame["rsi"] = indicators.rsi(frame["close"], self.period)
           return frame

       def entry_strategy(self, frame, positions=()):
           if positions or len(frame) < 2:
               return None
           prev_rsi, curr_rsi = frame["rsi"].iloc[-2:]
           if prev_rsi <= 30 and curr_rsi > 30:
               return TradeOrder.buy()
           if prev_rsi >= 70 and curr_rsi < 70:
               return TradeOrder.sell()
           return None

       def exit_strategy(self, frame, positions=()):
           if not positions:
               return None
           curr_rsi = frame["rsi"].iloc[-1]
           for pos in positions:
               if pos.side == "long" and curr_rsi >= 50:
                   return TradeOrder.close()
               if pos.side == "short" and curr_rsi <= 50:
                   return TradeOrder.close()
           return None

4. Running a Local Backtest
---------------------------

Execute the strategy through Q's deterministic kernel:

.. code-block:: python

   from q_backend.research import backtest

   result = backtest(
       bars,
       strategy=RSIReversion(),
       symbol="WIN$N",
       quantity=1,
       point_value=0.20,
       initial_capital=10_000.0,
   )

   print("Total Trades:", result.metrics["total_trades"])
   print("Total PnL:   ", result.metrics["total_pnl"])
   print("Win Rate:    ", result.metrics["win_rate"])
   print(result.trades.head())

5. Publishing to the Desktop UI
-------------------------------

Publish your backtest run to the Q Research desktop application for visual inspection:

.. code-block:: python

   run_id = result.publish()
   print(f"Published run: {run_id}")

The trade list, equity curves, drawdown series, and chart indicators will be instantly accessible in the desktop terminal.
