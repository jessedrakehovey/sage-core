"""slack.py (channels) - the second channel, as a stub that proves the seam (WP4, 2026-10-24).

Not shipped live: no token, no app. Every method exists with the interface's shape, and `render_card` shows what a
card body becomes (Block Kit sections with a button per option), so a second organization that lives in Slack
has one file to finish and nothing else to touch. `configured()` is False until `agent/profile.json ->
slack.token` names a stored token (never the token itself in the file: a Credential Manager key).
"""
from __future__ import annotations

import json
import urllib.request

from .base import Channel

API = "https://slack.com/api"


def render_card(body: dict) -> list:
    """A Chat card body (chat_push.card_body: cardsV2 with decoratedText / buttonList widgets) -> Slack blocks."""
    blocks = []
    for card in (body.get("cardsV2") or []):
        c = card.get("card") or {}
        head = (c.get("header") or {}).get("title")
        if head:
            blocks.append({"type": "header", "text": {"type": "plain_text", "text": str(head)[:150]}})
        for sec in c.get("sections") or []:
            for w in sec.get("widgets") or []:
                if "decoratedText" in w:
                    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _strip(str(w["decoratedText"].get("text") or ""))[:3000]}})
                elif "textParagraph" in w:
                    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _strip(str(w["textParagraph"].get("text") or ""))[:3000]}})
                elif "buttonList" in w:
                    els = []
                    for b in (w["buttonList"].get("buttons") or [])[:5]:
                        params = {p["key"]: p["value"] for p in ((b.get("onClick") or {}).get("action") or {}).get("parameters") or []}
                        els.append({"type": "button", "text": {"type": "plain_text", "text": str(b.get("text") or "")[:75]},
                                    "value": json.dumps(params), "action_id": f"card:{params.get('qid', '')}:{params.get('option', '')}"[:255]})
                    if els:
                        blocks.append({"type": "actions", "elements": els})
    if body.get("text") and not blocks:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": str(body["text"])[:3000]}})
    return blocks


def _strip(html: str) -> str:
    import re
    s = re.sub(r"<b>(.*?)</b>", r"*\1*", html)
    s = re.sub(r"<i>(.*?)</i>", r"_\1_", s)
    s = re.sub(r"<[^>]+>", "", s)
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


class Slack(Channel):
    name = "slack"

    def __init__(self, token: str = ""):
        self.token = token

    def configured(self) -> bool:
        return bool(self.token)

    def _call(self, method: str, payload: dict) -> dict:
        if not self.token:
            return {"ok": False, "error": "not_configured"}
        req = urllib.request.Request(f"{API}/{method}", data=json.dumps(payload).encode("utf-8"),
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(self, space: str, text: str, thread: str = "") -> str:
        r = self._call("chat.postMessage", {"channel": space, "text": self.to_markup(text), **({"thread_ts": thread} if thread else {})})
        return f"{space}/messages/{r['ts']}" if r.get("ok") else ""

    def post_body(self, space: str, body: dict, thread: str = "") -> str:
        r = self._call("chat.postMessage", {"channel": space, "text": str(body.get("text") or "card"), "blocks": render_card(body), **({"thread_ts": thread} if thread else {})})
        return f"{space}/messages/{r['ts']}" if r.get("ok") else ""

    def patch(self, name: str, text: str) -> bool:
        space, ts = name.split("/messages/")
        return bool(self._call("chat.update", {"channel": space, "ts": ts, "text": self.to_markup(text)}).get("ok"))

    def patch_body(self, name: str, body: dict) -> bool:
        space, ts = name.split("/messages/")
        return bool(self._call("chat.update", {"channel": space, "ts": ts, "text": str(body.get("text") or "card"), "blocks": render_card(body)}).get("ok"))

    def delete(self, name: str) -> bool:
        space, ts = name.split("/messages/")
        return bool(self._call("chat.delete", {"channel": space, "ts": ts}).get("ok"))

    def get(self, name: str) -> dict:
        space, ts = name.split("/messages/")
        r = self._call("conversations.history", {"channel": space, "latest": ts, "inclusive": True, "limit": 1})
        return (r.get("messages") or [{}])[0] if r.get("ok") else {}

    def find_dm(self, user: str) -> str:
        r = self._call("conversations.open", {"users": user})
        return str((r.get("channel") or {}).get("id") or "") if r.get("ok") else ""

    def thread_of(self, message_name: str) -> str:
        return message_name.split("/messages/")[-1] if "/messages/" in message_name else ""

    def to_markup(self, text: str) -> str:
        import re
        s = re.sub(r"\*\*(.+?)\*\*", r"*\1*", str(text or ""))
        return re.sub(r"^- ", "• ", s, flags=re.M)
