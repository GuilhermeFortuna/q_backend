from datetime import datetime

import numpy as np

from q_backend.market_data.tick_cache import (
    COLUMNAR_TICK_KEYS,
    load,
    make_cache_key,
    store,
)


def _sample_arrays(count: int = 3) -> dict[str, np.ndarray]:
    return {
        "time_msc": np.array([1000, 2000, 3000], dtype=np.int64)[:count],
        "bid": np.array([1.0, 2.0, 3.0], dtype=np.float64)[:count],
        "ask": np.array([1.1, 2.1, 3.1], dtype=np.float64)[:count],
        "last": np.array([1.05, 2.05, 3.05], dtype=np.float64)[:count],
        "volume": np.array([10.0, 20.0, 30.0], dtype=np.float64)[:count],
        "flags": np.array([1, 2, 3], dtype=np.int32)[:count],
    }


def test_make_cache_key_is_deterministic():
    start = datetime(2026, 1, 1, 10, 0)
    end = datetime(2026, 1, 2, 10, 0)
    key_a = make_cache_key("PETR4", start, end, 7)
    key_b = make_cache_key("PETR4", start, end, 7)
    key_c = make_cache_key("WIN$", start, end, 7)

    assert key_a == key_b
    assert key_a != key_c
    assert key_a.startswith("PETR4_")


def test_store_and_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_TICK_CACHE_DIR", str(tmp_path))
    arrays = _sample_arrays()
    key = "test_key"

    store(key, arrays)
    loaded = load(key)

    assert loaded is not None
    for col in COLUMNAR_TICK_KEYS:
        assert np.array_equal(loaded[col], arrays[col])
        assert loaded[col].dtype == arrays[col].dtype


def test_load_returns_none_for_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_TICK_CACHE_DIR", str(tmp_path))
    corrupt = tmp_path / "bad.parquet"
    corrupt.write_text("not parquet", encoding="utf-8")

    assert load("bad") is None
