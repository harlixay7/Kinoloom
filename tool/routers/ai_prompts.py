"""Kinoloom - the movie's video prompts and the AI settings.

The video prompts of a project's LTX-2.5 / MiniMax H3 shots (movie_prompts):
their state for the Shots page, writing them with the configured AI, the
brief for a chat AI and importing its reply. A single prompt is edited with
POST /api/projects/{pid}/shots/{sid}/prompt (routers/project_shots.py).

The AI settings (settings `architect`, shared with the Story Architect): a
provider preset or any OpenAI-compatible endpoint, its model and key. The key
is never sent back (only its last four characters), never sent to another
endpoint than the one it was saved for, and the model list and the
connection test use the saved settings only.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter
from pydantic import BaseModel

from core import log_setup, save_config

from .common import Bible, cfg, db, prompt_writer, store

router = APIRouter()
log = log_setup("api.ai_prompts")

_IMPORT_MAX = 400_000              # characters of a pasted reply


def _project(pid: str) -> dict:
    proj = store.get(pid)
    if not proj:
        raise FileNotFoundError(pid)
    return proj


# ------------------------------------------------------------- AI settings
def _masked(key: str) -> str:
    key = str(key or "")
    return ("*" * 8 + key[-4:]) if len(key) > 8 else ("*" * 8 if key else "")


def _origin(url: str) -> tuple[str, str]:
    s = urlsplit(str(url or ""))
    return s.scheme.lower(), s.netloc.lower()


def ai_settings() -> dict:
    """The AI settings as the settings dialog shows them (the key masked)."""
    import llm
    arch = dict(cfg.get("architect") or {})
    base = str(arch.get("base_url") or "")
    provider = llm.provider_of(base) if base else ""
    return {"provider": provider, "base_url": base, "model": str(arch.get("model") or ""),
            "api_key": _masked(arch.get("api_key") or ""), "has_key": bool(arch.get("api_key")),
            "tools": str(arch.get("tools") or "auto"), "local": llm.is_local(base),
            "needs_key": bool((llm.PROVIDERS.get(provider) or {}).get("needs_key")),
            "ready": llm.LLMClient(cfg).config_ok()}


def save_ai_settings(base_url: str, model: str, api_key: str = "", *, provider: str = "",
                     tools: str | None = None, clear_key: bool = False) -> dict:
    """Store the AI settings. A key typed in replaces the stored one (a masked
    one keeps it); the stored key is dropped when the endpoint moves to
    another host, since it was issued for the old one."""
    import llm
    arch = dict(cfg.get("architect") or {})
    url = str(base_url or "").strip() or str((llm.PROVIDERS.get(provider) or {}).get("base_url") or "")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return {"ok": False, "error": "the endpoint must be an http(s) URL"}
    typed = api_key.strip() if api_key and not api_key.startswith("*") else ""
    cleared = False
    if typed:
        arch["api_key"] = typed
    elif arch.get("api_key") and (clear_key or _origin(url) != _origin(arch.get("base_url", ""))):
        arch["api_key"] = ""
        cleared = True
        log.info("AI settings: stored API key cleared (%s)",
                 "on request" if clear_key else "the endpoint moved to another host")
    arch["base_url"] = url.rstrip("/")
    arch["model"] = str(model or "").strip()
    if tools in ("auto", "off"):
        arch["tools"] = tools
    cfg["architect"] = arch
    save_config(cfg)
    import movie_prompts
    llm.forget_reach()
    movie_prompts._DOWN.clear()       # the writers ask the new settings at once
    return {"ok": True, "api_key_cleared": cleared, **ai_settings()}


class AiSettingsBody(BaseModel):
    provider: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    tools: str | None = None
    clear_key: bool = False


@router.get("/api/ai/providers")
def ai_providers() -> dict:
    import llm
    return {"providers": [{"id": k, **v} for k, v in llm.PROVIDERS.items()]}


@router.get("/api/ai/config")
def ai_config() -> dict:
    return ai_settings()


@router.get("/api/ai/reachable")
def ai_reachable() -> dict:
    """Whether the saved AI answers (asked apart from /api/ai/config, which
    every page loads, so a server that does not answer never slows a page)."""
    import llm
    arch = cfg.get("architect") or {}
    base = str(arch.get("base_url") or "")
    import movie_prompts
    ready = llm.LLMClient(cfg).config_ok()
    up = ready and llm.reachable(base)
    if up:
        movie_prompts._DOWN.pop(base, None)   # it answers again: the writers may ask it
    return {"ready": ready, "reachable": up, "base_url": base, "local": llm.is_local(base)}


@router.post("/api/ai/config")
def ai_config_save(body: AiSettingsBody) -> dict:
    return save_ai_settings(body.base_url, body.model, body.api_key, provider=body.provider,
                            tools=body.tools, clear_key=body.clear_key)


@router.get("/api/ai/models")
def ai_models() -> dict:
    """The models the saved endpoint offers."""
    import llm
    return llm.LLMClient(cfg).list_models()


@router.post("/api/ai/test")
def ai_test() -> dict:
    """One short request to the saved endpoint and model."""
    import llm
    return llm.LLMClient(cfg).test()


# ------------------------------------------------------------ video prompts
@router.get("/api/projects/{pid}/prompts")
def project_prompts(pid: str) -> dict:
    """Every LTX-2.5 / H3 shot's prompt and whether it is ready to render."""
    import movie_prompts
    _project(pid)
    view = movie_prompts.project_view(db, Bible(pid), pid, cfg)
    return {"ok": True, **view, "writer": prompt_writer.status(pid)["active"]}


class PromptBriefBody(BaseModel):
    shots: list[str] | None = None
    limit: int | None = None


@router.post("/api/projects/{pid}/prompts/brief")
def project_prompts_brief(pid: str, body: PromptBriefBody) -> dict:
    """The brief for a chat AI, a batch at a time (movie_prompts.chat_brief):
    the next shots without a ready prompt, takes of one scene with up to
    `limit` prompts to write (default movie.prompt_segments_per_reply), or
    the takes of `shots`; with the reply format and how far the prompts are."""
    import movie_prompts
    proj = _project(pid)
    limit = max(1, min(40, int(body.limit))) if body.limit else None
    return movie_prompts.chat_brief(db, Bible(pid), proj, body.shots or None, limit, cfg)


class PromptImportBody(BaseModel):
    text: str


@router.post("/api/projects/{pid}/prompts/import")
def project_prompts_import(pid: str, body: PromptImportBody) -> dict:
    """Store a chat AI's reply to a brief."""
    import movie_prompts
    proj = _project(pid)
    if len(body.text) > _IMPORT_MAX:
        return {"ok": False, "error": f"the reply is longer than {_IMPORT_MAX} characters"}
    return movie_prompts.import_reply(db, Bible(pid), proj, body.text, cfg)


def _opening_unit(pid: str, sid: str):
    import movie_prompts
    _project(pid)
    unit = movie_prompts.unit_for(db, pid, sid, cfg)
    if unit is None or unit.sids[0] != sid:
        raise FileNotFoundError(sid)
    return unit


@router.get("/api/projects/{pid}/shots/{sid}/opening-frame")
def project_opening_frame(pid: str, sid: str):
    """The opening frame a unit starts from (opening_frames), when made."""
    import opening_frames
    from fastapi.responses import FileResponse
    p = opening_frames.frame_for(db, _opening_unit(pid, sid), cfg=cfg)
    if p is None:
        raise FileNotFoundError(sid)
    return FileResponse(str(p), media_type="image/png", headers={"Cache-Control": "no-cache"})


_AGAIN: set[tuple[str, str]] = set()


@router.post("/api/projects/{pid}/shots/{sid}/opening-frame/again")
def project_opening_frame_again(pid: str, sid: str) -> dict:
    """Make the unit's opening frame again with new seeds (in the
    background: the Shots page refreshes when it is in)."""
    import threading
    import opening_frames
    unit = _opening_unit(pid, sid)
    if not opening_frames.planned(db, unit, cfg=cfg) or opening_frames.frame_path(db, unit) is None:
        return {"ok": False, "error": "this shot does not open on an opening frame"}
    if opening_frames.waits(db, unit):
        return {"ok": False, "error": "this match cut's frame is made from the last frame of the shot "
                                      "before: it is made once that shot has rendered"}
    if (pid, sid) in _AGAIN:
        return {"ok": True, "making": True}
    _AGAIN.add((pid, sid))

    def run() -> None:
        try:
            opening_frames.again(db, Bible(pid), unit, cfg)
        except Exception as e:  # noqa: BLE001 - logged; the frame stays as it was
            log.warning(f"{pid}/{sid}: the opening frame was not made again: {e}")
        finally:
            _AGAIN.discard((pid, sid))
    threading.Thread(target=run, name=f"frame-{sid}", daemon=True).start()
    return {"ok": True, "making": True}


class PromptWriteBody(BaseModel):
    shots: list[str] | None = None
    force: bool = False


@router.post("/api/projects/{pid}/prompts/write")
def project_prompts_write(pid: str, body: PromptWriteBody) -> dict:
    """Write the missing and outdated prompts (or those of `shots`; `force`
    writes ready AI prompts again) with the configured AI, in the background."""
    _project(pid)
    return prompt_writer.start(pid, body.shots or None, bool(body.force))


@router.get("/api/projects/{pid}/prompts/write/status")
def project_prompts_write_status(pid: str) -> dict:
    return prompt_writer.status(pid)


@router.post("/api/projects/{pid}/prompts/write/stop")
def project_prompts_write_stop(pid: str) -> dict:
    return prompt_writer.stop(pid)
