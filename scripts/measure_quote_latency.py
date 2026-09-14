"""Measure quote stream append latency during the Q-013 live-session check."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

import numpy as np
import pyarrow as pa
import redis

from q_backend.market_data.timezone import mt5_datetime_to_utc_iso, unix_seconds_to_brasilia_naive


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--minutes", type=float, required=True)
    parser.add_argument("--redis-url", default="redis://localhost:6380/0")
    args = parser.parse_args()
    client = redis.Redis.from_url(args.redis_url, decode_responses=False)
    started = time.time()
    cursor = "0-0"
    latency_ms: list[float] = []
    while time.time() - started < args.minutes * 60:
        for entry_id, fields in client.xread({"q:stream:quotes": cursor}, block=1000, count=100):
            for stream_id, values in fields:
                cursor = stream_id
                header = json.loads(values[b"h"])
                if header.get("key", {}).get("symbol") != args.symbol:
                    continue
                appended_ms = int(stream_id.decode().split("-", 1)[0])
                ticks = pa.ipc.open_stream(values[b"p"]).read_all()["time_msc"].to_numpy()
                for tick_msc in ticks:
                    local = unix_seconds_to_brasilia_naive(int(tick_msc) // 1000).replace(
                        microsecond=(int(tick_msc) % 1000) * 1000
                    )
                    tick_utc = datetime.fromisoformat(mt5_datetime_to_utc_iso(local)).replace(tzinfo=timezone.utc)
                    latency_ms.append(appended_ms - tick_utc.timestamp() * 1000)
    if not latency_ms:
        print("no quote ticks observed")
        return
    values = np.asarray(latency_ms)
    print(f"symbol={args.symbol} ticks={len(values)} p50_ms={np.percentile(values, 50):.1f} " f"p95_ms={np.percentile(values, 95):.1f} max_ms={values.max():.1f}")


if __name__ == "__main__":
    main()
