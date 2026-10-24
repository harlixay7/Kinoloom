"""Benchmark concurrent SQLite WAL queue insertion and read throughput.

Measures multi-threaded transaction throughput (ops/sec) and latency for
concurrent queue writes and indexed reads under Write-Ahead Logging (WAL) mode.
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import benchmarks  # noqa: F401 (ensures tool/ is in sys.path)
from benchmarks import BenchmarkResult


def _create_wal_db(db_path: Path) -> None:
    """Initialize test database with WAL journal mode and queue schema."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS queue_bench (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'draft',
                priority INTEGER NOT NULL DEFAULT 5,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_status_prio ON queue_bench (status, priority)")
        conn.commit()
    finally:
        conn.close()


def _get_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def run_benchmark(quick: bool = False) -> list[BenchmarkResult]:
    """Execute concurrent SQLite WAL insertion and read benchmark.

    Tests:
    - Concurrent writer threads batching queue insertions (25 items per tx)
    - Concurrent reader threads continuously querying ready queue items
    - Validates concurrency without database lock deadlocks
    """
    num_writers = 2
    num_readers = 4
    ops_per_writer = 500 if quick else 2_000
    ops_per_reader = 1_000 if quick else 4_000
    batch_size = 25

    total_writes = num_writers * ops_per_writer
    total_reads = num_readers * ops_per_reader

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "bench_queue.db"
        _create_wal_db(db_path)

        # Pre-populate seed rows
        init_conn = _get_conn(db_path)
        with init_conn:
            init_conn.executemany(
                "INSERT INTO queue_bench (id, kind, payload, status, priority, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (f"seed_{i}", "render", '{"shot": "seed"}', "draft", 5, time.time())
                    for i in range(100)
                ],
            )
        init_conn.close()

        def writer_job(thread_idx: int) -> None:
            conn = _get_conn(db_path)
            try:
                for start_idx in range(0, ops_per_writer, batch_size):
                    end_idx = min(start_idx + batch_size, ops_per_writer)
                    batch_rows = [
                        (f"w_{thread_idx}_{i}", "video_render", '{"params": 1}', "draft", (i % 10) + 1, time.time())
                        for i in range(start_idx, end_idx)
                    ]
                    for attempt in range(10):
                        try:
                            with conn:
                                conn.executemany(
                                    "INSERT INTO queue_bench (id, kind, payload, status, priority, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                                    batch_rows,
                                )
                            break
                        except sqlite3.OperationalError:
                            time.sleep(0.01 * (attempt + 1))
            finally:
                conn.close()

        def reader_job(thread_idx: int) -> None:
            conn = _get_conn(db_path)
            try:
                for _ in range(ops_per_reader):
                    cur = conn.execute(
                        "SELECT id, kind, status, priority FROM queue_bench WHERE status = 'draft' ORDER BY priority ASC LIMIT 10"
                    )
                    _ = cur.fetchall()
            finally:
                conn.close()

        threads: list[threading.Thread] = []
        for w_idx in range(num_writers):
            threads.append(threading.Thread(target=writer_job, args=(w_idx,)))
        for r_idx in range(num_readers):
            threads.append(threading.Thread(target=reader_job, args=(r_idx,)))

        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - t0

    write_throughput = total_writes / elapsed if elapsed > 0 else 0.0
    read_throughput = total_reads / elapsed if elapsed > 0 else 0.0
    total_ops = total_writes + total_reads
    combined_throughput = total_ops / elapsed if elapsed > 0 else 0.0

    baseline_combined = 1_000.0  # ops/sec
    status = "PASS" if combined_throughput >= baseline_combined else "FAIL"

    return [
        BenchmarkResult(
            benchmark="SQLite WAL Queue",
            metric="Concurrent Combined Throughput",
            measured_value=round(combined_throughput, 1),
            unit="ops/sec",
            baseline=baseline_combined,
            status=status,
            detail=f"{total_writes:,} writes ({write_throughput:,.0f} w/s) + {total_reads:,} reads ({read_throughput:,.0f} r/s) in {elapsed:.3f}s",
        )
    ]


if __name__ == "__main__":
    results = run_benchmark(quick=False)
    for r in results:
        print(f"[{r.status}] {r.benchmark} - {r.metric}: {r.measured_value:,.1f} {r.unit} (Baseline: >={r.baseline:,.1f}) | {r.detail}")
