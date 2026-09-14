"""Generate the deterministic Q-013 synthetic gateway replay fixture."""

from __future__ import annotations

from pathlib import Path

import numpy as np

SEED = 13_013
TICK_COUNT = 2_400


def main() -> None:
    rng = np.random.default_rng(SEED)
    output = Path(__file__).resolve().parents[1] / "tests/fixtures/market/synthetic_b3_ticks_session.npz"
    output.parent.mkdir(parents=True, exist_ok=True)

    # Repeated groups deliberately land across 300-row windows and at the 50-row
    # overlap boundary used by cursor regression tests.
    group_sizes = np.where(np.arange(TICK_COUNT) % 97 == 0, 3, 1)
    times: list[int] = []
    current = 1_726_131_600_000
    for size in group_sizes:
        times.extend([current] * int(size))
        current += int(rng.integers(1, 5))
        if len(times) >= TICK_COUNT:
            break
    time_msc = np.asarray(times[:TICK_COUNT], dtype=np.int64)
    price = 130_000.0 + np.cumsum(rng.normal(0.0, 1.5, TICK_COUNT))
    np.savez_compressed(
        output,
        time_msc=time_msc,
        bid=price - 0.5,
        ask=price + 0.5,
        last=price,
        volume=rng.uniform(1.0, 15.0, TICK_COUNT),
        flags=np.where(np.arange(TICK_COUNT) % 2, 32, 64).astype(np.int32),
    )


if __name__ == "__main__":
    main()
