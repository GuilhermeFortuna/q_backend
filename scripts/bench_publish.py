#!/usr/bin/env python3
"""Throughput and latency benchmark for ephemeral stream publishing.

Measures publishing throughput and latency across N concurrent worker processes.
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import time
from typing import Any

import numpy as np

from q_backend.streaming.publisher import EphemeralPublisher
from q_backend.streaming.redis_binary import get_binary_redis


def _worker_process(
    proc_id: int,
    topic: str,
    stop_event: Any,
    result_queue: Any,
) -> None:
    client = get_binary_redis()
    publisher = EphemeralPublisher(client, topic, producer_id=f"bench-pub-{proc_id}")

    count = 0
    latencies: list[float] = []

    payload_template = {
        "job_id": f"bench-job-{proc_id}",
        "kind": "backtest",
        "status": "running",
        "progress": 0.5,
    }
    routing_key = {"kind": "backtest", "job_id": f"bench-job-{proc_id}"}
    schema = "schema/stream/payloads/job-progress.schema.json"

    while not stop_event.is_set():
        count += 1
        t0 = time.perf_counter()
        publisher.publish(
            routing_key=routing_key,
            payload_kind="control",
            payload_schema=schema,
            payload=payload_template,
        )
        t1 = time.perf_counter()
        latencies.append((t1 - t0) * 1000.0)

    result_queue.put((count, latencies))


def run_publish_benchmark(
    processes: int = 8,
    duration_seconds: int = 60,
    topic: str = "jobs.progress",
) -> dict[str, float]:
    stop_event = mp.Event()
    result_queue: mp.Queue = mp.Queue()

    print(f"Starting ephemeral publish benchmark: {processes} processes, {duration_seconds}s on topic '{topic}'...")

    workers = [
        mp.Process(
            target=_worker_process,
            args=(i, topic, stop_event, result_queue),
        )
        for i in range(processes)
    ]

    start_time = time.perf_counter()
    for w in workers:
        w.start()

    time.sleep(duration_seconds)
    stop_event.set()

    total_published = 0
    all_latencies: list[float] = []

    for _ in range(processes):
        cnt, lats = result_queue.get()
        total_published += cnt
        all_latencies.extend(lats)

    for w in workers:
        w.join(timeout=5.0)

    elapsed = time.perf_counter() - start_time
    throughput = total_published / elapsed if elapsed > 0 else 0.0

    lat_arr = np.array(all_latencies) if all_latencies else np.array([0.0])
    p50 = float(np.percentile(lat_arr, 50))
    p95 = float(np.percentile(lat_arr, 95))
    p99 = float(np.percentile(lat_arr, 99))
    max_lat = float(np.max(lat_arr))

    print("\n==================================================")
    print(" Ephemeral Publish Benchmark Results")
    print("==================================================")
    print(f" Processes:             {processes}")
    print(f" Duration:              {elapsed:.2f} s")
    print(f" Total published:       {total_published} entries")
    print(f" Throughput:            {throughput:.2f} entries/sec")
    print(f" Latency p50:           {p50:.2f} ms")
    print(f" Latency p95:           {p95:.2f} ms")
    print(f" Latency p99:           {p99:.2f} ms")
    print(f" Latency max:           {max_lat:.2f} ms")
    print("==================================================\n")

    return {
        "processes": float(processes),
        "duration": elapsed,
        "total_published": float(total_published),
        "throughput": throughput,
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
        "max_ms": max_lat,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Ephemeral publish benchmark")
    parser.add_argument(
        "--processes",
        type=int,
        default=8,
        help="Number of publishing worker processes (default: 8)",
    )
    parser.add_argument(
        "--seconds",
        type=int,
        default=60,
        help="Benchmark duration in seconds (default: 60)",
    )
    parser.add_argument(
        "--topic",
        type=str,
        default="jobs.progress",
        help="Ephemeral topic to publish to (default: jobs.progress)",
    )

    args = parser.parse_args()
    run_publish_benchmark(
        processes=args.processes,
        duration_seconds=args.seconds,
        topic=args.topic,
    )


if __name__ == "__main__":
    main()
