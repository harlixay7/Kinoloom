"""LoRA registry — the extensibility contract.
Entries resolve which trained LoRAs apply to which shots, by type:
character / power / environment / anatomy / quality / style.
Adding a character or power = dataset + train + registry entry — done."""
from __future__ import annotations

import json
from pathlib import Path

from core import ROOT, load_config, log_setup, write_text

log = log_setup("lora_registry")


class LoRARegistry:
    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or load_config()
        self.path = ROOT / self.cfg["lora"]["registry"]
        self._entries: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._entries = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning(f"registry load failed ({e}) — starting empty")
                self._entries = {}

    def _save(self) -> None:
        write_text(self.path, json.dumps(self._entries, indent=2))

    # ------------------------------------------------------------- entries
    def register(self, filename: str, meta: dict) -> None:
        """meta: {type, char_id?, power_id?, location?, movie?, status,
        steps?, verdict?, trained_at?}"""
        meta.setdefault("status", "ready")
        self._entries[filename] = meta
        self._save()
        log.info(f"registry: {filename} -> {meta}")

    def update(self, filename: str, **kw) -> None:
        if filename in self._entries:
            self._entries[filename].update(kw)
            self._save()

    def all(self) -> dict:
        return dict(self._entries)

    def by_type(self, t: str) -> dict[str, dict]:
        return {k: v for k, v in self._entries.items()
                if v.get("type") == t and v.get("status") == "ready"}

    # ---------------------------------------------------------- resolution
    def resolve(self, shot: dict, movie: str = "") -> dict:
        """Which LoRA filenames apply to this shot ('' = none)."""
        lc = self.cfg["lora"]
        out: dict[str, str] = {
            "CHAR_LORA": "none", "POWER_LORA": "none", "ENV_LORA": "none",
            "ANATOMY_LORA": "none", "QUALITY_LORA": "none", "STYLE_LORA": "none",
        }
        char_map = {v.get("char_id"): k for k, v in
                    self.by_type("character").items() if v.get("char_id")}
        for c in shot.get("characters", []):
            if c["id"] in char_map:
                out["CHAR_LORA"] = char_map[c["id"]]
                break
        power_map = {v.get("power_id"): k for k, v in
                     self.by_type("power").items() if v.get("power_id")}
        pwr = shot.get("power")
        if pwr and pwr in power_map:
            out["POWER_LORA"] = power_map[pwr]
        env_map = {v.get("location"): k for k, v in
                   self.by_type("environment").items() if v.get("location")}
        loc = shot.get("location")
        if loc and loc in env_map:
            out["ENV_LORA"] = env_map[loc]
        if lc.get("anatomy"):
            a = self.by_type("anatomy")
            if a:
                out["ANATOMY_LORA"] = next(iter(a))
        if lc.get("quality"):
            q = self.by_type("quality")
            if q:
                out["QUALITY_LORA"] = next(iter(q))
        if lc.get("style") and lc.get("style_lora"):
            out["STYLE_LORA"] = lc["style_lora"]
        return out

    # strengths from settings (per-type)
    def strengths(self) -> dict:
        lc = self.cfg["lora"]
        return {
            "CHAR_STR": lc.get("char_strength", 0.8),
            "POWER_STR": lc.get("power_strength", 0.7),
            "ENV_STR": lc.get("env_strength", 0.6),
            "ANATOMY_STR": lc.get("anatomy_strength", 0.7),
            "QUALITY_STR": lc.get("quality_strength", 0.6),
            "STYLE_STR": lc.get("style_strength", 0.7),
        }
