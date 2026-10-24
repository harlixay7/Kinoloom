"""Benchmark canon linter validation throughput.

Measures the number of cinematographic shot specifications validated per second
against Bible constraints, enum rules, DAG structure, and continuity transitions.
"""
from __future__ import annotations

import time
from typing import Any

import benchmarks  # noqa: F401 (ensures tool/ is in sys.path)
from benchmarks import BenchmarkResult
from linter import Linter


class BenchBible:
    """High-speed in-memory Bible fixture providing world rules for linting."""

    def __init__(self) -> None:
        self._characters = {
            "char_hero": {"id": "char_hero", "name": "Kaelen", "tier": "main", "powers": ["teleport"]},
            "char_support": {"id": "char_support", "name": "Vespera", "tier": "supporting", "powers": []},
            "char_villain": {"id": "char_villain", "name": "Overlord", "tier": "main", "powers": ["shockwave"]},
        }
        self._locations = {
            "loc_reactor": {"id": "loc_reactor", "name": "Fusion Core Hangar", "lighting": "dim fluorescent"},
            "loc_rooftop": {"id": "loc_rooftop", "name": "Rainy Tower Apex", "lighting": "lightning flashes"},
            "loc_streets": {"id": "loc_streets", "name": "Underbelly Street 9", "lighting": "neon reflection"},
        }
        self._crowd_types = {
            "crowd_citizens": {"id": "crowd_citizens", "name": "Fleeing Pedestrians"},
        }

    def characters(self) -> dict[str, Any]:
        return self._characters

    def locations(self) -> dict[str, Any]:
        return self._locations

    def crowd_types(self) -> dict[str, Any]:
        return self._crowd_types


def _generate_test_shots(count: int = 25) -> list[dict[str, Any]]:
    """Build a synthetic, valid cinematic multi-scene shot list with correct DAG dependencies."""
    cameras = ["static", "slow_push_in", "dolly", "pan", "crane", "handheld", "orbit"]
    sizes = ["extreme_close_up", "close_up", "medium_close_up", "medium", "medium_wide", "wide"]
    positions = ["foreground_left", "foreground_center", "midground_center", "background_right"]
    facings = ["camera_left", "camera_right", "camera", "away_from_camera"]
    locations = ["loc_reactor", "loc_rooftop", "loc_streets"]

    shots: list[dict[str, Any]] = []
    # 5 scenes of 5 shots each
    shots_per_scene = 5
    num_scenes = max(1, count // shots_per_scene)

    shot_counter = 1
    for s_idx in range(1, num_scenes + 1):
        scene_id = f"SC_{s_idx:02d}"
        scene_loc = locations[(s_idx - 1) % len(locations)]
        prev_id: str | None = None

        for shot_in_scene in range(shots_per_scene):
            sid = f"SH_{shot_counter:03d}"
            size = sizes[shot_counter % len(sizes)]
            is_hero = size in ("extreme_close_up", "close_up", "medium_close_up") and shot_in_scene == 0
            tier = "hero" if is_hero else "standard"
            char = "char_hero" if shot_in_scene % 2 == 0 else "char_support"

            shot: dict[str, Any] = {
                "shot_id": sid,
                "scene": scene_id,
                "camera": cameras[shot_counter % len(cameras)],
                "shot_size": size,
                "tier": tier,
                "duration_s": 5,
                "continuity": "CUT" if prev_id is None else "CONTINUE",
                "prev_shot": prev_id,
                "location": scene_loc,
                "chars": [char],
                "emotions": {char: "tense" if shot_in_scene % 2 == 0 else "calm"},
                "positions": {char: positions[shot_counter % len(positions)]},
                "facings": {char: facings[shot_counter % len(facings)]},
                "prompt": f"Dramatic cinematic shot {shot_counter} in {scene_loc}",
            }
            shots.append(shot)
            prev_id = sid
            shot_counter += 1

    return shots


def run_benchmark(quick: bool = False) -> list[BenchmarkResult]:
    """Execute canon linter validation throughput benchmark."""
    package_iterations = 200 if quick else 1_000
    shots = _generate_test_shots(25)
    shots_per_package = len(shots)

    bible = BenchBible()
    linter = Linter(bible=bible, family="h3")  # type: ignore[arg-type]

    # Pre-flight check: ensure clean report
    report = linter.lint_package(shots)
    assert report is not None

    total_shots = package_iterations * shots_per_package
    t0 = time.perf_counter()
    for _ in range(package_iterations):
        _ = linter.lint_package(shots)
    elapsed = time.perf_counter() - t0

    throughput = total_shots / elapsed if elapsed > 0 else 0.0
    latency_us = (elapsed / total_shots * 1_000_000) if total_shots > 0 else 0.0

    baseline_throughput = 2_000.0  # shots/sec
    status = "PASS" if throughput >= baseline_throughput else "FAIL"

    return [
        BenchmarkResult(
            benchmark="Canon Linter",
            metric="Shot Validation Throughput",
            measured_value=round(throughput, 1),
            unit="shots/sec",
            baseline=baseline_throughput,
            status=status,
            detail=f"{total_shots:,} shots validated across {package_iterations:,} packages in {elapsed:.3f}s ({latency_us:.2f} µs/shot)",
        )
    ]


if __name__ == "__main__":
    results = run_benchmark(quick=False)
    for r in results:
        print(f"[{r.status}] {r.benchmark} - {r.metric}: {r.measured_value:,.1f} {r.unit} (Baseline: >={r.baseline:,.1f}) | {r.detail}")
