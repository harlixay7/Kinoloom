"""Video LoRA registry V2 + dynamic chain routing.

Scans trained (models/loras) and external (external_loras/VideoGeneration)
video LoRAs, tags categories/metadata, and resolves an ordered DYNAMIC LoRA
chain per shot with render profiles (Preview / Production Fast /
Effect-Quality). Additive to lora_registry.py — the legacy 7-slot path is
unchanged and keeps working."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from core import ROOT, jsave, load_config, log_setup

log = log_setup("lora_video")

CATEGORIES = ("Acceleration", "Character", "Environment", "Power", "Motion",
              "Camera", "VFX", "Anatomy", "Quality", "Style")

LORAS_DIR = ROOT / "models" / "loras"
EXTERNAL_DIR = Path(os.environ.get("EXTERNAL_LORA_DIR", str(ROOT / "external_loras" / "VideoGeneration")))
STORE = ROOT / "tool" / "video_lora_registry.json"

_BUNDLED = ("turbo", "fastwan", "4step")
_MANUAL = ("display", "trigger", "strength", "steps", "cfg", "enabled", "notes")
_CAM_EXTERNAL = ("dolly", "tracking", "pull_back", "slow_push_in")
_CLOSEUPS = ("close_up", "medium_close_up", "extreme_close_up")

KNOWN_EXTERNAL: dict[str, dict[str, Any]] = {
    "aether_punch": {
        "display": "Aether Punch", "category": "VFX", "mode": "i2v",
        "trigger": "aether punch", "strength": 0.7, "steps": 10, "cfg": 4.5,
        "preferred_shot": "impact/punch action", "duration": 5,
        "compat": ["wan22_5b"], "conflicts": ["turbo acceleration"],
    },
    "aether_blast": {
        "display": "Aether Blast", "category": "VFX", "mode": "both",
        "trigger": "aether blast", "strength": 0.7, "steps": 10, "cfg": 4.5,
        "preferred_shot": "explosion/blast", "duration": 5,
        "compat": ["wan22_5b"], "conflicts": ["turbo acceleration"],
    },
    "aether_crash_zoom": {
        "display": "Aether Crash Zoom", "category": "Camera", "mode": "both",
        "trigger": "crash zoom", "strength": 0.65, "steps": 8, "cfg": 4.5,
        "preferred_shot": "dolly/tracking/pull_back", "duration": 5,
        "compat": ["wan22_5b"], "conflicts": ["turbo acceleration"],
    },
    "aether_splash": {
        "display": "Aether Splash", "category": "VFX", "mode": "both",
        "trigger": "aether splash", "strength": 0.7, "steps": 10, "cfg": 4.5,
        "preferred_shot": "water/impact splash", "duration": 5,
        "compat": ["wan22_5b"], "conflicts": ["turbo acceleration"],
    },
    "zoom_art": {
        "display": "Zoom Art", "category": "Camera", "mode": "both",
        "trigger": "zoom art", "strength": 0.6, "steps": 8, "cfg": 4.5,
        "preferred_shot": "push_in/zoom", "duration": 5,
        "compat": ["wan22_5b"], "conflicts": ["turbo acceleration"],
    },
    "glowingeyes": {
        "display": "Glowing Eyes", "category": "VFX", "mode": "i2v",
        "trigger": "glowing eyes", "strength": 0.6, "steps": 8, "cfg": 4.5,
        "preferred_shot": "power close-ups", "duration": 5,
        "compat": ["wan22_5b"], "conflicts": ["turbo acceleration"],
    },
    "realistic_fire": {
        "display": "Realistic Fire", "category": "VFX", "mode": "both",
        "trigger": "realistic fire", "strength": 0.7, "steps": 10, "cfg": 4.5,
        "preferred_shot": "fire power", "duration": 5,
        "compat": ["wan22_5b"], "conflicts": ["turbo acceleration"],
    },
}


class VideoLoraRegistry:
    """Registry of video LoRAs with categories + metadata (registry v2)."""

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or load_config()
        self.path = STORE
        self._entries: dict[str, dict] = {}
        self._legacy: dict[str, dict] = {}
        lpath = ROOT / str((self.cfg.get("lora") or {}).get("registry", ""))
        if lpath.exists():
            try:
                self._legacy = json.loads(
                    lpath.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning(f"legacy LoRA registry load failed ({e})")
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._entries = json.loads(
                    self.path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning(f"video LoRA registry load failed ({e}) — "
                            f"starting empty")
                self._entries = {}

    def save(self) -> None:
        jsave(self.path, self._entries)

    # ------------------------------------------------------------- scanning
    @staticmethod
    def _family(name: str) -> str:
        nrm = name.replace("_", "").replace("-", "").replace(".", "")
        if "a14b" in nrm:
            return "wan22_a14b"
        if "wan22" in nrm or "wan2" in nrm:
            return "wan22_5b"
        if "ltx" in nrm:
            return "ltx"
        if "sdxl" in nrm or "xl" in nrm or "illustrious" in nrm \
                or "pony" in nrm:
            return "sdxl"
        # trained-pattern files (char_*/env_*/power_*/*_face) are video
        # LoRAs by construction
        low = name.lower()
        if low.startswith(("char_", "env_", "power_")) \
                or low.endswith("_face.safetensors"):
            return "wan22_5b"
        return "unknown"

    @staticmethod
    def _mode(name: str) -> str:
        if "i2v" in name:
            return "i2v"
        if "t2v" in name:
            return "t2v"
        return "both"

    def _hash(self, p: Path) -> str:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def _build(self, p: Path, source: str) -> dict:
        name = p.name
        low = name.lower()
        stem = p.stem
        entry: dict[str, Any] = {
            "id": stem, "file": name, "path": str(p), "sha256": None,
            "display": stem, "family": self._family(low), "mode": self._mode(low),
            "category": "Quality", "trigger": "", "strength": None,
            "steps": None, "cfg": None, "preferred_shot": "",
            "duration": None, "compat": [], "conflicts": [],
            "perf": {}, "enabled": True, "source": source,
            "status": "unverified",
        }
        lstem = stem.lower()
        if lstem.startswith("char_"):
            entry.update(category="Character", status="verified",
                         char_id=stem[5:])
        elif lstem.endswith("_face"):
            entry.update(category="Character", status="verified",
                         char_id=stem[:-5])
        elif lstem.startswith("env_"):
            entry.update(category="Environment", status="verified",
                         location=stem[4:])
        elif lstem.startswith("power_"):
            entry.update(category="Power", status="verified",
                         power_id=stem[6:])
        else:
            for key, meta in KNOWN_EXTERNAL.items():
                if key in low:
                    entry.update(meta)
                    entry["pattern"] = key
                    entry["mode"] = meta.get("mode", entry["mode"])
                    entry["display"] = meta["display"]
                    entry["category"] = meta["category"]
                    entry["status"] = "verified"
                    break
            else:
                if any(b in low for b in _BUNDLED):
                    entry.update(category="Acceleration", status="verified")
        # trained-registry metadata (roster/loras/registry.json) is
        # authoritative for ids (e.g. power_PowHoly1 -> power_id "Holy1")
        lm = self._legacy.get(name)
        if lm:
            t = lm.get("type")
            if t == "character" and lm.get("char_id"):
                entry.update(category="Character", status="verified",
                             char_id=lm["char_id"])
            elif t == "environment" and lm.get("location"):
                entry.update(category="Environment", status="verified",
                             location=lm["location"])
            elif t == "power" and lm.get("power_id"):
                entry.update(category="Power", status="verified",
                             power_id=lm["power_id"])
        st = p.stat()
        old = self._entries.get(name)
        if (old and old.get("sha256")
                and old.get("perf", {}).get("mtime") == st.st_mtime
                and old.get("perf", {}).get("size") == st.st_size):
            entry["sha256"] = old["sha256"]
        else:
            entry["sha256"] = self._hash(p)
        entry["perf"] = {"mtime": st.st_mtime, "size": st.st_size}
        return entry

    def scan(self) -> dict:
        found: dict[str, dict] = {}
        if LORAS_DIR.is_dir():
            for p in sorted(LORAS_DIR.glob("*.safetensors")):
                source = ("bundled"
                          if any(b in p.name.lower() for b in _BUNDLED)
                          else "trained")
                found[p.name] = self._build(p, source)
        if EXTERNAL_DIR.is_dir():
            for p in sorted(EXTERNAL_DIR.glob("*.safetensors")):
                found[p.name] = self._build(p, "external")
        else:
            log.warning(f"external LoRA dir missing, skipping: {EXTERNAL_DIR}")
        # merge: keep manual edits, refresh file existence, add new files
        for name, new in found.items():
            old = self._entries.get(name)
            if old:
                for f in _MANUAL:
                    if f in old:
                        new[f] = old[f]
                for k, v in old.items():
                    if k not in new:
                        new[k] = v
            self._entries[name] = new
        for name, old in list(self._entries.items()):
            if name not in found:
                old["status"] = "missing"
                old["enabled"] = False
        self.save()
        log.info(f"video LoRA scan: {len(found)} files found, "
                 f"{len(self._entries)} registry entries")
        return self.all()

    def rescan(self) -> dict:
        return self.scan()

    # -------------------------------------------------------------- access
    def all(self) -> dict:
        return dict(self._entries)

    def entries(self) -> list[dict]:
        return [self._entries[k] for k in sorted(self._entries)]

    def get(self, file_or_id: str) -> dict | None:
        for name, e in self._entries.items():
            if file_or_id == name or file_or_id == e.get("id"):
                return e
        return None

    def update(self, ident: str, **kw) -> bool:
        for name, e in self._entries.items():
            if ident == name or ident == e.get("id"):
                e.update(kw)
                self.save()
                return True
        return False


# ------------------------------------------------------------- routing+profiles
def pick_profile(shotspec: dict, chain: list[dict],
                 cfg: dict | None = None) -> str:
    """Render profile for a resolved chain."""
    if (shotspec or {}).get("render_profile") == "preview":
        return "preview"
    for e in chain or []:
        if not e.get("enabled", True):
            continue
        steps = e.get("steps")
        cfgv = e.get("cfg")
        if (steps is not None and steps > 6) or (cfgv is not None and cfgv > 5):
            return "effect_quality"
    return "production_fast"


def resolve_chain(shotspec: dict, bible: Any, registry: VideoLoraRegistry,
                  overrides: dict | None = None,
                  profile: str = "production_fast",
                  is_i2v: bool = True) -> list[dict]:
    """Ordered dynamic LoRA chain for one shot:
    [{name(file), strength, enabled, category, source, reason, steps, cfg}].
    Degrades gracefully with an empty registry / missing ids.

    Overrides from the project Bible win over registry detection:
    step 1 honors a character's "lora" entry; step 3 honors the owner
    character's power entry "lora" ({"file", "strength"}) — applied even
    when the file is not present in the registry (reason "power lora
    override")."""
    if registry is None:
        registry = VideoLoraRegistry()
    cfg = getattr(registry, "cfg", None) or load_config()
    lc = (cfg.get("lora") or {})
    chain: list[dict] = []

    def _video_ok(e: dict | None) -> bool:
        """Only video-family LoRAs may enter a Wan render chain. SDXL/
        Illustrious entries are for the image studio, not video."""
        if e is None:
            return False
        fam = e.get("family") or "unknown"
        if fam in ("wan22_5b", "wan21", "wan22_a14b", "ltx"):
            return True
        if fam == "sdxl":
            return False
        stem = (e.get("id") or "").lower()
        return bool(e.get("source") == "trained") and (
            stem.startswith(("char_", "env_", "power_"))
            or stem.endswith("_face"))

    def rget(name: str) -> dict | None:
        e = registry.get(name)
        return e if _video_ok(e) else None

    # bible character map — shared by the step 1 and step 3 overrides
    chars_map: dict[str, dict] = {}
    try:
        chars_map = bible.characters() if bible is not None else {}
    except Exception:
        chars_map = {}

    def find_by(category: str, field: str, value: Any) -> dict | None:
        for e in registry.entries():
            if (e.get("category") == category and e.get(field) == value
                    and e.get("enabled", True) and _video_ok(e)):
                return e
        return None

    def add(e: dict | None, strength: float, reason: str,
            source: str | None = None) -> None:
        chain.append({
            "id": e.get("id") if e else None,
            "name": e.get("file") if e else "",
            "strength": float(strength),
            "enabled": bool(e.get("enabled", True)) if e else True,
            "category": (e.get("category", "Quality") if e else "Quality"),
            "source": source or (e.get("source") if e else "unknown"),
            "reason": reason,
            "steps": e.get("steps") if e else None,
            "cfg": e.get("cfg") if e else None,
        })

    # 1. trained Character LoRA — first main/supporting character
    for c in (shotspec.get("characters") or []):
        if not isinstance(c, dict) or c.get("tier") not in ("main", "supporting"):
            continue
        cid = c.get("id")
        # project bible "lora" override wins over registry detection
        char_entry = chars_map.get(cid, {}) if cid else {}
        lora_ov = char_entry.get("lora")
        if isinstance(lora_ov, dict) and lora_ov.get("file"):
            fname = str(lora_ov["file"])
            strength = float(lora_ov.get("strength")
                             if lora_ov.get("strength") is not None
                             else lc.get("char_strength", 0.8))
            e = rget(fname)
            if e is not None:
                add(e, strength, "character lora override")
            else:
                chain.append({
                    "id": None, "name": fname, "strength": strength,
                    "enabled": True, "category": "Character",
                    "source": "override", "reason": "override (unscanned)",
                    "steps": None, "cfg": None,
                })
            break
        hit = None
        if cid:
            for cand in (f"char_{cid}.safetensors", f"{cid}_face.safetensors"):
                e = rget(cand)
                if e is not None:
                    hit = e
                    break
            if hit is None:
                hit = find_by("Character", "char_id", cid)
        if hit is not None:
            add(hit, lc.get("char_strength", 0.8), f"character {cid}")
        break

    # 2. trained Environment LoRA — location bible "lora" override wins
    loc = shotspec.get("location")
    if loc:
        loc_entry = (bible.locations() if bible is not None else {}).get(
            loc, {}) or {}
        lora_ov = loc_entry.get("lora")
        if isinstance(lora_ov, dict) and lora_ov.get("file"):
            fname = str(lora_ov["file"])
            strength = float(lora_ov.get("strength")
                             if lora_ov.get("strength") is not None
                             else lc.get("env_strength", 0.6))
            e = rget(fname)
            if e is not None:
                add(e, strength, "environment lora override")
            else:
                chain.append({
                    "id": None, "name": fname, "strength": strength,
                    "enabled": True, "category": "Environment",
                    "source": "override", "reason": "environment lora override",
                    "steps": None, "cfg": None,
                })
        else:
            e = rget(f"env_{loc}.safetensors")
            if e is None:
                e = find_by("Environment", "location", loc)
            if e is not None:
                add(e, lc.get("env_strength", 0.6), f"environment {loc}")

    # 3. trained Power LoRA
    pid = shotspec.get("power")
    if pid:
        # the OWNER character's bible power entry may carry a "lora"
        # override — applied before (and regardless of) the registry
        # lookup, even when the file is not in the registry
        power_lora_ov: dict | None = None
        for c in (shotspec.get("characters") or []):
            if not isinstance(c, dict):
                continue
            char_entry = chars_map.get(c.get("id"), {}) if c.get("id") else {}
            powers = char_entry.get("powers") or {}
            pentry = powers.get(pid) if isinstance(powers, dict) else None
            if isinstance(pentry, dict) and isinstance(pentry.get("lora"), dict):
                power_lora_ov = pentry["lora"]
                break
        if power_lora_ov is not None and power_lora_ov.get("file"):
            fname = str(power_lora_ov["file"])
            strength = float(power_lora_ov.get("strength")
                             if power_lora_ov.get("strength") is not None
                             else lc.get("power_strength", 0.7))
            e = rget(fname)
            if e is not None:
                add(e, strength, "power lora override")
            else:
                chain.append({
                    "id": None, "name": fname, "strength": strength,
                    "enabled": True, "category": "Power",
                    "source": "override", "reason": "power lora override",
                    "steps": None, "cfg": None,
                })
        else:
            e = rget(f"power_{pid}.safetensors")
            if e is None:
                e = find_by("Power", "power_id", pid)
            if e is not None:
                add(e, lc.get("power_strength", 0.7), f"power {pid}")

    # 4. external effect/camera LoRAs (enabled registry entries, max 2)
    camera = (shotspec.get("camera") or "").lower()
    power = str(shotspec.get("power") or "").lower()
    action = (shotspec.get("primary_action") or "").lower()
    prompt = (shotspec.get("prompt") or "").lower()
    size = (shotspec.get("shot_size") or "").lower()
    text = " ".join(x for x in (action, prompt) if x)

    def ext_entry(pattern: str) -> dict | None:
        for e in registry.entries():
            if e.get("pattern") == pattern and e.get("enabled", True) \
                    and _video_ok(e):
                return e
        return None

    picked: list[tuple[dict, str]] = []
    if camera in _CAM_EXTERNAL:
        e = ext_entry("aether_crash_zoom") or ext_entry("zoom_art")
        if e is not None:
            picked.append((e, f"camera {shotspec.get('camera')}"))
    if power == "fire":
        e = ext_entry("realistic_fire")
        if e is not None:
            picked.append((e, "fire power"))
    if power == "water" or "water" in text or "splash" in text:
        e = ext_entry("aether_splash")
        if e is not None:
            picked.append((e, "water/splash"))
    if "blast" in text or "explosion" in text:
        e = ext_entry("aether_blast")
        if e is not None:
            picked.append((e, "blast/explosion"))
    if is_i2v and ("punch" in action or "impact" in action):
        e = ext_entry("aether_punch")
        if e is not None:
            picked.append((e, "punch/impact action"))
    if size in _CLOSEUPS:
        e = ext_entry("glowingeyes")
        if e is not None:
            picked.append((e, "power close-up"))
    for e, reason in picked[:2]:
        strength = e.get("strength")
        add(e, strength if strength is not None else 0.7, reason)

    # 5. trained Quality LoRA — last trained slot
    if lc.get("quality", True):
        e = rget("quality_filmic.safetensors")
        if e is not None and e.get("enabled", True):
            add(e, lc.get("quality_strength", 0.6), "quality (settings)")

    # 6. turbo acceleration — dropped under the effect profile
    accel = [e for e in registry.entries()
             if e.get("category") == "Acceleration" and e.get("enabled", True)
             and _video_ok(e)]
    turbo = next((e for e in accel if "turbo" in e["file"].lower()), None)
    if turbo is None and accel:
        turbo = accel[0]
    if turbo is not None:
        if profile != "effect_quality":
            add(turbo, 1.0, "turbo acceleration")
        else:
            chain.append({
                "id": turbo.get("id"), "name": turbo["file"],
                "strength": 1.0, "enabled": False,
                "category": "Acceleration",
                "source": turbo.get("source", "bundled"),
                "reason": "dropped: effect profile",
                "steps": turbo.get("steps"), "cfg": turbo.get("cfg"),
            })

    # overrides (shotspec.selected_loras) — applied last
    if overrides is None:
        overrides = {}
        for l in (shotspec.get("selected_loras") or []):
            if isinstance(l, dict) and l.get("name"):
                overrides[l["name"]] = {"enabled": True,
                                        "strength": l.get("strength")}
    for name, ov in (overrides or {}).items():
        if not isinstance(ov, dict):
            continue
        hit = next((e for e in chain
                    if e.get("name") == name or e.get("id") == name), None)
        if ov.get("enabled") is False:
            if hit is not None:
                chain.remove(hit)
            continue
        if hit is not None:
            if ov.get("strength") is not None:
                hit["strength"] = float(ov["strength"])
            hit["enabled"] = True
            if "override" not in hit["reason"]:
                hit["reason"] += " (override)"
        else:
            e = rget(name)
            chain.append({
                "id": e.get("id") if e else None,
                "name": e.get("file") if e else name,
                "strength": float(
                    ov.get("strength")
                    if ov.get("strength") is not None
                    else (e.get("strength")
                          if e and e.get("strength") is not None else 0.7)),
                "enabled": True,
                "category": (e.get("category", "Style") if e else "Style"),
                "source": "override", "reason": "selected_loras",
                "steps": e.get("steps") if e else None,
                "cfg": e.get("cfg") if e else None,
            })

    # dedupe by file, cap at 6 enabled entries
    seen: set[str] = set()
    out: list[dict] = []
    for e in chain:
        key = e.get("name")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(e)
    chain = out
    while len([e for e in chain if e.get("enabled", True)]) > 6:
        q = next((i for i, e in enumerate(chain)
                  if e.get("enabled", True)
                  and e.get("category") == "Quality"), None)
        if q is not None:
            chain.pop(q)
            continue
        ext = [(i, e) for i, e in enumerate(chain)
               if e.get("enabled", True) and e.get("source") == "external"]
        if ext:
            chain.pop(min(ext, key=lambda t: t[1].get("strength") or 0)[0])
            continue
        break
    return chain


_EFFECT_CATEGORIES = ("VFX", "Camera", "Motion")
ADJUST_LORA_STAGE = 3       # qa_stage after the ladder's "adjust_lora" step


def soften_effect_loras(chain: list[dict], factor: float = 0.5) -> list[dict]:
    """QA retry ladder step "adjust_lora": scale the
    effect/camera LoRAs (the usual source of blown-out tensors and edge
    artifacts) by `factor`; identity, environment, quality and acceleration
    LoRAs are left alone. Mutates and returns `chain`."""
    for e in chain or []:
        if e.get("enabled", True) and e.get("category") in _EFFECT_CATEGORIES:
            e["strength"] = round(float(e.get("strength") or 0.0) * factor, 3)
            e["reason"] = f"{e.get('reason', '')} (QA: halved)".strip()
    return chain


def chain_to_extra(chain: list[dict]) -> list[dict]:
    """[{name, strength}] for enabled LoRAs, in chain order."""
    return [{"name": e["name"], "strength": float(e["strength"])}
            for e in (chain or [])
            if e.get("enabled", True) and e.get("name")]
