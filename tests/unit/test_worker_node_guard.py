"""A graph is refused before it is queued on a worker whose ComfyUI lacks
one of its node classes."""
from types import SimpleNamespace

import pytest

import workers


@pytest.fixture
def remote(monkeypatch):
    workers._node_cache.clear()
    st = SimpleNamespace(
        client=workers.RemoteWorkerClient("rig2"), calls=[],
        info={"KSampler": {"input": {}}, "SaveImage": {"input": {}}})

    def fake_req(method, path, *_a, **_kw):
        st.calls.append((method, path))
        return st.info if path == "/object_info" else {"prompt_id": "p1"}

    monkeypatch.setattr(st.client, "_req", fake_req)
    yield st
    workers._node_cache.clear()


GRAPH = {"1": {"class_type": "KSampler", "inputs": {}},
         "2": {"class_type": "MMH3SplitUpscale", "inputs": {}}}


def test_missing_node_refused_before_queueing(remote):
    with pytest.raises(RuntimeError, match="lacks MMH3SplitUpscale"):
        remote.client.queue_prompt(GRAPH)
    assert ("POST", "/job") not in remote.calls


def test_supported_graph_is_queued_and_node_set_cached(remote):
    ok = {"1": {"class_type": "KSampler"}, "2": {"class_type": "SaveImage"}}
    assert remote.client.queue_prompt(ok) == "p1"
    remote.client.queue_prompt(ok)
    assert remote.calls.count(("GET", "/object_info")) == 1


def test_unknown_node_set_does_not_block(remote):
    remote.info = {"detail": "comfy unreachable"}      # not an object_info map
    assert remote.client.queue_prompt(GRAPH) == "p1"


@pytest.mark.usefixtures("remote")
def test_reregistration_drops_the_cache():
    workers._node_cache["rig2"] = (1e18, frozenset({"KSampler"}))
    workers.register("rig2", "127.0.0.1", 8120, token="test-worker-token")
    assert "rig2" not in workers._node_cache
