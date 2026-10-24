"""HTTP security policy for the control room (pure functions, unit-tested).

The API exposes the whole studio (file serving, model management, worker
control, settings), so every request passes three gates in ``api.py``:

1. **Host gate (DNS-rebinding defence).** The ``Host`` header must be an IP
   literal, ``localhost``, or a name listed in ``security.allowed_hosts``.
   A rebinding attack needs a *hostname* that resolves to 127.0.0.1, so it is
   rejected here before the loopback bypass can be abused.
2. **Origin gate (CSRF defence).** State-changing requests (POST/PUT/PATCH/
   DELETE) that carry a browser ``Origin`` header must come from the studio's
   own origin or a configured CORS origin. Requests without ``Origin`` (CLI
   tools, worker agents) are judged by ``Sec-Fetch-Site`` when present.
3. **Authentication.** Loopback clients on a loopback Host may skip the token
   (``security.local_bypass``, default on). Everyone else needs the API token
   (``Authorization: Bearer``, ``X-API-Token`` or the ``kinoloom_token``
   cookie). With no token configured, remote access is refused outright.
   The three worker callback routes are exempt from the API token because
   their handlers verify the fleet's worker token themselves.
"""
from __future__ import annotations

import ipaddress
import os
import re
import secrets
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Shell pages and static assets must load without a token so the UI can
# show its token prompt; /api/health is an unauthenticated liveness probe.
PUBLIC_PATHS = frozenset({"/", "/project", "/studio-tools", "/favicon.ico",
                          "/api/health"})
PUBLIC_PREFIXES = ("/static/",)

# Worker agent -> control room callbacks. Their handlers verify the worker
# shared token (tool/workers.py::_check_token), so they bypass the API token.
_WORKER_CALLBACKS = (
    re.compile(r"^/api/workers/register$"),
    re.compile(r"^/api/workers/[A-Za-z0-9._-]{1,64}/heartbeat$"),
    re.compile(r"^/api/workers/[A-Za-z0-9._-]{1,64}/result$"),
)

PLACEHOLDER_WORKER_TOKEN = "__WORKER_SHARED_TOKEN__"
REDACTED = "********"

# (section, key) pairs that are secrets everywhere they appear.
SECRET_KEYS: tuple[tuple[str, str], ...] = (
    ("security", "api_token"),
    ("workers", "shared_token"),
    ("architect", "api_key"),
    ("hf", "token"),
)


# --------------------------------------------------------------- settings
def settings(cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalised security settings (env var overrides the stored token)."""
    sec = dict((cfg or {}).get("security") or {}) if isinstance(cfg, Mapping) else {}
    token = os.environ.get("KINOLOOM_API_TOKEN") or str(sec.get("api_token") or "")
    origins = sec.get("cors_origins") or []
    if isinstance(origins, str):
        origins = [o.strip() for o in origins.split(",") if o.strip()]
    hosts = sec.get("allowed_hosts") or []
    if isinstance(hosts, str):
        hosts = [h.strip() for h in hosts.split(",") if h.strip()]
    return {
        "api_token": token,
        "local_bypass": bool(sec.get("local_bypass", True)),
        "cors_origins": [str(o).rstrip("/") for o in origins],
        "allowed_hosts": [str(h).lower() for h in hosts],
    }


# ------------------------------------------------------------- host gate
def split_host(host_header: str | None) -> str:
    """Return the bare host name/IP from a Host header (no port, no [])."""
    h = (host_header or "").strip().lower()
    if not h:
        return ""
    if h.startswith("["):                      # [::1]:8080
        end = h.find("]")
        return h[1:end] if end > 0 else h
    if h.count(":") == 1:                      # name:port or v4:port
        return h.split(":", 1)[0]
    return h                                   # bare name or bare IPv6


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def host_allowed(host_header: str | None, allowed_hosts: Iterable[str]) -> bool:
    """IP literals and localhost always pass; names must be allow-listed.

    ``allowed_hosts`` entries may be exact names, ``*.suffix`` wildcards, or
    ``*`` (disables the gate; not recommended).
    """
    host = split_host(host_header)
    if not host:
        return False
    if host in LOOPBACK_NAMES or _is_ip(host):
        return True
    for entry in allowed_hosts:
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry == "*" or entry == host:
            return True
        if entry.startswith("*.") and host.endswith(entry[1:]):
            return True
    return False


def is_loopback_client(client_host: str | None) -> bool:
    if not client_host:
        return False
    if client_host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        return False


def is_loopback_host_header(host_header: str | None) -> bool:
    host = split_host(host_header)
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ----------------------------------------------------------- origin gate
def origin_allowed(method: str, headers: Mapping[str, str], host_header: str | None,
                   cors_origins: Iterable[str]) -> bool:
    """CSRF check for state-changing requests."""
    if method.upper() not in UNSAFE_METHODS:
        return True
    origin = (headers.get("origin") or "").strip()
    if origin and origin.lower() != "null":
        o = origin.rstrip("/")
        if o in set(cors_origins):
            return True
        parts = urlsplit(o)
        return bool(parts.netloc) and parts.netloc.lower() == (host_header or "").strip().lower()
    if origin.lower() == "null":               # sandboxed iframe / file:// page
        return False
    # No Origin header: non-browser client, or an old browser. Fall back to
    # Fetch Metadata when the browser sent it.
    site = (headers.get("sec-fetch-site") or "").strip().lower()
    return site not in ("cross-site",)


# ---------------------------------------------------------- authentication
def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def is_worker_callback(method: str, path: str) -> bool:
    return method.upper() == "POST" and any(p.match(path) for p in _WORKER_CALLBACKS)


def provided_token(headers: Mapping[str, str], cookies: Mapping[str, str]) -> str:
    auth = headers.get("authorization") or ""
    if auth[:7].lower() == "bearer ":
        tok = auth[7:].strip()
        if tok:
            return tok
    return (headers.get("x-api-token") or cookies.get("kinoloom_token") or "").strip()


def token_matches(provided: str, expected: str) -> bool:
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


# --------------------------------------------------------------- secrets
def redact(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Deep copy of the settings with every secret replaced by REDACTED."""
    import copy
    out = copy.deepcopy(dict(cfg))
    for section, key in SECRET_KEYS:
        sec = out.get(section)
        if isinstance(sec, dict) and sec.get(key):
            sec[key] = REDACTED
    return out


def merge_secrets(new_cfg: dict[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """Settings posted back by the UI carry REDACTED placeholders; keep the
    stored secret wherever the client did not supply a new value."""
    for section, key in SECRET_KEYS:
        sec = new_cfg.get(section)
        if not isinstance(sec, dict):
            continue
        if sec.get(key) == REDACTED:
            old = (current.get(section) or {}).get(key) if isinstance(current.get(section), Mapping) else None
            if old:
                sec[key] = old
            else:
                sec.pop(key, None)
    return new_cfg


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def ensure_secrets(cfg: dict[str, Any], bind_host: str | None) -> list[str]:
    """Fill in missing secrets in-place. Returns human-readable notes.

    * The worker fleet token is generated when empty or still the shipped
      placeholder (a public constant would authenticate anyone).
    * The API token is generated when the server binds a non-loopback
      address and none is configured, because remote access is refused
      without one.
    """
    notes: list[str] = []
    workers = cfg.setdefault("workers", {})
    if not isinstance(workers, dict):
        workers = cfg["workers"] = {}
    if str(workers.get("shared_token") or "") in ("", PLACEHOLDER_WORKER_TOKEN):
        workers["shared_token"] = generate_token()
        notes.append("generated workers.shared_token (fleet token)")
    sec = cfg.setdefault("security", {})
    if not isinstance(sec, dict):
        sec = cfg["security"] = {}
    host = (bind_host or "127.0.0.1").strip().lower()
    if not is_loopback_host_header(host) and not os.environ.get("KINOLOOM_API_TOKEN") \
            and not sec.get("api_token"):
        sec["api_token"] = generate_token()
        notes.append("generated security.api_token because ui.host is "
                     f"{host!r} (non-loopback); remote browsers must enter it")
    return notes
