"""Benchmark GPU broker FIFO ticket queue lease acquisition and release overhead under contention.

Measures synchronization throughput (leases/sec) and arbitration latency (ms)
when multiple worker threads contend for the single-GPU execution mutex.
"""
from __future__ import annotations

import threading
import time

import benchmarks  # noqa: F401 (ensures tool/ is in sys.path)
from benchmarks import BenchmarkResult
from gpu_broker import _Broker, _Instance


def run_benchmark(quick: bool = False) -> list[BenchmarkResult]:
    """Execute GPU broker lease arbitration benchmark under contention."""
    num_threads = 4
    leases_per_thread = 250 if quick else 500
    total_leases = num_threads * leases_per_thread

    # Construct clean broker instance
    broker = _Broker()
    broker.is_local = lambda p: True  # type: ignore[assignment]
    broker._prepare_for = lambda lease, s: None  # type: ignore[assignment]
    bench_inst = _Instance(8188)
    broker._instance = lambda p: bench_inst  # type: ignore[assignment]

    latencies_ms: list[float] = []
    lat_lock = threading.Lock()

    def worker_loop(thread_idx: int) -> None:
        local_latencies: list[float] = []
        for i in range(leases_per_thread):
            t_start = time.perf_counter()
            lease = broker.acquire(port=8188, purpose=f"bench_{thread_idx}_{i}")
            broker.release(lease)
            t_end = time.perf_counter()
            local_latencies.append((t_end - t_start) * 1000.0)

        with lat_lock:
            latencies_ms.extend(local_latencies)

    threads = [threading.Thread(target=worker_loop, args=(t_id,)) for t_id in range(num_threads)]

    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0

    throughput = total_leases / elapsed if elapsed > 0 else 0.0
    mean_latency = (sum(latencies_ms) / len(latencies_ms)) if latencies_ms else 0.0

    baseline_throughput = 500.0  # leases/sec under 4-way thread contention
    status = "PASS" if throughput >= baseline_throughput else "FAIL"

    return [
        BenchmarkResult(
            benchmark="GPU Broker FIFO Mutex",
            metric="Lease Arbitration Rate",
            measured_value=round(throughput, 1),
            unit="leases/sec",
            baseline=baseline_throughput,
            status=status,
            detail=f"{total_leases:,} leases acquired & released across {num_threads} threads in {elapsed:.3f}s (mean latency: {mean_latency:.2f} ms)",
        )
    ]


if __name__ == "__main__":
    results = run_benchmark(quick=False)
    for r in results:
        print(f"[{r.status}] {r.benchmark} - {r.metric}: {r.measured_value:,.1f} {r.unit} (Baseline: >={r.baseline:,.1f}) | {r.detail}")
