**What this changes**
A short description, and why.

**How you tested it**
- [ ] `python -m pytest tests` passes (the suite copies `tool/` and `install/` into a sandbox, see `tests/conftest.py`)
- [ ] `ruff check --select E9,F63,F7,F82,F601 tool tests benchmarks install` is clean
- [ ] If you changed a render path: what you rendered, on which GPU, and how long it took

**Docs**
- [ ] I updated the docs this change touches (`docs/README.md` lists them)
- [ ] Every new number in the docs comes from a run I can point to

See `CONTRIBUTING.md` for the setup and the rules the code keeps.
