"""Kinoloom — Reproducible Benchmark Suite.

Provides standardized throughput and latency benchmarks for critical studio subsystems:
- Combo Scheduler (jobs.py:build_combos)
- JSON Extraction & Self-Healing (llm.py:extract_json)
- Canon Linter (linter.py:Linter)
- SQLite WAL Database Concurrency (task_queue.py / SQLite WAL)
- GPU Broker Mutex & FIFO Ticket Arbitration (gpu_broker.py:_Broker)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

# Ensure 'tool' directory is on sys.path for direct imports
BENCHMARKS_DIR = Path(__file__).resolve().parent
ROOT_DIR = BENCHMARKS_DIR.parent
TOOL_DIR = ROOT_DIR / "tool"

if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


@dataclass
class BenchmarkResult:
    benchmark: str
    metric: str
    measured_value: float
    unit: str
    baseline: float
    status: str
    detail: str = ""

    def passes_baseline(self) -> bool:
        return self.status.upper() == "PASS"
