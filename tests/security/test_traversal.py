"""File-serving endpoints never escape their media roots.

Every probe points at a file that really exists in the sandbox
(tool/settings.yaml, which holds the API and fleet tokens), so a missing
guard would serve it rather than fail on a missing file.
"""
from pathlib import Path

import pytest
from fastapi import HTTPException

import api
import core
from core import is_safe_component

SECRET = Path(core.ROOT) / "tool" / "settings.yaml"


def _probes() -> list[str]:
    s = SECRET.resolve()
    return [
        str(s),                                     # absolute
        s.as_posix(),
        "../../tool/settings.yaml",
        "..\\..\\tool\\settings.yaml",
        "../" * 8 + "tool/settings.yaml",
        "sub/../../../tool/settings.yaml",
    ]


ENDPOINTS = {
    "dataset_img": lambda p: api.dataset_img("power", "lightning", p),
    "images_refs_file": lambda p: api.images_refs_file(p),
    "images_file": lambda p: api.images_file(p),
    "video_poster": lambda p: api.video_poster("raw", p),
    "video": lambda p: api.video("raw", p),
    "video_file": lambda p: api.video_file(p),
    "videostudio_ref_image": lambda p: api.videostudio_ref_image(p),
    "videostudio_file": lambda p: api.videostudio_file(p),
}


def test_secret_target_exists():
    assert SECRET.is_file()


def test_is_safe_component():
    """is_safe_component rejects traversal indicators, path separators, null bytes, and non-strings."""
    for bad in ("../", "..\\", "..", "C:/Windows/win.ini", "C:\\boot.ini",
                "/etc/passwd", "folder/subfolder", "folder\\subfolder",
                "file\x00name.png", "", None):
        assert not is_safe_component(bad)
    for good in ("valid_name", "hero-character_01", "Cyberpunk City"):
        assert is_safe_component(good)


@pytest.mark.parametrize("name", sorted(ENDPOINTS))
@pytest.mark.parametrize("probe", _probes())
def test_file_endpoint_refuses_traversal(name, probe):
    # the app maps these to 404 / 403 / 400 (api.py exception handlers);
    # the target exists, so an unguarded endpoint would serve it instead
    try:
        res = ENDPOINTS[name](probe)
    except HTTPException as e:
        assert e.status_code in (400, 403, 404), (name, probe, e.status_code)
        return
    except (FileNotFoundError, PermissionError, core.InvalidInput):
        return
    served = Path(str(getattr(res, "path", ""))).resolve()
    assert served != SECRET.resolve(), f"{name} served {served} for {probe!r}"


@pytest.mark.parametrize("url", [
    "/api/dataset_img?type=power&name=lightning&file={p}",
    "/api/images/file?name={p}",
    "/api/video/poster?folder=raw&name={p}",
    "/api/videostudio/ref_image?name={p}",
])
@pytest.mark.parametrize("probe", _probes() + ["settings.yaml\x00.png"])
def test_http_traversal_gets_a_4xx_without_the_file(test_client, url, probe):
    # a NUL byte is refused by the security gate before any handler runs
    # (pathlib raises ValueError on it, which used to surface as a 500)
    from urllib.parse import quote
    r = test_client.get(url.format(p=quote(probe, safe="")))
    assert r.status_code in (400, 403, 404), (url, probe, r.status_code)
    assert b"shared_token" not in r.content and b"api_token" not in r.content
