"""Text-to-video is only offered where the mapped graph can actually do it."""
import pytest

import video_models
import video_studio


def test_workflow_classes_reads_tokenized_templates():
    # templates carry unquoted {TOKENS}; a json.loads scan saw nothing
    classes = video_models.workflow_classes("h3_turbo4_1080p")
    assert "LoraLoaderModelOnly" in classes and "SaveVideo" in classes


@pytest.mark.parametrize("mid", ["minimax_h3_singularity_544p",
                                 "ltx25_temporal_10s",
                                 "minimax_h3_turbo4_1080p"])
def test_broken_t2v_mappings_are_refused(mid):
    if mid not in video_models.VIDEO_MODELS:
        pytest.skip(f"{mid} not in this registry")
    note = video_models.t2v_problem(mid)
    assert note
    st = video_models.model_status(mid)
    assert "t2v" not in st["supported_modes"] and st["t2v_note"] == note


@pytest.mark.parametrize("mid", ["minimax_h3_hero", "ltx25_w4a8_refs",
                                 "minimax_h3_singularity32"])
def test_production_t2v_paths_stay_available(mid):
    if mid not in video_models.VIDEO_MODELS:
        pytest.skip(f"{mid} not in this registry")
    assert video_models.t2v_problem(mid) is None
    assert "t2v" in video_models.model_status(mid)["supported_modes"]


def test_build_refuses_reference_graph_for_t2v():
    with pytest.raises(ValueError, match="reference graph"):
        video_studio._check_t2v_graph("x", "h3_ref2va", None)
