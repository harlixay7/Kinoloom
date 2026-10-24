"""Assembler. Plain scene concat, loudnorm audio (when
drafts have audio), draft NVENC + final x264 masters."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from core import ROOT, ffmpeg_exe, is_safe_component, load_config, log_setup

log = log_setup("assembler")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if p.returncode != 0:
        log.error(p.stderr[-2000:])
    return p


def safe_title(title: str | None, default: str = "movie") -> str:
    """A film title as a file-name stem: letters, digits, space, '_', '-',
    '.' only (no separators, no ':' drive prefix, no leading dots)."""
    t = re.sub(r"[^A-Za-z0-9 _.\-]", "", str(title or "")).strip(" .")
    t = re.sub(r"\.{2,}", ".", t)[:80].strip(" .")
    return t or default


def _concat_list(paths: list[Path]) -> str:
    # ffmpeg concat demuxer quoting: a ' inside '...' is written as '\''
    return "".join("file '" + p.as_posix().replace("'", "'\\''") + "'\n"
                   for p in paths)


def project_clips(rows: list[dict], scenes: list[str] | None = None) -> dict[str, list[Path]]:
    """A project's PASSED shots per scene in story order (the prev_shot chain):
    the post-processed clip when there is one, else the raw render (post-off
    projects never write output/drafts, and drafts are not namespaced per
    project). The shots of a seamless take are consecutive cuts of one take,
    so concatenating them in this order rebuilds the take frame for frame.
    scenes: only these (None = every scene with a passed shot)."""
    from shots_db import story_order
    by_scene: dict[str, list[dict]] = {}
    for r in rows:
        if scenes is None or r.get("scene") in scenes:
            by_scene.setdefault(str(r.get("scene") or ""), []).append(r)
    out: dict[str, list[Path]] = {}
    for scene in sorted(by_scene):
        for r in story_order(by_scene[scene]):
            src = r.get("final_path") or r.get("video_path")
            if r.get("status") == "passed" and src and Path(src).is_file():
                out.setdefault(scene, []).append(Path(src))
    return out


def _check_scene(scene: str) -> str:
    if not is_safe_component(str(scene)):
        raise ValueError(f"invalid scene id {scene!r}")
    return str(scene)


class Assembler:
    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or load_config()
        self.ff = ffmpeg_exe(self.cfg)

    def _shots_for_scene(self, scene: str) -> list[Path]:
        """Legacy (project-less) drafts: output/drafts/<scene>_<shot>.mp4,
        ordered by the shot number."""
        out = ROOT / "output" / "drafts"
        def _key(v: Path) -> int:
            digits = "".join(ch for ch in v.name.split("_")[-1]
                             .split(".")[0] if ch.isdigit())
            return int(digits or 0)
        return sorted(out.glob(f"{_check_scene(scene)}_*.mp4"), key=_key)

    def _has_audio(self, vid: Path) -> bool:
        try:
            p = subprocess.run([str(self.ff), "-i", str(vid)],
                               capture_output=True, text=True)
            return "Audio:" in (p.stderr or "")
        except Exception:
            return True

    def assemble_scene(self, scene: str, clips: list[Path] | None = None,
                       ns: str = "") -> Path | None:
        """Plain concat of the scene's shots (no crossfades), audio loudnorm
        applied when they have an audio stream.

        clips: the scene's shot videos in order (a project passes its own
        PASSED shots). None = the legacy glob of output/drafts. ns: a project
        id that namespaces the scene output, because scene ids repeat across
        projects (every story has an s01)."""
        scene = _check_scene(scene)
        if ns and not is_safe_component(ns):
            raise ValueError(f"invalid project id {ns!r}")
        vids = list(clips) if clips is not None else self._shots_for_scene(scene)
        vids = [v for v in vids if Path(v).is_file()]
        if not vids:
            log.warning(f"no shot videos for scene {scene}")
            return None
        stem = f"{ns}__{scene}" if ns else scene
        has_audios = [self._has_audio(v) for v in vids]
        any_audio = any(has_audios)
        all_audio = all(has_audios)

        concat_vids: list[Path] = []
        if any_audio and not all_audio:
            for i, v in enumerate(vids):
                if has_audios[i]:
                    concat_vids.append(v)
                else:
                    silent_vid = ROOT / "temp" / f"silent_{stem}_{i:03d}.mp4"
                    silent_vid.parent.mkdir(parents=True, exist_ok=True)
                    p = _run([self.ff, "-y", "-i", str(v), "-f", "lavfi",
                              "-i", "anullsrc=r=48000:cl=stereo", "-c:v", "copy",
                              "-c:a", "aac", "-b:a", "192k", "-shortest",
                              str(silent_vid)])
                    if p.returncode == 0 and silent_vid.exists():
                        concat_vids.append(silent_vid)
                    else:
                        concat_vids.append(v)
        else:
            concat_vids = list(vids)

        listfile = ROOT / "temp" / f"{stem}.txt"
        listfile.parent.mkdir(parents=True, exist_ok=True)
        listfile.write_text(_concat_list(concat_vids), encoding="utf-8")
        out = ROOT / "output" / "scenes" / f"{stem}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [self.ff, "-y", "-f", "concat", "-safe", "0", "-i",
               str(listfile)]
        if any_audio:
            lufs = ((self.cfg.get("post") or {}).get("audio") or {}) \
                .get("loudness_target_lufs", -16)
            cmd += ["-af", f"loudnorm=I={lufs}:TP=-1.5:LRA=11"]
        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-pix_fmt", "yuv420p", str(out)]
        p = _run(cmd)
        if p.returncode != 0 or not out.exists() or out.stat().st_size < 1024:
            if out.exists():
                out.unlink()
            raise RuntimeError(
                f"scene encode failed for {scene}: "
                f"{p.stderr[-500:] if p.stderr else 'no stderr'}")
        log.info(f"assembled scene {stem}: {len(vids)} shots")
        return out

    def assemble_movie(self, scenes: list[str], title: str = "movie",
                       clips: dict[str, list[Path]] | None = None,
                       ns: str = "") -> Path:
        """Final film: concat scenes, then draft (NVENC) + master (x264 slow).

        clips / ns: see assemble_scene — a project passes its passed shots per
        scene and its id, so another project's drafts never leak in."""
        title = safe_title(title)
        scene_vids = []
        for s in scenes:
            v = self.assemble_scene(
                s, clips.get(s, []) if clips is not None else None, ns=ns)
            if v:
                scene_vids.append(v)
        if not scene_vids:
            raise RuntimeError("no scenes to assemble")
        listfile = ROOT / "temp" / f"film_{title}.txt"
        listfile.parent.mkdir(parents=True, exist_ok=True)
        listfile.write_text(_concat_list(scene_vids), encoding="utf-8")

        draft = ROOT / "output" / "films" / f"{title}_draft.mp4"
        master = ROOT / "output" / "films" / f"{title}_master.mp4"
        draft.parent.mkdir(parents=True, exist_ok=True)
        p = _run([self.ff, "-y", "-f", "concat", "-safe", "0", "-i",
                  str(listfile), "-c:v", "h264_nvenc", "-preset", "p7",
                  "-tune", "hq", "-rc", "vbr", "-cq", "19", "-b:v", "0",
                  "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                  str(draft)])
        if p.returncode != 0 or not draft.exists() or \
                draft.stat().st_size < 1024:
            log.warning("h264_nvenc unavailable (driver/ffmpeg NVENC API "
                        "mismatch?) — falling back to libx264 for draft")
            if draft.exists():
                draft.unlink()
            p = _run([self.ff, "-y", "-f", "concat", "-safe", "0", "-i",
                      str(listfile), "-c:v", "libx264", "-preset", "veryfast",
                      "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac",
                      "-b:a", "192k", str(draft)])
        if p.returncode != 0 or not draft.exists() or \
                draft.stat().st_size < 1024:
            raise RuntimeError(
                f"draft encode failed: "
                f"{p.stderr[-500:] if p.stderr else 'no stderr'}")
        log.info(f"draft: {draft}")
        p = _run([self.ff, "-y", "-f", "concat", "-safe", "0", "-i",
                  str(listfile), "-c:v", "libx264", "-preset", "slow",
                  "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac",
                  "-b:a", "192k", str(master)])
        if p.returncode != 0 or not master.exists() or \
                master.stat().st_size < 1024:
            raise RuntimeError(
                f"master encode failed: "
                f"{p.stderr[-500:] if p.stderr else 'no stderr'}")
        log.info(f"master: {master}")
        return master
