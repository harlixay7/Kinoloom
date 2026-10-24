"""Benchmark combo scheduler (build_combos) throughput and latency.

Measures combination generation rate (combos/sec) across character, matrix,
power, and environment prompt generation spaces.
"""
from __future__ import annotations

import time
from typing import Any

import benchmarks  # noqa: F401 (ensures tool/ is in sys.path)
from benchmarks import BenchmarkResult
from jobs import build_combos


def run_benchmark(quick: bool = False) -> list[BenchmarkResult]:
    """Execute combo scheduler throughput benchmark.

    Tests:
    1. Multi-variation character prompt matrix
    2. Wardrobe x weapon equipment matrix
    3. Multi-subtype environment combinatorial space
    4. Power intensity progression
    """
    iterations = 2_000 if quick else 10_000

    test_cases: list[tuple[str, dict[str, Any]]] = [
        (
            "character",
            {
                "emotions": ["stoic", "smirking", "intense", "somber", "defiant", "terrified"],
                "shots": ["close-up", "medium", "wide", "extreme wide", "dutch angle"],
            },
        ),
        (
            "character",
            {
                "matrix": [
                    {"outfit": "combat exo-suit", "weapon": "heavy railgun"},
                    {"outfit": "stealth fatigues", "weapon": "silenced pistol"},
                    {"outfit": "dress uniform", "weapon": "ceremonial blade"},
                    {"outfit": "civilian attire", "weapon": ""},
                    {"outfit": "damaged armor", "weapon": "overheated plasma carbine"},
                ],
                "emotions": ["focused"],
                "shots": ["medium"],
            },
        ),
        (
            "power",
            {
                "intensities": ["dormant", "whisper", "surge", "cataclysmic", "aftermath"],
            },
        ),
        (
            "environment",
            {
                "subtypes": ["cyberpunk_slums", "orbital_station", "neon_harbor"],
                "locations": {
                    "cyberpunk_slums": ["back alley", "rooftop garden", "steam vent corridor"],
                    "orbital_station": ["decompression airlock", "observation cupola"],
                    "neon_harbor": ["cargo docks", "floating bazaar", "shipyard"],
                },
                "times": ["perpetual twilight", "solar flare noon", "dead of space"],
                "cameras": ["slow sweeping dolly", "crane high angle", "handheld tracking"],
            },
        ),
    ]

    # Warm-up pass
    for mode, params in test_cases:
        _ = build_combos(mode, params)

    total_combos = 0
    t0 = time.perf_counter()
    for _ in range(iterations):
        for mode, params in test_cases:
            combos = build_combos(mode, params)
            total_combos += len(combos)
    elapsed = time.perf_counter() - t0

    throughput = total_combos / elapsed if elapsed > 0 else 0.0
    latency_us = (elapsed / total_combos * 1_000_000) if total_combos > 0 else 0.0

    baseline_throughput = 20_000.0  # minimum acceptable combos/sec
    status = "PASS" if throughput >= baseline_throughput else "FAIL"

    return [
        BenchmarkResult(
            benchmark="Scheduler",
            metric="Combo Generation Throughput",
            measured_value=round(throughput, 1),
            unit="combos/sec",
            baseline=baseline_throughput,
            status=status,
            detail=f"{total_combos:,} combos generated in {elapsed:.3f}s ({latency_us:.2f} µs/combo)",
        )
    ]


if __name__ == "__main__":
    results = run_benchmark(quick=False)
    for r in results:
        print(f"[{r.status}] {r.benchmark} - {r.metric}: {r.measured_value:,.1f} {r.unit} (Baseline: >={r.baseline:,.1f}) | {r.detail}")
