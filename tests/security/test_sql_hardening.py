import pytest
from shots_db import assert_safe_identifier as shots_assert_safe_id, KNOWN_SHOT_FIELDS
from projects import assert_safe_identifier as proj_assert_safe_id, KNOWN_FIELDS as PROJ_KNOWN_FIELDS, ProjectStore


def test_assert_safe_identifier_valid():
    """Valid SQL identifiers pass assertion and return the identifier name."""
    assert shots_assert_safe_id("status") == "status"
    assert shots_assert_safe_id("video_path") == "video_path"
    assert proj_assert_safe_id("title") == "title"
    assert proj_assert_safe_id("duration_target") == "duration_target"


def test_assert_safe_identifier_sql_injection_rejection():
    """SQL injection payloads and illegal characters are rejected with ValueError."""
    malicious_inputs = [
        '"; DROP TABLE shots;--',
        "title' OR 1=1--",
        "status; SELECT * FROM users;",
        "col name with spaces",
        "1starts_with_number",
        "column-with-dash",
        "column$special",
        "admin'",
        "",
        None,
        12345,
    ]

    for bad in malicious_inputs:
        with pytest.raises(ValueError):
            shots_assert_safe_id(bad)
        with pytest.raises(ValueError):
            proj_assert_safe_id(bad)


def test_assert_safe_identifier_allowlist_enforcement():
    """Syntactically valid identifiers not in the allowlist are rejected."""
    # Syntactically valid identifier, but not in shots allowlist
    with pytest.raises(ValueError, match="Disallowed SQL column identifier"):
        shots_assert_safe_id("unregistered_secret_col", allowed=KNOWN_SHOT_FIELDS)

    # Valid whitelisted shots columns pass
    for col in KNOWN_SHOT_FIELDS:
        assert shots_assert_safe_id(col, allowed=KNOWN_SHOT_FIELDS) == col

    # Syntactically valid identifier, but not in projects allowlist
    with pytest.raises(ValueError, match="Disallowed SQL column identifier"):
        proj_assert_safe_id("malicious_extra_field", allowed=PROJ_KNOWN_FIELDS)

    # Valid whitelisted projects columns pass
    for col in PROJ_KNOWN_FIELDS:
        assert proj_assert_safe_id(col, allowed=PROJ_KNOWN_FIELDS) == col


def test_project_store_update_rejects_unwhitelisted_columns(isolated_db):
    """ProjectStore.update rejects arbitrary column injection attempts."""
    store = ProjectStore(isolated_db)
    created = store.create("SQL Hardening Test")
    pid = created["id"]

    # Attempting to update non-whitelisted column raises ValueError
    with pytest.raises(ValueError, match="Disallowed SQL column identifier"):
        store.update(pid, unwhitelisted_column="exploit")
