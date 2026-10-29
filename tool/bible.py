"""Bible: canonical project specifications and entity store.
Loads/validates/saves universe.yaml, identity_tiers.yaml, characters/*,
crowd_types.yaml, locations/*, scenes/*. Git-tracked per-project versioning."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from core import ROOT, InvalidInput, ensure_dir, is_safe_component, write_text

BIBLE_DIR = ROOT / "bible"
_sys_git = shutil.which("git")
GIT = Path(_sys_git) if _sys_git else (ROOT / "tools" / "git" / "cmd" / "git.exe")


class Bible:
    def __init__(self, project_id: str | None = None) -> None:
        if project_id is not None:
            if not is_safe_component(project_id):
                raise ValueError(f"invalid project_id: {project_id!r}")
            self.dir = BIBLE_DIR / "projects" / project_id
        else:
            self.dir = BIBLE_DIR
        for sub in ("characters", "locations", "scenes", "luts"):
            ensure_dir(self.dir / sub)
        self._git_init()

    # ------------------------------------------------------------------ git
    def _git_init(self) -> None:
        if not (self.dir / ".git").exists():
            subprocess.run([str(GIT), "init", str(self.dir)], capture_output=True)
        # ignore runtime artifacts
        write_text(self.dir / ".gitignore", "luts/\n")

    def _git(self, *args: str) -> None:
        subprocess.run([str(GIT), "-C", str(self.dir), *args], capture_output=True)

    def git_commit(self, msg: str) -> str:
        self._git("add", "-A")
        self._git("commit", "-m", msg, "--allow-empty")
        out = subprocess.run(
            [str(GIT), "-C", str(self.dir), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True)
        return out.stdout.strip() or "none"

    def git_log(self) -> list[str]:
        out = subprocess.run(
            [str(GIT), "-C", str(self.dir), "log", "--oneline", "-n", "20"],
            capture_output=True, text=True)
        return out.stdout.splitlines()

    # ------------------------------------------------------- project snapshot
    def snapshot_from_global(self, project_id: str) -> None:
        """Copy the global Bible's reusable cast/world bank into the project
        bible dir and commit as 'project snapshot'. The target folder is
        CLEARED first so a recreated project never inherits stale cast files.
        luts/ is intentionally not copied."""
        if not is_safe_component(project_id):
            raise ValueError(f"invalid project_id: {project_id!r}")
        target = BIBLE_DIR / "projects" / project_id
        for sub in ("characters", "locations", "scenes"):
            p = target / sub
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            ensure_dir(p)
        if not (target / ".git").exists():
            subprocess.run([str(GIT), "init", str(target)], capture_output=True)
        # commits need an identity; add a repo-local one only when none
        # exists anywhere (mirrors the global bible repo setup)
        if subprocess.run([str(GIT), "-C", str(target), "config", "user.name"],
                          capture_output=True).returncode != 0:
            subprocess.run([str(GIT), "-C", str(target), "config",
                            "user.name", "Kinoloom"], capture_output=True)
            subprocess.run([str(GIT), "-C", str(target), "config",
                            "user.email", "studio@ai.local"], capture_output=True)
        write_text(target / ".gitignore", "luts/\n")
        for name in ("universe.yaml", "crowd_types.yaml"):
            p = BIBLE_DIR / name
            if p.exists():
                shutil.copy2(p, target / name)
        for sub in ("characters", "locations", "scenes"):
            for p in sorted((BIBLE_DIR / sub).glob("*.yaml")):
                shutil.copy2(p, target / sub / p.name)
        subprocess.run([str(GIT), "-C", str(target), "add", "-A"],
                       capture_output=True)
        subprocess.run([str(GIT), "-C", str(target), "commit", "-m",
                        "project snapshot", "--allow-empty"], capture_output=True)

    # -------------------------------------------------------------- loading
    def _load_yaml(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or default

    def universe(self) -> dict:
        return self._load_yaml(self.dir / "universe.yaml", {})

    def tiers(self) -> dict:
        return self._load_yaml(self.dir / "identity_tiers.yaml", {})

    def characters(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for p in sorted((self.dir / "characters").glob("*.yaml")):
            d = self._load_yaml(p, {})
            if isinstance(d, dict) and "id" in d:
                out[d["id"]] = d
        return out

    def crowd_types(self) -> dict[str, dict]:
        return self._load_yaml(self.dir / "crowd_types.yaml", {})

    def locations(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for p in sorted((self.dir / "locations").glob("*.yaml")):
            d = self._load_yaml(p, {})
            if isinstance(d, dict) and "id" in d:
                out[d["id"]] = d
        return out

    def scenes(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for p in sorted((self.dir / "scenes").glob("*.yaml")):
            d = self._load_yaml(p, {})
            if isinstance(d, dict) and "id" in d:
                out[d["id"]] = d
        return out

    def all(self) -> dict[str, Any]:
        return {
            "universe": self.universe(),
            "tiers": self.tiers(),
            "characters": self.characters(),
            "crowd_types": self.crowd_types(),
            "locations": self.locations(),
            "scenes": self.scenes(),
            "commit": self._head(),
        }

    def _head(self) -> str:
        out = subprocess.run(
            [str(GIT), "-C", str(self.dir), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True)
        return out.stdout.strip() or "none"

    # --------------------------------------------------------------- saving
    def _entity_path(self, sub: str, entity: dict[str, Any]) -> Path:
        """File for a character/location/scene. The id becomes a filename,
        so it must be a single safe path component."""
        if not isinstance(entity, dict):
            raise InvalidInput(f"{sub[:-1]} must be a mapping")
        eid = entity.get("id")
        if not isinstance(eid, str) or not is_safe_component(eid):
            raise InvalidInput(f"invalid {sub[:-1]} id: {eid!r} (letters, digits, "
                               "space, '_' or '-'; max 64 chars)")
        # Windows and macOS file names ignore case: "sara.yaml" IS "Sara.yaml".
        # An id that differs from another only in capitals would write over it.
        d = self.dir / sub
        if d.is_dir():
            for q in d.glob("*.yaml"):
                if q.stem != eid and q.stem.lower() == eid.lower():
                    held = self._load_yaml(q, {})
                    if isinstance(held, dict) and held.get("id") == eid:
                        return q          # the file that already holds this record
                    raise InvalidInput(f"{sub[:-1]} '{q.stem}' already exists: '{eid}' differs "
                                       "from it only in capitals")
        return d / f"{eid}.yaml"

    def save_character(self, char: dict[str, Any]) -> None:
        p = self._entity_path("characters", char)
        write_text(p, yaml.safe_dump(char, sort_keys=False, allow_unicode=True))
        self.git_commit(f"character {char['id']}")

    def save_location(self, loc: dict[str, Any]) -> None:
        p = self._entity_path("locations", loc)
        write_text(p, yaml.safe_dump(loc, sort_keys=False, allow_unicode=True))
        self.git_commit(f"location {loc['id']}")

    def save_scene(self, scene: dict[str, Any]) -> None:
        p = self._entity_path("scenes", scene)
        write_text(p, yaml.safe_dump(scene, sort_keys=False, allow_unicode=True))
        self.git_commit(f"scene {scene['id']}")

    def save_universe(self, uni: dict[str, Any]) -> None:
        write_text(self.dir / "universe.yaml",
                   yaml.safe_dump(uni, sort_keys=False, allow_unicode=True))
        self.git_commit("universe")

    def save_crowd_types(self, ct: dict[str, Any]) -> None:
        write_text(self.dir / "crowd_types.yaml",
                   yaml.safe_dump(ct, sort_keys=False, allow_unicode=True))
        self.git_commit("crowd types")

    # ------------------------------------------------------------ roster refs
    def face_refs(self, char_id: str) -> list[Path]:
        """Character face refs resolved to real files (Bible path or roster)."""
        char = self.characters().get(char_id, {})
        out: list[Path] = []
        roots = [(ROOT / "bible").resolve(), (ROOT / "roster").resolve()]
        for rel in char.get("face_refs", []):
            for base in roots:
                c = (base / str(rel)).resolve()
                if c.is_relative_to(base) and c.is_file():
                    out.append(c)
                    break
        return out
