"""base.py - the channel interface every chat backend implements (WP4, 2026-10-24).

A channel moves messages and card bodies; it never decides what is sent (cards.py, drain.py and the gates do).
Message names are opaque strings the channel hands back; a thread is whatever the channel calls one; `to_markup`
turns the agent's plain text (dash bullets, **bold**, links) into the channel's own markup.
"""
from __future__ import annotations


class Channel:
    name = "base"

    def configured(self) -> bool:
        return False

    def post(self, space: str, text: str, thread: str = "") -> str:
        """-> the message name ('' when nothing was posted)."""
        raise NotImplementedError

    def post_body(self, space: str, body: dict, thread: str = "") -> str:
        """A ready-made card body (chat_push.card_body / stack_body) -> message name."""
        raise NotImplementedError

    def patch(self, name: str, text: str) -> bool:
        raise NotImplementedError

    def patch_body(self, name: str, body: dict) -> bool:
        raise NotImplementedError

    def delete(self, name: str) -> bool:
        raise NotImplementedError

    def get(self, name: str) -> dict:
        raise NotImplementedError

    def find_dm(self, user: str) -> str:
        """The direct-message space for a user id ('' when none)."""
        raise NotImplementedError

    def thread_of(self, message_name: str) -> str:
        raise NotImplementedError

    def to_markup(self, text: str) -> str:
        return str(text or "")

    # the old module-level names chat_push used on the gchat module, kept so the seam is a drop-in
    def to_gchat(self, text: str) -> str:
        return self.to_markup(text)


class NullChannel(Channel):
    """Posts nowhere, remembers everything: tests and a brand-new organization with no channel yet."""
    name = "null"

    def __init__(self):
        self.posted, self.patched, self.deleted = [], [], []
        self._n = 0

    def configured(self) -> bool:
        return False

    def _name(self, space: str) -> str:
        self._n += 1
        return f"{space}/messages/null{self._n}"

    def post(self, space: str, text: str, thread: str = "") -> str:
        n = self._name(space)
        self.posted.append({"name": n, "space": space, "text": text, "thread": thread})
        return n

    def post_body(self, space: str, body: dict, thread: str = "") -> str:
        n = self._name(space)
        self.posted.append({"name": n, "space": space, "body": body, "thread": thread})
        return n

    def patch(self, name: str, text: str) -> bool:
        self.patched.append({"name": name, "text": text})
        return True

    def patch_body(self, name: str, body: dict) -> bool:
        self.patched.append({"name": name, "body": body})
        return True

    def delete(self, name: str) -> bool:
        self.deleted.append(name)
        return True

    def get(self, name: str) -> dict:
        return next((p for p in self.posted if p["name"] == name), {})

    def find_dm(self, user: str) -> str:
        return ""

    def thread_of(self, message_name: str) -> str:
        return message_name.split("/messages/")[0] + "/threads/t" if "/messages/" in message_name else ""
