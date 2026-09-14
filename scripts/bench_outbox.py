#!/usr/bin/env python3
"""Contention benchmark for transactional outbox recording.

Measures throughput and recording latency across N writer threads on one topic.
Optionally measures commit-to-append latency of the stream relay.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import threading
import time

import numpy as np
import redis
from sqlalchemy.orm import sessionmaker

from q_backend.storage.db.engine import get_engine
from q_backend.streaming.codec import decode_entry
from q_backend.streaming.keys import stream_key
from q_backend.streaming.outbox import record_event
from q_backend.streaming.redis_binary import get_binary_redis


def run_benchmark(
    writers: int,
    duration_seconds: int,
    topic: str = "jobs.terminal",
    measure_relay: bool = False,
) -> dict[str, float]:
    engine = get_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    stop_event = threading.Event()
    all_latencies: list[float] = []
    latencies_lock = threading.Lock()
    total_events = 0
    total_lock = threading.Lock()

    relay_latencies: list[float] = []
    reader_stop = threading.Event()
    s_key = stream_key(topic)

    def relay_reader_task() -> None:
        client = get_binary_redis()
        last_id = b"$"
        # Read from existing tail
        try:
            entries = client.xrevrange(s_key, count=1)
            if entries:
                last_id = entries[0][0]
        except redis.RedisError:
            last_id = b"0-0"

        while not reader_stop.is_set():
            try:
                streams_data = client.xread({s_key: last_id}, count=500, block=100)
                if not streams_data:
                    continue
                for _, stream_entries in streams_data:
                    for entry_id, fields in stream_entries:
                        last_id = entry_id
                        try:
                            env, _ = decode_entry(fields)
                            if isinstance(env.payload, dict) and env.payload.get("error"):
                                commit_ts = float(env.payload["error"])
                                append_ts_ms = int(entry_id.decode("utf-8").split("-")[0])
                                append_ts = append_ts_ms / 1000.0
                                lat_ms = max(0.0, (append_ts - commit_ts) * 1000.0)
                                relay_latencies.append(lat_ms)
                        except (KeyError, ValueError, TypeError):
                            continue
            except redis.RedisError:
                time.sleep(0.05)

    reader_thread: threading.Thread | None = None
    if measure_relay:
        reader_thread = threading.Thread(target=relay_reader_task, daemon=True)
        reader_thread.start()

    print(
        f"Starting outbox benchmark: {writers} writers, {duration_seconds}s on topic '{topic}'"
        f"{' (measuring relay latency)' if measure_relay else ''}..."
    )

    def writer_task(writer_id: int) -> None:
        local_latencies: list[float] = []
        count = 0
        while not stop_event.is_set():
            count += 1
            t_commit = time.time()
            payload = {
                "job_id": f"bench-{writer_id}-{count}",
                "kind": "backtest",
                "status": "completed",
                "finished_at": "2026-09-12T00:00:00Z",
                "error": str(t_commit) if measure_relay else None,
            }
            t0 = time.perf_counter()
            with session_factory() as session:
                record_event(
                    session,
                    topic,
                    payload,
                    payload_schema="schema/stream/payloads/job-terminal.schema.json",
                    producer_id=f"bench-writer-{writer_id}",
                )
                session.commit()
            t1 = time.perf_counter()
            local_latencies.append((t1 - t0) * 1000.0)

        with latencies_lock:
            all_latencies.extend(local_latencies)
        with total_lock:
            nonlocal total_events
            total_events += len(local_latencies)

    start_time = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=writers) as executor:
        futures = [executor.submit(writer_task, i) for i in range(writers)]

        time.sleep(duration_seconds)
        stop_event.set()

        for f in concurrent.futures.as_completed(futures):
            f.result()

    elapsed = time.perf_counter() - start_time
    events_per_sec = total_events / elapsed if elapsed > 0 else 0.0

    if measure_relay and reader_thread is not None:
        # Give relay up to 3s to drain recent appends
        time.sleep(1.5)
        reader_stop.set()
        reader_thread.join(timeout=3.0)

    lat_arr = np.array(all_latencies) if all_latencies else np.array([0.0])
    p50 = float(np.percentile(lat_arr, 50))
    p95 = float(np.percentile(lat_arr, 95))
    p99 = float(np.percentile(lat_arr, 99))

    results: dict[str, float] = {
        "writers": float(writers),
        "duration": elapsed,
        "total_events": float(total_events),
        "events_per_sec": events_per_sec,
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
    }

    print("\nBenchmark results (Outbox Record):")
    print(f"  Writers:               {writers}")
    print(f"  Duration:              {elapsed:.2f} s")
    print(f"  Total events recorded: {total_events}")
    print(f"  Events per second:     {events_per_sec:.2f}")
    print(f"  Latency p50:           {p50:.2f} ms")
    print(f"  Latency p95:           {p95:.2f} ms")
    print(f"  Latency p99:           {p99:.2f} ms")

    if measure_relay:
        relay_arr = np.array(relay_latencies) if relay_latencies else np.array([0.0])
        r_p50 = float(np.percentile(relay_arr, 50))
        r_p95 = float(np.percentile(relay_arr, 95))
        r_max = float(np.max(relay_arr))
        results["relay_p50_ms"] = r_p50
        results["relay_p95_ms"] = r_p95
        results["relay_max_ms"] = r_max
        print("\nRelay latency (commit-to-append):")
        print(f"  Sample count:          {len(relay_latencies)}")
        print(f"  Latency p50:           {r_p50:.2f} ms")
        print(f"  Latency p95:           {r_p95:.2f} ms")
        print(f"  Latency max:           {r_max:.2f} ms\n")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Transactional outbox contention benchmark")
    parser.add_argument("--writers", type=int, default=8, help="Number of concurrent writer threads (default: 8)")
    parser.add_argument("--seconds", type=int, default=60, help="Benchmark duration in seconds (default: 60)")
    parser.add_argument("--topic", type=str, default="jobs.terminal", help="Durable topic to record to")
    parser.add_argument(
        "--measure-relay",
        action="store_true",
        help="Measure commit-to-append latency through Redis Streams relay",
    )

    args = parser.parse_args()
    run_benchmark(
        writers=args.writers,
        duration_seconds=args.seconds,
        topic=args.topic,
        measure_relay=args.measure_relay,
    )


if __name__ == "__main__":
    main()
