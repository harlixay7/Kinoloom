import sys
import importlib.util
from unittest.mock import MagicMock, patch
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NODE_PATH = ROOT / "ComfyUI_Qwen21" / "custom_nodes" / "ComfyUI-Qwen21-Optimized" / "nodes" / "node_cpu_text_bridge.py"


@pytest.fixture(scope="module")
def cpu_text_bridge_mod():
    """Load node_cpu_text_bridge with torch and ComfyUI dependencies mocked."""
    torch_mock = MagicMock()
    torch_mock.is_tensor.return_value = False
    torch_mock.cuda.is_available.return_value = False

    orig_torch = sys.modules.get("torch")
    sys.modules["torch"] = torch_mock
    sys.modules["ComfyUI_Qwen21_Optimized.qwen21_common"] = MagicMock()

    spec = importlib.util.spec_from_file_location(
        "ComfyUI_Qwen21_Optimized.nodes.node_cpu_text_bridge",
        str(NODE_PATH),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    yield mod

    # Cleanup sys.modules
    if orig_torch is not None:
        sys.modules["torch"] = orig_torch
    else:
        sys.modules.pop("torch", None)
    sys.modules.pop("ComfyUI_Qwen21_Optimized.qwen21_common", None)
    sys.modules.pop("ComfyUI_Qwen21_Optimized.nodes.node_cpu_text_bridge", None)


def test_conditioning_budget_overflow_raises_value_error(cpu_text_bridge_mod):
    """Simulate conditioning payload > 32 MB and assert ValueError reports len(refs)."""
    node = cpu_text_bridge_mod.TextEncodeQwenImage21CPU()
    clip = MagicMock()
    clip.patcher.model.parameters.return_value = []
    clip.tokenize.return_value = ["dummy_token"]
    clip.encode_from_tokens_scheduled.return_value = [["dummy_cond"]]

    # Mock _count_conditioning_bytes to return 35 MB (> 32 MB)
    with patch.object(cpu_text_bridge_mod, "_count_conditioning_bytes", return_value=35_000_000):
        with pytest.raises(ValueError) as excinfo:
            node.encode(
                clip=clip,
                prompt="photographic portrait of hero",
                token_budget=4096,
                measure_dma=False,
                image1="ref_a.png",
                image2="ref_b.png",
            )

        err_msg = str(excinfo.value)
        assert "35.0 MB exceeds the 32 MB budget" in err_msg
        assert "token budget (4096)" in err_msg
        assert "reference count (2)" in err_msg


def test_conditioning_within_budget_succeeds(cpu_text_bridge_mod):
    """Conditioning payload <= 32 MB completes without error."""
    node = cpu_text_bridge_mod.TextEncodeQwenImage21CPU()
    clip = MagicMock()
    clip.patcher.model.parameters.return_value = []
    clip.tokenize.return_value = ["dummy_token"]
    clip.encode_from_tokens_scheduled.return_value = [["dummy_cond"]]

    # 4.2 MB is standard expected payload
    with patch.object(cpu_text_bridge_mod, "_count_conditioning_bytes", return_value=4_200_000):
        res = node.encode(
            clip=clip,
            prompt="photographic portrait of hero",
            token_budget=4096,
            measure_dma=False,
            image1="ref_a.png",
        )
        assert res is not None
        assert len(res) == 1


def test_references_cap_validation(cpu_text_bridge_mod):
    """Exceeding MAX_REFERENCES raises ValueError before encode."""
    node = cpu_text_bridge_mod.TextEncodeQwenImage21CPU()
    clip = MagicMock()

    with patch.object(cpu_text_bridge_mod, "MAX_REFERENCES", 2):
        with pytest.raises(ValueError) as excinfo:
            node.encode(
                clip=clip,
                prompt="portrait",
                token_budget=4096,
                measure_dma=False,
                image1="1.png",
                image2="2.png",
                image3="3.png",
            )
        assert "reference images exceed the contract cap of 2" in str(excinfo.value)
