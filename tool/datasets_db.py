"""Dataset provenance + generation-run persistence.

One sqlite database (`tool/datasets.db`) holds:
  - dataset_images : per-image provenance (model, prompt, seed, settings,
                     variations, timestamps, status) — enables filtering
                     by model, reproduction, regeneration, diagnostics
  - gen_runs       : generation jobs with per-image tasks and state —
                     resume-safe across application restarts

Existing datasets are LAZILY BACKFILLED from meta.json/curation.json on
first access — old data is never destroyed or reinterpreted.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from core import ROOT, log_setup

log = log_setup("datasets_db")
_DB_LOCK = threading.RLock()

DB = ROOT / "tool" / "datasets.db"
DATASETS_DIR = ROOT / "roster" / "datasets"
VALID_TYPES = ("face", "power", "environment", "weapon", "outfit", "anatomy", "quality")

RUN_QUEUED, RUN_ACTIVE, RUN_DONE, RUN_FAILED, RUN_CANCELED, RUN_PARTIAL = (
    "queued", "active", "done", "failed", "canceled", "partial")
IMG_PENDING, IMG_DONE, IMG_FAILED, IMG_SKIPPED = (
    "pending", "done", "failed", "skipped")

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}\Z")


def assert_safe_identifier(name: str, allowed: frozenset[str] | set[str] | None = None) -> str:
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL column identifier: {name!r}")
    if allowed is not None and name not in allowed:
        raise ValueError(f"Disallowed SQL column identifier: {name!r}")
    return name


KNOWN_RUN_FIELDS = frozenset({
    "status", "total", "done", "failed", "params", "error",
    "updated", "created", "name", "dtype"
})


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB), timeout=30.0, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=30000;")
    return con


def init() -> None:
    with _DB_LOCK:
        con = _con()
        con.executescript("""
    CREATE TABLE IF NOT EXISTS dataset_images(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dataset_type TEXT, dataset_name TEXT,
        file TEXT, run_id INTEGER,
        model TEXT, prompt TEXT, negative TEXT,
        seed INTEGER, steps INTEGER, cfg REAL,
        sampler TEXT, scheduler TEXT, resolution TEXT,
        loras TEXT, variations TEXT,
        source TEXT DEFAULT 'generated',
        ts TEXT, duration REAL,
        status TEXT DEFAULT 'done'
    );
    CREATE TABLE IF NOT EXISTS gen_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dataset_type TEXT, dataset_name TEXT,
        status TEXT, total INTEGER, done INTEGER, failed INTEGER,
        params TEXT, created TEXT, updated TEXT, error TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_img_ds ON dataset_images(dataset_type, dataset_name);
    CREATE INDEX IF NOT EXISTS idx_img_run ON dataset_images(run_id);
    """)
        con.commit()
        con.close()


# ---------------------------------------------------------------- backfill
def backfill_dataset(dtype: str, name: str) -> None:
    """Scan an existing dataset folder + meta.json into provenance rows.
    Idempotent: skips files already recorded (by file path)."""
    d = DATASETS_DIR / dtype / name
    if not d.is_dir():
        return
    mp = d / "meta.json"
    meta = {}
    if mp.exists():
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Failed to parse %s: %s", mp, e)
            meta = {}
    with _DB_LOCK:
        con = _con()
        known = {r["file"] for r in con.execute(
            "SELECT file FROM dataset_images WHERE dataset_type=? "
            "AND dataset_name=?", (dtype, name))}
        rows = []
        for sub in ("", "stills", "frames"):
            base = d if not sub else d / sub
            if not base.is_dir():
                continue
            for f in sorted(base.glob("*.png")):
                rel = f.relative_to(d).as_posix()
                if rel in known:
                    continue
                m = meta.get(f.name, {}) if isinstance(meta, dict) else {}
                rows.append((dtype, name, rel, None,
                             m.get("model", ""),
                             m.get("prompt", ""),
                             m.get("negative", ""),
                             m.get("seed"), m.get("steps"), m.get("cfg"),
                             m.get("sampler", ""), m.get("scheduler", ""),
                             m.get("resolution", ""),
                             json.dumps(m.get("loras", []), ensure_ascii=False),
                             json.dumps({k: v for k, v in m.items()
                                         if k not in ("model", "prompt",
                                                      "negative", "seed",
                                                      "steps", "cfg", "sampler",
                                                      "scheduler", "resolution",
                                                      "loras")},
                                        ensure_ascii=False),
                             "imported" if m.get("source") != "generated"
                             else "generated",
                             m.get("ts", ""), None, "done"))
        if rows:
            con.executemany(
                "INSERT INTO dataset_images(dataset_type, dataset_name, file, "
                "run_id, model, prompt, negative, seed, steps, cfg, sampler, "
                "scheduler, resolution, loras, variations, source, ts, "
                "duration, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            con.commit()
            log.info(f"backfilled {len(rows)} provenance rows for "
                     f"{dtype}/{name}")
        con.close()


def backfill_all() -> None:
    for t in VALID_TYPES:
        td = DATASETS_DIR / t
        if not td.is_dir():
            continue
        for d in td.iterdir():
            if d.is_dir():
                backfill_dataset(t, d.name)


# ------------------------------------------------------------------ runs
def create_run(dtype: str, name: str, params: dict, total: int = 0) -> int:
    with _DB_LOCK:
        con = _con()
        cur = con.execute(
            "INSERT INTO gen_runs(dataset_type, dataset_name, status, total, "
            "done, failed, params, created, updated) VALUES(?,?,?,?,?,?,?,?,?)",
            (dtype, name, RUN_QUEUED, total, 0, 0,
             json.dumps(params, ensure_ascii=False),
             time.strftime("%Y-%m-%d %H:%M:%S"),
             time.strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
        rid = cur.lastrowid
        con.close()
        return rid


def get_run(rid: int) -> dict | None:
    with _DB_LOCK:
        con = _con()
        r = con.execute("SELECT * FROM gen_runs WHERE id=?", (rid,)).fetchone()
        con.close()
        return dict(r) if r else None


def list_runs(status: str | None = None) -> list[dict]:
    """All gen_runs rows (optionally filtered by status), oldest first."""
    with _DB_LOCK:
        con = _con()
        if status:
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM gen_runs WHERE status=? ORDER BY id", (status,))]
        else:
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM gen_runs ORDER BY id")]
        con.close()
        return rows


def count_images_for_run(rid: int) -> int:
    """Number of provenance rows recorded for a run. Jobs are deterministic
    and saved in order, so this many leading tasks are already done —
    retry/continuation skips them instead of re-rendering."""
    with _DB_LOCK:
        con = _con()
        n = con.execute("SELECT COUNT(*) FROM dataset_images WHERE run_id=?",
                        (rid,)).fetchone()[0]
        con.close()
        return int(n)


def update_run(rid: int, **kw: Any) -> None:
    with _DB_LOCK:
        con = _con()
        fields = []
        args: list = []
        for k, v in kw.items():
            assert_safe_identifier(k, KNOWN_RUN_FIELDS)
            fields.append(f"{k}=?")
            args.append(v)
        fields.append("updated=?")
        args.append(time.strftime("%Y-%m-%d %H:%M:%S"))
        args.append(rid)
        con.execute(f"UPDATE gen_runs SET {', '.join(fields)} WHERE id=?", args)
        con.commit()
        con.close()


def set_run_status(rid: int, status: str, error: str | None = None) -> None:
    if error is not None:
        update_run(rid, status=status, error=error)
    else:
        update_run(rid, status=status)


def recover_runs() -> int:
    """After a restart: runs stuck in 'active' become 'partial' — we
    remember exactly where they left off (done images are preserved and
    never re-rendered on resume) but do NOT auto-continue; the user
    resumes via the "Continue the batch" button on the dataset card."""
    with _DB_LOCK:
        con = _con()
        n = con.execute("UPDATE gen_runs SET status='partial', updated=? "
                        "WHERE status='active'",
                        (time.strftime("%Y-%m-%d %H:%M:%S"),)).rowcount
        con.commit()
        con.close()
        if n:
            log.info(f"recovered {n} interrupted run(s) -> partial (paused)")
        return n


def run_stats(rid: int) -> dict:
    with _DB_LOCK:
        con = _con()
        r = con.execute("SELECT status, total, done, failed, params, error "
                        "FROM gen_runs WHERE id=?", (rid,)).fetchone()
        if not r:
            con.close()
            return {}
        per_model: dict[str, int] = {}
        for row in con.execute(
                "SELECT model, COUNT(*) n FROM dataset_images WHERE run_id=? "
                "GROUP BY model", (rid,)):
            per_model[row["model"]] = row["n"]
        con.close()
        return {"status": r["status"], "total": r["total"], "done": r["done"],
                "failed": r["failed"], "params": json.loads(r["params"] or "{}"),
                "error": r["error"], "per_model": per_model}


# -------------------------------------------------------------- images
def add_image(rid: int, dtype: str, name: str, file: str, model: str,
              prompt: str, negative: str, seed: int | None, steps: int,
              cfg: float, sampler: str, scheduler: str, res: str,
              loras: list | None, variations: dict | None,
              source: str = "generated", duration: float | None = None,
              status: str = IMG_DONE) -> None:
    with _DB_LOCK:
        con = _con()
        con.execute(
            "INSERT INTO dataset_images(dataset_type, dataset_name, file, "
            "run_id, model, prompt, negative, seed, steps, cfg, sampler, "
            "scheduler, resolution, loras, variations, source, ts, duration, "
            "status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (dtype, name, file, rid, model, prompt, negative, seed, steps, cfg,
             sampler, scheduler, res,
             json.dumps(loras or [], ensure_ascii=False),
             json.dumps(variations or {}, ensure_ascii=False),
             source, time.strftime("%Y-%m-%d %H:%M:%S"), duration, status))
        con.commit()
        con.close()


def list_images(dtype: str | None = None, name: str | None = None,
                model: str | None = None, limit: int = 10000) -> list[dict]:
    with _DB_LOCK:
        con = _con()
        q = "SELECT * FROM dataset_images"
        where: list[str] = []
        args: list = []
        if dtype:
            where.append("dataset_type=?"); args.append(dtype)
        if name:
            where.append("dataset_name=?"); args.append(name)
        if model:
            where.append("model=?"); args.append(model)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += f" ORDER BY id DESC LIMIT {int(limit)}"
        rows = [dict(r) for r in con.execute(q, args)]
        con.close()
        for r in rows:
            r["loras"] = json.loads(r["loras"] or "[]")
            r["variations"] = json.loads(r["variations"] or "{}")
        return rows


def images_by_dataset(dtype: str, name: str) -> list[dict]:
    return list_images(dtype, name)


def model_distribution(dtype: str, name: str) -> dict[str, int]:
    with _DB_LOCK:
        con = _con()
        rows = con.execute(
            "SELECT model, COUNT(*) n FROM dataset_images "
            "WHERE dataset_type=? AND dataset_name=? AND model!='' "
            "GROUP BY model", (dtype, name)).fetchall()
        con.close()
        return {r["model"]: r["n"] for r in rows}


def delete_image(dtype: str, name: str, file: str) -> None:
    with _DB_LOCK:
        con = _con()
        con.execute("DELETE FROM dataset_images WHERE dataset_type=? "
                    "AND dataset_name=? AND file=?", (dtype, name, file))
        con.commit()
        con.close()
