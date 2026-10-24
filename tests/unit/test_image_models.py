"""Image model registry: every entry builds, and dataset defaults resolve."""
import pytest

import models


@pytest.mark.parametrize("mid", models.model_ids())
def test_every_registered_model_builds(mid):
    m = models.get_model(mid)
    w, h = map(int, m["default_size"].split("x"))
    wf = models.build_workflow(mid, "a castle at dusk", m["negative"], 1,
                               w, h, int(m["steps"]), float(m["cfg"]))
    assert wf and all("class_type" in n for n in wf.values())


def test_dataset_default_models_are_registered():
    # dataset_gen.start: stack "standard"/"fastwan" -> "wan", else "realvis"
    assert "wan" in models.model_ids()
    assert "realvis" in models.model_ids()


def test_wan_image_uses_turbo_lora():
    wf = models.build_workflow("wan", "x", "", 1, 1024, 1024, 20, 1.0)
    text = repr(wf)
    assert "Wan2.2-TI2V-5B-Q4_K_M.gguf" in text
    assert "Wan22_TI2V_5B_Turbo_lora_rank_64_fp16.safetensors" in text
