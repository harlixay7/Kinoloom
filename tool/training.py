"""Training backend. Manages datasets, runs DB,
trainer subprocess execution (ai-toolkit / kohya) with progress parsing
(step/it_s/ETA/loss), post-run validation, registry updates, and checkpoint
resume support."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from core import ROOT, load_config, log_setup, write_text, jload, jsave, \
    is_safe_component
from lora_registry import LoRARegistry

log = log_setup("training")

RUNS_DB = ROOT / "tool" / "training_runs.json"   # simple JSON store (crash-safe)
DATASETS_DIR = ROOT / "roster" / "datasets"

VALID_TYPES = ("face", "power", "environment", "weapon", "outfit", "anatomy", "quality")
# dataset type -> (LoRA file prefix, registry type); one row per VALID_TYPES
_LORA_NAMING = {
    "face": ("char", "character"),
    "power": ("power", "power"),
    "environment": ("env", "environment"),
    "weapon": ("weapon", "weapon"),
    "outfit": ("outfit", "outfit"),
    "anatomy": ("anatomy", "anatomy"),
    "quality": ("quality", "quality"),
}

_runs_lock = threading.Lock()   # guards _load_runs()/_save_runs() pairs


# ------------------------------------------------------------- datasets
def dataset_dirs() -> list[dict]:
    import datasets_db as ddb
    out = []
    if not DATASETS_DIR.exists():
        return out
    for t in VALID_TYPES:
        td = DATASETS_DIR / t
        if not td.exists():
            continue
        for d in sorted(td.iterdir()):
            if not d.is_dir():
                continue
            candidates = (sorted(d.glob("*.png")) + sorted(d.glob("*.jpg"))
                          + sorted((d / "frames").glob("*.png")) + sorted((d / "stills").glob("*.png"))
                          + sorted((d / "frames").glob("*.jpg")) + sorted((d / "stills").glob("*.jpg")))
            curation = d / "curation.json"
            state = jload(curation) if curation.exists() else {}
            # verdicts live under _auto in the current format (legacy flat
            # files keyed directly) — iterate the actual verdict map
            cv = state.get("_auto") if isinstance(state, dict) else None
            if not isinstance(cv, dict):
                cv = state if isinstance(state, dict) else {}
            kept = sum(1 for v in cv.values()
                       if isinstance(v, dict) and v.get("keep"))
            rejected = sum(1 for v in cv.values()
                           if isinstance(v, dict) and v.get("keep") is False)
            names = []
            for c in candidates:
                names.append(c.relative_to(d).as_posix())
            meta = {}
            mp = d / "meta.json"
            if mp.exists():
                try:
                    meta = jload(mp)
                except Exception:
                    meta = {}
            last = ""
            mtimes = [c.stat().st_mtime for c in candidates]
            if mtimes:
                import time as _t
                last = _t.strftime("%Y-%m-%d %H:%M",
                                   _t.localtime(max(mtimes)))
            ddb.backfill_dataset(t, d.name)
            # per-file source map (generated/imported) for curation filters
            sources: dict[str, str] = {}
            try:
                for row in ddb.list_images(t, d.name, limit=200000):
                    sources[row["file"]] = row.get("source") or "generated"
            except Exception:
                pass
            # generation status: any queued/active job for this dataset
            gen_status = ""
            import sqlite3 as _sql
            try:
                con = _sql.connect(str(ROOT / "tool" / "datasets.db"))
                r = con.execute(
                    "SELECT status, done, total FROM gen_runs WHERE "
                    "dataset_type=? AND dataset_name=? AND status IN "
                    "('queued','active') ORDER BY id DESC LIMIT 1",
                    (t, d.name)).fetchone()
                if r:
                    gen_status = f"{r[0]} {r[1]}/{r[2]}"
                con.close()
            except Exception:
                pass
            _ref_list: list[str] = []
            _rd = d / "refs"
            if _rd.is_dir():
                _ref_list = [f"refs/{f.name}" for f in sorted(_rd.iterdir())
                             if f.suffix.lower()
                             in (".png", ".jpg", ".jpeg")]
            # per-candidate auto-filter verdicts from curation.json._auto
            # (relative posix keys; legacy files used absolute keys — accept
            # both). The web UI renders these as curation state.
            curation_state: dict[str, dict] = {}
            auto = state.get("_auto") if isinstance(state, dict) else {}
            if not isinstance(auto, dict):
                auto = state if isinstance(state, dict) else {}
            for c in candidates:
                rel = c.relative_to(d).as_posix()
                v = auto.get(rel)
                if v is None:
                    v = auto.get(str(c))
                if v is None:
                    v = auto.get(c.as_posix())
                if isinstance(v, dict):
                    curation_state[rel] = v
            out.append({
                "type": t, "name": d.name, "path": str(d),
                "candidates": names, "kept": kept, "rejected": rejected,
                "caption": state.get("_caption", ""),
                "license": state.get("_license", ""),
                "filtered": state.get("_filtered", []),
                "models": ddb.model_distribution(t, d.name),
                "sources": sources,
                "refs": _ref_list,
                "meta": {k: v for k, v in meta.items()
                         if k in ("_params", "_emotions", "_angles")},
                "last_modified": last,
                "generation_status": gen_status,
                "ready": kept >= 5,
                "thumbnail": (candidates[0].relative_to(d).as_posix()
                              if candidates else ""),
                "curationState": curation_state,
            })
    return out


def curate_save(dataset: Path, state: dict) -> None:
    jsave(dataset / "curation.json", state)


# --------------------------------------------------------------- runs
def _load_runs() -> dict:
    """Runs keyed by run id. Anything else on disk (the store once shipped
    as an empty JSON list) reads as no runs instead of breaking callers."""
    if RUNS_DB.exists():
        try:
            data = jload(RUNS_DB)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_runs(runs: dict) -> None:
    jsave(RUNS_DB, runs)


def _safetensors_step(p: Path) -> int:
    """Numeric training step embedded in a step-save filename, so
    `step_1000` sorts AFTER `step_900` (lexicographic sort gets this
    wrong). Non-step files (final output) sort first."""
    m = re.search(r"step[_-](\d+)", p.stem)
    if m:
        return int(m.group(1))
    nums = re.findall(r"\d+", p.stem)
    return int(nums[-1]) if nums else -1


class TrainingRunner:
    """Launches trainer subprocesses (ai-toolkit / kohya), tails logs for metrics,
    supports checkpoint resumption, and records run validation states."""

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or load_config()
        self.proc: subprocess.Popen | None = None
        self.run_id: str | None = None
        self._log_pos = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ config
    def _prepare_training_set(self, t: str, dataset: Path, run: dict) -> Path:
        """Stage a clean FLAT training set (ai-toolkit layout): ONLY
        curated-kept images + a real .txt caption per image. Rejected
        images never reach the trainer; captions carry the identity block
        + emotion/angle from meta.json."""
        import shutil as sh
        from dataset import captions_bible
        out = ROOT / "temp" / "trainsets" / f"{t}_{dataset.name}"
        if out.exists():
            sh.rmtree(out)
        out.mkdir(parents=True, exist_ok=True)
        state = jload(dataset / "curation.json") \
            if (dataset / "curation.json").exists() else {}
        auto = state.get("_auto") if isinstance(state, dict) else None
        if not isinstance(auto, dict):
            auto = state   # legacy flat curation files key directly
        meta = jload(dataset / "meta.json") \
            if (dataset / "meta.json").exists() else {}
        cands: list[Path] = []
        for sdir in ("", "stills", "frames"):
            base = dataset if not sdir else dataset / sdir
            for ext in ("*.png", "*.jpg", "*.jpeg"):
                cands += sorted(base.glob(ext))
        def kept_rel(c: Path) -> bool:
            # new curation files key _auto by relative posix path; legacy
            # ones used absolute paths — accept both forms
            rel = c.relative_to(dataset).as_posix()
            v = auto.get(rel)
            if v is None:
                v = auto.get(str(c))
            if v is None:
                v = auto.get(c.as_posix())
            return v is None or (isinstance(v, dict) and v.get("keep"))
        kept = [c for c in cands if kept_rel(c)]
        if not kept:
            kept = cands  # nothing curated yet -> use everything
        params = meta.get("_params") or {}
        id_name = run.get("id_name") or dataset.name
        cap_override = state.get("_caption", "")
        auto = captions_bible(t, dataset.name,
                              char_id=id_name if t == "face" else "",
                              power_id=id_name if t == "power" else "",
                              location=id_name if t == "environment" else "")
        for c in kept:
            dst = out / f"{c.stem}.png"
            sh.copy2(c, dst)
            if cap_override:
                txt = cap_override
            elif auto.get(str(c)):
                txt = auto[str(c)]
            else:
                m = meta.get(c.name) or {}
                if t == "face":
                    from dataset_gen import identity_block, EMOTION_LOOKUP
                    var = m.get("variations") or {} \
                        if isinstance(m, dict) else {}
                    emotion = m.get("emotion") or var.get("emotion")
                    angle = m.get("angle") or var.get("angle")
                    txt = (f"portrait of {id_name}, {identity_block(params)}"
                           + (f", {emotion} expression" if emotion else "")
                           + (f", {angle} view" if angle else ""))
                elif t == "power":
                    from dataset_gen import power_caption
                    txt = power_caption(params, m)
                elif t == "environment":
                    from dataset_gen import env_caption
                    txt = env_caption(params, m)
                else:
                    txt = dataset.name.replace("_", " ")
            (out / f"{c.stem}.txt").write_text(txt, encoding="utf-8")
        log.info(f"training set staged: {out} ({len(kept)} images)")
        return out

    # --------------------------------------------------- ai-toolkit engine
    def _aitk_cmd(self, run: dict, staged: Path, out_name: str) -> list[str]:
        """Write an ai-toolkit config YAML + return the launch command.
        ai-toolkit is the standard LoRA trainer. Output lands in aitk_out/{name}/{name}.safetensors
        in comfy-format keys, which ComfyUI loads directly."""
        import yaml
        tr = self.cfg["training"]
        t = run["type"]
        steps = int(run.get("steps") or tr["steps_presets"].get(t, 4000))
        rank = int(run.get("rank") or tr["rank_presets"].get(t, 24))
        lr = float(run.get("lr") or tr["lr_presets"].get(t, 1e-4))
        cfg = {
            "job": "extension",
            "config": {
                "name": out_name,
                "process": [{
                    "type": "sd_trainer",
                    "training_folder": str(ROOT / tr["aitk_out"]),
                    "performance_log_every": max(5, steps // 40),
                    "device": "cuda:0",
                    "network": {"type": "lora",
                                "linear": rank, "linear_alpha": max(1, rank // 2)},
                    "save": {"dtype": "float16",
                             "save_every": max(25, steps // 6),
                             "max_step_saves_to_keep": 2,
                             "push_to_hub": False},
                    "datasets": [{
                        "folder_path": str(staged),
                        "caption_ext": "txt",
                        "caption_dropout_rate": 0.05,
                        "shuffle_tokens": False,
                        "cache_latents_to_disk": True,
                        "resolution": [512],
                        "num_frames": 1,
                    }],
                    "train": {
                        "batch_size": 1,
                        "steps": steps,
                        "gradient_accumulation": 1,
                        "train_unet": True,
                        "train_text_encoder": False,
                        "gradient_checkpointing": True,
                        "noise_scheduler": "flowmatch",
                        "timestep_type": "linear",
                        "optimizer": "adamw8bit",
                        "lr": lr,
                        "optimizer_params": {"weight_decay": 1e-4},
                        "disable_sampling": True,
                        "dtype": "fp16",
                        "cache_text_embeddings": True,
                    },
                    "model": {
                        "name_or_path": str(ROOT / tr["wan22_diffusers"]),
                        "arch": "wan21",
                        "layer_offloading": True,
                        "layer_offloading_transformer_percent": 0.7,
                        "low_vram": True,
                    },
                }],
            },
            "meta": {"name": "[name]", "version": "1.0"},
        }
        cfg_path = ROOT / "temp" / f"train_{out_name}.yaml"
        cfg_path.write_text(yaml.dump(cfg, default_flow_style=False),
                            encoding="utf-8")
        return [
            str(ROOT / tr["aitk_python"]),
            str(ROOT / tr["ai_toolkit_dir"] / "run.py"),
            str(cfg_path),
            "-l", str(ROOT / "logs" / f"aitk_{out_name}.log"),
        ]

    # ------------------------------------------------------------- start
    def start(self, run: dict) -> dict:
        t = run.get("type")
        if t not in VALID_TYPES:
            return {"ok": False, "error": f"type must be one of {VALID_TYPES}"}
        dataset = DATASETS_DIR / t / run.get("dataset", "")
        if not dataset.is_dir():
            return {"ok": False, "error": f"dataset not found: {dataset}"}
        if self.proc and self.proc.poll() is None:
            return {"ok": False, "error": "a training run is already active"}
        run_id = f"{t}_{run.get('dataset', 'x')}_{int(time.time())}" \
                 f"_{uuid.uuid4().hex[:6]}"
        try:
            out_name = self._out_name(t, run)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        tr = self.cfg["training"]
        resolved = {
            "steps": run.get("steps") or tr["steps_presets"].get(t, 4000),
            "rank": run.get("rank") or tr["rank_presets"].get(t, 24),
            "lr": run.get("lr") or tr["lr_presets"].get(t, 1e-4),
        }
        run = {**run, **resolved}
        try:
            staged = self._prepare_training_set(t, dataset, run)
        except Exception as e:
            log.exception("staging failed")
            return {"ok": False, "error": f"staging failed: {e}"}
        cmd = self._aitk_cmd(run, staged, out_name)
        logfile = ROOT / "logs" / f"train_{run_id}.log"
        if logfile.exists():
            logfile.unlink()
        lf = open(logfile, "a", encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                    "HF_HUB_DISABLE_SYMLINKS_WARNING": "1"})
        self.proc = subprocess.Popen(
            cmd, stdout=lf, stderr=subprocess.STDOUT,
            cwd=str(ROOT / "tools" / "ai-toolkit"),
            env=env)
        lf.close()   # child owns the handle now; never leak the parent fd
        self.run_id = run_id
        self._log_pos = 0
        with _runs_lock:
            runs = _load_runs()
            runs[run_id] = {
                "id": run_id, "type": t, "dataset": run.get("dataset"),
                "steps": run.get("steps"), "rank": run.get("rank"),
                "lr": run.get("lr"), "out_name": out_name,
                "status": "running", "started": time.time(),
                "log": str(logfile), "verdict": None,
            }
            _save_runs(runs)
        log.info(f"training started: {run_id}")
        return {"ok": True, "run_id": run_id}

    def _out_name(self, t: str, run: dict) -> str:
        """Registry-compatible naming."""
        prefix = _LORA_NAMING[t][0]
        ident = run.get("id_name") or run.get("dataset", "x")
        if run.get("id_name") and not is_safe_component(ident):
            raise ValueError(f"unsafe id_name {ident!r}")
        return f"{prefix}_{ident}"

    # ----------------------------------------------------------- progress
    def status(self) -> dict:
        with _runs_lock:
            runs = _load_runs()
            for rid, r in list(runs.items()):
                if r["status"] != "running":
                    continue
                self._update_from_log(rid, r, runs)
            if self.proc is not None:
                code = self.proc.poll()
                if code is not None:
                    self.proc = None
                    for rid, r in list(runs.items()):
                        if r.get("status") == "running":
                            r["status"] = "error" if code != 0 else "completed"
                            r["ended"] = time.time()
                            if code == 0:
                                self._on_complete(r)
                    _save_runs(runs)
            elif any(r.get("status") == "running" for r in runs.values()):
                # no live process but the DB says running -> the server
                # restarted mid-run; mark interrupted (terminal)
                for rid, r in list(runs.items()):
                    if r.get("status") == "running":
                        r["status"] = "interrupted"
                        r["ended"] = time.time()
                        r["error"] = "server restarted — run interrupted"
                _save_runs(runs)
        return runs

    def _update_from_log(self, rid: str, r: dict, runs: dict) -> None:
        try:
            path = Path(r["log"])
            with open(path, encoding="utf-8", errors="replace") as f:
                f.seek(self._log_pos)
                text = f.read()
                self._log_pos = f.tell()
            # ai-toolkit tqdm line:
            #   smoke_wan22:  75%|...| 15/20 [01:07<00:17, 3.48s/it, lr: 1.0e-04 loss: 2.369e-01]
            m = re.findall(
                r"\|[^\[]*?\|\s*(\d+)/(\d+)\s*\[[^,]*,\s*"
                r"([\d.]+)(s/it|it/s),.*?loss:\s*([\d.eE+-]+)",
                text)
            if m:
                step, total, rate, unit, loss = m[-1]
                # tqdm switches to "it/s" once a step takes under a second
                rate_f = float(rate)
                if unit == "it/s":
                    s_per_it = 1.0 / rate_f if rate_f > 0 else 0.0
                else:
                    s_per_it = rate_f
                r["step"] = int(step)
                r["total_steps"] = int(total)
                r["loss"] = float(loss)
                r["it_s"] = 1.0 / s_per_it if s_per_it > 0 else 0
                r["eta_s"] = max(0, (int(total) - int(step))) * s_per_it
            _save_runs(runs)
        except Exception as e:
            log.warning(f"log parse failed: {e}")

    # ---------------------------------------------------------- complete
    def _on_complete(self, r: dict) -> None:
        """Copy the trained LoRA into models/loras, auto-validate it loads
        in ComfyUI, and write the registry entry."""
        import shutil as sh
        src = ROOT / self.cfg["training"]["aitk_out"] / \
            r["out_name"] / f"{r['out_name']}.safetensors"
        out = ROOT / "models" / "loras" / f"{r['out_name']}.safetensors"
        try:
            if src.exists():
                sh.copy2(src, out)
            else:
                # fall back to the newest step-save in the run folder
                folder = ROOT / self.cfg["training"]["aitk_out"] / r["out_name"]
                saves = sorted(folder.glob("*.safetensors"),
                               key=_safetensors_step)
                if saves:
                    sh.copy2(saves[-1], out)
        except Exception as e:
            r["status"] = "error"
            r["error"] = f"copy failed: {e}"
            return
        if not out.exists():
            r["status"] = "error"
            r["error"] = "output .safetensors missing"
            return
        meta = {
            "type": _LORA_NAMING[r["type"]][1],
            "status": "validating",
            "steps": r.get("steps"), "trained_at": time.time(),
        }
        if r["type"] == "face":
            meta["char_id"] = r.get("id_name") or r.get("dataset")
        elif r["type"] == "power":
            meta["power_id"] = r.get("id_name") or r.get("dataset")
        elif r["type"] == "environment":
            meta["location"] = r.get("id_name") or r.get("dataset")
        ok = self._validate_load(r["out_name"])
        meta["status"] = "ready" if ok else "error"
        if not ok:
            meta["error"] = "ComfyUI load validation failed"
        LoRARegistry(self.cfg).register(r["out_name"] + ".safetensors", meta)
        r["validate_ok"] = ok

    def _validate_load(self, lora_name: str) -> bool:
        """submit+interrupt a tiny workflow using the LoRA — no render."""
        try:
            from renderer import ComfyClient
            from workflows import WorkflowBuilder
            b = WorkflowBuilder()
            wf = b.build("wan2.2_t2v_5b", prompt="t", negative="n", seed=1,
                         width=512, height=288, steps=1, frames=25,
                         cfg=self.cfg,
                         extra={"SHOT_ID": "valtrain",
                                "CHAR_LORA": lora_name + ".safetensors",
                                **{k: 0.8 for k in
                                   ("CHAR_STR", "POWER_STR", "ENV_STR",
                                    "ANATOMY_STR", "QUALITY_STR",
                                    "STYLE_STR")}})
            c = ComfyClient(self.cfg["ensemble"]["comfyui_port"])
            pid = c.queue_prompt(wf)
            c.interrupt()
            return pid is not None
        except Exception as e:
            log.warning(f"validate_load failed: {e}")
            return False

    # -------------------------------------------------------------- stop
    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            with _runs_lock:
                runs = _load_runs()
                for rid, r in list(runs.items()):
                    if r.get("status") == "running":
                        r["status"] = "stopped"
                        r["ended"] = time.time()
                _save_runs(runs)
            self.proc.terminate()
            time.sleep(2)
            if self.proc.poll() is None:
                self.proc.kill()
