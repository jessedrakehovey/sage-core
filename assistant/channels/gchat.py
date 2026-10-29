"""gchat.py (channels) - Google Chat behind the channel interface (WP4, 2026-10-24).

A one-to-one wrapper over assistant/agent/gchat.py (the live Pub/Sub + REST module): every method delegates, so
behaviour, auth and message names are exactly what they were. The point is the seam: chat_push and everything
over it call `channels.get()` and never the module.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from .base import Channel

AGENT = Path(__file__).resolve().parents[1] / "agent"


def _mod():
    """The live module assistant/agent/gchat.py, the same object the bridge holds (sys.modules['gchat']); this package's
    own module is `channels.gchat`, so the two never shadow each other."""
    m = sys.modules.get("gchat")
    if m is not None and hasattr(m, "to_gchat"):
        return m
    if str(AGENT) not in sys.path:
        sys.path.insert(0, str(AGENT))
    return importlib.import_module("gchat")


class GChat(Channel):
    name = "gchat"

    @property
    def mod(self):
        return _mod()

    def configured(self) -> bool:
        try:
            return bool(self.mod.configured())
        except Exception:  # noqa: BLE001
            return False

    def post(self, space: str, text: str, thread: str = "") -> str:
        return self.mod.post(space, text, thread)

    def post_body(self, space: str, body: dict, thread: str = "") -> str:
        return self.mod.post_body(space, body, thread)

    def patch(self, name: str, text: str) -> bool:
        return self.mod.patch(name, text)

    def patch_body(self, name: str, body: dict) -> bool:
        return self.mod.patch_body(name, body)

    def delete(self, name: str) -> bool:
        return self.mod.delete(name)

    def get(self, name: str) -> dict:
        return self.mod.get(name)

    def find_dm(self, user: str) -> str:
        return self.mod.find_dm(user)

    def thread_of(self, message_name: str) -> str:
        return self.mod.thread_of(message_name)

    def to_markup(self, text: str) -> str:
        return self.mod.to_gchat(text)

    def __getattr__(self, item):
        # anything chat_push still reaches for by the module's name (pull, parse_click, record_added, cfg ...)
        return getattr(_mod(), item)
