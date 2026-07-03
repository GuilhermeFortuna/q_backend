"""WO179 regression: session context survives the OHLCV parquet cache round-trip.

The parquet cache (``tasks/data.py``) drops ``DataFrame.attrs``, so a prepared
frame read back from cache loses the in-memory ``_ctx_prepared`` marker while
keeping its ``_ctx_*`` columns. ``prepare_evaluation_frame`` must recognise the
durable columns and treat cache-hit and cache-miss frames identically — never
recomputing and never double-attaching context.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from q_backend.backtesting.session_context import prepare_evaluation_frame
from q_backend.backtesting.session_context.compute import RUN_CONTEXT_COLUMNS
from q_backend.backtesting.session_context.frame import _PREPARED_MARKER


def _intraday_ohlcv(days: int = 6) -> pd.DataFrame:
    rows = []
    price = 100.0
    start = datetime(2024, 1, 1, 9, 0)
    for day in range(days):
        for hour in range(6):
            ts = start + timedelta(days=day, hours=hour)
            price += 0.5 if (day + hour) % 3 else -0.3
            rows.append(
                {
                    "time": ts,
                    "open": price,
                    "high": price + 1.0,
                    "low": price - 1.0,
                    "close": price,
                    "volume": 1000.0,
                }
            )
    df = pd.DataFrame(rows).set_index("time")
    return df


def _round_trip_through_cache(df: pd.DataFrame, path) -> pd.DataFrame:
    """Mirror tasks/data.py write/read; parquet does not carry ``attrs``.

    The write frame's ``attrs`` are cleared to model exactly what parquet does
    to metadata (drops it), independent of the installed pandas/pyarrow's choice
    to warn-and-default vs. raise when it meets a non-serialisable attr value.
    """
    to_write = df.rename_axis("time").reset_index()
    to_write.attrs = {}
    to_write.to_parquet(path, index=False)
    cached = pd.read_parquet(path)
    cached = cached.set_index("time")
    cached.index = pd.to_datetime(cached.index, format="ISO8601")
    return cached


def test_prepared_frame_survives_cache_round_trip(tmp_path):
    raw = _intraday_ohlcv()

    # Cache-miss path: prepare, then persist through the parquet cache.
    prepared = prepare_evaluation_frame(raw)
    assert RUN_CONTEXT_COLUMNS.issubset(prepared.columns)
    assert prepared.attrs.get(_PREPARED_MARKER) is True

    path = tmp_path / "ohlcv.parquet"
    reloaded = _round_trip_through_cache(prepared, path)

    # The round-trip keeps the columns but drops the in-memory marker.
    assert RUN_CONTEXT_COLUMNS.issubset(reloaded.columns)
    assert _PREPARED_MARKER not in reloaded.attrs

    # Cache-hit path: preparing the reloaded frame must short-circuit.
    cols_before = list(reloaded.columns)
    re_prepared = prepare_evaluation_frame(reloaded)

    # Marker restored, no recomputation, no duplicated/extra columns.
    assert re_prepared.attrs.get(_PREPARED_MARKER) is True
    assert list(re_prepared.columns) == cols_before
    assert re_prepared is reloaded  # returned unchanged, not a recomputed copy

    # Context columns are identical to the cache-miss frame that produced them.
    for column in RUN_CONTEXT_COLUMNS:
        pd.testing.assert_series_equal(
            re_prepared[column].reset_index(drop=True),
            prepared[column].reset_index(drop=True),
            check_names=False,
        )
