import pytest
import sqlite3
from shots_db import ShotsDB, _decode


def test_shots_db_wal_mode(isolated_db):
    """ShotsDB initializes with WAL journal mode for concurrent read/write access."""
    row = isolated_db.con.execute("PRAGMA journal_mode;").fetchone()
    assert row[0].lower() == "wal"


def test_shots_db_composite_primary_key(isolated_db):
    """Composite primary key (shot_id, project_id) isolates shots across projects."""
    shot_spec = '{"camera": "static", "tier": "hero"}'

    # Insert shot s01_001 in project A
    isolated_db.con.execute(
        "INSERT INTO shots (shot_id, project_id, scene, status, json) VALUES (?, ?, ?, ?, ?)",
        ("s01_001", "project_alpha", "scene_01", "pending", shot_spec),
    )
    isolated_db.con.commit()

    # Insert identical shot_id s01_001 in project B - must succeed due to composite PK
    isolated_db.con.execute(
        "INSERT INTO shots (shot_id, project_id, scene, status, json) VALUES (?, ?, ?, ?, ?)",
        ("s01_001", "project_beta", "scene_01", "pending", shot_spec),
    )
    isolated_db.con.commit()

    # Verify both rows exist independently
    rows = isolated_db.con.execute(
        "SELECT shot_id, project_id FROM shots WHERE shot_id='s01_001' ORDER BY project_id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["project_id"] == "project_alpha"
    assert rows[1]["project_id"] == "project_beta"

    # Attempting to insert duplicate (shot_id, project_id) fails with IntegrityError
    with pytest.raises(sqlite3.IntegrityError):
        isolated_db.con.execute(
            "INSERT INTO shots (shot_id, project_id, scene, status, json) VALUES (?, ?, ?, ?, ?)",
            ("s01_001", "project_alpha", "scene_01", "pending", shot_spec),
        )


def test_shots_db_safe_decode():
    """_decode safely parses JSON and Python literals without executing code."""
    # 1. Valid JSON object
    assert _decode('{"shot_id": "s01_001", "duration": 5}') == {
        "shot_id": "s01_001",
        "duration": 5,
    }

    # 2. Python dict literal fallback
    assert _decode("{'shot_id': 's01_002', 'tier': 'hero'}") == {
        "shot_id": "s01_002",
        "tier": "hero",
    }

    # 3. Code execution attempt - must return empty dict, never execute
    evil_payload = '__import__("os").system("echo hacked")'
    assert _decode(evil_payload) == {}

    # 4. Empty, None, or invalid syntax
    assert _decode("") == {}
    assert _decode(None) == {}
    assert _decode("invalid non-json syntax {[[") == {}
    assert _decode("12345") == {}  # Not a dict or list
