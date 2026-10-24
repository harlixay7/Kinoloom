"""Smoke-test one read-only route per router cluster as the local operator."""
import pytest


@pytest.fixture
def client(test_client):
    return test_client


def test_api_health_is_public(make_client):
    remote = make_client(base_url="http://192.168.1.10:8080", peer="192.168.1.50")
    res = remote.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_api_status_cluster(client):
    """Status route cluster returns studio system state."""
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert {"time", "orchestrator", "gpu", "versions"} <= set(data)


def test_api_projects_cluster(client):
    """Projects route cluster supports listing projects."""
    res = client.get("/api/projects")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list) or "projects" in data


def test_api_bible_cluster(client):
    """Bible route cluster returns lore and bible content."""
    res = client.get("/api/bible")
    assert res.status_code == 200
    assert isinstance(res.json(), dict)


def test_api_images_cluster(client):
    """Images route cluster returns gallery and status information."""
    res_status = client.get("/api/images/status")
    assert res_status.status_code == 200
    assert isinstance(res_status.json(), dict)

    res_list = client.get("/api/images/list")
    assert res_list.status_code == 200
    assert isinstance(res_list.json(), (dict, list))


def test_api_training_cluster(client):
    """Training route cluster exposes presets and dataset categories."""
    res_presets = client.get("/api/training/presets")
    assert res_presets.status_code == 200
    assert isinstance(res_presets.json(), (dict, list))

    res_datasets = client.get("/api/training/datasets")
    assert res_datasets.status_code == 200
    assert isinstance(res_datasets.json(), (dict, list))


def test_api_workers_cluster(client):
    """Workers route cluster lists registered worker nodes (no token hashes)."""
    res = client.get("/api/workers")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, dict)
    assert all("token_hash" not in w for w in data.get("workers", []))


def test_api_queue_cluster(client):
    """Queue route cluster returns task queue items and summary."""
    res_summary = client.get("/api/queue/summary")
    assert res_summary.status_code == 200
    assert isinstance(res_summary.json(), dict)

    res_queue = client.get("/api/queue")
    assert res_queue.status_code == 200
    assert isinstance(res_queue.json(), (dict, list))


def test_videostudio_models_reports_registry(client):
    res = client.get("/api/videostudio/models")
    assert res.status_code == 200
    data = res.json()
    ids = {m["id"] for m in data["models"]}
    # the default movie-pipeline model is a registered entry
    assert data["production_model"] in ids
    assert "registry_error" in data
