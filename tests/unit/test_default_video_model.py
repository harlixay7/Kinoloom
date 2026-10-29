"""Wan 2.2 is legacy: the movie pipeline and the pickers default to LTX-2.5
(video_models.DEFAULT_MODEL), a project renders Wan only when it picks an
installed Wan entry, and keyframe stills come from an Images-tab model."""
import orchestrator
import pytest
import video_models as vm
from keyframe_gen import KeyframeGen, _keyframe_model, _keyframe_size

import models


@pytest.fixture
def installed(monkeypatch):
    """installed(*ids): only these registry entries report available."""
    real = vm.model_status

    def apply(*ids):
        def fake(mid):
            st = real(mid)
            if mid in vm.VIDEO_MODELS:
                st = {**st, "available": mid in ids,
                      "missing": [] if mid in ids else ["weights"]}
            return st
        monkeypatch.setattr(vm, "model_status", fake)
    return apply


class _ProjectRow:
    """ShotsDB stand-in: the project row's (video_family, video_models)."""

    def __init__(self, video_family=""):
        self.con, self._row = self, (video_family, "{}")

    def execute(self, *_):
        return self

    def fetchone(self):
        return self._row


def _family(shot=None, video_family=""):
    o = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
    o.db = _ProjectRow(video_family)
    shot = shot or {}
    return o._video_family(shot, shot, "p1")


def test_the_default_is_ltx_when_it_is_installed(installed):
    installed(vm.DEFAULT_MODEL, "wan22_5b_q4")
    assert vm.DEFAULT_MODEL == "ltx25_s6_cq"
    assert vm.default_model_id() == vm.production_model_id() == "ltx25_s6_cq"


def test_the_default_falls_back_to_a_movie_tier_never_to_wan(installed):
    installed("minimax_h3_768p", "wan22_5b_q4")
    assert vm.default_model_id() == "minimax_h3_768p"
    installed("wan22_5b_q4")
    assert vm.default_model_id() is None
    assert vm.production_model_id() == vm.DEFAULT_MODEL     # shown as unavailable


def test_the_wan_entry_says_it_is_legacy_and_weaker():
    st = vm.model_status("wan22_5b_q4")
    assert st["declared_status"] == "legacy"
    assert "legacy" in st["display_name"] and "not as good" in st["note"].lower()
    assert dict(vm.CATALOG_GROUPS)["wan"].endswith("legacy, not recommended")
    assert not st["catalog"]["recommended"]
    assert models.MODELS["wan"]["name"] == "Wan 2.2 (legacy)"
    assert list(models.MODELS)[-1] == "wan"                 # listed last


def test_a_project_without_a_model_renders_on_the_default(installed):
    installed(vm.DEFAULT_MODEL, "wan22_5b_q4")
    assert _family(video_family="") == "ltx25_s6_cq"


def test_wan_renders_only_when_picked_and_installed(installed):
    installed(vm.DEFAULT_MODEL, "wan22_5b_q4")
    assert _family(video_family="wan22_5b_q4") == ""        # the legacy Wan path
    assert _family({"video_model": "wan22_5b_q4"}) == ""
    installed(vm.DEFAULT_MODEL)
    assert _family(video_family="wan22_5b_q4") == "ltx25_s6_cq"


def test_a_picked_movie_model_wins_and_a_missing_one_falls_back(installed):
    installed(vm.DEFAULT_MODEL, "minimax_h3_768p")
    assert _family(video_family="minimax_h3_768p") == "minimax_h3_768p"
    assert _family(video_family="minimax_h3_hero") == "ltx25_s6_cq"     # not installed
    assert _family(video_family="no_such_model") == "ltx25_s6_cq"


def test_no_movie_model_installed_is_a_clear_error(installed):
    installed()
    with pytest.raises(orchestrator.NoVideoModel, match="LTX-2.5 or MiniMax H3"):
        _family(video_family="")
    installed("wan22_5b_q4")
    with pytest.raises(orchestrator.NoVideoModel):
        _family(video_family="")                         # Wan is never a fallback


def test_no_movie_model_on_a_card_that_cannot_run_them_names_the_card(installed, monkeypatch):
    import gpu_support
    installed()
    old = ("the NVIDIA GeForce GTX 1080 (compute capability 6.1) cannot run LTX-2.5 and MiniMax H3, "
           "built on CUDA 13.0 for a GTX 16- or RTX 20-series card or newer")
    monkeypatch.setattr(gpu_support, "problem", lambda runtime, runs="": old if runtime == "cu130" else "")
    with pytest.raises(orchestrator.NoVideoModel) as e:
        _family(video_family="")
    assert str(e.value) == old + ", and the project does not pick the legacy Wan 2.2 model"


def test_keyframes_use_an_image_model_at_its_own_size():
    m = models.MODELS["realvis"]
    assert _keyframe_size(m, 768, 480) == (1216, 832)
    gen = KeyframeGen({"ensemble": {"comfyui_port": 8188}})
    make = gen._image_graphs("realvis", m, None, {}, "s01", "a knight at dawn",
                             768, 480, None)
    text = str(make(7))
    assert m["files"]["unet"] in text and "Wan" not in text
    assert "a knight at dawn" in text and "'seed': 7" in text


def test_keyframes_fall_back_to_an_installed_image_model(monkeypatch):
    have = set()
    monkeypatch.setattr(models, "is_available", lambda mid: mid in have)
    have.update({"realvis", "qwen21"})
    assert _keyframe_model({})[0] == "realvis"                       # the default
    assert _keyframe_model({"model": "zimage"})[0] == "realvis"      # not installed
    have.discard("realvis")
    assert _keyframe_model({})[0] == "qwen21"                        # a fresh install
    assert _keyframe_model({"model": "no_such"})[0] == "qwen21"
    assert _keyframe_model({"model": "wan"})[0] == "wan"             # an explicit opt-in
    have.clear()
    assert _keyframe_model({})[0] == "realvis"                       # fails loudly later
