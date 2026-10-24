import pytest
from jobs import build_combos, distribute, build_plan, MAX_BATCH


def test_build_combos_character_identity_plates():
    """Identity plates mode yields close, medium, and full hero plates."""
    params = {"identity_plates": True}
    combos = build_combos("character", params)
    assert len(combos) == 3
    assert [c["plate"] for c in combos] == ["close", "medium", "full"]
    assert all(c["emotion"] == "stoic" for c in combos)


def test_build_combos_character_cross_product():
    """Standard character mode multiplies emotions x shots."""
    params = {
        "emotions": ["stoic", "smiling", "angry"],
        "shots": ["portrait", "full-body"],
    }
    combos = build_combos("character", params)
    assert len(combos) == 6
    assert combos[0] == {"emotion": "stoic", "shot": "portrait"}
    assert combos[-1] == {"emotion": "angry", "shot": "full-body"}


def test_distribute_determinism_with_identical_seeds():
    """Identical seeds produce byte-identical task plans and seed assignments."""
    model_rows = [
        {"model": "realvis", "count": 4, "steps": 25, "cfg": 7.0},
        {"model": "albedobase", "count": 2, "steps": 30, "cfg": 6.5},
    ]
    combos = [
        {"emotion": "stoic", "shot": "portrait"},
        {"emotion": "determined", "shot": "full-body"},
    ]

    tasks_run1 = distribute(model_rows, combos, seed=123456)
    tasks_run2 = distribute(model_rows, combos, seed=123456)

    assert len(tasks_run1) == 6
    assert len(tasks_run2) == 6
    for t1, t2 in zip(tasks_run1, tasks_run2):
        assert t1["model"] == t2["model"]
        assert t1["seed"] == t2["seed"]
        assert t1["combo"] == t2["combo"]
        assert t1["k"] == t2["k"]

    # Changing the seed changes the task seeds deterministically
    tasks_diff_seed = distribute(model_rows, combos, seed=999999)
    assert all(t1["seed"] != td["seed"] for t1, td in zip(tasks_run1, tasks_diff_seed))


def test_distribute_strided_distribution_and_seed_advancement():
    """Strided combo selection distributes combos and increments repeat seeds per (model, combo)."""
    model_rows = [{"model": "realvis", "count": 4}]
    combos = [
        {"emotion": "calm", "shot": "close-up"},
        {"emotion": "furious", "shot": "wide"},
        {"emotion": "stoic", "shot": "medium"},
        {"emotion": "smiling", "shot": "portrait"},
    ]
    tasks = distribute(model_rows, combos, seed=1000)
    assert len(tasks) == 4

    # Each combo is selected across the 4 tasks
    assigned_combos = [t["combo"]["emotion"] for t in tasks]
    assert assigned_combos == ["calm", "furious", "stoic", "smiling"]
    assert all(t["seed"] == 1000 for t in tasks)

    # When repeated combos occur, the repeat count increments the seed
    tasks_rep = distribute([{"model": "realvis", "count": 2}], [combos[0]], seed=1000)
    assert len(tasks_rep) == 2
    assert tasks_rep[0]["seed"] == 1000
    assert tasks_rep[1]["seed"] == 1001


def test_scheduler_max_batch_safety_cap():
    """Batch requests exceeding MAX_BATCH (120) produce safety cap warnings."""
    assert MAX_BATCH == 120

    # Under cap (50 images)
    safe_plan = build_plan({
        "mode": "character",
        "emotions": ["stoic"],
        "shots": ["portrait"],
        "models": [{"model": "realvis", "count": 50}],
    })
    assert safe_plan["total"] == 50
    assert not any("exceeds the safety cap" in w for w in safe_plan["warnings"])

    # Over cap (150 images)
    overflow_plan = build_plan({
        "mode": "character",
        "emotions": ["stoic"],
        "shots": ["portrait"],
        "models": [
            {"model": "realvis", "count": 80},
            {"model": "albedobase", "count": 70},
        ],
    })
    assert overflow_plan["total"] == 150
    assert any(f"exceeds the safety cap of {MAX_BATCH} images" in w for w in overflow_plan["warnings"])
