"""Unit tests for lake_query module (Q-020)."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import time
from unittest.mock import patch

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from q_backend.market_data.clients.metatrader import _empty_ticks_columnar
from q_backend.market_data.lake_query import (
    BAR_COLUMNS,
    PATTERN_CHARACTERS,
    LakePathError,
    _get_instance,
    _reset_instance,
    query_cursor,
    read_bars_table,
    read_ticks_arrays,
)
from q_backend.market_data.tick_cache import COLUMNAR_TICK_KEYS, _TICK_DTYPE_MAP


def test_timezone_is_utc_and_second_cursor_also_utc(monkeypatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    _reset_instance()

    cur1 = query_cursor()
    tz1 = cur1.execute("SELECT current_setting('TimeZone')").fetchone()[0]
    assert tz1 == "UTC"

    cur2 = query_cursor()
    tz2 = cur2.execute("SELECT current_setting('TimeZone')").fetchone()[0]
    assert tz2 == "UTC"


def test_extensions_settings_are_false() -> None:
    cur = query_cursor()
    autoinstall = cur.execute("SELECT current_setting('autoinstall_known_extensions')").fetchone()[0]
    autoload = cur.execute("SELECT current_setting('autoload_known_extensions')").fetchone()[0]
    assert str(autoinstall).lower() == "false"
    assert str(autoload).lower() == "false"


def test_pattern_and_relative_path_refused_without_query_cursor() -> None:
    def forbidden_cursor(*args, **kwargs):
        raise AssertionError("query_cursor should not be called on invalid paths")

    with patch("q_backend.market_data.lake_query.query_cursor", forbidden_cursor):
        # Pattern character in path
        pattern_path = Path("/lake/ohlcv/A*/D1/2024.parquet")
        with pytest.raises(LakePathError):
            read_bars_table([pattern_path], datetime(2024, 1, 1), datetime(2024, 1, 2))

        # Other pattern characters
        for char in PATTERN_CHARACTERS:
            with pytest.raises(LakePathError):
                read_bars_table(
                    [Path(f"/lake/ohlcv/A{char}/D1/2024.parquet")], datetime(2024, 1, 1), datetime(2024, 1, 2)
                )

        # Relative path
        rel_path = Path("ohlcv/PETR4/D1/2024.parquet")
        with pytest.raises(LakePathError):
            read_bars_table([rel_path], datetime(2024, 1, 1), datetime(2024, 1, 2))

        with pytest.raises(LakePathError):
            read_ticks_arrays([rel_path], 0, 1000)


def test_empty_paths_return_empty_without_query_cursor() -> None:
    def forbidden_cursor(*args, **kwargs):
        raise AssertionError("query_cursor should not be called on empty paths")

    with patch("q_backend.market_data.lake_query.query_cursor", forbidden_cursor):
        t = read_bars_table([], datetime(2024, 1, 1), datetime(2024, 1, 2))
        assert t.num_rows == 0
        assert tuple(t.column_names) == BAR_COLUMNS
        assert t.schema.field("time").type == pa.timestamp("us")

        ticks = read_ticks_arrays([], 0, 1)
        expected_empty = _empty_ticks_columnar()
        assert set(ticks.keys()) == set(expected_empty.keys())
        for k in expected_empty:
            assert len(ticks[k]) == 0
            assert ticks[k].dtype == expected_empty[k].dtype


def test_explain_filter_pushdown_bars_and_ticks() -> None:
    fixture_dir = Path(__file__).resolve().parents[2] / "fixtures/lake"
    petr4_paths = [
        fixture_dir / "ohlcv/PETR4/D1/2024.parquet",
        fixture_dir / "ohlcv/PETR4/D1/2025.parquet",
    ]
    cur = query_cursor()

    # Bars explain check
    start_bars = datetime(2024, 12, 16)
    end_bars = datetime(2025, 1, 10)
    str_paths = [str(p.resolve()) for p in petr4_paths]
    bars_q = """
    EXPLAIN SELECT CAST(time AS TIMESTAMP) AS time, open, high, low, close, tick_volume, spread, real_volume
    FROM read_parquet(?, union_by_name=true, filename=true, file_row_number=true)
    WHERE CAST(time AS TIMESTAMP) BETWEEN ? AND ?
    ORDER BY time, list_position(?, filename), file_row_number
    """
    bars_plan = cur.execute(bars_q, [str_paths, start_bars, end_bars, str_paths]).fetchall()
    bars_plan_text = "\n".join(r[1] for r in bars_plan)
    assert "READ_PARQUET" in bars_plan_text
    assert "Filters:" in bars_plan_text
    assert "time>=" in bars_plan_text or "time >= " in bars_plan_text

    # Ticks explain check
    tick_paths = [
        fixture_dir / "ticks/WIN$N/2025-01.parquet",
        fixture_dir / "ticks/WIN$N/2025-02.parquet",
    ]
    str_tick_paths = [str(p.resolve()) for p in tick_paths]
    ticks_q = """
    EXPLAIN SELECT time_msc, bid, ask, last, volume, flags
    FROM read_parquet(?, filename=true, file_row_number=true)
    WHERE time_msc BETWEEN ? AND ?
    ORDER BY time_msc, list_position(?, filename), file_row_number
    """
    ticks_plan = cur.execute(ticks_q, [str_tick_paths, 1736935200000, 1739181604000, str_tick_paths]).fetchall()
    ticks_plan_text = "\n".join(r[1] for r in ticks_plan)
    assert "READ_PARQUET" in ticks_plan_text
    assert "Filters:" in ticks_plan_text
    assert "time_msc>=" in ticks_plan_text or "time_msc >= " in ticks_plan_text


def test_tie_ordering_listed_file_order(tmp_path: Path) -> None:
    f1 = tmp_path / "f1.parquet"
    f2 = tmp_path / "f2.parquet"
    schema = pa.schema(
        [
            ("time", pa.timestamp("us")),
            ("open", pa.float64()),
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("close", pa.float64()),
            ("tick_volume", pa.int64()),
            ("spread", pa.int64()),
            ("real_volume", pa.int64()),
        ]
    )
    ts = int(datetime(2025, 1, 2, 10, 0, 0).timestamp() * 1_000_000)
    t1 = pa.Table.from_arrays(
        [
            pa.array([ts], type=pa.timestamp("us")),
            pa.array([10.0]),
            pa.array([10.0]),
            pa.array([10.0]),
            pa.array([100.0]),
            pa.array([1]),
            pa.array([1]),
            pa.array([1]),
        ],
        schema=schema,
    )
    t2 = pa.Table.from_arrays(
        [
            pa.array([ts], type=pa.timestamp("us")),
            pa.array([10.0]),
            pa.array([10.0]),
            pa.array([10.0]),
            pa.array([200.0]),
            pa.array([1]),
            pa.array([1]),
            pa.array([1]),
        ],
        schema=schema,
    )
    pq.write_table(t1, f1)
    pq.write_table(t2, f2)

    res1 = read_bars_table([f1, f2], datetime(2025, 1, 2, 0, 0), datetime(2025, 1, 2, 23, 59))
    assert res1["close"].to_pylist() == [100.0, 200.0]

    res2 = read_bars_table([f2, f1], datetime(2025, 1, 2, 0, 0), datetime(2025, 1, 2, 23, 59))
    assert res2["close"].to_pylist() == [200.0, 100.0]


def test_unexpected_tick_column_raises_value_error(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad_ticks.parquet"
    schema = pa.schema(
        [
            ("time_msc", pa.int64()),
            ("bid", pa.float64()),
            ("ask", pa.float64()),
            ("last", pa.float64()),
            ("volume", pa.float64()),
            ("flags", pa.int32()),
            ("foo", pa.int32()),  # Extra column
        ]
    )
    tbl = pa.Table.from_arrays(
        [
            pa.array([1000], type=pa.int64()),
            pa.array([10.0]),
            pa.array([10.0]),
            pa.array([10.0]),
            pa.array([1.0]),
            pa.array([6], type=pa.int32()),
            pa.array([99], type=pa.int32()),
        ],
        schema=schema,
    )
    pq.write_table(tbl, bad_file)

    with pytest.raises(ValueError) as exc_info:
        read_ticks_arrays([bad_file], 0, 2000)
    assert str(bad_file.resolve()) in str(exc_info.value) or str(bad_file) in str(exc_info.value)


def test_pid_change_creates_new_instance(monkeypatch) -> None:
    _reset_instance()
    inst1 = _get_instance()
    assert inst1 is _get_instance()

    # Change pid
    fake_pid = os.getpid() + 10000
    monkeypatch.setattr(os, "getpid", lambda: fake_pid)

    inst2 = _get_instance()
    assert inst2 is not inst1
