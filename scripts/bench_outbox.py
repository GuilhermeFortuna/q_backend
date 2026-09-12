#!/usr/bin/env python3
"""Contention benchmark for transactional outbox recording.

Measures throughput and recording latency across N writer threads on one topic.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import threading
import time

import numpy as np
from sqlalchemy.orm import sessionmaker

from q_backend.storage.db.engine import get_engine
from q_backend.streaming.outbox import record_event


def run_benchmark(writers: int, duration_seconds: int, topic: str = "jobs.terminal") -> dict[str, float]:
    engine = get_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    stop_event = threading.Event()
    all_latencies: list[float] = []
    latencies_lock = threading.Lock()
    total_events = 0
    total_lock = threading.Lock()

    print(f"Starting outbox benchmark: {writers} writers, {duration_seconds}s on topic '{topic}'...")

    def writer_task(writer_id: int) -> None:
        local_latencies: list[float] = []
        count = 0
        while not stop_event.is_set():
            count += 1
            payload = {
                "job_id": f"bench-{writer_id}-{count}",
                "kind": "backtest",
                "status": "completed",
                "finished_at": "2026-09-12T00:00:00Z",
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

    lat_arr = np.array(all_latencies) if all_latencies else np.array([0.0])
    p50 = float(np.percentile(lat_arr, 50))
    p95 = float(np.percentile(lat_arr, 95))
    p99 = float(np.percentile(lat_arr, 99))

    print("\nBenchmark results:")
    print(f"  Writers:               {writers}")
    print(f"  Duration:              {elapsed:.2f} s")
    print(f"  Total events recorded: {total_events}")
    print(f"  Events per second:     {events_per_sec:.2f}")
    print(f"  Latency p50:           {p50:.2f} ms")
    print(f"  Latency p95:           {p95:.2f} ms")
    print(f"  Latency p99:           {p99:.2f} ms\n")

    return {
        "writers": float(writers),
        "duration": elapsed,
        "total_events": float(total_events),
        "events_per_sec": events_per_sec,
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Transactional outbox contention benchmark")
    parser.add_argument("--writers", type=int, default=8, help="Number of concurrent writer threads (default: 8)")
    parser.add_argument("--seconds", type=int, default=60, help="Benchmark duration in seconds (default: 60)")
    parser.add_argument("--topic", type=str, default="jobs.terminal", help="Durable topic to record to")

    args = parser.parse_args()
    run_benchmark(writers=args.writers, duration_seconds=args.seconds, topic=args.topic)


if __name__ == "__main__":
    main()
