[Kinoloom](README.md) › [Docs](docs/README.md) › Security

# Security policy

> **In short:** Kinoloom is built for one person on one Windows PC, with optional worker PCs on a trusted network. It is not hardened for the internet. This page lists the protections the code really enforces, the settings that control them, and what they do not cover. If you found a security problem, section 6 says how to report it privately.

Kinoloom runs generative AI work on this PC and, if you add workers, on other machines. The control room API gives access to the whole studio: file serving, model management, worker control and settings.

**On this page**

- [1. Threat model](#1-threat-model)
- [2. Authentication and authorization (control room)](#2-authentication-and-authorization-control-room)
- [3. Worker fleet authentication](#3-worker-fleet-authentication)
- [4. Server-side request forgery (SSRF) defenses](#4-server-side-request-forgery-ssrf-defenses)
- [5. Path traversal and injection safeguards](#5-path-traversal-and-injection-safeguards)
- [6. Reporting a vulnerability](#6-reporting-a-vulnerability)

---

## 1. Threat model

The studio is built for **one person on one Windows PC, optionally with worker PCs on a trusted LAN or mesh VPN**. It is not hardened for exposure to the internet. It has four surfaces, that is, four ways in:

1. **Control room API (`tool/api.py`, `tool/routers/`, policy in `tool/security.py`):**
   - It binds to `ui.host:ui.port`, by default `127.0.0.1:8080` (this PC only). The launcher passes `ui.host` to uvicorn as `--host`.
   - Every request passes the gate in `api.py` (`_security_gate`): a NUL-byte check, a Host-header allowlist (DNS-rebinding defence), an Origin check on state-changing requests (CSRF defence), then token authentication. See section 2. The one exception is a CORS preflight (an `OPTIONS` request with `Origin` and `Access-Control-Request-Method` headers): `CORSMiddleware`, the outermost layer, answers it itself, before the gate.
   - It serves plain HTTP. There is no TLS, so with `ui.host: 0.0.0.0` the API token and the fleet token cross the network unencrypted.

2. **Worker fleet (`tool/workers.py`, `tool/routers/fleet.py`, `tool/worker/worker_agent.py`):**
   - It is authenticated by **one shared secret** (`workers.shared_token`), compared in constant time. This is not a per-worker credential or a signature scheme. See section 3.
   - A worker is marked offline when it has sent no heartbeat for `workers.offline_after_s` (default 30 s). Its running queue items are then handled according to `workers.on_worker_loss`.
   - Workers run whatever ComfyUI graph a token holder submits. Envelopes are not signed.

3. **External data ingestion (`tool/dataset.py`, `POST /api/training/import`):**
   - It downloads image URLs for LoRA training datasets, so it is an SSRF surface (a way to make the server fetch an address someone else picked). See section 4.

4. **Filesystem and database access:**
   - Client-supplied names and paths are confined to their folders (section 5). Dynamic SQL column names are checked against fixed allow-lists.

### What the design trusts

- **Every process on the studio PC.** With `security.local_bypass: true` (the default), any program running on the machine can call the full API without a token. It can read the fleet token (`GET /api/workers/installer`, `POST /api/workers/token`), change settings, and read files inside the media roots. Set `local_bypass: false` to require the token from local clients too.
- **The local ComfyUI instances** (8188, 8189, 8199, 8190) listen on `127.0.0.1` without authentication, because ComfyUI has none. Any local process can drive them directly and bypass the GPU broker.
- **Everyone who holds the fleet token.** See section 3.
- **The network between the control room and its workers.** LAN workers are dialed over plain `http://host:port`, unless a worker advertises an `https://` `public_base_url`.

### LAN exposure (`ui.host: 0.0.0.0`)

- On the first start with a non-loopback `ui.host` (or `KINOLOOM_BIND_HOST`) and no token configured, the control room generates `security.api_token` and saves it in `tool/settings.yaml`. (Loopback means an address only this PC can reach.) The console only notes that it did so, so read the value from the file. Remote browsers must enter it.
- If you bind to a non-loopback address with `start.bat --host 0.0.0.0` instead of setting `ui.host`, no token is generated. Remote requests are then refused (401) until you set one.
- **Reverse proxies and tunnels on the same PC:** their requests arrive from `127.0.0.1`. If such a proxy also sends a loopback `Host` header, remote users skip the token through the local bypass. Set `local_bypass: false` in that setup.

### Third-party packages

- Every Python package is pinned to an exact version: `tool/requirements.txt`, the venv snapshots in `install/requirements-frozen/`, and `videolab/requirements.txt`.
- `.github/workflows/dependency-audit.yml` checks those pins against the public advisory databases (pip-audit) whenever a list changes, and once a week. The CUDA builds of torch, torchvision and torchaudio and the two git-installed packages (sd-scripts, SAM 2) are not on PyPI, so it cannot check them.
- A pin with a known vulnerability is moved to a release that fixes it once the test suite and a short render on each model pass with that release.

---

## 2. Authentication and authorization (control room)

### Request gate (in order)

1. **NUL byte:** a request whose path or query contains a NUL byte (`\x00` or `%00`) is rejected with **400**.
2. **Host header (`security.allowed_hosts`):** IP literals and `localhost` always pass. Any other name must be listed, or the request gets **400**. This blocks DNS rebinding, an attack in which a web page reaches the studio through a hostname that resolves to 127.0.0.1.
3. **Origin (`security.cors_origins`):** for `POST`/`PUT`/`PATCH`/`DELETE`:
   - A browser `Origin` must equal the request's own `Host` (same origin) or be listed in `cors_origins`.
   - `Origin: null` is refused.
   - Without an `Origin` header (CLI tools, worker agents), the request passes unless `Sec-Fetch-Site: cross-site` is present.

   Failures get **403**. (CSRF is another website making your browser send state-changing requests to the studio.)
4. **Public paths** (`security.is_public_path`): `/`, `/project`, `/studio-tools`, `/favicon.ico`, `/api/health`, and everything under `/static/`. They are served without a token, so the UI shell can load and show its token prompt. `/api/health` returns only `{"ok": true}`.
5. **Worker callbacks** (`security.is_worker_callback`): `POST /api/workers/register`, `POST /api/workers/{id}/heartbeat` and `POST /api/workers/{id}/result` skip the API token, because their handlers check the fleet token themselves (section 3). Steps 1–3 still apply. So a worker that reaches the studio by host name (rather than by IP) needs that name in `allowed_hosts`.
6. **Local bypass:** if `security.local_bypass` is on, the peer address is loopback **and** the `Host` header is a loopback name or address, the request passes without a token.
7. **Token:** everything else needs the API token. If none is configured, the request gets **401** ("Remote access requires an API token"), so remote access is refused outright. A wrong or missing token also gets **401**.

### How the API token is passed

Use any one of these:
```http
Authorization: Bearer <token>
X-API-Token: <token>
Cookie: kinoloom_token=<token>
```
The token is compared with `secrets.compare_digest`. The web UI asks for it after an HTTP 401. It stores it in `localStorage` (`kinoloom_token`) and in the `kinoloom_token` cookie, so `<img>`/`<video>` URLs authenticate too. The cookie is set by JavaScript, so it is not `HttpOnly`. It is `SameSite=Lax`, and `Secure` only when the page is served over https.

The `KINOLOOM_API_TOKEN` environment variable, when it is not empty, overrides `security.api_token`:
```powershell
$env:KINOLOOM_API_TOKEN = "your-strong-random-token-here"
```

### `security` settings (as read by `tool/security.py` and `tool/core.py`)

```yaml
# tool/settings.yaml (defaults from tool/settings.example.yaml)
ui:
  host: 127.0.0.1        # 0.0.0.0 exposes the studio to the LAN
  port: 8080
security:
  api_token: ""
  local_bypass: true
  cors_origins:
    - "http://127.0.0.1:8080"
    - "http://localhost:8080"
  allowed_hosts: []
  media_roots: []
```

| Key | Type and default | What it does |
| :--- | :--- | :--- |
| `security.api_token` | string, `""` | The API token for requests that are not bypassed. `KINOLOOM_API_TOKEN` overrides it. It is generated at startup (`security.ensure_secrets`, `secrets.token_urlsafe(32)`) only when the bind host is non-loopback and neither value is set. Redacted in `GET /api/settings`. |
| `security.local_bypass` | bool, `true` | A loopback peer plus a loopback `Host` header skips the token. `false` requires the token everywhere. |
| `security.cors_origins` | list (or comma-separated string) | Extra browser origins that the Origin gate accepts. The gate reads it on every request. Same-origin requests always pass. The same list feeds `CORSMiddleware` (`allow_credentials=True`), which reads it once at startup and falls back to `http://127.0.0.1:8080` and `http://localhost:8080` when the list is empty. Trailing slashes are ignored. |
| `security.allowed_hosts` | list (or comma-separated string), `[]` | Host names the studio answers to. Entries are exact names or `*.suffix`. `*` turns the Host gate off (not recommended). IP literals and `localhost` always pass. |
| `security.media_roots` | list, `[]` | Extra folders that client-supplied absolute file paths may point into (`core.resolve_user_path`). Used by story import from a file, shot-list ingest from a path, LTX reference images and Video Studio continuation clips. The studio root, `paths.comfyui`, `paths.shots`, `paths.output`, `paths.roster`, `paths.temp` and `paths.extra_model_dirs` are always allowed. |

### Secrets in settings

`GET /api/settings` replaces `security.api_token`, `workers.shared_token`, `architect.api_key` and `hf.token` with `********`. `POST /api/settings` puts the stored values back wherever the client sent the placeholder. When the studio generates a secret, it rewrites `tool/settings.yaml` with `yaml.safe_dump`, so comments in that file are not kept. `tool/settings.yaml` is gitignored. `install\INSTALL-MAIN.bat` mode [2] copies it, tokens included.

### Other responses

The `_response_headers` middleware adds `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN` and `Referrer-Policy: same-origin` to every response that passes through it. A CORS preflight answer does not carry them, because `CORSMiddleware` sends it from outside that layer. `FileNotFoundError` maps to 404, `PermissionError` to 403, and `core.InvalidInput` to 400.

---

## 3. Worker fleet authentication

### The shared secret

- `workers.shared_token` is generated at control-room startup when it is empty or still the shipped placeholder `__WORKER_SHARED_TOKEN__`. If it is empty at request time (for example, cleared through the Settings editor), every worker callback is refused. In other words, the fleet fails closed.
- **register** and **heartbeat** carry the token in the JSON body (`"token"`). **result** carries it in the `X-Worker-Token` header (or `Authorization: Bearer` / `X-API-Token`). A missing or wrong token gets **401**.
- At registration the SHA-256 of the token is stored with the worker, and heartbeats and result uploads must match it. Every worker uses the same token, so this check does not tell one worker from another.
- Anyone holding the token can register a worker under any name. Registering an existing name overwrites that worker's record, **including its host**, so later jobs for that worker id go to the new address. Treat the token like a password.
- **Rotation:** `POST /api/workers/token` (Workers page) generates a new token and returns it. Every installed worker then needs reinstalling, or its `worker_config.yaml` `token` updated.
- **Installer:** `GET /api/workers/installer` serves `install/INSTALL-WORKER.bat` with the control room URL and the token filled in. Treat the downloaded file as a secret. Values that contain batch metacharacters are refused.
- `GET /api/workers` reports only whether a token is set, never the token itself. The worker list leaves out `token_hash`.

### Result uploads (`POST /api/workers/{id}/result`)

- `Content-Length` is required (otherwise **411**).
- A declared body larger than `workers.max_result_mb` (plus 64 KB of multipart framing) gets **413**. The multipart parser spools the upload to disk. The file is then copied into place in chunks, and the copy is stopped with **413** if it passes the limit.
- The `job` and `name` query parameters must be bare file names. Files land in `temp/worker_results/{job}/`. They are written to a temporary name, then renamed.

### Control room to worker

- The control room calls each worker with the shared token in `X-Worker-Token`.
- A worker's advertised host may be private, CGNAT/Tailscale or loopback, but never link-local (which includes `169.254.169.254`), multicast, unspecified or reserved. The host is resolved again before every call, and redirects from a worker are refused, so the token is never forwarded to another host.

### Worker agent (`tool/worker/worker_agent.py`)

- It listens on `0.0.0.0` at `agent_port` (default 8120), taken from its own `worker_config.yaml`.
- Every route except `GET /ping` requires `X-Worker-Token`. If the agent has no token configured, it serves loopback callers only.
- A token holder can submit any ComfyUI graph (`POST /job`), upload files into the worker's ComfyUI input folder, and install or delete model files under its models tree. Those model paths are checked for traversal (escaping the folder with `..` and similar).
- Model integrity:
  - Files pushed from the main PC (`POST /api/workers/{id}/install`) are checked for size and SHA-256 by the agent.
  - Pulled files (`/install_url`, `/install_group`, `colab/fetch_models.py`) are checked against SHA-256 only when the manifest entry has one, and against size only when `bytes` is set. Every `sha256` in `install/colab_model_groups.json` is empty today, so those pulls are **not hash-verified**.
  - `/install_url` accepts only `https` URLs whose host resolves to public addresses, checked again on every redirect, unless `install_allow_http` / `install_allow_private_hosts` are set in `worker_config.yaml`. The Hugging Face token is not forwarded to another host on a redirect.

### `workers` settings (as read by `tool/workers.py` and `tool/routers/fleet.py`)

```yaml
workers:
  enabled: true
  shared_token: __WORKER_SHARED_TOKEN__   # replaced on first start
  max_result_mb: 8192
  heartbeat_s: 10
  offline_after_s: 30
  on_worker_loss: requeue_local
  agent_port: 8120
  remote_timeout_scale: 3.0
```

| Key | Default | What it does |
| :--- | :--- | :--- |
| `workers.shared_token` | placeholder | The fleet secret described above. Redacted in `GET /api/settings`. |
| `workers.max_result_mb` | `8192` in code and example (minimum 1) | Largest result upload accepted, in MiB (the key says MB, the code multiplies by 1024 × 1024). |
| `workers.offline_after_s` | `30` | A worker without a heartbeat for this long counts as offline. The dispatcher's reaper marks it offline and applies `on_worker_loss`. |
| `workers.on_worker_loss` | `requeue_local` | What happens to a dead worker's running items: `requeue_local` queues them for the local machine, `fail` fails them, and any other value requeues them for the same worker. An item that has used up its `max_attempts` fails whatever the setting. |
| `workers.remote_timeout_scale` | `1.0` in code, `3.0` in the example | Multiplies the remote job deadline sent to the agent (3600 s × scale) and the control room's wait for it. |
| `workers.heartbeat_s` | `10` | Only shown on the Workers page. The agent's heartbeat interval comes from its own `worker_config.yaml`. |
| `workers.enabled`, `workers.agent_port` | `true`, `8120` | Present in `settings.example.yaml` but not read by the control room. The agent's port is set in its `worker_config.yaml`. |

---

## 4. Server-side request forgery (SSRF) defenses

When you import datasets from URLs (`POST /api/training/import`, `dataset.import_urls`):

- **Scheme:** only `https://` is accepted (`http://`, `file://`, `ftp://` and other schemes are rejected). URLs that start with `-` are rejected.
- **Credentials:** URLs with embedded user info (`user:pass@host`) are refused.
- **Public addresses only:** the host is resolved before any download, and **every** DNS answer must be a public address (`ipaddress.is_global` and not multicast, IPv4-mapped IPv6 included). This excludes:
  - loopback (`127.0.0.0/8`, `::1`);
  - private networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) and CGNAT (`100.64.0.0/10`);
  - link-local and cloud metadata (`169.254.0.0/16`, `fe80::/10`);
  - multicast, reserved and unspecified ranges.
- **Pinned address:** curl downloads from the validated IP (`--resolve`), so a second DNS answer cannot redirect the download. Redirects are turned off (`--max-redirs 0`, `--proto =https`).
- **Limits:** at most 500 URLs per request, 50 MiB per file, 120 s per download. A file is kept only if it is over 10,000 bytes and decodes as an image (OpenCV). The import needs `curl` on PATH.

---

## 5. Path traversal and injection safeguards

- **Single names** (project, shot and dataset ids and similar) are checked with `core.is_safe_component()`:
  ```python
  re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _\-]{0,63}", s)
  ```
- **Relative names under a base folder** go through `core.safe_join()`. It refuses absolute paths, drive letters, UNC paths, `..` escapes and NUL bytes (it raises `InvalidInput`, which becomes a 400).
- **Absolute local paths from clients** go through `core.resolve_user_path()`. The path must lie inside a media root (see `security.media_roots`). UNC paths (`\\host\share`) are always refused, because opening one makes Windows authenticate to that host with the user's NTLM credentials. Drive-relative paths (`C:foo`) are refused.
- **Worker model installs** accept only paths inside `models/` or `ComfyUI/models/` on the main PC (`fleet._install_source`).
- **SQL:** values are bound as parameters. Where an `UPDATE` builds its column list dynamically, the names are checked against fixed allow-lists (`assert_safe_identifier` with `KNOWN_SHOT_FIELDS` in `shots_db.py`, `KNOWN_QUEUE_FIELDS` in `task_queue.py`, `KNOWN_RUN_FIELDS` in `datasets_db.py` and `KNOWN_FIELDS` in `projects.py`).
- **Frontend XSS:** dynamic content written with `innerHTML` must go through the escaping helpers (`esc()` in `tool/static/app.js` and `tool/static/js/ui.js`, `escapeHtml()` in `app.js`).

The policy is tested by `tests/unit/test_security_policy.py` and the suites in `tests/security/` (auth/CORS, SSRF, traversal, SQL hardening).

---

## 6. Reporting a vulnerability

If you find a security vulnerability in Kinoloom, please report it privately. Please do not put the details in a public GitHub issue.

1. Use GitHub's private vulnerability reporting for this repository: open the **Security** tab and choose **Report a vulnerability**.
2. If that option is not there (private reporting is not enabled), open an issue that asks me for a private channel. Do not include exploit details or reproduction steps in it.
3. In your report, include the affected version or commit, the steps to reproduce it, and the impact.

This is a small project, so I can't promise a response time. I handle reports on a best-effort basis, and I agree on disclosure timing with you.
