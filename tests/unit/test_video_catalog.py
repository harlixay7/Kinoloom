"""The video model catalog: every registry entry has a picker place, the
pickers get the groups in order with recommended configs first, and the
seamless-take capability is reported per model. Ids and configs are untouched."""
import json

import pytest

import video_models as vm
from core import ROOT


def _user_registry() -> dict:
    return json.loads((ROOT / "tool" / "video_models_user.json").read_text(encoding="utf-8"))["models"]


def test_every_active_entry_has_a_known_group_and_a_short_label():
    groups = {g for g, _ in vm.CATALOG_GROUPS}
    for mid, m in vm.VIDEO_MODELS.items():
        c = vm.catalog_entry(mid, m)
        assert c["group"] in groups, mid
        assert c["label"] and len(c["label"]) <= 48, (mid, c["label"])


def test_catalog_blocks_only_add_display_fields():
    # the registry keeps every config: an entry's catalog block is the only new key
    for mid, entry in _user_registry().items():
        if entry.get("disabled"):
            continue
        assert set(entry["catalog"]) <= {"group", "label", "detail", "rank", "recommended"}, mid
        assert vm.VIDEO_MODELS[mid]["display_name"] == entry["display_name"]


def test_one_recommended_config_per_group_at_most():
    seen: dict[str, list[str]] = {}
    for mid, m in vm.VIDEO_MODELS.items():
        c = vm.catalog_entry(mid, m)
        if c["recommended"]:
            seen.setdefault(c["group"], []).append(mid)
    assert all(len(v) == 1 for v in seen.values()), seen
    assert seen.get("ltx25") == ["ltx25_s6_cq"]
    assert seen.get("h3_1080p") == ["minimax_h3_singularity32"]


def test_catalog_payload_is_grouped_in_order_recommended_first():
    cat = vm.catalog()
    order = [g["id"] for g in cat["groups"]]
    assert order == [g for g, _ in vm.CATALOG_GROUPS if g in order]
    rank = {g: i for i, g in enumerate(order)}
    keys = [(rank[m["catalog"]["group"]], not m["catalog"]["recommended"], m["catalog"]["rank"])
            for m in cat["models"]]
    assert keys == sorted(keys)
    assert {m["id"] for m in cat["models"]} == set(vm.VIDEO_MODELS)
    for g in order:
        first = next(m for m in cat["models"] if m["catalog"]["group"] == g)
        if any(m["catalog"]["recommended"] for m in cat["models"] if m["catalog"]["group"] == g):
            assert first["catalog"]["recommended"], g


def test_an_entry_without_a_catalog_block_falls_back_to_its_family():
    assert vm.catalog_entry("x", {"family": "ltx25", "display_name": "X"})["group"] == "ltx25"
    assert vm.catalog_entry("x", {"family": "h3", "refine_target": "1920x1088"})["group"] == "h3_1080p"
    assert vm.catalog_entry("x", {"family": "h3"})["group"] == "h3_draft"
    assert vm.catalog_entry("x", {"family": "wan22_ti2v_5b"})["group"] == "wan"
    c = vm.catalog_entry("x", {"family": "odd", "catalog": {"group": "nope"}})
    assert c == {"group": "legacy", "label": "x", "detail": "", "rank": 99, "recommended": False}


def test_seamless_kind_gates_on_the_chain_capable_entries():
    assert vm.seamless_kind({"family": "ltx25", "comfy_root": "ComfyUI_LTX", "workflows": {"chain": "x"}}) == "ltx"
    assert vm.seamless_kind({"family": "ltx25", "comfy_root": "ComfyUI_LTX", "workflows": {"t2v_dfr": "x"}}) is None
    assert vm.seamless_kind({"family": "h3", "workflows": {"refs": "x"}}) == "h3"
    assert vm.seamless_kind({"family": "wan22", "workflows": {"refs": "x"}}) is None


@pytest.mark.parametrize("mid,kind", [("ltx25_s6_cq", "ltx"), ("ltx25_w4a8_refs", None),
                                      ("minimax_h3_singularity32", "h3"), ("minimax_h3_768p", "h3"),
                                      ("wan22_5b_q4", None)])
def test_model_status_reports_the_seamless_engine(mid, kind):
    st = vm.model_status(mid)
    assert st["seamless"] == kind
    assert st["catalog"] == vm.catalog_entry(mid, vm.VIDEO_MODELS[mid])


def test_models_route_serves_the_catalog(test_client):
    r = test_client.get("/api/videostudio/models").json()
    assert [g["id"] for g in r["groups"]] == [g["id"] for g in vm.catalog()["groups"]]
    assert [m["id"] for m in r["models"]] == [m["id"] for m in vm.catalog()["models"]]
    assert "production_model" in r and "registry_error" in r


def test_only_chains_with_a_finish_pass_offer_the_draft_preview():
    assert vm.model_status("ltx25_s6_cq")["chain_finish"] is True
    assert vm.model_status("minimax_h3_singularity32")["chain_finish"] is True       # 1080p refine
    assert vm.model_status("minimax_h3_singularity_544p")["chain_finish"] is False   # base take = delivery
    assert vm.model_status("ltx25_w4a8_refs")["chain_finish"] is False               # no seamless engine


def test_the_seamless_marker_follows_the_engine_switch(test_client, monkeypatch):
    import video_studio
    on = test_client.get("/api/videostudio/models").json()["models"]
    assert {m["id"] for m in on if m["seamless"]} >= {"ltx25_s6_cq", "minimax_h3_singularity32"}
    monkeypatch.setattr(video_studio, "load_config", lambda: {"video_studio": {"seamless_chains": False}})
    off = test_client.get("/api/videostudio/models").json()["models"]
    assert not any(m["seamless"] for m in off)
