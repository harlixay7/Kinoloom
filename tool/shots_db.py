"""Shot database (sqlite). Statuses, seed assignment (6.8), QA scores,
retries. Resume-safe — the single source of truth for the queue."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from core import ROOT, jsave, jload, log_setup

log = log_setup("shots_db")
_DB_LOCK = threading.RLock()

DB = ROOT / "tool" / "studio.db"
SHOT_DIR = ROOT / "shots"

# shot status lifecycle
PENDING, DRAFTING, QUEUED, RENDERING, POST, QA, PASSED, FAILED, REVIEW, ASSEMBLED = (
    "pending", "drafting", "queued", "rendering", "post", "qa",
    "passed", "failed", "review", "assembled")

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}\Z")


def assert_safe_identifier(name: str, allowed: frozenset[str] | set[str] | None = None) -> str:
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL column identifier: {name!r}")
    if allowed is not None and name not in allowed:
        raise ValueError(f"Disallowed SQL column identifier: {name!r}")
    return name


KNOWN_SHOT_FIELDS = frozenset({
    "status", "render_time_s", "video_path", "error", "updated",
    "tail_frame", "qa_notes", "prompt", "model", "loras", "json",
    "attempts", "scene", "tier", "negative", "keyframe_path",
    "ref_image", "final_path", "render_profile", "qa_stage",
    "project_id", "spec_version", "seed", "resolution", "qa_clip",
    "qa_face", "qa_motion"
})


# Additive columns for project membership and
# per-shot render/QA metadata. Applied idempotently by ShotsDB._migrate().
_MIGRATIONS = (
    ("project_id", "TEXT"),
    ("spec_version", "INTEGER DEFAULT 1"),
    ("render_profile", "TEXT"),
    ("qa_stage", "INTEGER DEFAULT 0"),
    ("keyframe_path", "TEXT"),
    ("tail_frame", "TEXT"),
    ("prompt", "TEXT"),
    ("negative", "TEXT"),
    ("ref_image", "TEXT"),
    # the post-processed (upscaled/graded) clip, when post ran: the card shows
    # the RAW render immediately and the final here once the worker finishes
    ("final_path", "TEXT"),
)


def _decode(raw: str) -> Any:
    """Decode stored shot JSON. New rows are json.dumps; legacy rows are
    Python dict literals."""
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, (dict, list)) else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        val = ast.literal_eval(raw)
        return val if isinstance(val, (dict, list)) else {}
    except (ValueError, SyntaxError, MemoryError, TypeError) as e:
        log.warning("Failed to decode shot raw JSON: %.100s (error: %s)", raw, e)
        return {}


def story_order(shots: list[dict]) -> list[dict]:
    """One scene's shots in story order: walk the prev_shot chain from the
    root (prev_shot None, stored in the shot JSON), then append any remaining
    shots sorted by shot_id (orphan-safe)."""
    def _prev(s: dict) -> str | None:
        j = s.get("json")
        return j.get("prev_shot") if isinstance(j, dict) else None

    roots = sorted((s for s in shots if _prev(s) is None), key=lambda s: s["shot_id"])
    ordered: list[dict] = []
    seen: set[str] = set()
    cur = roots[0] if roots else None
    while cur is not None and cur["shot_id"] not in seen:
        seen.add(cur["shot_id"])
        ordered.append(cur)
        nxt = [s for s in shots if _prev(s) == cur["shot_id"]]
        cur = nxt[0] if len(nxt) == 1 else None
    ordered.extend(sorted((s for s in shots if s["shot_id"] not in seen),
                          key=lambda s: s["shot_id"]))
    return ordered


class ShotsDB:
    def __init__(self) -> None:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = _DB_LOCK
        with self._lock:
            self.con = sqlite3.connect(str(DB), timeout=30.0, check_same_thread=False)
            self.con.row_factory = sqlite3.Row
            self.con.execute("PRAGMA journal_mode=WAL;")
            self.con.execute("PRAGMA busy_timeout=30000;")
            self.con.executescript("""
        CREATE TABLE IF NOT EXISTS shots(
            shot_id TEXT PRIMARY KEY,
            scene TEXT,
            json TEXT,
            status TEXT DEFAULT 'pending',
            tier TEXT,
            model TEXT,
            resolution TEXT,
            seed INTEGER,
            attempts INTEGER DEFAULT 0,
            render_time_s REAL,
            qa_clip REAL,
            qa_face REAL,
            qa_motion REAL,
            qa_notes TEXT,
            video_path TEXT,
            error TEXT,
            updated TEXT
        );
        CREATE TABLE IF NOT EXISTS scene_state(
            scene TEXT PRIMARY KEY,
            state TEXT
        );
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
        CREATE TABLE IF NOT EXISTS shot_prompts(
            project_id TEXT NOT NULL,
            shot_id TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            sig TEXT NOT NULL DEFAULT '',
            image TEXT NOT NULL DEFAULT '',
            unit TEXT NOT NULL DEFAULT '[]',
            model TEXT NOT NULL DEFAULT '',
            issues TEXT NOT NULL DEFAULT '[]',
            updated TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (project_id, shot_id)
        );
        """)
            self.con.commit()
            self._migrate()

    # ------------------------------------------------------------ migrations
    def _migrate(self) -> None:
        """Additive schema migration. Checks PRAGMA table_info
        first; each ALTER is also try/except guarded. Then rebuilds the
        shots table with a COMPOSITE primary key (shot_id, project_id) —
        two projects may both have an s01_001 and must never share a row."""
        with self._lock:
            cols = {r["name"] for r in
                    self.con.execute("PRAGMA table_info(shots)").fetchall()}
            for col, ddl in _MIGRATIONS:
                if col not in cols:
                    try:
                        self.con.execute(f"ALTER TABLE shots ADD COLUMN {col} {ddl}")
                    except sqlite3.Error as e:
                        log.debug("Migration column %s failed or exists: %s", col, e)
            self.con.commit()
            self._rebuild_pk()

    def _rebuild_pk(self) -> None:
        """Rebuild the shots table so its primary key is
        (shot_id, project_id) instead of shot_id alone."""
        import re
        with self._lock:
            row = self.con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='shots'"
            ).fetchone()
            sql = (row["sql"] if row else "") or ""
            m = re.search(r"PRIMARY KEY\s*\(([^)]*)\)", sql)
            pk_cols = [c.strip().strip('"') for c in (m.group(1) if m else "").split(",")] if m else []
            if "project_id" in pk_cols:
                return  # already composite
            cols = [r["name"] for r in
                    self.con.execute("PRAGMA table_info(shots)").fetchall()]
            col_sql = ", ".join(
                f'"{c}"' for c in cols if c != "shot_id")  # noqa: F841
            self.con.executescript("""
        BEGIN;
        ALTER TABLE shots RENAME TO shots_old;
        CREATE TABLE shots(
            shot_id TEXT NOT NULL,
            scene TEXT,
            json TEXT,
            status TEXT DEFAULT 'pending',
            tier TEXT,
            model TEXT,
            resolution TEXT,
            seed INTEGER,
            attempts INTEGER DEFAULT 0,
            render_time_s REAL,
            qa_clip REAL,
            qa_face REAL,
            qa_motion REAL,
            qa_notes TEXT,
            video_path TEXT,
            error TEXT,
            updated TEXT,
            project_id TEXT,
            spec_version INTEGER DEFAULT 1,
            render_profile TEXT,
            qa_stage INTEGER DEFAULT 0,
            keyframe_path TEXT,
            tail_frame TEXT,
            prompt TEXT,
            negative TEXT,
            ref_image TEXT,
            final_path TEXT,
            PRIMARY KEY (shot_id, project_id)
        );
        INSERT OR REPLACE INTO shots(
            shot_id, scene, json, status, tier, model, resolution, seed,
            attempts, render_time_s, qa_clip, qa_face, qa_motion, qa_notes,
            video_path, error, updated, project_id, spec_version,
            render_profile, qa_stage, keyframe_path, tail_frame, prompt,
            negative, ref_image, final_path)
        SELECT shot_id, scene, json, status, tier, model, resolution, seed,
            attempts, render_time_s, qa_clip, qa_face, qa_motion, qa_notes,
            video_path, error, updated, project_id, spec_version,
            render_profile, qa_stage, keyframe_path, tail_frame, prompt,
            negative, ref_image, final_path
        FROM shots_old;
        DROP TABLE shots_old;
        COMMIT;
        """)
            self.con.commit()

    # ------------------------------------------------------------ ingestion
    def upsert_shots(self, shots: list[dict]) -> dict:
        with self._lock:
            added = updated = 0
            for s in shots:
                sid = s["shot_id"]
                pid = s.get("project_id")
                cur = self.con.execute(
                    "SELECT json FROM shots WHERE shot_id=? AND project_id IS ?",
                    (sid, pid)).fetchone()
                if cur is None:
                    added += 1
                    self.con.execute(
                        "INSERT INTO shots(shot_id, scene, json, tier, status,"
                        " project_id, updated)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (sid, s.get("scene", ""),
                         json.dumps(s, ensure_ascii=False), s.get("tier", ""),
                         PENDING, pid, ""))
                else:
                    updated += 1
                    self.con.execute(
                        "UPDATE shots SET json=?, scene=?, tier=?"
                        " WHERE shot_id=? AND project_id IS ?",
                        (json.dumps(s, ensure_ascii=False),
                         s.get("scene", ""), s.get("tier", ""), sid, pid))
            self.con.commit()
            return {"added": added, "updated": updated}

    def delete_shot(self, sid: str, project: str | None = None) -> None:
        with self._lock:
            self.con.execute(
                "DELETE FROM shots WHERE shot_id=? AND project_id IS ?",
                (sid, project))
            self.con.execute(
                "DELETE FROM shot_prompts WHERE shot_id=? AND project_id IS ?",
                (sid, project))
            self.con.commit()

    # --------------------------------------------------------- movie prompts
    # The video prompt of a project shot rendered on LTX-2.5 / MiniMax H3,
    # written by an AI or by hand (movie_prompts). `sig` is what it was written
    # for (movie_prompts.shot_sigs), `unit` the shot ids written together,
    # `image` the opening image's prompt (a unit's first shot), `issues` the
    # prompt check's warnings when it was stored.
    _PROMPT_FIELDS = ("text", "source", "sig", "image", "unit", "model", "issues")

    def prompts(self, project: str, sids: list[str] | None = None) -> dict[str, dict]:
        """shot id -> its stored prompt, for the whole project or `sids`."""
        q, args = "SELECT * FROM shot_prompts WHERE project_id=?", [project]
        if sids is not None:
            if not sids:
                return {}
            q += f" AND shot_id IN ({', '.join(['?'] * len(sids))})"
            args += list(sids)
        with self._lock:
            rows = self.con.execute(q, args).fetchall()
        out = {}
        for r in rows:
            d = dict(r)
            d["unit"] = _decode(d.get("unit") or "[]") or []
            d["issues"] = _decode(d.get("issues") or "[]") or []
            out[d["shot_id"]] = d
        return out

    def prompt(self, sid: str, project: str) -> dict | None:
        return self.prompts(project, [sid]).get(sid)

    def save_prompt(self, sid: str, project: str, **kw: Any) -> None:
        """Insert or update one shot's prompt; fields left out keep their value."""
        vals = {k: kw[k] for k in self._PROMPT_FIELDS if k in kw}
        for k in ("unit", "issues"):
            if k in vals and not isinstance(vals[k], str):
                vals[k] = json.dumps(vals[k], ensure_ascii=False)
        from core import now
        vals["updated"] = now()
        with self._lock:
            self.con.execute(
                f"INSERT INTO shot_prompts(project_id, shot_id, {', '.join(vals)})"
                f" VALUES(?, ?, {', '.join(['?'] * len(vals))})"
                f" ON CONFLICT(project_id, shot_id) DO UPDATE SET "
                + ", ".join(f"{k}=excluded.{k}" for k in vals),
                [project, sid, *vals.values()])
            self.con.commit()

    def delete_prompt(self, sid: str, project: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM shot_prompts WHERE shot_id=? AND project_id=?",
                             (sid, project))
            self.con.commit()

    # --------------------------------------------------------------- queries
    def all(self, scene: str | None = None, status: str | None = None,
            project: str | None = None, limit: int = 5000) -> list[dict]:
        with self._lock:
            q = "SELECT * FROM shots"; where: list[str] = []; args: list = []
            if scene:
                where.append("scene=?"); args.append(scene)
            if status:
                where.append("status=?"); args.append(status)
            if project:
                where.append("project_id=?"); args.append(project)
            if where:
                q += " WHERE " + " AND ".join(where)
            q += f" ORDER BY scene, shot_id LIMIT {int(limit)}"
            rows = self.con.execute(q, args).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["json"] = _decode(d["json"])
                out.append(d)
            return out

    def get(self, sid: str, project: str | None = None) -> dict | None:
        with self._lock:
            r = self.con.execute(
                "SELECT * FROM shots WHERE shot_id=? AND project_id IS ?",
                (sid, project)).fetchone()
            if not r:
                return None
            d = dict(r)
            d["json"] = _decode(d["json"])
            return d

    def stats(self) -> dict:
        with self._lock:
            rows = self.con.execute(
                "SELECT status, COUNT(*) n FROM shots GROUP BY status").fetchall()
            stats = {r["status"]: r["n"] for r in rows}
            total = self.con.execute("SELECT COUNT(*) n FROM shots").fetchone()["n"]
            hero = self.con.execute(
                "SELECT COUNT(*) n FROM shots WHERE tier='hero'").fetchone()["n"]
            if total:
                stats["hero_ratio"] = hero / total
            stats["total"] = total
            return stats

    def scene_order(self, scene: str, project: str | None = None) -> list[dict]:
        """Shots of one scene in story order (story_order)."""
        return story_order(self.all(scene=scene, project=project))

    def project_stats(self, project_id: str) -> dict:
        """Per-project summary: shot counts by status and by tier, plus the
        summed render time in seconds."""
        with self._lock:
            rows = self.con.execute(
                "SELECT status, tier, COUNT(*) n, SUM(render_time_s) t FROM shots"
                " WHERE project_id=? GROUP BY status, tier", (project_id,)).fetchall()
            by_status: dict[str, int] = {}
            by_tier: dict[str, int] = {}
            total = 0
            render_time_s = 0.0
            for r in rows:
                by_status[r["status"]] = by_status.get(r["status"], 0) + r["n"]
                tier = r["tier"] or ""
                by_tier[tier] = by_tier.get(tier, 0) + r["n"]
                total += r["n"]
                render_time_s += r["t"] or 0.0
            return {"by_status": by_status, "by_tier": by_tier, "total": total,
                    "render_time_s": render_time_s}

    # --------------------------------------------------------------- updates
    def set_status(self, sid: str, status: str, project: str | None = None,
                   **kw: Any) -> None:
        with self._lock:
            fields = ["status=?"]
            args: list = [status]
            for k, v in kw.items():
                if v is None:
                    continue
                assert_safe_identifier(k, KNOWN_SHOT_FIELDS)
                fields.append(f"{k}=?")
                args.append(v)
            args.append(sid)
            args.append(project)
            self.con.execute(
                f"UPDATE shots SET {', '.join(fields)} WHERE shot_id=?"
                f" AND project_id IS ?", args)
            self.con.commit()

    def record_render(self, sid: str, render_time_s: float, video_path: str,
                      project: str | None = None) -> None:
        # error="" CLEARS a stale failure message: a shot that failed once and
        # then rendered fine kept showing the old error on its card forever
        self.set_status(sid, POST, render_time_s=render_time_s,
                        video_path=str(video_path), error="", updated="",
                        project=project)

    def set_json(self, sid: str, json_dict: dict,
                 project: str | None = None) -> None:
        """Replace the stored shot JSON (prompt/lora/continuity edits)."""
        import json as _json
        with self._lock:
            self.con.execute(
                "UPDATE shots SET json=? WHERE shot_id=? AND project_id IS ?",
                (_json.dumps(json_dict, ensure_ascii=False), sid, project))
            self.con.commit()

    def record_qa(self, sid: str, passed: bool, clip: float | None,
                  face: float | None, motion: float | None, notes: str = "",
                  project: str | None = None) -> None:
        with self._lock:
            self.con.execute(
                "UPDATE shots SET status=?, qa_clip=?, qa_face=?, qa_motion=?, "
                "qa_notes=?, updated=? WHERE shot_id=? AND project_id IS ?",
                (PASSED if passed else FAILED, clip, face, motion, notes, "", sid, project))
            self.con.commit()

    def increment_attempts(self, sid: str,
                           project: str | None = None) -> int:
        with self._lock:
            self.con.execute(
                "UPDATE shots SET attempts=attempts+1 WHERE shot_id=?"
                " AND project_id IS ?", (sid, project))
            self.con.commit()
            return self.get(sid, project)["attempts"]

    # ------------------------------------------------------------ seed engine
    def assign_seed(self, shot: dict) -> int:
        """Seed coherence (6.8): same base seed within a scene."""
        scene = shot["scene"]
        base = int(hashlib.md5(scene.encode("utf-8")).hexdigest()[:8], 16) \
            % (2 ** 31)
        pid = shot.get("project_id")
        with self._lock:
            idx = self.con.execute(
                "SELECT COUNT(*) n FROM shots WHERE scene=? AND status!='pending'"
                " AND project_id IS ?", (scene, pid)).fetchone()["n"]
            return (base + idx) % (2 ** 31)

    def retry_seed(self, shot: dict, attempt: int) -> int:
        base = self.assign_seed(shot)
        return (base + attempt * 10000) % (2 ** 31)

    def stable_seed(self, shot: dict) -> int:
        """A shot's own first seed: the scene's base seed (6.8) plus the
        shot's place in the scene's story order. Unlike assign_seed it does
        not move with the statuses of the other shots. `shot` is a row or a
        shot json."""
        j = shot.get("json")
        sj: dict = j if isinstance(j, dict) else shot
        scene = str(shot.get("scene") or sj.get("scene") or "")
        pid = shot.get("project_id") or sj.get("project_id")
        sid = shot.get("shot_id") or sj.get("shot_id")
        base = int(hashlib.md5(scene.encode("utf-8")).hexdigest()[:8], 16) % (2 ** 31)
        order = [r["shot_id"] for r in self.scene_order(scene, project=pid)]
        return (base + (order.index(sid) if sid in order else len(order))) % (2 ** 31)

    # ------------------------------------------------------------ scene state
    def scene_state(self, scene: str) -> dict:
        with self._lock:
            r = self.con.execute(
                "SELECT state FROM scene_state WHERE scene=?", (scene,)).fetchone()
            if not r:
                return {}
            return jload(SHOT_DIR / "state" / f"{scene}.json")

    def save_scene_state(self, scene: str, state: dict) -> None:
        p = SHOT_DIR / "state" / f"{scene}.json"
        jsave(p, state)
        with self._lock:
            self.con.execute(
                "INSERT OR REPLACE INTO scene_state(scene, state) VALUES(?,?)",
                (scene, str(p)))
            self.con.commit()
