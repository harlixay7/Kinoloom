"""LLM client (the provider) for the Story Architect.

Speaks the OpenAI-compatible /chat/completions protocol, supporting
OpenAI, Anthropic gateways, DeepSeek, and local servers (Ollama, LM Studio).
Extracts and parses structured JSON from model completions.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from core import load_config, log_setup

log = log_setup("llm")


_STAGE_MARKERS = ('{"treatment"', '{"shots"', '{"beats"', '{"bible"',
                  '{"characters"')
_SALVAGE_TRIES = 200          # bounded: each try is one json.loads of <= len(text)


def _try_parse(s: str) -> dict | None:
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _scan_object(t: str, start: int) -> dict | None:
    """Parse the object that opens at t[start] ('{').

    One linear pass tracks string state and the container stack. A balanced
    object is parsed directly. A TRUNCATED one (the reply was cut off) is
    salvaged by closing whatever is still open at the last few points where
    a value had just completed — at most _SALVAGE_TRIES parses, so a 200 KB
    truncated reply costs milliseconds, not minutes."""
    stack: list[str] = []
    in_str = False
    esc = False
    checkpoints: list[tuple[int, str]] = []   # (cut position, closers)
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
                checkpoints.append((i + 1, "".join(reversed(stack))))
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            if not stack:
                return _try_parse(t[start:i + 1])
            checkpoints.append((i + 1, "".join(reversed(stack))))
        elif ch in "0123456789elnrstu" and i + 1 < len(t) and t[i + 1] in ",}] \r\n\t":
            # end of a number / true / false / null literal
            checkpoints.append((i + 1, "".join(reversed(stack))))
    # truncated: close at the latest complete-value boundaries
    for pos, closers in reversed(checkpoints[-_SALVAGE_TRIES:]):
        head = t[start:pos].rstrip().rstrip(",")
        obj = _try_parse(head + closers)
        if obj is not None:
            return obj
    return None


def _top_level_start(t: str, idx: int) -> int:
    """Start of the outermost object that contains position idx (or idx
    itself when it is at top level). A stage package nests the same marker
    ({"shots": {"shots": [...]}}), so the LAST marker is often the inner
    one — the reply is the object that encloses it."""
    opens: list[int] = []
    in_str = esc = False
    for i in range(0, idx):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            opens.append(i)
        elif ch in "}]" and opens:
            opens.pop()
    for pos in opens:
        if t[pos] == "{":
            return pos
    return idx


def extract_json(text: str) -> dict | None:
    """Pull the reply's JSON object out of an AI answer.

    Handles ```json fences (the LAST fenced block wins — earlier ones are
    usually the echoed prompt's examples), prose around the object, trailing
    commas, the prompt text echoed above the reply, and TRUNCATED replies
    (incomplete JSON is salvaged by closing whatever is still open)."""
    if not text:
        return None
    t = text.strip()
    fences = re.findall(r"```(?:json)?\s*(.*?)```", t, re.S)
    bodies = [f.strip() for f in reversed(fences) if "{" in f] or [t]
    if fences and bodies[0] is not t:
        bodies.append(t)          # unfenced tail after the last fence
    for body in bodies:
        # trailing-comma tolerance: strip commas before } or ]
        b = re.sub(r",\s*([}\]])", r"\1", body)
        # the reply's real object is usually the LAST stage marker (the
        # prompt itself contains example JSON — prefer the tail)
        start = max(b.rfind(m) for m in _STAGE_MARKERS)
        start = _top_level_start(b, start) if start >= 0 else b.find("{")
        if start < 0:
            continue
        obj = _scan_object(b, start)
        if obj is None and start != b.find("{"):
            obj = _scan_object(b, b.find("{"))
        if obj is not None:
            return obj
    return None


class LLMClient:
    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or load_config()
        self.arch = self.cfg.get("architect") or {}

    def config_ok(self) -> bool:
        return bool(self.arch.get("base_url") and self.arch.get("model"))

    def complete(self, system: str, user: str, max_tokens: int | None = None,
                 json_mode: bool = False) -> str | None:
        """One chat completion. Returns text or None on failure."""
        arch = self.arch
        base = str(arch.get("base_url") or "").rstrip("/")
        if not base:
            log.error("llm: architect.base_url not configured")
            return None
        url = base + "/chat/completions"
        payload: dict[str, Any] = {
            "model": arch.get("model", "llama3.1:70b"),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": int(max_tokens or arch.get("max_tokens") or 32000),
            "temperature": 0.8,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {arch.get('api_key') or 'ollama'}"}
        try:
            retries = max(0, min(5, int(arch.get("retries", 2))))
        except (TypeError, ValueError):
            retries = 2
        timeout = int(arch.get("timeout_s") or 600)
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(min(30.0, 2.0 ** attempt))
            try:
                # follow_redirects=False: never replay the Bearer key to a
                # host the configured endpoint redirects to
                with httpx.Client(timeout=timeout, follow_redirects=False) as c:
                    r = c.post(url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                log.warning("llm: attempt %d/%d failed: %s", attempt + 1,
                            retries + 1, e)
                continue
            except Exception as e:  # noqa: BLE001
                log.error("llm: request failed: %s", e)
                return None
            if r.status_code == 429 or r.status_code >= 500:
                log.warning("llm: attempt %d/%d HTTP %s: %s", attempt + 1,
                            retries + 1, r.status_code, r.text[:200])
                continue
            if r.status_code >= 400:
                log.error("llm: HTTP %s: %s", r.status_code, r.text[:300])
                return None
            try:
                data = r.json()
                return (data.get("choices") or [{}])[0] \
                    .get("message", {}).get("content")
            except Exception as e:  # noqa: BLE001
                log.error("llm: malformed response: %s", e)
                return None
        log.error("llm: giving up after %d attempt(s)", retries + 1)
        return None

    def test(self) -> dict:
        if not self.config_ok():
            return {"ok": False, "error": "architect.base_url / model not "
                                          "configured"}
        out = self.complete("You are a helpful assistant.",
                            "Reply with exactly: OK", max_tokens=16)
        if out is None:
            return {"ok": False, "error": "request failed — check base_url / "
                                          "model / key and that the server "
                                          "is reachable"}
        return {"ok": True, "reply": str(out).strip()[:100]}
