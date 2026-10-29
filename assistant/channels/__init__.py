"""assistant/channels - the chat channel behind one seam (WP4 of the stand-alone proposal, 2026-10-24).

Google Chat is how the owners talk to Sage today; 17 modules reached `agent/gchat.py` directly. The card grammar
(`question`, `options`, `details`, `groups`, `[TEXT PREVIEW]`) is already channel-neutral; what the channel does
is post, patch and delete messages and card bodies, resolve a thread, find an owner's DM. That is the interface
here (`base.Channel`). `gchat.GChat` wraps the live module one to one; `slack.Slack` is the stub that proves the
seam (every method, nothing shipped). `get()` picks the channel from `agent/profile.json -> channel` (default
gchat) and is what `chat_push._g()` returns, so every card, result line, digest and say goes through it.

  from channels import get
  ch = get()            # .post(space, text, thread) .post_body(space, body, thread) .patch(name, text)
                        # .patch_body(name, body) .delete(name) .get(name) .find_dm(user) .thread_of(name)
                        # .to_markup(text) .configured() .name
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

_CACHE = {}


def channel_name() -> str:
    env = os.environ.get("ASSISTANT_CHANNEL")
    if env:
        return env.strip().lower()
    try:
        prof = json.loads((ROOT / "agent" / "profile.json").read_text(encoding="utf-8-sig"))
        return str(prof.get("channel") or "gchat").strip().lower()
    except (OSError, ValueError):
        return "gchat"


def get(name: str = ""):
    name = (name or channel_name()).lower()
    if name in _CACHE:
        return _CACHE[name]
    if name == "slack":
        from .slack import Slack
        ch = Slack()
    elif name in ("null", "none", "test"):
        from .base import NullChannel
        ch = NullChannel()
    else:
        from .gchat import GChat
        ch = GChat()
    _CACHE[name] = ch
    return ch
