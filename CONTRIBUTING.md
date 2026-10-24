[Kinoloom](README.md) › [Docs](docs/README.md) › Contributing

# Contributing to Kinoloom

> **In short:** This page is for anyone who wants to change Kinoloom and send the change back. It shows how to set up a development copy, which rules the code must keep, how to run the tests, and what a pull request needs to pass. You don't need a GPU, ComfyUI or model weights to run the tests.

Thanks for your interest in Kinoloom. Contributions are welcome. Please keep the project's invariants (the rules the rest of the code relies on) intact: GPU resource containment, the security boundaries, narrative coherence, and a codebase that stays easy to read.

The studio has been tested on one machine, a PC with an RTX 3070 8 GB, so reports from other hardware are especially welcome, whether it worked or not.

---

## 1. Development setup

### Prerequisites
- **Operating system:** the studio itself runs on Windows (the launcher uses Win32 APIs). The test suite, lint and benchmarks also run on Linux. CI uses Ubuntu.
- **Python:** 3.12 exactly (`requires-python = "==3.12.*"` in `pyproject.toml`). The pinned scipy 1.18.0 needs at least 3.12, and 3.12 is the only version tested.
- **Git**.
- **ffmpeg** on PATH for the encode and assembly tests (`tests/unit/test_post_encode.py` and a few chain and take tests; `test_post_encode.py` also finds `tools/ffmpeg`). Without it, those tests are skipped.
- **Node.js** if you want to run the JavaScript syntax check locally. CI uses Node 24.
- You do **not** need a GPU, ComfyUI or model weights for the tests. To run the studio itself, you need the full setup in [README.md](README.md#quick-start) and [RESTORE_INSTRUCTIONS.md](docs/setup/RESTORE_INSTRUCTIONS.md).

### Clone and set up the environment
```bash
git clone https://github.com/harlixay7/Kinoloom.git
cd Kinoloom

# A virtual environment for development
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Linux:
source .venv/bin/activate

# The slim, pinned set the test suite needs (the same file CI installs)
python -m pip install -r tests/requirements-ci.txt
# optional: mypy and type stubs as well (make install-dev)
```
Kinoloom is not an installable package. `pyproject.toml` holds metadata and tool configuration only, so don't install it with `pip install -e .`. The modules in `tool/` use flat imports and run from the tree. The full runtime stack (CUDA torch, training libraries, packages built from git) is pinned in `tool/requirements.txt`. You only need it to run the studio (`make install`, or `install\INSTALL-MAIN.bat` on Windows).

### First check
These commands match the CI steps:
```bash
python -m pytest tests -q                 # hermetic test suite
python install/verify_install.py --quick  # install gate (passes on a fresh clone without weights)
python -m benchmarks.run --quick          # informational, see section 4
```

---

## 2. Architecture and invariants

Please read these rules before you propose a change:

1. **videolab is a snapshot.**
   `videolab/` is a snapshot of the research lab (see the README). Its documents and configs are a record of what the lab measured, so please don't edit their figures: corrections go into the fact-checked copies in `docs/research/`. Fixes to the model downloader (`videolab.py download` and `configs/models.manifest.json`, for example a moved source) are welcome.

2. **Network security and the auth boundary** (details in [SECURITY.md](SECURITY.md)):
   - Every request to the control room passes `_security_gate` in `tool/api.py`. It rejects a NUL byte in the path or query first, then checks the Host allowlist, then the Origin on state-changing methods, then the API token. Only these skip the token: loopback clients (with `security.local_bypass`), the public shell/static paths and the three worker callbacks. New routes are covered automatically. Don't add paths to `security.PUBLIC_PATHS` or to the worker-callback list without a strong reason.
   - Client-supplied names go through `core.is_safe_component` / `core.safe_join`. Client-supplied local paths go through `core.resolve_user_path`.
   - External URL ingestion (training data imports) enforces HTTPS. Every DNS answer must be a public address, and the download is pinned to the validated IP.
   - The control room binds to `127.0.0.1` by default. To bind to `0.0.0.0`, you need `ui.host` in `tool/settings.yaml` (or `--host` on the launcher).

3. **GPU resource arbitration:**
   - Submit local ComfyUI work through `renderer.ComfyClient.queue_prompt` or under an explicit `gpu_broker` lease. `tool/gpu_broker.py` serializes both (runs them one after another).
   - Don't POST to a local ComfyUI `/prompt` directly. Don't use `KINOLOOM_ALLOW_UNBROKERED=1` outside throwaway experiments.

4. **Narrative grounding and canon rules:**
   - LLM generation is grounded in the Bible (`tool/bible.py`).
   - `tool/linter.py` validates shot packages. Errors block a package and are fed back to the LLM. Warnings are advisory. New shot fields belong in the linter.

5. **Cross-platform portability:**
   - Never commit hardcoded absolute drive letters (`C:\`, `D:\`, `B:\`).
   - Use paths relative to `core.ROOT`, or directories from `settings.yaml` (for example `paths.extra_model_dirs` for model roots outside the tree).

---

## 3. Code conventions and standards

- **Linting:**
  CI runs only the definite-bug subset of ruff, and it must pass:
  ```bash
  ruff check --select E9,F63,F7,F82,F601 tool tests benchmarks install
  ```
  `pyproject.toml` selects the broader `E/F/W/B/I` style set (`make lint` runs `ruff check tool/ benchmarks/`). **That set is not clean across the codebase yet.** Please don't add new findings in the code you touch, but a clean run is not a merge requirement.
- **Type hints:**
  New functions should carry type annotations. mypy is configured in `pyproject.toml` (`make typecheck`), but it is not part of CI.
- **Error handling:**
  In new code, avoid bare `except Exception: pass`. Catch specific exceptions where you can, log with the module logger (`log.warning(...)`, or `log.exception(...)` inside a handler), and return an error that tells the user what to do.
- **Frontend JavaScript:**
  The UI is vanilla JavaScript, with no framework. User-controlled text written with `innerHTML` must go through `esc()` / `escapeHtml()`. CI runs `node --check` on `tool/static/*.js`. When you change a static file, bump its `?v=` cache-bust in `index.html` / `project.html`.
- **Runtime state stays out of git:**
  Files the studio writes while it runs (databases, the scanned LoRA registry, render-time records, outputs) are gitignored, and the code must start cleanly when one is missing. Add a new one to `.gitignore` instead of committing it.
- **Docs:**
  Setup guides go in `docs/setup/`, measurements and lab write-ups in `docs/research/`, and every doc is listed in the [docs map](docs/README.md). A number in a doc comes from a saved record; if it was only printed on screen, the doc says so.

---

## 4. Testing and verification

Tests run **only** through the hermetic sandbox (a throwaway copy of the studio). `tests/conftest.py` copies `tool/` and `install/` into a temporary directory. It writes a settings file there with the GPU broker and the VRAM watchdog disabled, and imports the studio modules from that copy. The application lifespan is never started. If the sandbox is not active, the suite refuses to run. Always start pytest from the repository root:

```bash
# Full suite (what CI runs)
python -m pytest tests -q

# Targeted suites
python -m pytest tests/unit                      # fast unit tests (make test-quick)
python -m pytest tests/security                  # auth/CORS, SSRF, traversal, SQL hardening
python -m pytest tests/unit/test_security_policy.py
python -m pytest tests/unit/test_linter.py
python -m pytest tests/integration/test_api_routes.py
```

Some tests are Windows-only and are skipped elsewhere: the launcher tests and the worker installer tests. Two of the worker installer tests also need `robocopy`.

If your pull request changes behaviour, please include tests that show the new behaviour and guard against regressions.

**Benchmarks** (`python -m benchmarks.run [--quick]`, `make benchmark`) are not sandboxed. They import the studio modules from this tree directly. So run them on a development checkout, not on a machine with a live studio. In CI they are informational (`continue-on-error`), because shared runners vary in speed.

---

## 5. Pull requests and review

1. **Small, focused PRs:**
   Keep each pull request to one problem or feature. Avoid large diffs across unrelated subsystems.
2. **Commit messages:**
   Use short, imperative commit subjects:
   - `Fix ...`
   - `Add ...`
   - `Refactor ...`
   - `Document ...`
3. **CI must pass:**
   The blocking checks in `.github/workflows/ci.yml` must pass before review:
   - `py_compile` of every tracked `.py` file;
   - the ruff definite-bug subset;
   - `node --check` of `tool/static/*.js`;
   - `python -m pytest tests -q`;
   - `python install/verify_install.py --quick`.

   The benchmark step is informational.
4. **Benchmark evidence:**
   If your change affects performance-critical paths (scheduler, queue, broker, linter, or model streaming), attach the before/after output of `python -m benchmarks.run` and name the machine it ran on.
5. **License of contributions:**
   By opening a pull request, you agree that your contribution is released under the [MIT License](LICENSE), like the rest of Kinoloom's own code. Changes to the files listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) section 1 stay under those files' own licenses.
