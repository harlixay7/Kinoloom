"""The install manifests ship exactly what the production graphs load."""
import json
import re
from pathlib import Path

import core

INSTALL = Path(core.ROOT) / "install"
WORKFLOWS = Path(core.ROOT) / "tool" / "workflows"
_FILE = re.compile(r'"([^"{}/\\]+\.(?:safetensors|gguf|pth|pt|onnx))"')


def _loaded_by(prefix: str) -> set[str]:
    names: set[str] = set()
    for wf in WORKFLOWS.glob(f"{prefix}*.json"):        # skips *.json.bak_*
        names.update(_FILE.findall(wf.read_text(encoding="utf-8")))
    return names


def test_ltx_package_matches_the_ltx_graphs():
    pkg = json.loads((INSTALL / "model_packages.json").read_text(encoding="utf-8"))
    shipped = {Path(p).name for p in pkg["packages"]["production-ltx"]["paths"]}
    assert shipped == _loaded_by("ltx25")


def test_colab_ltx_group_matches_the_package():
    pkg = json.loads((INSTALL / "model_packages.json").read_text(encoding="utf-8"))
    groups = json.loads((INSTALL / "colab_model_groups.json").read_text(encoding="utf-8"))
    ltx = next(g for gid, g in groups["groups"].items() if gid.startswith("ltx"))
    assert {Path(f["path"]).name for f in ltx["files"]} == \
        {Path(p).name for p in pkg["packages"]["production-ltx"]["paths"]}
