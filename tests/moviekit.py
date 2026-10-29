"""A small movie for the movie pipeline tests (the `movie` fixture in
tests/conftest.py): one scene of four shots (a CUT, two CONTINUE shots after
it, a CUT), one character, and an AI that answers from a script (FakeLLM)."""
from __future__ import annotations

import json

MID = "ltx25_s6_cq"
H3 = "minimax_h3_hero_v2"
SIDS = ["s01_001", "s01_002", "s01_003", "s01_004"]


class Bible:
    def __init__(self, pid=None):
        self.pid = pid

    def characters(self):
        return {"mara": {"id": "mara", "name": "Mara", "tier": "main",
                         "body": "a woman in her late twenties, auburn hair, green eyes"}}

    def locations(self):
        return {"hall": {"name": "the great hall", "descriptor": "a stone hall with banners",
                         "lighting": "torchlight"}}

    def crowd_types(self):
        return {}


class FakeLLM:
    """An OpenAI-compatible client that answers from `replies`: a string is a
    text reply, a dict an assistant message (tool calls)."""

    def __init__(self, replies=(), tools=False):
        self.replies = list(replies)
        self.tools = tools
        self.calls: list[list[dict]] = []
        self.released = 0

    def config_ok(self):
        return True

    def tools_ok(self):
        return self.tools

    def chat(self, messages, tools=None, max_tokens=None, temperature=0.7):
        self.calls.append([dict(m) for m in messages])
        r = self.replies.pop(0)
        msg = r if isinstance(r, dict) else {"role": "assistant", "content": r}
        return {"ok": True, "message": msg, "tools": bool(tools), "finish": "stop"}

    def release_memory(self):
        self.released += 1


def tool_call(name, args, n=0):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"call_{name}_{n}", "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}


def shot_rows(pid, **over):
    rows: list[dict] = [
        {"shot_id": "s01_001", "continuity": "CUT", "tier": "standard",
         "primary_action": "Mara walks into the hall and stops at the long table"},
        {"shot_id": "s01_002", "continuity": "CONTINUE", "prev_shot": "s01_001", "tier": "standard",
         "primary_action": "Mara lifts a letter from the table and reads it"},
        {"shot_id": "s01_003", "continuity": "CONTINUE", "prev_shot": "s01_002", "tier": "hero",
         "primary_action": "Mara crushes the letter in her fist"},
        {"shot_id": "s01_004", "continuity": "CUT", "prev_shot": "s01_003", "tier": "standard",
         "primary_action": "The doors of the hall swing open"}]
    for r in rows:
        r.update({"project_id": pid, "scene": "s01", "location": "hall", "shot_size": "medium",
                  "characters": [{"id": "mara", "tier": "main"}]})
        r.update(over.get(r["shot_id"], {}))
    return rows


def take_reply(sids=SIDS[:3], image=""):
    """A writer's reply for the take's shots, in the brief's format."""
    lines = [f"Opening image: {image}"] if image else []
    for sid in sids:
        lines.append(f"Shot {sid}: Medium shot in the torchlit hall. Mara, a woman in her late "
                     f"twenties with auburn hair, moves through the action of {sid}. "
                     f"Diegetic audio: a crackling torch and her footsteps; no score.")
    return "\n\n".join(lines)
