import pytest
from projects import ProjectStore


def test_projects_crud_lifecycle(isolated_db):
    """Test full ProjectStore lifecycle: create, get, list, update, and delete."""
    store = ProjectStore(isolated_db)

    # 1. Create
    created = store.create(
        title="Chronos Protocol",
        idea="A temporal paradox thriller",
        genre="Sci-Fi",
        tone="Suspenseful",
        duration_target=120,
    )
    pid = created["id"]
    assert pid.startswith("chronos_protocol")

    # 2. Get
    proj = store.get(pid)
    assert proj is not None
    assert proj["title"] == "Chronos Protocol"
    assert proj["genre"] == "Sci-Fi"
    assert proj["duration_target"] == 120
    assert proj["status"] == "draft"
    assert isinstance(proj["gates"], dict)
    assert isinstance(proj["story"], dict)

    # 3. List
    all_projs = store.list()
    assert any(p["id"] == pid for p in all_projs)

    # 4. Update
    updated = store.update(pid, title="Chronos Protocol: Redux", mode="autopilot")
    assert updated is True

    proj_after = store.get(pid)
    assert proj_after["title"] == "Chronos Protocol: Redux"
    assert proj_after["mode"] == "autopilot"

    # 5. Delete
    del_res = store.delete(pid)
    assert "removed_shots" in del_res
    assert store.get(pid) is None


def test_story_stages_versioning_and_rollback(isolated_db):
    """Test story stage creation, incremental versioning, and rollback."""
    store = ProjectStore(isolated_db)
    created = store.create(title="Versioned Story")
    pid = created["id"]

    # Version 1 of treatment
    res_v1 = store.set_story_stage(pid, "treatment", {"summary": "Version 1 - Initial draft"})
    assert res_v1["version"] == 1

    # Verify project story updated
    proj_v1 = store.get(pid)
    assert proj_v1["story"]["treatment"]["summary"] == "Version 1 - Initial draft"

    # Version 2 of treatment
    res_v2 = store.set_story_stage(pid, "treatment", {"summary": "Version 2 - Major overhaul"})
    assert res_v2["version"] == 2

    proj_v2 = store.get(pid)
    assert proj_v2["story"]["treatment"]["summary"] == "Version 2 - Major overhaul"

    # Inspect history versions
    versions = store.story_versions(pid, stage="treatment")
    assert len(versions) == 2
    assert [v["version"] for v in versions] == [1, 2]

    # Rollback to version 1
    rolled = store.rollback_stage(pid, "treatment", 1)
    assert rolled is True

    # After rollback, project reflects version 1 content
    proj_rolled = store.get(pid)
    assert proj_rolled["story"]["treatment"]["summary"] == "Version 1 - Initial draft"
