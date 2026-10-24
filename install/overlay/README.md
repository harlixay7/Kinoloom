[Kinoloom](../../README.md) › [Docs](../../docs/README.md) › Progress relay overlay

# progress_relay.html: deployment overlay

> **In short:** progress_relay.html is the studio's live-progress relay page.
> ComfyUI serves its web pages from inside a pip package, so this file has to
> be copied into that package. This note says where the copies live and how
> to put them back after a rebuild.

This file is the studio's own, but it has to live inside a pip package. ComfyUI serves
its web root from `comfyui_frontend_package/static/`. So the live-progress
relay is deployed by dropping this file into that directory in every Python
runtime that hosts a ComfyUI instance.

This overlay is the source of truth. The copies deployed now:
  - tool/.venv/Lib/site-packages/comfyui_frontend_package/static/  (used by
    the production farm via launcher PYTHONPATH injection)
  - ComfyUI_LTX/venv/Lib/site-packages/comfyui_frontend_package/static/

After a venv rebuild or a frontend package upgrade, copy
progress_relay.html back into each `comfyui_frontend_package/static/` and
restart the instance. Both deployed copies were checked against it by SHA-256.
