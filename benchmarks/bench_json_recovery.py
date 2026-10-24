"""Benchmark extract_json latency across clean, fenced, corrupted, and truncated JSON payloads.

Measures extraction latency (ms) and recovery throughput (ops/sec) across
all five stages of the Story Architect self-healing JSON parser.
"""
from __future__ import annotations

import time

import benchmarks  # noqa: F401 (ensures tool/ is in sys.path)
from benchmarks import BenchmarkResult
from llm import extract_json

PAYLOAD_CLEAN = """
{
    "bible": {
        "title": "Neon Horizon",
        "genre": "cyberpunk noir",
        "characters": [
            {"id": "char_01", "name": "Kaelen Drake", "role": "rogue operative", "tier": "hero"},
            {"id": "char_02", "name": "Vespera", "role": "synth informant", "tier": "supporting"}
        ],
        "locations": [
            {"id": "loc_01", "name": "Lower Slums Sector 4", "mood": "rain-soaked neon"}
        ]
    }
}
"""

PAYLOAD_FENCED = """
Here is the production breakdown matching your specifications:

```json
{
    "shots": [
        {"shot_id": "SH_001", "scene": "SC_01", "camera": "dolly", "duration": 4.5, "chars": ["char_01"]},
        {"shot_id": "SH_002", "scene": "SC_01", "camera": "slow_push_in", "duration": 3.0, "chars": ["char_01", "char_02"]}
    ]
}
```

Please review the framing coordinates before queuing GPU render.
"""

PAYLOAD_CORRUPTED = """
Prompt instructions:
Output strictly in JSON format like {"example": true}

Response:
{
    "treatment": {
        "act1": "Discovery of the corrupted memory core",
        "turning_point": "Ambush at the mag-train transit hub",
        "characters": ["Kaelen", "Vespera", "Enforcer Unit 9",],
        "logistics": {
            "vram_budget_mb": 7680,
            "render_priority": 1,
        },
    }
}
"""

PAYLOAD_TRUNCATED = """
{
    "treatment": {
        "title": "The Glass Citadel",
        "acts": [
            {
                "act_num": 1,
                "scenes": [
                    {"scene_id": "SC_01", "summary": "Opening sunrise over the broken sea", "shots": [
                        {"shot_id": "SH_01", "prompt": "wide establishing shot of rusted spires"
"""


def run_benchmark(quick: bool = False) -> list[BenchmarkResult]:
    """Execute extract_json benchmark across payload archetypes."""
    iterations = 500 if quick else 2_500

    payloads = {
        "clean": PAYLOAD_CLEAN,
        "fenced": PAYLOAD_FENCED,
        "corrupted": PAYLOAD_CORRUPTED,
        "truncated": PAYLOAD_TRUNCATED,
    }

    # Verify all payloads parse successfully
    for name, p in payloads.items():
        res = extract_json(p)
        assert res is not None, f"Payload {name} failed to parse in extract_json"

    results: list[BenchmarkResult] = []
    total_ops = 0
    total_time = 0.0

    for name, payload in payloads.items():
        t0 = time.perf_counter()
        for _ in range(iterations):
            _ = extract_json(payload)
        elapsed = time.perf_counter() - t0

        total_ops += iterations
        total_time += elapsed

        ops_per_sec = iterations / elapsed if elapsed > 0 else 0.0
        latency_ms = (elapsed / iterations) * 1000.0 if iterations > 0 else 0.0

        # Sub-millisecond latency baseline for clean/fenced/corrupted/truncated
        baseline_ops = 1_000.0
        status = "PASS" if ops_per_sec >= baseline_ops else "FAIL"

        results.append(
            BenchmarkResult(
                benchmark=f"JSON Recovery ({name})",
                metric="Parsing Latency",
                measured_value=round(latency_ms, 3),
                unit="ms/op",
                baseline=1.0,
                status=status,
                detail=f"{ops_per_sec:,.0f} ops/sec across {iterations:,} executions",
            )
        )

    # Composite throughput
    composite_throughput = total_ops / total_time if total_time > 0 else 0.0
    status_composite = "PASS" if composite_throughput >= 2_000.0 else "FAIL"
    results.append(
        BenchmarkResult(
            benchmark="JSON Recovery (All Archetypes)",
            metric="Composite Throughput",
            measured_value=round(composite_throughput, 1),
            unit="ops/sec",
            baseline=2_000.0,
            status=status_composite,
            detail=f"{total_ops:,} mixed operations completed in {total_time:.3f}s",
        )
    )

    return results


if __name__ == "__main__":
    results = run_benchmark(quick=False)
    for r in results:
        print(f"[{r.status}] {r.benchmark} - {r.metric}: {r.measured_value} {r.unit} (Baseline: {r.baseline}) | {r.detail}")
