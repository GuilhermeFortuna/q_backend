====================
Strategy development
====================

The :class:`~q_backend.research.ResearchStrategy` abstract base class lets you express custom entry and exit rules as Python methods.
Only ``entry_strategy`` is required; the other hooks have default implementations.

Lifecycle hooks
---------------

A research strategy implements up to four hooks:

1. ``compute_indicators(self, frame: DataFrame) -> DataFrame``
   Called once per backtest on the full historical dataset. Adds indicator columns. Must not alter existing market columns or index.
2. ``entry_strategy(self, frame: DataFrame, positions: tuple[ResearchPosition, ...] = ()) -> TradeOrder | None``
   Evaluated per closed bar. Returns :meth:`~q_backend.research.TradeOrder.buy`, :meth:`~q_backend.research.TradeOrder.sell`, or ``None``.
3. ``exit_strategy(self, frame: DataFrame, positions: tuple[ResearchPosition, ...] = (), *, phase: str = "bar") -> TradeOrder | None``
   Evaluated per closed bar before ``entry_strategy``. Returns :meth:`~q_backend.research.TradeOrder.close` or ``None``.
   The optional ``phase`` parameter enables intrabar exits; see :doc:`execution`.
4. ``chart_indicators(self) -> Sequence[ChartIndicator]``
   Declares which indicator columns to render on the Trade Chart in the research desktop terminal. Each entry can draw a line, step, markers, histogram or pivot segment; ``kind`` and its fields are described in the ``chart_indicators`` section of ``docs/research-library.md``.

Position context
----------------

Both decision hooks optionally receive the ``positions`` parameter, containing an immutable tuple of open :class:`~q_backend.research.ResearchPosition` objects:

.. list-table::
   :header-rows: 1
   :widths: 25 25 50

   * - Field
     - Type
     - Description
   * - ``symbol``
     - ``str``
     - Traded instrument symbol.
   * - ``side``
     - ``Literal["long", "short"]``
     - Direction of the position.
   * - ``entry_time``
     - :class:`pandas.Timestamp`
     - Timezone-aware fill timestamp.
   * - ``entry_price``
     - ``float``
     - Filled entry price.
   * - ``quantity``
     - ``float``
     - Filled quantity from position sizing.

Example: close below the entry
------------------------------

This long-only example requests an exit when a completed candle closes below
the entry price. The exit fills at the next open; it is not an intrabar stop.

.. code-block:: python

   from q_backend.research import ResearchStrategy, TradeOrder

   class CloseBelowEntry(ResearchStrategy):
       def entry_strategy(self, frame, positions=()):
           if positions or len(frame) < 2:
               return None
           if frame["close"].iloc[-1] > frame["open"].iloc[-1]:
               return TradeOrder.buy()
           return None

       def exit_strategy(self, frame, positions=()):
           close = frame["close"].iloc[-1]
           for pos in positions:
               # Close long if price dips below entry
               if pos.side == "long" and close < pos.entry_price:
                   return TradeOrder.close()
           return None

Label orders with reasons
-------------------------

Every entry factory (:meth:`~q_backend.research.TradeOrder.buy`, ``sell``, ``buy_stop``,
``buy_limit``, ``sell_stop`` and ``sell_limit``) and :meth:`~q_backend.research.TradeOrder.close`
accept an optional ``reason``. It says why the order was placed and is shown verbatim:

- An entry reason becomes the ``entry_reason`` of the trade the order opens. For a resting
  stop or limit order, it is the reason of the order that placed it.
- A close reason becomes the ``exit_reason`` of the trades it closes, in place of the engine's
  ``SIGNAL`` label. Only ``SIGNAL`` exits take it: stop-loss, take-profit, end-of-day, force-close
  and exit-rule exits keep their engine labels.

``reason`` must be a non-blank string. :meth:`~q_backend.research.TradeOrder.amend` and
:meth:`~q_backend.research.TradeOrder.cancel` do not accept one. Orders without a reason behave as
before: the entry reason is null and the exit stays ``SIGNAL``.

.. code-block:: python

   class BreakoutLabels(ResearchStrategy):
       def entry_strategy(self, frame, positions=()):
           if positions or len(frame) < 2:
               return None
           if frame["close"].iloc[-1] > frame["high"].iloc[-2]:
               return TradeOrder.buy(reason="Breakout above prior high")
           return None

       def exit_strategy(self, frame, positions=()):
           if positions and frame["close"].iloc[-1] < frame["open"].iloc[-1]:
               return TradeOrder.close(reason="Bearish candle")
           return None

Keep indicator calculations causal
----------------------------------

Although ``compute_indicators`` receives the full dataset, each row's indicator
must use only that row and earlier rows. Avoid negative shifts, centered rolling
windows, or fitting a model to the entire test period. Do not change the index
or existing market-data columns.

Show indicators on the chart
----------------------------

Declare computed columns with :class:`~q_backend.research.ChartIndicator`.
For the RSI strategy in the quickstart, add this method to ``RSIReversion``:

.. code-block:: python

   def chart_indicators(self):
       from q_backend.research import ChartIndicator

       return (ChartIndicator("rsi", pane="oscillator", label="RSI"),)

Use ``pane="price"`` for overlays such as moving averages and
``pane="oscillator"`` for separate indicator plots. Column names must match
those returned by ``compute_indicators``. Use ``kind="markers"`` for signal
glyphs, and ``kind="segment"`` with ``start_column`` to draw a pivot from the bar
where it started to the bar that confirmed it. See :doc:`publishing` to review them
in the desktop.
