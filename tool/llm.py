"""LLM client (the provider) for the Story Architect and the movie's prompt
writer (movie_prompts).

Speaks the OpenAI-compatible /chat/completions protocol (with function
calling when the server supports it), which cloud providers and local servers
(Ollama, LM Studio) offer. PROVIDERS lists the presets the AI settings offer.
Extracts and parses structured JSON from model completions.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from core import load_config, log_setup

log = log_setup("llm")

# The AI settings' provider presets: the OpenAI-compatible endpoint, where its
# key is made, whether one is needed, a line on what it is, and a few model
# names to start from. The models are suggestions only: "List models" in the
# AI settings shows what the endpoint offers the key. "custom" takes any other
# endpoint.
PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {"label": "OpenAI", "blurb": "GPT models",
               "base_url": "https://api.openai.com/v1",
               "key_url": "https://platform.openai.com/api-keys", "needs_key": True,
               "models": ["gpt-5-mini", "gpt-5"]},
    "gemini": {"label": "Google Gemini", "blurb": "Gemini models",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "key_url": "https://aistudio.google.com/apikey", "needs_key": True,
               "models": ["gemini-2.5-flash", "gemini-2.5-pro"]},
    "openrouter": {"label": "OpenRouter", "blurb": "Many providers behind one key",
                   "base_url": "https://openrouter.ai/api/v1",
                   "key_url": "https://openrouter.ai/keys", "needs_key": True,
                   "models": ["openai/gpt-5-mini", "google/gemini-2.5-flash", "deepseek/deepseek-chat"]},
    "deepseek": {"label": "DeepSeek", "blurb": "DeepSeek models",
                 "base_url": "https://api.deepseek.com/v1",
                 "key_url": "https://platform.deepseek.com/api_keys", "needs_key": True,
                 "models": ["deepseek-chat", "deepseek-reasoner"]},
    "groq": {"label": "Groq", "blurb": "Open models on fast hosting",
             "base_url": "https://api.groq.com/openai/v1",
             "key_url": "https://console.groq.com/keys", "needs_key": True,
             "models": ["llama-3.3-70b-versatile", "openai/gpt-oss-120b"]},
    "mistral": {"label": "Mistral", "blurb": "Mistral models",
                "base_url": "https://api.mistral.ai/v1",
                "key_url": "https://console.mistral.ai/api-keys", "needs_key": True,
                "models": ["mistral-medium-latest", "mistral-large-latest"]},
    "ollama": {"label": "Ollama", "blurb": "Runs on this PC, no key",
               "base_url": "http://127.0.0.1:11434/v1",
               "key_url": "", "needs_key": False, "local": True,
               "models": ["qwen3:8b", "llama3.1:8b", "gemma3:12b"]},
    "lmstudio": {"label": "LM Studio", "blurb": "Runs on this PC, no key",
                 "base_url": "http://127.0.0.1:1234/v1",
                 "key_url": "", "needs_key": False, "local": True, "models": []},
    "custom": {"label": "Other", "blurb": "Any OpenAI-compatible endpoint",
               "base_url": "", "key_url": "", "needs_key": False, "models": []},
}


def provider_of(base_url: str) -> str:
    """The preset whose endpoint `base_url` is, else "custom"."""
    want = str(base_url or "").rstrip("/").lower()
    for pid, p in PROVIDERS.items():
        if p["base_url"] and p["base_url"].rstrip("/").lower() == want:
            return pid
    return "custom"


_REACH: dict[str, tuple[float, bool]] = {}   # base_url -> (when, reachable)


def reachable(base_url: str, probe=None) -> bool:
    """Whether a server answers at `base_url`: GET <base>/models, where any
    answer counts (a 401 without a key, an old Ollama's 404) but a proxy's
    502/504, a refused connection or a timeout do not. Remembered for 30 s
    when it answers and 5 s when it does not, so a server just started is
    seen at once. `probe` replaces the request (tests)."""
    base = str(base_url or "").rstrip("/")
    if not base:
        return False
    hit = _REACH.get(base)
    if hit and time.time() - hit[0] < (30.0 if hit[1] else 5.0):
        return hit[1]
    if probe is None:
        def probe(url: str) -> int:
            with httpx.Client(timeout=3.0, follow_redirects=False) as c:
                return c.get(url + "/models").status_code
    try:
        ok = probe(base) not in (502, 504)
    except Exception:  # noqa: BLE001 - refused, unknown host, timeout
        ok = False
    _REACH[base] = (time.time(), ok)
    return ok


def forget_reach(base_url: str = "") -> None:
    """Drop the remembered answer (the settings changed)."""
    if base_url:
        _REACH.pop(str(base_url).rstrip("/"), None)
    else:
        _REACH.clear()


def is_local(base_url: str) -> bool:
    """An endpoint on this PC: it shares the GPU with the renders."""
    host = (urlsplit(str(base_url or "")).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


# (base_url, model) -> False once the server refused the `tools` field
_NO_TOOLS: set[tuple[str, str]] = set()


def _adapt(payload: dict, error: str) -> bool:
    """Change `payload` the way a 400 reply asks, once per field: newer OpenAI
    reasoning models take `max_completion_tokens` instead of `max_tokens`, and
    only their default temperature. True when something changed."""
    low = str(error or "").lower()
    if "max_completion_tokens" in low and "max_tokens" in payload:
        payload["max_completion_tokens"] = payload.pop("max_tokens")
        return True
    if "temperature" in low and "temperature" in payload and re.search(
            r"unsupported|not support|only the default|does not support", low):
        payload.pop("temperature")
        return True
    return False


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
        # why the last complete() returned None, and whether nothing answered
        self.last_error = ""
        self.last_unreachable = False

    def config_ok(self) -> bool:
        return bool(self.arch.get("base_url") and self.arch.get("model"))

    def _headers(self) -> dict:
        return {"Content-Type": "application/json",
                "Authorization": f"Bearer {self.arch.get('api_key') or 'ollama'}"}

    def _post(self, payload: dict, retries: int | None = None) -> dict:
        """POST /chat/completions with retries on transport errors, 429 and
        5xx. {"ok": True, "data": ...} or {"ok": False, "status", "error"}."""
        base = str(self.arch.get("base_url") or "").rstrip("/")
        if not base:
            log.error("llm: architect.base_url not configured")
            return {"ok": False, "status": 0, "error": "no AI endpoint is configured"}
        if retries is None:
            try:
                retries = max(0, min(5, int(self.arch.get("retries", 2))))
            except (TypeError, ValueError):
                retries = 2
        timeout = int(self.arch.get("timeout_s") or 600)
        last = {"ok": False, "status": 0, "error": "no reply"}
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(min(30.0, 2.0 ** attempt))
            try:
                # follow_redirects=False: never replay the Bearer key to a
                # host the configured endpoint redirects to
                with httpx.Client(timeout=timeout, follow_redirects=False) as c:
                    r = c.post(base + "/chat/completions", json=payload, headers=self._headers())
            except (httpx.TimeoutException, httpx.TransportError) as e:
                log.warning("llm: attempt %d/%d failed: %s", attempt + 1,
                            retries + 1, e)
                # unreachable: nothing listens there, or the name does not
                # resolve (a slow model times out instead)
                last = {"ok": False, "status": 0, "error": f"the AI endpoint did not answer ({e})",
                        "unreachable": isinstance(e, httpx.ConnectError)}
                continue
            except Exception as e:  # noqa: BLE001
                log.error("llm: request failed: %s", e)
                return {"ok": False, "status": 0, "error": f"request failed: {e}"}
            if r.status_code == 429 or r.status_code >= 500:
                log.warning("llm: attempt %d/%d HTTP %s: %s", attempt + 1,
                            retries + 1, r.status_code, r.text[:200])
                last = {"ok": False, "status": r.status_code,
                        "error": f"HTTP {r.status_code}: {r.text[:200]}"}
                continue
            if r.status_code == 400 and _adapt(payload, r.text):
                log.info("llm: the model takes other request fields - asking again")
                return self._post(payload, retries)
            if r.status_code >= 400:
                log.error("llm: HTTP %s: %s", r.status_code, r.text[:300])
                return {"ok": False, "status": r.status_code,
                        "error": f"HTTP {r.status_code}: {r.text[:300]}"}
            try:
                return {"ok": True, "data": r.json()}
            except Exception as e:  # noqa: BLE001
                log.error("llm: malformed response: %s", e)
                return {"ok": False, "status": r.status_code, "error": f"malformed response: {e}"}
        log.error("llm: giving up after %d attempt(s)", retries + 1)
        return last

    def complete(self, system: str, user: str, max_tokens: int | None = None,
                 json_mode: bool = False) -> str | None:
        """One chat completion. Returns text or None on failure."""
        payload: dict[str, Any] = {
            "model": self.arch.get("model", "llama3.1:70b"),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": int(max_tokens or self.arch.get("max_tokens") or 32000),
            "temperature": 0.8,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        res = self._post(payload)
        self.last_error, self.last_unreachable = "", False
        if not res["ok"]:
            self.last_error = str(res.get("error") or "request failed")
            self.last_unreachable = bool(res.get("unreachable"))
            return None
        try:
            content = (res["data"].get("choices") or [{}])[0].get("message", {}).get("content")
        except Exception as e:  # noqa: BLE001
            log.error("llm: malformed response: %s", e)
            self.last_error = f"malformed response: {e}"
            return None
        # an answer with no text (a reasoning model that ran out of tokens) is
        # an empty reply, asked again like any unreadable one, not a failure
        return content if content is not None else ""

    def tools_ok(self) -> bool:
        """Whether to offer tools: on unless settings `architect.tools` is
        "off" or this server refused them before."""
        if str(self.arch.get("tools") or "auto").lower() == "off":
            return False
        return (str(self.arch.get("base_url") or ""), str(self.arch.get("model") or "")) not in _NO_TOOLS

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             max_tokens: int | None = None, temperature: float = 0.7) -> dict:
        """One turn of a conversation, with OpenAI-style function tools when
        given and supported. {"ok": True, "message": {...}, "tools": bool}
        (the assistant message, and whether the tools were offered), or
        {"ok": False, "error"}. A server that rejects the tools field is asked
        again without it, and remembered."""
        key = (str(self.arch.get("base_url") or ""), str(self.arch.get("model") or ""))
        payload: dict[str, Any] = {
            "model": self.arch.get("model", "llama3.1:70b"),
            "messages": messages,
            "max_tokens": int(max_tokens or self.arch.get("max_tokens") or 8192),
            "temperature": temperature,
        }
        offered = bool(tools) and self.tools_ok()
        if offered:
            payload["tools"] = tools
        res = self._post(payload)
        if (not res["ok"] and offered and res.get("status") in (400, 404, 422)
                and re.search(r"tool|function", str(res.get("error")), re.I)):
            log.info("llm: %s does not take tools - asking without them", key[1] or key[0])
            _NO_TOOLS.add(key)
            payload.pop("tools", None)
            offered = False
            res = self._post(payload)
        if not res["ok"]:
            out = {"ok": False, "error": res.get("error") or "request failed"}
            if res.get("unreachable"):
                out["unreachable"] = True
            return out
        try:
            choice = (res["data"].get("choices") or [{}])[0]
            msg = dict(choice.get("message") or {})
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"malformed response: {e}"}
        msg.setdefault("content", "")
        return {"ok": True, "message": msg, "tools": offered,
                "finish": str(choice.get("finish_reason") or "")}

    def list_models(self) -> dict:
        """The model ids the endpoint offers (GET /models)."""
        base = str(self.arch.get("base_url") or "").rstrip("/")
        if not base:
            return {"ok": False, "error": "no AI endpoint is configured"}
        try:
            with httpx.Client(timeout=30, follow_redirects=False) as c:
                r = c.get(base + "/models", headers=self._headers())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"the endpoint did not answer ({e})"}
        if r.status_code >= 400:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        try:
            data = r.json()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"malformed response: {e}"}
        items = data.get("data") if isinstance(data, dict) else data
        ids = sorted({str(m.get("id") or "").removeprefix("models/") for m in items or []
                      if isinstance(m, dict) and m.get("id")})
        return {"ok": True, "models": ids}

    def release_memory(self) -> bool:
        """Unload the model of an Ollama server on this PC, which shares the
        GPU with the renders (its native API: /api/generate with keep_alive
        0). Other servers keep their model loaded. True when asked."""
        base = str(self.arch.get("base_url") or "").rstrip("/")
        parts = urlsplit(base)
        if not is_local(base) or (parts.port != 11434 and provider_of(base) != "ollama"):
            return False
        root = base[:-3] if base.endswith("/v1") else base
        with httpx.Client(timeout=30, follow_redirects=False) as c:
            r = c.post(root + "/api/generate", json={"model": self.arch.get("model"), "keep_alive": 0})
        r.raise_for_status()
        return True

    def test(self) -> dict:
        if not self.config_ok():
            return {"ok": False, "error": "architect.base_url / model not "
                                          "configured"}
        res = self._post({"model": self.arch.get("model"), "max_tokens": 16, "temperature": 0,
                          "messages": [{"role": "system", "content": "You are a helpful assistant."},
                                       {"role": "user", "content": "Reply with exactly: OK"}]},
                         retries=0)
        if not res["ok"]:
            return {"ok": False, "error": res.get("error") or "request failed — check base_url / "
                                                              "model / key and that the server "
                                                              "is reachable"}
        try:
            out = (res["data"].get("choices") or [{}])[0].get("message", {}).get("content")
        except Exception:  # noqa: BLE001
            out = None
        return {"ok": True, "reply": str(out or "").strip()[:100]}
