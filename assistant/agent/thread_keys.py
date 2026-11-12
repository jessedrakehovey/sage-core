"""thread_keys.py - one Google Chat thread per subject, named by its first message.

the owner, 2026-11-04: people in the team spaces were lost in the hundreds of threads the agents opened, because
every post with no thread of its own started a new one in a threaded space. Now a post with no thread is given
one by SUBJECT:

    <subject>-<token>     the first org-defined subject the words name (org.json chat_thread_subjects, in order:
                          e.g. a ticket number for 30 days, a client or a unit for 7)
    day-<slug>-20261104   else the agent's own thread for the day in that space

The first post under a key goes out as a one-line HEADER ("B43 · WO 184741-5 · faucet") posted with Google's
`thread.threadKey`, so the key itself pins the thread even when this registry is lost; the real message and every
later post about the subject are replies in it. The header is the thread's label: the first line is the only
"label" Google Chat has, and the app can edit the messages it wrote (`retitle` / a mark such as "✅ done").
A thread ages out after its subject's idle days (a new generation gets a new key). A DM is never keyed.

Subjects are DATA (org.json), never code: `chat_thread_subjects` = [{"name": "ticket", "regex": "\\b(\\d{6}-\\d{1,2})\\b",
"days": 30, "label": "WO {0}"}, ...]; the engine ships none, so a fresh organization gets one thread per agent per day
per space until it names its subjects. `chat_thread_keys: false` turns the whole thing off.

Pure bookkeeping plus one JSON registry (DATA/thread_keys.json); the posting stays in gchat.post / post_body.

    python assistant/agent/thread_keys.py        # print the registry
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core  # noqa: E402

MENTION_RE = re.compile(r"<users/[^>]+>|@\w+")
SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")

DAY_DAYS = 1
MAX_KEY = 60        # Google's threadKey is opaque; keep it short and plain
TITLE_MAX = 90
REGISTRY_MAX = 400
REGISTRY = core.DATA / "thread_keys.json"


def _orgcfg():
    try:
        import orgcfg
        return orgcfg
    except Exception:  # noqa: BLE001
        return None


def enabled() -> bool:
    o = _orgcfg()
    v = o.get("chat_thread_keys") if o else None
    return True if v is None else bool(v)


def subjects() -> list:
    """The organization's subject patterns, in priority order, each
    {"name", "regex", "days", "label"} (label formats the matched groups)."""
    o = _orgcfg()
    raw = (o.get("chat_thread_subjects") if o else None) or []
    out = []
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict) or not s.get("name") or not s.get("regex"):
            continue
        try:
            rx = re.compile(str(s["regex"]), re.I)
        except re.error:
            continue
        out.append({"name": SAFE_RE.sub("-", str(s["name"]))[:20], "rx": rx,
                    "days": max(1, int(s.get("days") or 7)), "label": str(s.get("label") or "{0}")})
    return out


def safe_key(s: str) -> str:
    return SAFE_RE.sub("-", str(s or "")).strip("-")[:MAX_KEY]


def _matches(text: str) -> list:
    """[(subject, token, label)] for every subject the words name, in order."""
    out = []
    text = str(text or "")
    for s in subjects():
        m = s["rx"].search(text)
        if not m:
            continue
        groups = [g for g in m.groups() if g] or [m.group(0)]
        tok = re.sub(r"\s+", "", groups[0]).upper()
        try:
            label = s["label"].format(*([tok] + groups[1:]))
        except (IndexError, KeyError, ValueError):
            label = tok
        out.append((s, tok, label, m.group(0)))
    return out


def base_key(text: str = "", slug: str = "agent", day: datetime | None = None) -> tuple:
    """(base key, idle days) for the words: the first subject named, else the agent's day."""
    for s, tok, _label, _raw in _matches(text):
        return safe_key(f"{s['name']}-{tok}"), s["days"]
    d = (day or datetime.now()).strftime("%Y%m%d")
    return safe_key(f"day-{slug}-{d}"), DAY_DAYS


def body_words(body) -> str:
    """Every string in a Chat message body (text, card header, section text, button labels)."""
    out = []

    def walk(x, depth=0):
        if depth > 12 or len(out) > 80:
            return
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            for k, v in x.items():
                if k in ("url", "name", "cardId", "thread", "imageUrl", "altText", "onClick", "requestId"):
                    continue
                walk(v, depth + 1)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v, depth + 1)
    walk(body)
    return " ".join(out)[:4000]


def title_for(text: str, slug: str = "agent", agent: str = "", day: datetime | None = None) -> str:
    """The header line that names the thread: the subject labels, then the first line of the
    message with those tokens taken out; or '<Agent> · Tuesday 11/04' for the day thread."""
    hits = _matches(text)
    if not hits:
        d = day or datetime.now()
        return f"{agent or slug.title()} · {d.strftime('%A')} {d.month}/{d.day:02d}"
    topic = MENTION_RE.sub("", str(text or "")).strip()
    topic = re.sub(r"\s+", " ", topic.splitlines()[0].strip()) if topic else ""
    labels = []
    for _s, _tok, label, raw in hits:
        if label not in labels:
            labels.append(label)
        topic = re.sub(r"(?i)\b(?:WO\s*)?" + re.escape(raw) + r"\b[\s:·,.-]*", "", topic).strip()
    topic = topic.strip(" ·:-,.")
    bits = list(labels)
    if topic:
        bits.append(topic[:max(10, TITLE_MAX - sum(len(b) + 3 for b in bits))].rstrip(" ,.;:-"))
    return " · ".join(bits)[:TITLE_MAX + 20]


# ------------------------------------------------------------------ registry

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | None = None) -> str:
    return (d or _now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s or "").replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class Registry:
    """{space: {base_key: {"key", "thread", "header", "title", "mark", "opened", "last", "n", "gen", "day"}}}."""

    def __init__(self, path: Path = REGISTRY):
        self.path = Path(path)
        self._d = None

    def _load(self) -> dict:
        if self._d is None:
            d = core.load(self.path, {})
            self._d = d if isinstance(d, dict) else {}
        return self._d

    def _save(self) -> None:
        d = self._load()
        for space, ents in list(d.items()):
            if isinstance(ents, dict) and len(ents) > REGISTRY_MAX:
                keep = sorted(ents.items(), key=lambda kv: str(kv[1].get("last") or ""))[-REGISTRY_MAX:]
                d[space] = dict(keep)
        try:
            core.save(self.path, d)
        except OSError:
            pass

    def entry(self, space: str, base: str, max_days: int, now: datetime | None = None) -> dict | None:
        """The live entry for (space, base), or None when there is none or it has aged out."""
        ent = (self._load().get(space) or {}).get(base)
        if not isinstance(ent, dict) or not (ent.get("key") or ent.get("thread")):
            return None
        now = now or _now()
        if max_days <= 1:
            if (ent.get("day") or "") != now.astimezone().strftime("%Y%m%d"):
                return None
        else:
            last = _parse(ent.get("last") or ent.get("opened"))
            if last and now - last > timedelta(days=max_days):
                return None
        return ent

    def open(self, space: str, base: str, title: str, now: datetime | None = None) -> dict:
        """A new generation under base: the key Google will see; the header post fills the thread."""
        ents = self._load().setdefault(space, {})
        prev = ents.get(base) if isinstance(ents.get(base), dict) else {}
        gen = int(prev.get("gen") or 0) + 1
        now = now or _now()
        key = base if gen == 1 else safe_key(f"{base}-{now.astimezone().strftime('%Y%m%d')}-{gen}")
        ent = {"key": key, "thread": "", "header": "", "title": title, "mark": "", "opened": _iso(now),
               "last": _iso(now), "n": 0, "gen": gen, "day": now.astimezone().strftime("%Y%m%d")}
        ents[base] = ent
        self._save()
        return ent

    def bind(self, space: str, base: str, thread: str = "", header: str = "") -> dict | None:
        ent = (self._load().get(space) or {}).get(base)
        if not isinstance(ent, dict):
            return None
        if thread and not ent.get("thread"):
            ent["thread"] = thread
        if header and not ent.get("header"):
            ent["header"] = header
        self._save()
        return ent

    def touch(self, space: str, base: str, now: datetime | None = None) -> None:
        ent = (self._load().get(space) or {}).get(base)
        if isinstance(ent, dict):
            ent["last"] = _iso(now)
            ent["n"] = int(ent.get("n") or 0) + 1
            self._save()

    def adopt(self, space: str, thread: str, text: str = "", slug: str = "agent",
              now: datetime | None = None) -> dict | None:
        """A person opened a thread about a subject before the agent did: that thread becomes the
        subject's (no header is posted into a person's thread; the agent follows them there)."""
        if not thread or not str(thread).startswith("spaces/"):
            return None
        base, days = base_key(text, slug, now)
        if base.startswith("day-"):
            return None
        cur = self.entry(space, base, days, now)
        if cur and cur.get("thread"):
            return cur
        ents = self._load().setdefault(space, {})
        prev = ents.get(base) if isinstance(ents.get(base), dict) else {}
        now = now or _now()
        ent = {"key": "", "thread": thread, "header": "", "title": title_for(text, slug), "mark": "",
               "opened": _iso(now), "last": _iso(now), "n": 0, "gen": int(prev.get("gen") or 0) + 1,
               "day": now.astimezone().strftime("%Y%m%d"), "adopted": True}
        ents[base] = ent
        self._save()
        return ent

    def retitle(self, space: str, base: str, title: str, mark: str = "") -> dict | None:
        ent = (self._load().get(space) or {}).get(base)
        if isinstance(ent, dict):
            ent["title"] = title
            ent["mark"] = mark
            self._save()
        return ent

    def dump(self) -> dict:
        return json.loads(json.dumps(self._load()))


if __name__ == "__main__":
    for space, ents in Registry().dump().items():
        print(space)
        for base, e in sorted(ents.items(), key=lambda kv: str(kv[1].get("last") or ""), reverse=True):
            print(f"  {base:28} {e.get('last', '')[:16]} n={e.get('n', 0):<3} gen={e.get('gen', 1)} "
                  f"{'adopted ' if e.get('adopted') else ''}{e.get('title', '')[:60]}")
