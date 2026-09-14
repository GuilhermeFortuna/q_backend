"""Generate deterministic synthetic market data lake fixture for Q-017."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

# Set environment before importing local_store
PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_LAKE_DIR = PROJECT_ROOT / "tests/fixtures/lake"
if FIXTURE_LAKE_DIR.exists():
    shutil.rmtree(FIXTURE_LAKE_DIR)
FIXTURE_LAKE_DIR.mkdir(parents=True, exist_ok=True)
os.environ["Q_MARKET_DATA_ROOT"] = str(FIXTURE_LAKE_DIR)

from q_backend.market_data import local_store
from q_backend.market_data.clients.metatrader import _naive_local_to_time_msc
from q_backend.market_data.models import OHLCV


def generate() -> None:
    # 1. PETR4 D1 (2024 and 2025)
    petr4_2024_bars = [
        OHLCV(
            time=datetime(2024, 12, 16 + i, 10, 0, 0),
            open=35.0 + i,
            high=36.0 + i,
            low=34.5 + i,
            close=35.5 + i,
            tick_volume=10_000 + i * 100,
            spread=1,
            real_volume=500_000 + i * 5_000,
        )
        for i in range(5)
    ]
    petr4_2025_bars = [
        OHLCV(
            time=datetime(2025, 1, 6 + i, 10, 0, 0),
            open=40.0 + i,
            high=41.0 + i,
            low=39.5 + i,
            close=40.5 + i,
            tick_volume=12_000 + i * 100,
            spread=1,
            real_volume=600_000 + i * 5_000,
        )
        for i in range(5)
    ]
    local_store.write_ohlcv("PETR4", "D1", petr4_2024_bars + petr4_2025_bars)

    # 2. WIN$N M15 (2025) including a 09:00 Brasília bar
    winn_bars = [
        OHLCV(
            time=datetime(2025, 1, 2, 9, 0, 0) + timedelta(minutes=15 * i),
            open=130_000.0 + i * 50.0,
            high=130_100.0 + i * 50.0,
            low=129_950.0 + i * 50.0,
            close=130_050.0 + i * 50.0,
            tick_volume=5_000 + i * 50,
            spread=5,
            real_volume=20_000 + i * 200,
        )
        for i in range(12)  # 09:00 to 11:45
    ]
    local_store.write_ohlcv("WIN$N", "M15", winn_bars)

    # 3. WIN$N ticks (two months: 2025-01 and 2025-02)
    jan_ticks_time = [datetime(2025, 1, 15, 10, 0, 0) + timedelta(seconds=i) for i in range(5)]
    feb_ticks_time = [datetime(2025, 2, 10, 10, 0, 0) + timedelta(seconds=i) for i in range(5)]
    all_tick_times = jan_ticks_time + feb_ticks_time
    n_ticks = len(all_tick_times)
    time_msc = np.array([_naive_local_to_time_msc(t) for t in all_tick_times], dtype=np.int64)
    tick_prices = 130_500.0 + np.arange(n_ticks, dtype=np.float64) * 5.0
    winn_ticks = {
        "time_msc": time_msc,
        "bid": tick_prices - 2.5,
        "ask": tick_prices + 2.5,
        "last": tick_prices,
        "volume": np.full(n_ticks, 5.0, dtype=np.float64),
        "flags": np.where(np.arange(n_ticks) % 2 == 0, 32, 64).astype(np.int32),
    }
    local_store.write_ticks("WIN$N", winn_ticks)

    # Record expected reads for 3 ranges
    # Range 1: PETR4 D1 across 2024-2025 boundary
    petr4_read = local_store.read_ohlcv("PETR4", "D1", datetime(2024, 12, 15, 0, 0, 0), datetime(2025, 1, 15, 0, 0, 0))
    # Range 2: WIN$N M15 within 2025
    winn_read = local_store.read_ohlcv("WIN$N", "M15", datetime(2025, 1, 2, 9, 0, 0), datetime(2025, 1, 2, 10, 30, 0))
    # Range 3: WIN$N ticks across both months
    ticks_read = local_store.read_ticks_columnar(
        "WIN$N", datetime(2025, 1, 1, 0, 0, 0), datetime(2025, 2, 28, 23, 59, 59)
    )

    expected_reads = {
        "petr4_d1": {
            "symbol": "PETR4",
            "timeframe": "D1",
            "start": "2024-12-15T00:00:00",
            "end": "2025-01-15T00:00:00",
            "bars": [bar.model_dump(mode="json") for bar in petr4_read],
        },
        "winn_m15": {
            "symbol": "WIN$N",
            "timeframe": "M15",
            "start": "2025-01-02T09:00:00",
            "end": "2025-01-02T10:30:00",
            "bars": [bar.model_dump(mode="json") for bar in winn_read],
        },
        "winn_ticks": {
            "symbol": "WIN$N",
            "start": "2025-01-01T00:00:00",
            "end": "2025-02-28T23:59:59",
            "ticks": {k: v.tolist() for k, v in ticks_read.items()},
        },
    }

    baseline_path = FIXTURE_LAKE_DIR / "expected_reads.json"
    with open(baseline_path, "w", encoding="utf-8") as f:
        json.dump(expected_reads, f, indent=2)
        f.write("\n")

    print(f"Generated lake fixture at {FIXTURE_LAKE_DIR}")
    print(f"Recorded expected reads at {baseline_path}")


if __name__ == "__main__":
    generate()
