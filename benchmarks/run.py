"""Unified CLI benchmark runner for Kinoloom.

Runs the complete studio benchmark suite:
- Combo Scheduler Throughput (bench_scheduler)
- Story Architect JSON Recovery (bench_json_recovery)
- Canon Linter Throughput (bench_linter)
- SQLite WAL Concurrency (bench_sqlite_wal)
- GPU Broker FIFO Mutex (bench_gpu_broker)

Usage:
    python -m benchmarks.run [--quick]
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Callable

import benchmarks  # noqa: F401 (ensures tool/ is in sys.path)
from benchmarks import BenchmarkResult
from benchmarks.bench_gpu_broker import run_benchmark as run_gpu_broker
from benchmarks.bench_json_recovery import run_benchmark as run_json_recovery
from benchmarks.bench_linter import run_benchmark as run_linter
from benchmarks.bench_scheduler import run_benchmark as run_scheduler
from benchmarks.bench_sqlite_wal import run_benchmark as run_sqlite_wal


BENCHMARK_SUITE: list[tuple[str, Callable[[bool], list[BenchmarkResult]]]] = [
    ("Combo Scheduler", run_scheduler),
    ("JSON Self-Healing", run_json_recovery),
    ("Canon Linter", run_linter),
    ("SQLite WAL Queue", run_sqlite_wal),
    ("GPU Broker Mutex", run_gpu_broker),
]


def print_summary_table(results: list[BenchmarkResult], total_elapsed: float) -> int:
    """Print an aligned ASCII benchmark report table and return process exit code."""
    line_width = 100
    print("\n" + "=" * line_width)
    print("  Kinoloom - Core Subsystem Benchmark Suite")
    print("=" * line_width)

    header = f"{'Benchmark':<30} {'Metric':<30} {'Measured':>12} {'Unit':<12} {'Baseline':>10} {'Status':^8}"
    print(header)
    print("-" * line_width)

    all_passed = True
    for r in results:
        status_str = f"[{r.status}]"
        # Format measured value nicely
        if r.measured_value >= 1000:
            val_str = f"{r.measured_value:,.1f}"
            base_str = f">={r.baseline:,.0f}" if r.baseline > 0 else f"<={r.baseline:,.0f}"
        else:
            val_str = f"{r.measured_value:.3f}" if r.unit == "ms/op" else f"{r.measured_value:.1f}"
            base_str = f"<={r.baseline:.1f}" if r.unit == "ms/op" else f">={r.baseline:.1f}"

        row = f"{r.benchmark:<30} {r.metric:<30} {val_str:>12} {r.unit:<12} {base_str:>10} {status_str:^8}"
        print(row)
        if not r.passes_baseline():
            all_passed = False

    print("=" * line_width)
    total_checks = len(results)
    passed_count = sum(1 for r in results if r.passes_baseline())
    pass_pct = (passed_count / total_checks * 100.0) if total_checks > 0 else 0.0

    overall_verdict = "PASSED" if all_passed else "FAILED"
    print(
        f"Summary: {overall_verdict} - {passed_count}/{total_checks} metrics met baseline ({pass_pct:.1f}%) in {total_elapsed:.2f}s"
    )
    print("=" * line_width + "\n")
    return 0 if all_passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Kinoloom Subsystem Benchmark Suite")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run reduced iterations for quick smoke / CI verification",
    )
    args = parser.parse_args()

    mode_label = "QUICK" if args.quick else "STANDARD"
    print(f"[*] Starting Kinoloom benchmarks (Mode: {mode_label})...")

    all_results: list[BenchmarkResult] = []
    t0 = time.perf_counter()

    for name, bench_fn in BENCHMARK_SUITE:
        print(f"  -> Running {name}...", end="", flush=True)
        sub_t0 = time.perf_counter()
        try:
            results = bench_fn(args.quick)
            sub_elapsed = time.perf_counter() - sub_t0
            all_results.extend(results)
            print(f" done ({sub_elapsed:.2f}s)")
        except Exception as e:
            sub_elapsed = time.perf_counter() - sub_t0
            print(f" ERROR ({e})")
            all_results.append(
                BenchmarkResult(
                    benchmark=name,
                    metric="Execution",
                    measured_value=0.0,
                    unit="error",
                    baseline=0.0,
                    status="FAIL",
                    detail=str(e),
                )
            )

    total_elapsed = time.perf_counter() - t0
    return print_summary_table(all_results, total_elapsed)


if __name__ == "__main__":
    sys.exit(main())
