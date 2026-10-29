=====================================
Decision timeframes and closed bars
=====================================

A strategy normally decides once per bar of the frame it runs on. Some rules are
decisions on a finer timeframe that read coarser indicators, for example "after
the breakout, leave if a one-minute bar closes back inside the range". A strategy
can express them by deciding on M1 bars while its indicators come from M5 bars.

Reading coarse bars without look-ahead
--------------------------------------

:func:`q_backend.research.closed_bars` aggregates a research frame to a coarser
timeframe, on B3 session boundaries, the same way :meth:`~q_backend.research.TickStore.bars`
does. It returns the coarse OHLCV aligned to the frame's index, and each row holds
the last coarse bar that had closed by that row's close. A row inside a forming
coarse bar sees the coarse bar before it.

.. code-block:: python

   from q_backend.research import closed_bars

   m5 = closed_bars(frame, "M5")    # same index as frame; columns open, high, low, close, tick_volume
   frame["range_high"] = m5["high"]

The coarser timeframe must be a multiple of the frame's timeframe. If the frame
records no timeframe, as a frame inside a hook does not, the timeframe is read
from the spacing of its bars.

``closed_bars`` repeats each coarse bar on every row it covers. A row's value is
therefore the value of the coarse bar that is closed at that row, not a window of
coarse bars. Indicators that use only the last closed coarse bar, such as the
bar's high, low, or direction, need nothing more. A rolling indicator over coarse
bars needs one value per coarse bar, which this function does not return.

Deciding on the decision timeframe
----------------------------------

Declare the frame timeframe a strategy decides on with the class attribute
``decision_timeframe``:

.. code-block:: python

   from q_backend.research import ResearchStrategy, TradeOrder, closed_bars


   class RangeExitOnM1(ResearchStrategy):
       """Exit when a one-minute bar closes back inside the range of the last closed M5 bar."""

       decision_timeframe = "M1"

       def compute_indicators(self, frame):
           out = frame.copy()
           bar = closed_bars(frame, "M5")
           out["range_high"] = bar["high"]
           out["range_low"] = bar["low"]
           return out

       def entry_strategy(self, frame, positions=()):
           # Exit-only example: the entry rule is left out.
           return None

       def exit_strategy(self, frame, positions=()):
           if positions and frame["range_low"].iloc[-1] <= frame["close"].iloc[-1] <= frame["range_high"].iloc[-1]:
               return TradeOrder.close()
           return None

A declared decision timeframe has two effects:

* A stack run refuses a frame of any other timeframe. Run the strategy on M1 bars.
* A saved strategy version deploys only at the timeframe it declares. A version that
  declares ``M1`` may deploy at M1, which is below the M5 minimum that other
  forward-execution strategies keep. Registry strategies keep the M5 minimum.

The causality check (``backtest(..., check_causality=True)``) also cuts its
prefixes inside sessions for a strategy that declares a decision timeframe, so an
indicator that reads a coarse bar before it has closed is reported by column and
bar. A strategy without a declaration is checked at session ends only.
