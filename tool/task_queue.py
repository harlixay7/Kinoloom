"""Unified task queue — persistent task tracking across generation types.

Layer, not replacement: the four engines (jobs.py dataset batches, the shot
orchestrator, video_studio.py, images.py) remain the executors with their own
persistence and single-flight guards. This module adds:

  * queue.db (WAL sqlite; SEPARATE from studio.db / datasets.db — schemas
    of those are never touched) with tables queue_items / workers /
    shot_assignments / meta,
  * draft/hold lifecycle so submissions can be managed fully BEFORE they
    start,
  * a single dispatcher thread with one lane per worker ("local" plus each
    online worker PC). The local lane runs at most one dispatched item at a
    time, which preserves every engine's existing single-flight invariant,
  * startup reaper: items stuck `running` after a crash go back to `queued`
    (attempts+1, capped) — same philosophy as jobs.enqueue_pending and
    _recover_stuck_shots, extended to everything else.

Engine adapters are registered at api startup via init_engines(); each
adapter knows how to execute one kind and how to poll its progress. `shot`
items are never dispatched here — the orchestrator keeps the chain order
(tail-of-previous-shot); queue rows for shots mirror status and carry the
per-shot worker assignment used by orchestrator.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core import ROOT, load_config, log_setup, now

log = log_setup("taskqueue")

DB_PATH = ROOT / "tool" / "queue.db"

STATUSES = ("draft", "queued", "running", "done", "failed", "canceled", "held")
KINDS = ("image", "video", "dataset", "shot", "refbatch")

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}\Z")


def assert_safe_identifier(name: str, allowed: frozenset[str] | set[str] | None = None) -> str:
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL column identifier: {name!r}")
    if allowed is not None and name not in allowed:
        raise ValueError(f"Disallowed SQL column identifier: {name!r}")
    return name


KNOWN_QUEUE_FIELDS = frozenset({
    "label", "worker", "status", "priority", "payload", "error",
    "finished", "engine_ref", "attempts", "updated", "progress_done",
    "progress_total", "prompt_id", "worker_job_id", "started", "lease",
    "max_attempts", "parent_id"
})

_lock = threading.Lock()
_dispatcher: threading.Thread | None = None
_dispatcher_stop = threading.Event()
_engines: dict[str, Any] = {}
_cfg: dict[str, Any] = {}
_initialized = False


# --------------------------------------------------------------------- db
def connect() -> sqlite3.Connection:
    """Fresh WAL connection per call — matches datasets_db's connection pattern
    over shared-handle approaches and keeps call sites lock-free.
    Self-healing: ensures the schema exists (idempotent), so any entry
    point works even before app startup ran init()."""
    global _initialized
    if not _initialized:
        init()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init() -> None:
    global _cfg, _initialized
    _cfg = (load_config().get("queue") or {})
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    try:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS queue_items (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '{}',
            worker TEXT NOT NULL DEFAULT 'local',
            status TEXT NOT NULL DEFAULT 'draft',
            priority INTEGER NOT NULL DEFAULT 5,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 2,
            prompt_id TEXT DEFAULT '',
            worker_job_id TEXT DEFAULT '',
            progress_done INTEGER NOT NULL DEFAULT 0,
            progress_total INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            parent_id TEXT DEFAULT '',
            engine_ref TEXT NOT NULL DEFAULT '',
            created TEXT NOT NULL,
            updated TEXT NOT NULL,
            started TEXT DEFAULT '',
            finished TEXT DEFAULT '',
            lease TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS ix_items_status ON queue_items(status);
        CREATE INDEX IF NOT EXISTS ix_items_parent ON queue_items(parent_id);
        CREATE TABLE IF NOT EXISTS workers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            host TEXT NOT NULL DEFAULT '',
            port INTEGER NOT NULL DEFAULT 8120,
            token_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'offline',
            gpu TEXT NOT NULL DEFAULT '{}',
            models TEXT NOT NULL DEFAULT '[]',
            capabilities TEXT NOT NULL DEFAULT '{}',
            agent_version TEXT NOT NULL DEFAULT '',
            created TEXT NOT NULL,
            last_heartbeat TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS shot_assignments (
            project_id TEXT NOT NULL,
            shot_id TEXT NOT NULL DEFAULT '',
            worker TEXT NOT NULL DEFAULT 'local',
            updated TEXT NOT NULL,
            PRIMARY KEY (project_id, shot_id)
        );
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS run_workers (
            run_id INTEGER PRIMARY KEY,
            worker_map TEXT NOT NULL DEFAULT '{}'
        );
        """)
    finally:
        c.close()
    _initialized = True
    log.info("queue.db ready at %s", DB_PATH)


# ----------------------------------------------------------------- helpers
def _row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["payload"] = json.loads(d.get("payload") or "{}")
    except Exception:
        d["payload"] = {}
    return d


def _touch(c: sqlite3.Connection, iid: str, **fields: Any) -> None:
    fields["updated"] = now()
    for k in fields:
        assert_safe_identifier(k, KNOWN_QUEUE_FIELDS)
    cols = ", ".join(f"{k}=?" for k in fields)
    c.execute(f"UPDATE queue_items SET {cols} WHERE id=?",
              (*fields.values(), iid))


# ------------------------------------------------------------ item CRUD
def add(kind: str, label: str, payload: dict | None = None, *,
        worker: str = "local", status: str | None = None,
        priority: int = 5, parent_id: str = "",
        max_attempts: int | None = None) -> str:
    """Create a queue item. status None honours queue.default_mode
    (direct → queued, hold → draft). Explicit status wins."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if status is None:
        status = "draft" if _cfg.get("default_mode", "direct") == "hold" \
            and kind != "shot" else "queued"
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    iid = uuid.uuid4().hex[:12]
    if kind == "shot":
        status = "queued"          # shots are chain-ordered, never drafted
    ts = now()
    with _lock, connect() as c:
        c.execute(
            "INSERT INTO queue_items (id, kind, label, payload, worker, status,"
            " priority, max_attempts, parent_id, created, updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (iid, kind, label, json.dumps(payload or {}, ensure_ascii=False),
             worker, status, int(priority),
             int(max_attempts or _cfg.get("max_attempts", 2)),
             parent_id, ts, ts))
    log.info("queued %s %s '%s' worker=%s", kind, iid, label, worker)
    return iid


def item(iid: str) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM queue_items WHERE id=?", (iid,)).fetchone()
    return _row_to_dict(r) if r else None


def items(status: str | None = None, kind: str | None = None,
          parent_id: str | None = None, limit: int = 500) -> list[dict]:
    q = "SELECT * FROM queue_items WHERE 1=1"
    args: list[Any] = []
    if status:
        q += " AND status=?"
        args.append(status)
    if kind:
        q += " AND kind=?"
        args.append(kind)
    if parent_id:
        q += " AND parent_id=?"
        args.append(parent_id)
    q += " ORDER BY priority ASC, created ASC LIMIT ?"
    args.append(int(limit))
    with connect() as c:
        rows = c.execute(q, args).fetchall()
    return [_row_to_dict(r) for r in rows]


def patch(iid: str, label: str | None = None, worker: str | None = None,
          priority: int | None = None, payload: dict | None = None) -> dict:
    """Edit an item. Payload edits only while draft/held/queued (never once
    running); worker re-routing allowed until running."""
    it = item(iid)
    if not it:
        raise LookupError(iid)
    if it["status"] in ("running", "done"):
        raise ValueError(f"cannot edit a {it['status']} item")
    fields: dict[str, Any] = {}
    if label is not None:
        fields["label"] = label
    if worker is not None:
        fields["worker"] = worker
    if priority is not None:
        fields["priority"] = int(priority)
    if payload is not None:
        if it["status"] == "queued":
            raise ValueError("payload is locked once queued — hold it first")
        fields["payload"] = json.dumps(payload, ensure_ascii=False)
    if fields:
        with _lock, connect() as c:
            _touch(c, iid, **fields)
    return item(iid)


def start(iid: str) -> dict:
    it = _require(iid, ("draft", "held", "canceled", "failed"))
    with _lock, connect() as c:
        _touch(c, iid, status="queued", error="")
    log.info("start %s (%s)", iid, it["kind"])
    return item(iid)


def hold(iid: str) -> dict:
    it = _require(iid, ("draft", "queued"))
    with _lock, connect() as c:
        _touch(c, iid, status="held")
    return item(iid)


def cancel(iid: str) -> dict:
    it = _require(iid, tuple(STATUSES))
    if it["status"] == "running":
        cancel_running(it)
        return item(iid)
    with _lock, connect() as c:
        _touch(c, iid, status="canceled", finished=now())
    return item(iid)


def retry(iid: str) -> dict:
    _require(iid, ("failed", "canceled"))
    with _lock, connect() as c:
        _touch(c, iid, status="queued", error="", finished="")
    return item(iid)


def remove(iid: str) -> dict:
    it = _require(iid, ("draft", "held", "queued", "canceled", "failed", "done"))
    if it["status"] == "running":
        raise ValueError("cannot remove a running item")
    with _lock, connect() as c:
        c.execute("DELETE FROM queue_items WHERE id=?", (iid,))
    return {"ok": True, "removed": iid}


def _require(iid: str, allowed: tuple[str, ...]) -> dict:
    it = item(iid)
    if not it:
        raise LookupError(iid)
    if it["status"] not in allowed:
        raise ValueError(f"item is {it['status']}; expected one of {allowed}")
    return it


def bump(iid: str, delta: int) -> dict:
    it = item(iid)
    if not it:
        raise LookupError(iid)
    with _lock, connect() as c:
        _touch(c, iid, priority=max(0, min(99, it["priority"] + delta)))
    return item(iid)


def cancel_running(it: dict) -> dict:
    """Best-effort cancel of a running item. On a worker PC this is safe —
    its ComfyUI is dedicated, so /interrupt only kills our job. On the local
    machine ComfyUI's /interrupt is GLOBAL (jobs.py:690-692 documents why we
    must not call it blindly), so we refuse and point at the engine's own
    Stop control, which is exactly what the UI does today."""
    lane = it.get("lease") or it.get("worker") or "local"
    if lane == "local":
        raise ValueError(
            "local item is inside the engine's own runner — use that "
            "engine's Stop control (global interrupt), then the queue "
            "picks up the failure")
    agent = _engines.get("worker_cancel")
    if agent:
        agent(it)
    with _lock, connect() as c:
        _touch(c, it["id"], status="canceled", finished=now(),
               error="canceled by user")
    return item(it["id"])


# ------------------------------------------------------------- hold-all
def hold_all(on: bool | None = None) -> dict:
    """Global freeze: no NEW dispatches; running items finish naturally."""
    with _lock, connect() as c:
        if on is None:
            r = c.execute("SELECT value FROM meta WHERE key='hold_all'").fetchone()
            return {"hold_all": (r["value"] == "1") if r else False}
        c.execute("INSERT INTO meta (key,value) VALUES ('hold_all',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  ("1" if on else "0",))
    return {"hold_all": bool(on)}


# --------------------------------------------------- shot assignments
def set_shot_worker(project_id: str, shot_id: str, worker: str) -> dict:
    """shot_id '' sets the project default. Orchestrator consults this
    per shot — chain order itself is never changed here."""
    if not project_id:
        raise ValueError("project_id required")
    with _lock, connect() as c:
        c.execute(
            "INSERT INTO shot_assignments (project_id, shot_id, worker, updated)"
            " VALUES (?,?,?,?) ON CONFLICT(project_id, shot_id)"
            " DO UPDATE SET worker=excluded.worker, updated=excluded.updated",
            (project_id, shot_id or "", worker, now()))
    return {"ok": True, "project_id": project_id,
            "shot_id": shot_id or "", "worker": worker}


def shot_worker(project_id: str, shot_id: str = "",
                default: str = "local") -> str:
    if not project_id:
        return default
    with connect() as c:
        # per-shot override first, then the project default (shot_id '')
        for sid in ((shot_id, "") if shot_id else ("",)):
            r = c.execute(
                "SELECT worker FROM shot_assignments WHERE project_id=? AND shot_id=?",
                (project_id, sid)).fetchone()
            if r:
                return r["worker"]
    return default


def clear_shot_worker(project_id: str, shot_id: str = "") -> dict:
    with _lock, connect() as c:
        c.execute("DELETE FROM shot_assignments WHERE project_id=? AND shot_id=?",
                  (project_id, shot_id or ""))
    return {"ok": True}


# --------------------------------------------------- dataset worker maps
def set_run_worker_map(run_id: int, worker_map: dict) -> None:
    """Per-image worker assignment for a dataset batch (jobs.py). Keyed by
    0-based plan-task index, with '*' as the run-level default. Persisted in
    queue.db (NOT datasets.db — schema stays untouched) so an interrupted
    run resumes with the same assignments."""
    with _lock, connect() as c:
        c.execute("INSERT INTO run_workers (run_id, worker_map) VALUES (?,?)"
                  " ON CONFLICT(run_id) DO UPDATE SET worker_map=excluded.worker_map",
                  (int(run_id), json.dumps(worker_map, ensure_ascii=False)))


def get_run_worker_map(run_id: int) -> dict:
    with connect() as c:
        r = c.execute("SELECT worker_map FROM run_workers WHERE run_id=?",
                      (int(run_id),)).fetchone()
    try:
        return json.loads(r["worker_map"]) if r else {}
    except Exception:
        return {}


# ------------------------------------------------------------- reaper
def reap_on_startup() -> dict:
    """Crash recovery: anything marked running when we died goes back to
    queued (attempts+1); past the cap it fails with a readable error.
    Mirrors jobs.enqueue_pending / _recover_stuck_shots semantics."""
    fixed = {"requeued": 0, "failed": 0}
    with _lock, connect() as c:
        rows = c.execute("SELECT * FROM queue_items WHERE status='running'").fetchall()
        ts = now()
        for r in rows:
            it = _row_to_dict(r)
            attempts = it["attempts"] + 1
            if attempts >= it["max_attempts"]:
                c.execute(
                    "UPDATE queue_items SET status='failed', attempts=?,"
                    " error=?, finished=?, lease='', updated=? WHERE id=?",
                    (attempts, "did not survive an app restart", ts, ts, it["id"]))
                fixed["failed"] += 1
            else:
                c.execute(
                    "UPDATE queue_items SET status='queued', attempts=?,"
                    " lease='', updated=? WHERE id=?", (attempts, ts, it["id"]))
                fixed["requeued"] += 1
    if fixed["requeued"] or fixed["failed"]:
        log.info("startup reaper: %s", fixed)
    return fixed


# ------------------------------------------------------------- summary
def summary() -> dict:
    with connect() as c:
        by_status = dict(c.execute(
            "SELECT status, COUNT(*) FROM queue_items GROUP BY status").fetchall())
        by_kind = dict(c.execute(
            "SELECT kind, COUNT(*) FROM queue_items WHERE status IN"
            " ('draft','held','queued','running') GROUP BY kind").fetchall())
    return {"by_status": by_status, "active_by_kind": by_kind,
            "hold_all": hold_all().get("hold_all", False)}


# ------------------------------------------------------------ dispatcher
def init_engines(**refs: Any) -> None:
    """api.py startup injects singletons + adapter callables here.
    Required keys see register_adapter()."""
    _engines.update(refs)


def register_adapter(kind: str, execute: Callable, status: Callable) -> None:
    """execute(item, lane) -> engine_ref (opaque handle string)
    status(engine_ref) -> {status: running|done|failed,
                           done?, total?, error?}"""
    _engines[f"exec:{kind}"] = execute
    _engines[f"stat:{kind}"] = status


def _adapters_ready() -> bool:
    return all(f"exec:{k}" in _engines for k in ("video", "image", "dataset"))


def _lanes() -> list[str]:
    lanes = ["local"]
    try:
        import workers as workers_mod
        workers_mod.reap_offline()       # cheap: flips silent workers offline
        for w in workers_mod.online_ids():
            if w not in lanes:
                lanes.append(w)
    except Exception as e:                      # workers.py optional early on
        log.debug("lane scan: %s", e)
    return lanes


def _lane_busy(c: sqlite3.Connection, lane: str) -> bool:
    r = c.execute("SELECT COUNT(*) FROM queue_items WHERE status='running'"
                  " AND lease=?", (lane,)).fetchone()
    return bool(r[0])


def _claim(c: sqlite3.Connection, lane: str) -> dict | None:
    """Atomically claim one queued item for a lane.

    The old SELECT-then-touch pattern was NOT atomic: two dispatcher threads
    (e.g. during a control-room restart overlap — which really happened) could
    both claim the same item and run it twice on different engines. This
    UPDATE..RETURNING makes claiming a single atomic step, so exactly one
    dispatcher ever owns an item.
    """
    row = c.execute(
        "UPDATE queue_items SET status='running', lease=?, started=?,"
        " updated=?, error='' WHERE id=("
        "  SELECT id FROM queue_items WHERE status='queued'"
        "  AND kind!='shot' AND (worker=? OR worker='any')"
        "  ORDER BY (worker=?) DESC, priority ASC, created ASC LIMIT 1)"
        " AND status='queued' RETURNING *",
        (lane, now(), now(), lane, lane)).fetchone()
    if row is None:
        return None
    c.commit()
    return _row_to_dict(row)


def _dispatch(lane: str, it: dict) -> None:
    """Run the engine's execute() for an already-claimed item, then pin the
    engine ref. Status was set to running atomically by _claim()."""
    exec_fn = _engines.get(f"exec:{it['kind']}")
    engine_ref = exec_fn(it, lane) or ""
    with _lock, connect() as c:
        _touch(c, it["id"], engine_ref=str(engine_ref))
    log.info("dispatched %s %s → %s", it["kind"], it["id"], lane)


def _poll_one(it: dict) -> None:
    stat_fn = _engines.get(f"stat:{it['kind']}")
    try:
        st = stat_fn(it.get("engine_ref") or "", it) or {}
    except Exception as e:
        st = {"status": "failed", "error": f"status poll failed: {e}"}
    s = st.get("status")
    if s == "running":
        with _lock, connect() as c:
            _touch(c, it["id"],
                   progress_done=int(st.get("done", it["progress_done"])),
                   progress_total=int(st.get("total", it["progress_total"])))
    elif s == "done":
        with _lock, connect() as c:
            _touch(c, it["id"], status="done", finished=now(),
                   progress_done=int(st.get("done", it["progress_done"])),
                   progress_total=int(st.get("total", it["progress_total"])))
        log.info("done %s (%s)", it["id"], it["kind"])
    elif s in ("failed", "canceled"):
        msg = st.get("error", s)
        _fail_or_requeue(it, msg, canceled=(s == "canceled"))


def _fail_or_requeue(it: dict, error: str, canceled: bool = False) -> None:
    attempts = it["attempts"] + 1
    ts = now()
    with _lock, connect() as c:
        if canceled:
            c.execute("UPDATE queue_items SET status='canceled', error=?,"
                      " finished=?, lease='', updated=? WHERE id=?",
                      (error, ts, ts, it["id"]))
        elif attempts >= it["max_attempts"]:
            c.execute("UPDATE queue_items SET status='failed', attempts=?,"
                      " error=?, finished=?, lease='', updated=? WHERE id=?",
                      (attempts, error, ts, ts, it["id"]))
        else:
            c.execute("UPDATE queue_items SET status='queued', attempts=?,"
                      " error=?, lease='', updated=? WHERE id=?",
                      (attempts, f"retry after: {error}", ts, it["id"]))
    log.warning("item %s → %s (%s)", it["id"],
                "canceled" if canceled else "retry/failed", error)


def _tick() -> None:
    """One dispatcher pass: poll running items, then fill idle lanes."""
    with connect() as c:
        running = [ _row_to_dict(r) for r in c.execute(
            "SELECT * FROM queue_items WHERE status='running'").fetchall()]
    for it in running:
        _poll_one(it)
    if _adapters_ready() and not hold_all().get("hold_all"):
        for lane in _lanes():
            with connect() as c:
                if _lane_busy(c, lane):
                    continue
                it = _claim(c, lane)
            if it:
                try:
                    _dispatch(lane, it)
                except Exception as e:
                    _fail_or_requeue(it, f"dispatch failed: {e}")


def _dispatcher_loop() -> None:
    interval = float(_cfg.get("dispatcher_interval_s", 2) or 2)
    log.info("dispatcher online (interval %.1fs)", interval)
    while not _dispatcher_stop.wait(interval):
        try:
            _tick()
        except Exception as e:
            log.error("dispatcher tick failed: %s", e)


def start_dispatcher() -> None:
    global _dispatcher
    if _dispatcher and _dispatcher.is_alive():
        return
    _dispatcher_stop.clear()
    _dispatcher = threading.Thread(target=_dispatcher_loop,
                                   name="queue-dispatcher", daemon=True)
    _dispatcher.start()


def stop_dispatcher() -> None:
    _dispatcher_stop.set()
