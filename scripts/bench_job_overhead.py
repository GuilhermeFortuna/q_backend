#!/usr/bin/env python3
"""Overhead benchmark for jobs with progress and terminal streaming events.

Submits fixed optimization requests to POST /api/v1/optimize, polls
status until terminal, and prints wall-clock time per run.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import statistics
import sys
import time
from typing import Any

import httpx
import yaml


def _load_base_config(config_path: Path, trials: int) -> dict[str, Any]:
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    else:
        # Fallback to minimal fixed optimization config
        cfg = {
            "study": {
                "name": "bench_overhead",
                "direction": "maximize",
                "storage": {"type": "memory"},
            },
            "objective": {"mode": "maximize_net_profit"},
            "backtest": {
                "symbol": "WIN$",
                "timeframe": "D1",
                "start": "2024-01-01T00:00:00",
                "end": "2024-02-01T00:00:00",
                "initial_capital": 10000.0,
                "point_value": 0.2,
                "strategy": "MACrossover",
            },
            "search_space": {
                "strategy_params": {
                    "short_period": {"type": "int", "low": 2, "high": 5},
                    "long_period": {"type": "int", "low": 10, "high": 20},
                },
                "risk_params": {
                    "type": {"type": "categorical", "choices": ["fixed_quantity"]},
                    "quantity": {"type": "float", "low": 1.0, "high": 2.0},
                },
            },
        }

    cfg.setdefault("study", {})
    cfg["study"]["n_trials"] = trials
    # Ensure in-memory storage for benchmark isolation if SQLite not required
    if "storage" not in cfg["study"] or cfg["study"]["storage"].get("type") != "memory":
        cfg["study"]["storage"] = {"type": "memory"}

    return cfg


def run_benchmark(
    base_url: str,
    runs: int,
    trials: int,
    poll_interval: float,
    config_path: Path,
) -> int:
    base_url = base_url.rstrip("/")
    print(f"=== Job Overhead Benchmark ===")
    print(f"Target: {base_url}/api/v1/optimize")
    print(f"Runs: {runs}, Trials per run: {trials}")
    print(f"Poll interval: {poll_interval}s")
    print()

    durations: list[float] = []

    client = httpx.Client(base_url=base_url, timeout=30.0)

    for run_idx in range(1, runs + 1):
        config = _load_base_config(config_path, trials)
        config["study"]["name"] = f"bench_overhead_run_{run_idx}_{int(time.time())}"

        t0 = time.perf_counter()
        try:
            resp = client.post("/api/v1/optimize", json=config)
            resp.raise_for_status()
            data = resp.json()
            study_id = data.get("study_id")
            if not study_id:
                print(f"Run {run_idx}: ERROR - No study_id in response: {data}", file=sys.stderr)
                return 1
        except Exception as exc:  # noqa: BLE001 - CLI script reporting error
            print(f"Run {run_idx}: ERROR submitting optimization: {exc}", file=sys.stderr)
            return 1

        print(f"Run {run_idx}/{runs}: Submitted study {study_id}, polling...")

        final_status = None
        while True:
            try:
                status_resp = client.get(f"/api/v1/optimize/{study_id}")
                status_resp.raise_for_status()
                status_data = status_resp.json()
                raw_status = status_data.get("status", "").lower()
                if raw_status in ("completed", "failed", "cancelled", "done"):
                    final_status = raw_status
                    break
            except Exception as exc:  # noqa: BLE001 - CLI script polling error
                print(f"Run {run_idx}: Warning polling status: {exc}", file=sys.stderr)

            time.sleep(poll_interval)

        elapsed = time.perf_counter() - t0
        durations.append(elapsed)
        print(f"Run {run_idx}/{runs}: Finished in {elapsed:.3f}s (status: {final_status})")

    print()
    print("=== Summary ===")
    for i, d in enumerate(durations, 1):
        print(f"Run {i}: {d:.3f}s")

    median_duration = statistics.median(durations)
    mean_duration = statistics.mean(durations)
    print(f"Median wall-clock time: {median_duration:.3f}s")
    print(f"Mean wall-clock time:   {mean_duration:.3f}s")
    print(f"Min: {min(durations):.3f}s, Max: {max(durations):.3f}s")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark optimization job overhead via REST API.")
    parser.add_argument("--kind", choices=["optimization"], default="optimization", help="Job kind to benchmark")
    parser.add_argument("--runs", type=int, default=3, help="Number of benchmark runs")
    parser.add_argument("--trials", type=int, default=10, help="Number of trials per study")
    parser.add_argument("--poll-interval", type=float, default=0.1, help="Poll interval in seconds")
    parser.add_argument("--base-url", type=str, default="http://127.0.0.1:8000", help="Backend API base URL")
    parser.add_argument(
        "--config-path",
        type=str,
        default="configs/optimization/examples/ma_crossover_sharpe.yaml",
        help="Path to optimization configuration YAML template",
    )

    args = parser.parse_args()
    config_file = Path(args.config_path)

    sys.exit(
        run_benchmark(
            base_url=args.base_url,
            runs=args.runs,
            trials=args.trials,
            poll_interval=args.poll_interval,
            config_path=config_file,
        )
    )


if __name__ == "__main__":
    main()
