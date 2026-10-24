"""post.PostPipeline frame round-trip keeps duration and audio."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from post import PostPipeline

_FF = Path(__file__).resolve().parents[2] / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
FFMPEG = str(_FF) if _FF.exists() else shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")


def _info(ff, path):
    err = subprocess.run([ff, "-i", str(path)], capture_output=True,
                         text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return dur, "Audio:" in err


@pytest.fixture
def clip(tmp_path):
    src = tmp_path / "render.mp4"
    subprocess.run([FFMPEG, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc=size=128x72:rate=24:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(src)], check=True)
    return src


def test_roundtrip_keeps_duration_and_audio(tmp_path, clip):
    pp = PostPipeline(cfg={"paths": {"ffmpeg": FFMPEG}})
    assert pp.probe_fps(clip) == 24.0
    frames = tmp_path / "frames"
    assert pp.video_frames(clip, frames) == 48
    out = tmp_path / "final.mp4"
    pp.frames_to_video(frames, out, pp.probe_fps(clip), audio_src=clip)
    dur, has_audio = _info(FFMPEG, out)
    assert abs(dur - 2.0) < 0.15          # was ~1.0 s when encoded at 48 fps
    assert has_audio                      # H3 sound survives post


def test_silent_source_is_fine(tmp_path):
    silent = tmp_path / "silent.mp4"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=64x64:rate=24:duration=1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(silent)], check=True)
    pp = PostPipeline(cfg={"paths": {"ffmpeg": FFMPEG}})
    frames = tmp_path / "f"
    pp.video_frames(silent, frames)
    out = tmp_path / "o.mp4"
    pp.frames_to_video(frames, out, 24.0, audio_src=silent)
    dur, has_audio = _info(FFMPEG, out)
    assert out.exists() and not has_audio and abs(dur - 1.0) < 0.15
