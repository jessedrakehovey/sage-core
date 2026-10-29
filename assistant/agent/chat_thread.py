"""chat_thread.py - a Chat thread, and a space's latest lines, as Google Chat itself holds them.

the owner, 2026-10-29 09:47, in a thread under the agent's 09:00 "Have the back taxes gone in yet?": "No you should
know what I'm talking about based on the thread I am replying to a message in / fix that context awareness."
The agent had asked which of three unrelated things he meant, then said it "can't see the message at the top
of a thread". It could not: THIS THREAD SO FAR was built from the agent's own transcript and a remembered
"thread root", and both were incomplete (a line posted by a cron was filed without its thread; a later line in
a thread overwrote the remembered root). Guessing from an incomplete record is the fault.

Now the thread comes from Chat, verbatim, every time an owner writes inside one: ONE read of
`spaces.messages.list` filtered on the thread, with the reader sign-in (space_reader; app auth cannot filter by
thread). The same read gives a top-level message the space's newest lines, so "it" / "they" / "this" resolve to
what is actually on their screen, not to what happens to be on record. The local transcript is the fallback
when Chat cannot be read, and the two are merged so a card's line keeps the richer local text.

  thread_lines(space, thread)   -> rows oldest first, or None when Chat could not be read
  space_tail(space, n)          -> the newest rows of the space (every thread), or None
  render(rows)                  -> the lines the brain reads

Rows: {"name", "ts" (local), "who", "bot", "text", "thread", "root"}. Cached per thread (chat_threads.json, bounded)
so the `chat_thread` tool and the bridge do not read twice in one turn; a new message in the thread refreshes it.
"""
from __future__ import annotations

import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

CHAT = "https://chat.googleapis.com/v1"
CACHE = core.DATA / "chat_threads.json"
MAX_LINES = 40
TAIL_HOURS = 8
CACHE_MAX = 200
FRESH_S = 20                 # a second read of the same thread within this window (the tool after the block) uses the cache


def _log(msg: str) -> None:
    try:
        core.append(core.DATA / "chat_thread_log.jsonl", {"ts": core.stamp(), "msg": str(msg)[:300]})
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ the read
def _list(space: str, filt: str, order: str = "createTime asc", n: int = MAX_LINES) -> list:
    """The raw messages (tests replace this). Raises when Chat cannot be read."""
    import space_reader
    if not space_reader.configured():
        raise RuntimeError("the chat reader is not connected")
    q = urllib.parse.urlencode({"pageSize": str(min(max(n, 1), 100)), "orderBy": order, "filter": filt})
    return (space_reader._get(f"{CHAT}/{space}/messages?{q}") or {}).get("messages") or []


def available() -> bool:
    try:
        import space_reader
        return space_reader.configured()
    except Exception:  # noqa: BLE001
        return False


def _local_ts(create_time: str) -> str:
    try:
        dt = datetime.strptime(str(create_time)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(create_time or "")[:16].replace("T", " ")


def _who(sender: dict) -> tuple[str, bool]:
    bot = str((sender or {}).get("type") or "") == "BOT"
    if bot:
        return core.profile().get("name", "Agent"), True
    p = core.person_for(email=str((sender or {}).get("email") or "").lower(), chat_user=str((sender or {}).get("name") or ""))
    return (p.get("name") if p else "") or str((sender or {}).get("displayName") or "someone"), False


def _card_text(m: dict) -> str:
    """A card message carries no text: its header and first paragraphs."""
    out = []
    for c in m.get("cardsV2") or []:
        card = c.get("card") or {}
        h = card.get("header") or {}
        if h.get("title"):
            out.append(str(h["title"]) + (": " + str(h["subtitle"]) if h.get("subtitle") else ""))
        for s in card.get("sections") or []:
            for w in s.get("widgets") or []:
                t = (w.get("textParagraph") or {}).get("text") or (w.get("decoratedText") or {}).get("text")
                if t:
                    out.append(re.sub(r"<[^>]+>", "", str(t)))
    return "[card] " + " ".join(out)[:1500] if out else "[card]"


def _row(m: dict) -> dict:
    who, bot = _who(m.get("sender") or {})
    text = str(m.get("text") or m.get("formattedText") or "").strip() or (_card_text(m) if m.get("cardsV2") else "")
    if not text and m.get("attachment"):
        text = "[an attachment]"
    text = re.sub(r"<users/[^>]+>", "", text).strip()                 # the raw @mention tags Chat puts in text
    name = str(m.get("name") or "")
    mm = re.match(r"spaces/[^/]+/messages/([^/.]+)\.([^/.]+)$", name)
    return {"name": name, "ts": _local_ts(m.get("createTime", "")), "who": who, "bot": bot, "text": text,
            "thread": str((m.get("thread") or {}).get("name") or ""), "root": bool(mm) and mm.group(1) == mm.group(2)}


def _cache() -> dict:
    return core.load(CACHE, {}) or {}


def _remember(thread: str, rows: list) -> None:
    with core.locked:
        d = _cache()
        d[thread] = {"at": time.time(), "rows": rows[-MAX_LINES:]}
        if len(d) > CACHE_MAX:
            for k in sorted(d, key=lambda k: float(d[k].get("at") or 0))[:len(d) - CACHE_MAX]:
                d.pop(k, None)
        core.save(CACHE, d)


def thread_lines(space: str, thread: str, exclude: str = "", fresh: bool = False) -> list | None:
    """Every message of one thread, oldest first, as Chat holds it; the message named `exclude` (the one
    being answered) left out. None when Chat could not be read (the caller falls back to the transcript).
    The first message on record is remembered as the thread's root (core.note_thread_root), so the
    subject lines of JUST BEFORE THIS and YOUR LAST CARDS name it too."""
    if not thread or not space or not str(space).startswith("spaces/"):
        return None
    c = _cache().get(thread) or {}
    rows = None
    # the cache serves a second read for the SAME message (the tool after the block): a message the cache does
    # not hold yet means the thread grew, and Chat is read again
    if not fresh and c.get("rows") and time.time() - float(c.get("at") or 0) < FRESH_S \
            and exclude and any(r.get("name") == exclude for r in c["rows"]):
        rows = list(c["rows"])
    if rows is None:
        try:
            rows = [_row(m) for m in _list(space, f'thread.name = "{thread}"')]
        except Exception as e:  # noqa: BLE001
            _log(f"thread read failed for {thread.rsplit('/', 1)[-1]}: {type(e).__name__}: {str(e)[:160]}")
            rows = list(c["rows"]) if c.get("rows") else None
            if rows is None:
                return None
        else:
            rows.sort(key=lambda r: r.get("ts", ""))
            _remember(thread, rows)
            if rows and rows[0].get("root") and rows[0].get("text") and not core.thread_root(thread).get("text"):
                core.note_thread_root(thread, rows[0]["who"], rows[0]["text"], "chat", space)
    return [r for r in rows if not exclude or r.get("name") != exclude]


def space_tail(space: str, n: int = 4, exclude: str = "", hours: float = TAIL_HOURS) -> list | None:
    """The newest `n` messages of a space (every thread and the top level), oldest first. None when
    Chat could not be read."""
    if not space or not str(space).startswith("spaces/"):
        return None
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
    try:
        ms = _list(space, f'createTime > "{since}"', order="createTime desc", n=max(n * 3, 8))
    except Exception as e:  # noqa: BLE001
        _log(f"space tail failed for {space}: {type(e).__name__}: {str(e)[:160]}")
        return None
    rows = [_row(m) for m in ms]
    rows = [r for r in rows if r.get("text") and (not exclude or r.get("name") != exclude)]
    rows.sort(key=lambda r: r.get("ts", ""))
    return rows[-n:]


# ------------------------------------------------------------------ the lines the brain reads
def _clip(s: str, n: int = 320) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "..."


def merge_local(rows: list, local: list) -> list:
    """Chat's rows, with the local transcript's richer text for a line that is the same message (a card's
    line carries its id and options locally; Chat shows the rendered words)."""
    out = []
    for r in rows:
        head = _clip(r.get("text"), 60).lower()
        best = next((l for l in local if head and _clip(l.get("text"), 60).lower().startswith(head[:40])), None)
        if best and len(str(best.get("text") or "")) > len(str(r.get("text") or "")):
            r = {**r, "text": best["text"], "who": best.get("who") or r.get("who")}
        out.append(r)
    return out


def render(rows: list, clip: int = 320) -> list:
    return [f"  [{str(r.get('ts') or '')[5:16]}] {r.get('who')}: {_clip(r.get('text'), clip)}" for r in rows]


def subject(rows: list) -> str:
    """A few words that say what a thread is about: its first message."""
    txt = str(rows[0].get("text") or "") if rows else ""
    m = re.match(r"\[card q[0-9a-f]{12} \S* ?\S*\] ([^:\n]{3,70})", txt)
    return _clip(m.group(1) if m else re.sub(r"^\[[^\]]*\]\s*", "", txt), 48)
