"""Projects — top-level movie objects with story state,
automation mode, gates, and versioned story stages."""
from __future__ import annotations

import contextlib
import json
import re
from typing import Any

from core import log_setup, now

log = log_setup("projects")


def _db_lock(db):
    lock = getattr(db, "_lock", None)
    return lock if lock is not None else contextlib.nullcontext()

STAGES = ("treatment", "bible", "beats", "scenes", "shots")
DEFAULT_GATES = {"story": "auto", "keyframes": "manual", "review": "manual"}
KNOWN_FIELDS = {
    "title", "idea", "genre", "tone", "duration_target", "mode", "gates",
    "status", "stage", "story", "bible_commit", "created", "updated",
    "ref_fallback", "video_family", "video_models", "post_processing",
    "post_worker", "seamless_takes", "error",
}

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}\Z")


def assert_safe_identifier(name: str, allowed: frozenset[str] | set[str] | None = None) -> str:
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL column identifier: {name!r}")
    if allowed is not None and name not in allowed:
        raise ValueError(f"Disallowed SQL column identifier: {name!r}")
    return name


# Additive projects-table columns, applied idempotently per connection by
# ProjectStore. (Formerly one process-global "done" flag per column — a
# second database in the same process, e.g. a restored backup or a test,
# then silently skipped its migrations.)
#   ref_fallback    1 = allow the roster portrait as a start-image fallback;
#                   0 = render T2V instead when no ref/keyframe
#   video_family    "" = studio default, else a video_models registry id
#                   (e.g. 'ltx25_w4a8_refs'); an unknown/unavailable id falls
#                   back to the default with a logged warning, never bricks
#   video_models    per-tier overrides as JSON, {"hero": "...", "standard":
#                   "...", "volume": ""}; empty values fall back to
#                   video_family
#   post_processing 1 = full post chain (RIFE + upscale + face-fix + grade);
#                   0 = deliver the raw render (the agreed LTX-2.5 workflow)
#   post_worker     which worker PC runs the post stage ("" = local)
#   seamless_takes  1 = a run of CONTINUE shots on a seamless model renders as
#                   one continuous take (movie_takes); 0 = one shot at a time
#   error           why the director stopped the project (status 'error')
_PROJECT_COLUMNS = (
    ("ref_fallback", "INTEGER DEFAULT 1"),
    ("video_family", "TEXT DEFAULT ''"),
    ("video_models", "TEXT DEFAULT ''"),
    ("post_processing", "INTEGER DEFAULT 0"),
    ("post_worker", "TEXT DEFAULT ''"),
    ("seamless_takes", "INTEGER DEFAULT 1"),
    ("error", "TEXT DEFAULT ''"),
)


def _ensure_project_columns(db) -> None:
    with _db_lock(db):
        cols = {r["name"] for r in db.con.execute(
            "PRAGMA table_info(projects)").fetchall()}
        for name, ddl in _PROJECT_COLUMNS:
            if name not in cols:
                db.con.execute(
                    f"ALTER TABLE projects ADD COLUMN {name} {ddl}")
        db.con.commit()


def _parse_json(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw) or default
        except (json.JSONDecodeError, TypeError, ValueError):
            return default
    return default


def _slug(title: str) -> str:
    s = "".join(c.lower() if c.isalnum() else "_" for c in title.strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "project"


class ProjectStore:
    def __init__(self, db):  # db = ShotsDB instance (uses db.con)
        self.db = db
        self.con = db.con
        self._lock = getattr(db, "_lock", None) or contextlib.nullcontext()
        with self._lock:
            self.con.executescript("""
        CREATE TABLE IF NOT EXISTS projects(
            id TEXT PRIMARY KEY,
            title TEXT, idea TEXT, genre TEXT, tone TEXT,
            duration_target INTEGER DEFAULT 60,
            mode TEXT DEFAULT 'checkpoints',
            gates TEXT DEFAULT '{"story":"auto","keyframes":"manual","review":"manual"}',
            status TEXT DEFAULT 'draft', stage TEXT DEFAULT 'story',
            story TEXT DEFAULT '{}', bible_commit TEXT DEFAULT '',
            created TEXT, updated TEXT
        );
        CREATE TABLE IF NOT EXISTS story_versions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT, stage TEXT, version INTEGER, content TEXT, created TEXT
        );
        """)
            self.con.commit()
            _ensure_project_columns(db)

    # ------------------------------------------------------------------ crud
    def create(self, title, idea="", genre="", tone="", duration_target=60,
               mode="checkpoints", gates=None) -> dict:
        base = _slug(title)
        pid = base
        n = 2
        with self._lock:
            while self.get(pid) is not None:
                pid = f"{base}_{n}"
                n += 1
            g = gates or dict(DEFAULT_GATES)
            ts = now()
            self.con.execute(
                "INSERT INTO projects(id, title, idea, genre, tone,"
                " duration_target, mode, gates, status, stage, story, created,"
                " updated, ref_fallback) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, title, idea, genre, tone, int(duration_target), mode,
                 json.dumps(g, ensure_ascii=False), "draft", "story", "{}",
                 ts, ts, 1))
            self.con.commit()
            log.info("project created: %s (%s)", pid, title)
            return self.get(pid)

    @staticmethod
    def _row(r) -> dict:
        """One projects row with its JSON/flag columns decoded — shared by
        get() and list() so both return the same shape."""
        d = dict(r)
        d["gates"] = _parse_json(d.get("gates"), dict(DEFAULT_GATES))
        d["story"] = _parse_json(d.get("story"), {})
        d["ref_fallback"] = int(d["ref_fallback"]) \
            if d.get("ref_fallback") is not None else 1
        d["video_family"] = str(d.get("video_family") or "")
        d["video_models"] = _parse_json(d.get("video_models"), {})
        d["post_processing"] = int(d["post_processing"]) \
            if d.get("post_processing") is not None else 0
        d["post_worker"] = str(d.get("post_worker") or "")
        d["seamless_takes"] = int(d["seamless_takes"]) \
            if d.get("seamless_takes") is not None else 1
        d["error"] = str(d.get("error") or "")
        return d

    def get(self, pid) -> dict | None:
        with self._lock:
            r = self.con.execute(
                "SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
            return self._row(r) if r else None

    def list(self) -> list[dict]:
        with self._lock:
            rows = self.con.execute(
                "SELECT * FROM projects ORDER BY created DESC, rowid DESC").fetchall()
            return [self._row(r) for r in rows]

    def update(self, pid, **kw) -> bool:
        sets: list[str] = []
        args: list = []
        for k, v in kw.items():
            assert_safe_identifier(k, KNOWN_FIELDS)
            if k in ("gates", "story", "video_models") and isinstance(v, dict):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=?")
            args.append(v)
        if not sets:
            return False
        sets.append("updated=?")
        args.append(now())
        args.append(pid)
        with self._lock:
            cur = self.con.execute(
                f"UPDATE projects SET {', '.join(sets)} WHERE id=?", args)
            self.con.commit()
            return cur.rowcount > 0

    def delete(self, pid) -> dict:
        """Delete the project row, its shots rows, and its project Bible
        folder. Rendered video files on disk are intentionally kept."""
        with self._lock:
            removed = self.con.execute(
                "DELETE FROM shots WHERE project_id=?", (pid,)).rowcount
            self.con.execute(
                "DELETE FROM story_versions WHERE project_id=?", (pid,))
            self.con.execute("DELETE FROM projects WHERE id=?", (pid,))
            self.con.commit()
        # remove the project-private bible folder so a future project with
        # the same slug starts fresh (stale cast files used to leak in)
        import shutil
        from core import ROOT
        pdir = ROOT / "bible" / "projects" / pid
        if pdir.is_dir():
            try:
                shutil.rmtree(pdir, ignore_errors=True)
            except Exception as e:  # noqa: BLE001
                log.warning("bible folder cleanup failed for %s: %s", pid, e)
        log.info("project deleted: %s (%s shots removed)", pid, removed)
        return {"removed_shots": removed, "bible_removed": not pdir.exists()}

    # ----------------------------------------------------------- story stage
    def _next_version(self, pid: str, stage: str) -> int:
        with self._lock:
            r = self.con.execute(
                "SELECT COALESCE(MAX(version), 0) v FROM story_versions"
                " WHERE project_id=? AND stage=?", (pid, stage)).fetchone()
            return int(r["v"]) + 1

    def set_story_stage(self, pid, stage, content: dict) -> dict:
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage!r}")
        with self._lock:
            proj = self.get(pid)
            if proj is None:
                raise KeyError(pid)
            story = proj["story"] if isinstance(proj["story"], dict) else {}
            story[stage] = content
            version = self._next_version(pid, stage)
            self.con.execute(
                "INSERT INTO story_versions(project_id, stage, version, content,"
                " created) VALUES(?,?,?,?,?)",
                (pid, stage, version, json.dumps(content, ensure_ascii=False),
                 now()))
            self.update(pid, story=story)
            log.info("story stage saved: %s.%s v%d", pid, stage, version)
            return {"version": version}

    def story_versions(self, pid, stage=None) -> list[dict]:
        with self._lock:
            q = "SELECT * FROM story_versions WHERE project_id=?"
            args: list = [pid]
            if stage:
                q += " AND stage=?"
                args.append(stage)
            q += " ORDER BY version"
            out = []
            for r in self.con.execute(q, args).fetchall():
                d = dict(r)
                d["content"] = _parse_json(d.get("content"), {})
                out.append(d)
            return out

    def delete_story_version(self, pid: str, stage: str, version: int) -> bool:
        """Remove one history entry. The ACTIVE stage content is untouched —
        use clear_story() to wipe the live story."""
        if stage not in STAGES:
            return False
        with self._lock:
            cur = self.con.execute(
                "DELETE FROM story_versions WHERE project_id=? AND stage=?"
                " AND version=?", (pid, stage, int(version)))
            self.con.commit()
            return cur.rowcount > 0

    def clear_story(self, pid: str, stages: list[str] | None = None,
                    keep_history: bool = False) -> dict:
        """Wipe the LIVE story stages (and optionally their history).

        Used when a wrong generation must be removed before pasting a new
        package: clears the project's story JSON for the given stages, drops
        the version history for them, and (for 'shots') deletes the shot rows
        so the Scenes & Shots tab is empty again. The CAST is never touched —
        characters/locations are authored in the Cast tab and shared.
        """
        with self._lock:
            proj = self.get(pid)
            if proj is None:
                raise KeyError(pid)
            want = [s for s in (stages or list(STAGES)) if s in STAGES]
            story = proj["story"] if isinstance(proj["story"], dict) else {}
            for s in want:
                story.pop(s, None)
            if not keep_history:
                q = ("DELETE FROM story_versions WHERE project_id=?"
                     + (" AND stage IN (%s)" % ",".join("?" * len(want))
                        if want else ""))
                self.con.execute(q, [pid] + want)
                self.con.commit()
            self.update(pid, story=story)
            log.info("story cleared: %s stages=%s keep_history=%s",
                     pid, want, keep_history)
            return {"stages": want, "history_kept": bool(keep_history)}

    def rollback_stage(self, pid, stage, version) -> bool:
        with self._lock:
            r = self.con.execute(
                "SELECT content FROM story_versions"
                " WHERE project_id=? AND stage=? AND version=?",
                (pid, stage, int(version))).fetchone()
            if not r:
                return False
            content = _parse_json(r["content"], {})
            proj = self.get(pid)
            story = proj["story"] if proj and isinstance(proj["story"], dict) else {}
            story[stage] = content
            new_version = self._next_version(pid, stage)
            self.con.execute(
                "INSERT INTO story_versions(project_id, stage, version, content,"
                " created) VALUES(?,?,?,?,?)",
                (pid, stage, new_version, json.dumps(content, ensure_ascii=False),
                 now()))
            self.update(pid, story=story)
            log.info("story stage rolled back: %s.%s -> v%d", pid, stage,
                     new_version)
            return True

    # ------------------------------------------------------------- lifecycle
    def set_status(self, pid, status, stage=None, **kw) -> None:
        fields = dict(kw)
        if stage is not None:
            fields["stage"] = stage
        fields["status"] = status
        if status != "error":
            fields.setdefault("error", "")   # a resumed project drops the old reason
        self.update(pid, **fields)

    def mode(self, pid) -> str:
        proj = self.get(pid)
        if proj is None:
            return "checkpoints"
        return proj.get("mode") or "checkpoints"

    def gate(self, pid, name) -> str:
        proj = self.get(pid)
        gates = proj.get("gates") if proj else {}
        if not isinstance(gates, dict):
            gates = {}
        return gates.get(name, "manual")
