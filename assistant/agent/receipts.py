"""receipts.py - one status line per hand-off, in the place it was asked, edited in place.

the owner 2026-10-21: top-level lines about work he asked for in a thread ("This didn't get built and I'm
not going to keep repeating it", a doer report in the home space for a DM ask, a slipped notice
followed by the card anyway) were more than half of what Sage posted on its own. A hand-off now
owns exactly ONE message: the first outcome posts it where the ask was made (the thread, or the
owner's DM), every later outcome EDITS that same message, and nothing about it is ever posted
top-level or twice. Keyed on the hand-off's reference (a ticket id, a card id, a build note path).

  receipts.say(ref, convo, thread, text, space="")   -> the Chat message name ('' off Chat)
  receipts.note(ref, convo, thread, text, cap=4)     -> a NEW line in the hand-off's thread while it runs (2026-10-28)
  receipts.poster()                                   -> post(convo, thread, text, ref="") for commitments.check

A note is the one exception to "one message per hand-off": a milestone the doer reaches while a job runs
(signed in, dates set, the second item in the cart) is worth a line of its own, since an edit to the receipt
makes no sound on a phone. At most `cap` notes per ref reach Chat; the rest get a transcript row only. The
first note in a space with no thread starts the thread, and the receipt line then joins it.

Store: data/agent/receipts.json {ref: {name, convo, thread, space, text, ts, edits, notes}}. Rows older than
KEEP_DAYS are dropped on the next say(). Never raises.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

STORE = core.DATA / "receipts.json"
KEEP_DAYS = 14


def _load() -> dict:
    d = core.load(STORE, {})
    return d if isinstance(d, dict) else {}


def _save(d: dict) -> None:
    cut = time.time() - KEEP_DAYS * 86400
    core.save(STORE, {k: v for k, v in d.items() if float((v or {}).get("ts") or 0) >= cut})


def _space_for(convo: str, space: str = "") -> str:
    """Where a line for this conversation goes: the DM of the owner (dm:<key>), else the space itself."""
    if space and str(space).startswith("spaces/"):
        return str(space)
    c = str(convo or "")
    if c.startswith("dm:"):
        return str(core.load(core.DATA / "gchat_spaces.json", {}).get(c[3:]) or "")
    return c if c.startswith("spaces/") else ""


def _gchat():
    """The bridge's Chat client (one source, and the one tests replace); the module itself as the fallback."""
    try:
        import bridge
        return bridge._gchat()
    except Exception:  # noqa: BLE001
        try:
            import gchat
            return gchat if gchat.configured() else None
        except Exception:  # noqa: BLE001
            return None


def say(ref: str, convo: str, thread: str, text: str, space: str = "", channel: str = "receipt") -> str:
    """Post or edit the one line for `ref`. The transcript gets a row either way (an edit is marked),
    so the brain knows the current state. -> the Chat message name, '' when nothing reached Chat."""
    text = str(text or "").strip()
    ref = str(ref or "").strip()
    if not text:
        return ""
    name = core.profile().get("name", "Agent")
    d = _load()
    row = d.get(ref) if ref else None
    if row and row.get("text") == text:
        return str(row.get("name") or "")               # nothing new to say
    to = convo[3:] if str(convo).startswith("dm:") else "space"
    g = _gchat()
    msg = ""
    th = thread or (row or {}).get("thread", "")
    sp = _space_for(convo, space or (row or {}).get("space", ""))
    shown = _voice(text, channel)                       # a person's words, or '' for system talk (voice.py, 2026-10-27)
    quiet = not shown
    if g and row and row.get("name") and shown:
        try:
            if g.patch(row["name"], shown):
                msg = row["name"]
        except Exception as e:  # noqa: BLE001
            _log(f"receipt edit failed for {ref}: {type(e).__name__}: {str(e)[:120]}")
    if g and not msg and shown and sp and _fresh(th or sp, shown):
        try:
            msg = g.post(sp, shown, th or "")
            if not th and msg:
                # the line starts a thread: remember it as that thread's root, and file the transcript
                # row IN that thread, so an owner's reply there is understood
                th = _thread_of(msg)
                core.note_thread_root(th, name, shown, channel, convo)
        except Exception as e:  # noqa: BLE001
            _log(f"receipt post failed for {ref}: {type(e).__name__}: {str(e)[:120]}")
    edited = bool(row and row.get("name"))             # a row that only holds notes so far is not an edit
    core.append(core.DATA / "outbox.jsonl", {"ts": core.stamp(), "to": to, "text": shown or text, "via": channel, "convo": convo,
                                             "thread": th or "", "ref": ref, "edit": edited, **({"quiet": True} if quiet else {})})
    core.say(convo, name, shown or text, channel=channel,
             meta={"thread": th or "", "ref": ref, **({"edited": True} if edited else {}), **({"held": True} if quiet else {})})
    if ref:
        d[ref] = {"name": msg or (row or {}).get("name", ""), "convo": convo, "thread": th or "",
                  "space": space or (row or {}).get("space", ""), "text": text, "ts": time.time(),
                  "edits": int((row or {}).get("edits") or 0) + (1 if edited else 0),
                  "notes": int((row or {}).get("notes") or 0)}
        _save(d)
    return msg


def note(ref: str, convo: str, thread: str, text: str, space: str = "", channel: str = "doer", cap: int = 4) -> str:
    """A NEW line in the hand-off's thread (never an edit of the receipt): a milestone while the job runs.
    The first `cap` per ref reach Chat; every one gets a transcript row. -> the Chat message name, '' otherwise."""
    text = str(text or "").strip()
    ref = str(ref or "").strip()
    if not text:
        return ""
    name = core.profile().get("name", "Agent")
    d = _load()
    row = (d.get(ref) if ref else None) or {}
    n = int(row.get("notes") or 0)
    th = thread or row.get("thread", "")
    sp = _space_for(convo, space or row.get("space", ""))
    shown = _voice(text, channel)
    msg = ""
    g = _gchat()
    if g and shown and sp and n < max(0, int(cap)) and _fresh(th or sp, shown):
        try:
            msg = g.post(sp, shown, th or "")
            if not th and msg:
                th = _thread_of(msg)
                core.note_thread_root(th, name, shown, channel, convo)
        except Exception as e:  # noqa: BLE001
            _log(f"note post failed for {ref}: {type(e).__name__}: {str(e)[:120]}")
    quiet = not msg
    core.append(core.DATA / "outbox.jsonl", {"ts": core.stamp(), "to": convo[3:] if str(convo).startswith("dm:") else "space",
                                             "text": shown or text, "via": channel, "convo": convo, "thread": th or "", "ref": ref,
                                             "note": True, **({"quiet": True} if quiet else {})})
    core.say(convo, name, shown or text, channel=channel, meta={"thread": th or "", "ref": ref, "note": True, **({"held": True} if quiet else {})})
    if ref:
        d[ref] = {**row, "name": row.get("name", ""), "convo": convo, "thread": th or "", "space": space or row.get("space", ""),
                  "text": row.get("text", ""), "ts": time.time(), "edits": int(row.get("edits") or 0), "notes": n + (1 if msg else 0)}
        _save(d)
    return msg


def _voice(text: str, channel: str) -> str:
    try:
        import voice
        return voice.gate(text, channel)
    except Exception as e:  # noqa: BLE001
        _log(f"voice gate failed: {type(e).__name__}: {str(e)[:120]}")
        return str(text or "").strip()


def _fresh(space: str, text: str) -> bool:
    try:
        import voice
        return voice.fresh(space, text)
    except Exception:  # noqa: BLE001
        return True


def _thread_of(message_name: str) -> str:
    import re
    m = re.match(r"(spaces/[^/]+)/messages/([^/.]+)", str(message_name or ""))
    return f"{m.group(1)}/threads/{m.group(2)}" if m else ""


def _log(msg: str) -> None:
    try:
        import bridge
        bridge.log(msg)
    except Exception:  # noqa: BLE001
        pass


def poster():
    """The callable commitments.check takes: post(convo, thread, text, ref='')."""
    def post(convo: str, thread: str, text: str, ref: str = "") -> None:
        say(ref, convo, thread, text)
    return post


def status(ref: str) -> dict:
    return _load().get(str(ref or "")) or {}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for k, v in _load().items():
        print(f"{k}: {v.get('text', '')[:100]}  ({v.get('edits', 0)} edits, {v.get('convo')})")
