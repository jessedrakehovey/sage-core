"""gchat.py - the Google Chat channel: read the agent's Chat app events, post as the app.

Same tunnel-free shape as the property OS's example/example apps: the Chat app is configured with
a Cloud Pub/Sub connection, events land on a topic, and THIS machine pulls the
subscription. Nothing listens on the internet. The app posts back with its own service
account (chat.bot scope), only into spaces a known owner wrote from.

A Chat app receives every message in a DM with it, and messages that @mention it in a
space. So each owner talks to the agent in their own DM, or mentions it in a shared space.

Config (git-ignored): assistant/agent/gchat.local.json
  {"project_id": "...", "subscription": "chat-app-events-sub", "sa_key": "~/.claude/example_agent_sa.json"}
Setup: assistant/agent/GCHAT_SETUP.md. Until the config exists this channel is simply off.

  python assistant/agent/gchat.py --test       # auth + subscription reachable
  python assistant/agent/gchat.py --say "online" --space spaces/XXXX
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import clock  # noqa: E402

CONFIG = core.AGENT / "gchat.local.json"
SCOPES = ["https://www.googleapis.com/auth/chat.bot", "https://www.googleapis.com/auth/pubsub"]
MAX_TEXT = 3800
_TOK = {"v": None, "exp": 0.0}


def cfg() -> dict:
    return core.load(CONFIG, {})


def configured() -> bool:
    c = cfg()
    return bool(c.get("project_id") and c.get("subscription")
                and Path(os.path.expanduser(c.get("sa_key", ""))).exists())


def _token() -> str:
    clock.install()                     # token stamps from Google's time, not a wrong PC clock (2026-11-01)
    if _TOK["v"] and clock.now() < _TOK["exp"] - 60:
        return _TOK["v"]
    from google.oauth2 import service_account
    import google.auth.transport.requests
    creds = service_account.Credentials.from_service_account_file(os.path.expanduser(cfg()["sa_key"]), scopes=SCOPES)
    creds.refresh(google.auth.transport.requests.Request())
    _TOK["v"] = creds.token
    # creds.expiry is a NAIVE UTC datetime; .timestamp() would read it as local time and keep a dead
    # token for hours (seen live 2026-10-05: 401s until the bridge restarted)
    from datetime import timezone
    _TOK["exp"] = creds.expiry.replace(tzinfo=timezone.utc).timestamp() if creds.expiry else clock.now() + 3000
    return _TOK["v"]


def _req(url: str, body=None, method: str = "", idempotent: bool = False) -> dict:
    """PATCH/GET and idempotent POSTs (pull, acknowledge) retry; a message POST that timed out
    may have landed, so it is never retried (no double posts)."""
    data = json.dumps(body).encode() if body is not None else None
    meth = method or ("POST" if data else "GET")
    last = None
    for attempt in range(3):
        r = urllib.request.Request(url, data=data, method=meth,
                                   headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=45) as resp:
                txt = resp.read().decode()
                return json.loads(txt) if txt.strip() else {}
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:      # a token stamped from a stale clock: re-measure, fresh token, once
                clock.reset()
                _TOK["v"] = None
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if meth == "POST" and not idempotent and "timed out" in str(e).lower():
                raise
            time.sleep(2 * (attempt + 1))
    raise last


def parse_event(ev: dict) -> dict | None:
    """One Chat event -> {"email", "chat_user", "name", "space", "dm", "thread", "text"} for a human
    message, else None. Handles the classic event shape and the newer chat.messagePayload shape."""
    payload = ((ev.get("chat") or {}).get("messagePayload")) or ev
    if not ((ev.get("chat") or {}).get("messagePayload")) and ev.get("type") not in ("MESSAGE", None):
        return None
    msg = payload.get("message") or {}
    snd = msg.get("sender") or (ev.get("chat") or {}).get("user") or ev.get("user") or {}
    if not msg or snd.get("type") not in ("HUMAN", None):
        return None
    space = msg.get("space") or payload.get("space") or ev.get("space") or {}
    text = str(msg.get("argumentText") or msg.get("text") or "").strip()
    if not (text or msg.get("attachment")) or not space.get("name"):
        return None
    dm = space.get("type") == "DM" or space.get("spaceType") == "DIRECT_MESSAGE" or bool(space.get("singleUserBotDm"))
    return {"email": str(snd.get("email") or "").lower(), "chat_user": str(snd.get("name") or ""),
            "name": str(snd.get("displayName") or ""), "space": space["name"], "dm": dm,
            "thread": (msg.get("thread") or {}).get("name", ""), "text": text,
            "msg_name": msg.get("name", ""), "attachments": msg.get("attachment") or []}


def parse_click(ev: dict) -> dict | None:
    """A button tap on a card this app posted -> {"email", "chat_user", "name", "space", "msg_name",
    "function", "params"}; None for anything else. Handles the add-on shape
    (chat.buttonClickedPayload + commonEventObject) and the classic CARD_CLICKED shape."""
    chat = ev.get("chat") or {}
    common = ev.get("commonEventObject") or {}
    if chat.get("buttonClickedPayload") is not None:
        payload = chat["buttonClickedPayload"] or {}
        params = dict(common.get("parameters") or {})
        fn = str(common.get("invokedFunction") or "")
        if not fn:
            # Apps Script relay dialect (2026-10-06): the add-on runtime dispatches straight to the
            # named function and sends no invokedFunction, so the relay stamps it; failing that,
            # the button's own parameters say which of our two buttons it was.
            fn = str(payload.get("relayFunction") or ev.get("relayFunction") or "")
        dialog = str(common.get("dialogEventType") or (ev.get("common") or {}).get("dialogEventType") or "")
        if not fn and dialog == "SUBMIT_DIALOG":
            fn = "card_edit_submit"
        if not fn and params.get("qid"):
            fn = "card_answer" if "option" in params else "card_undo"
        snd = chat.get("user") or payload.get("user") or {}
    elif ev.get("type") == "CARD_CLICKED":
        payload = ev
        act = ev.get("action") or {}
        fn = str(act.get("actionMethodName") or act.get("function") or "")
        params = {str(p.get("key")): str(p.get("value", "")) for p in (act.get("parameters") or []) if isinstance(p, dict)}
        snd = ev.get("user") or {}
    else:
        return None
    msg = payload.get("message") or {}
    space = msg.get("space") or payload.get("space") or ev.get("space") or {}
    if not fn or not space.get("name"):
        return None
    form = {}
    for name, v in ((common.get("formInputs") or (ev.get("common") or {}).get("formInputs") or {}) or {}).items():
        vals = ((v or {}).get("stringInputs") or {}).get("value") or []
        if not vals and (v or {}).get("dateTimeInput"):     # a date-time picker (the Send later dialog, 2026-11-04)
            vals = [str((v or {}).get("dateTimeInput", {}).get("msSinceEpoch") or "")]
        form[str(name)] = str(vals[0]) if vals else ""
    return {"email": str(snd.get("email") or "").lower(), "chat_user": str(snd.get("name") or ""),
            "name": str(snd.get("displayName") or ""), "space": space["name"],
            "msg_name": str(msg.get("name") or ""), "function": fn, "params": {k: str(v) for k, v in params.items()},
            "dialog": str((common.get("dialogEventType") or (ev.get("common") or {}).get("dialogEventType")) or ""), "form": form}


def parse_home(ev: dict) -> dict | None:
    """The owner opened the app's HOME tab (relay function onAppHome: no payload, only chat.user) -> {"email", "chat_user",
    "name"}; None for anything else."""
    if str(ev.get("relayFunction") or "") != "onAppHome":
        return None
    snd = (ev.get("chat") or {}).get("user") or {}
    return {"email": str(snd.get("email") or "").lower(), "chat_user": str(snd.get("name") or ""), "name": str(snd.get("displayName") or "")}


def parse_command(ev: dict) -> dict | None:
    """A registered slash or quick command (add-on shape chat.appCommandPayload) -> {"id", "type", "text", "args",
    "email", "chat_user", "name", "space", "thread", "dm", "msg_name"}; None otherwise."""
    chat = ev.get("chat") or {}
    p = chat.get("appCommandPayload")
    if not isinstance(p, dict):
        return None
    meta = p.get("appCommandMetadata") or {}
    msg = p.get("message") or {}
    snd = msg.get("sender") or chat.get("user") or {}
    space = msg.get("space") or p.get("space") or {}
    text = str(msg.get("text") or "").strip()
    args = str(msg.get("argumentText") or "").strip()
    if not args and text.startswith("/"):
        args = text.split(" ", 1)[1].strip() if " " in text else ""
    return {"id": meta.get("appCommandId"), "type": str(meta.get("appCommandType") or ""), "text": text, "args": args,
            "email": str(snd.get("email") or "").lower(), "chat_user": str(snd.get("name") or ""), "name": str(snd.get("displayName") or ""),
            "space": str(space.get("name") or ""), "thread": str((msg.get("thread") or {}).get("name") or ""),
            "dm": space.get("type") == "DM" or space.get("spaceType") == "DIRECT_MESSAGE", "msg_name": str(msg.get("name") or "")}


def parse_added(ev: dict) -> dict | None:
    """Someone added the app (ADDED_TO_SPACE) -> {"email", "chat_user", "space", "dm"}; else None.
    Handles the classic shape and the add-on shape (chat.addedToSpacePayload)."""
    chat = ev.get("chat") or {}
    if chat.get("addedToSpacePayload") is not None:
        space = (chat["addedToSpacePayload"] or {}).get("space") or {}
        user = chat.get("user") or {}
    elif ev.get("type") == "ADDED_TO_SPACE":
        space, user = ev.get("space") or {}, ev.get("user") or {}
    else:
        return None
    if not space.get("name"):
        return None
    dm = space.get("type") == "DM" or space.get("spaceType") == "DIRECT_MESSAGE" or bool(space.get("singleUserBotDm"))
    return {"email": str(user.get("email") or "").lower(), "chat_user": str(user.get("name") or ""),
            "space": space["name"], "dm": dm}


def record_added(a: dict) -> str:
    """An OWNER who adds the app in a DM gets that DM recorded (assistant/data/agent/
    gchat_spaces.json), so cards reach them at once - nobody has to write to Sage first.
    A shared SPACE the app is added to works the same minute (the owner 2026-10-27, "I want Sage to work instantly
    in any google space I add it to"): the space list is re-read now instead of in ten minutes, the space's
    watermark starts at the add so nothing written there is missed, its members are fetched for @-tags, and one
    line says it is there. -> the owner's key, or '' (not an owner / not a DM: nothing recorded)."""
    space = str(a.get("space", ""))
    if not space.startswith("spaces/"):
        return ""
    person = core.person_for(email=a.get("email", ""), chat_user=a.get("chat_user", ""))
    if not a.get("dm"):
        _space_added(space, person)
        return ""
    if not person:
        return ""
    path = core.DATA / "gchat_spaces.json"
    sp = core.load(path, {})
    if sp.get(person["key"]) != a["space"]:
        sp[person["key"]] = a["space"]
        core.save(path, sp)
    return person["key"]


ADDED = core.DATA / "gchat_added.json"       # {space: {"ts", "by", "greeted"}}: every shared space the app was added to


def _space_added(space: str, person: dict | None) -> None:
    d = core.load(ADDED, {}) or {}
    if space in d:
        return
    d[space] = {"ts": core.stamp(), "by": str((person or {}).get("key") or "")}
    core.save(ADDED, d)
    try:
        import space_reader
        space_reader.invalidate()
        wm = core.load(space_reader.WATERMARKS, {}) or {}
        wm.setdefault(space, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock.now() - 60)))
        core.save(space_reader.WATERMARKS, wm)
    except Exception:  # noqa: BLE001
        pass
    try:
        refresh_members(space, max_age_s=0)
    except Exception:  # noqa: BLE001
        pass
    if person and core.is_owner(person):
        name = core.profile().get("name", "Agent")
        org = core.profile().get("organization") or core.profile().get("household") or "business"
        line = (f"I'm here. In this space I keep to {org.split(';')[0].split('(')[0].strip()} business and answer anyone who names or @mentions me; "
                f"owners can just write. Anything that needs a tap goes to the owners' space.")
        try:
            msg = post(space, line)
            d[space]["greeted"] = bool(msg)
            core.save(ADDED, d)
            core.say(space, name, line, channel="gchat", meta={"added_by": (person or {}).get("key", "")})
        except Exception:  # noqa: BLE001
            pass


UNPARSED = core.DATA / "gchat_unparsed.jsonl"      # events neither parser understood (debugging, capped)
INBOX_HTTP = core.DATA / "gchat_inbox.jsonl"       # messages that arrived over HTTP (mc_server), for the bridge
INBOX_CURSOR = core.DATA / "gchat_inbox_cursor.json"


def queue_inbound(m: dict) -> None:
    """A parsed human message that arrived over the HTTP endpoint; the bridge answers it.
    Chat (and the Apps Script relay) re-deliver an event they think went unanswered, so the same
    message arrived 3-5 times on 2026-10-07 and Sage answered each copy: one copy per message name."""
    name = str(m.get("msg_name") or "")
    if name and any(str(r.get("msg_name") or "") == name for r in core.rows(INBOX_HTTP)[-200:]):
        return
    core.append(INBOX_HTTP, {"ts": core.stamp(), **m})


def take_inbound() -> list:
    """Messages queued by the HTTP endpoint since the last call (bridge side)."""
    cur = core.load(INBOX_CURSOR, {"n": 0})
    rows = core.rows(INBOX_HTTP)
    n = int(cur.get("n", 0))
    if n > len(rows):                                   # the file was rotated or removed
        n = 0
    out = rows[n:]
    if out or n != len(rows):
        core.save(INBOX_CURSOR, {"n": len(rows)})
    return [r for r in out if isinstance(r, dict) and r.get("space")]



def _note_unparsed(ev: dict) -> None:
    try:
        if UNPARSED.exists() and UNPARSED.stat().st_size > 200_000:
            return
        core.append(UNPARSED, {"ts": core.stamp(), "keys": sorted(ev.keys())[:12], "type": ev.get("type", ""),
                               "chat_keys": sorted((ev.get("chat") or {}).keys())[:8],
                               "event": json.dumps(ev)[:4000]})     # the shape itself, so a new dialect can be parsed
    except Exception:  # noqa: BLE001
        pass


def pull(max_messages: int = 20, with_clicks: bool = False):
    """Pull and acknowledge pending events -> parsed human messages (oldest first).
    with_clicks=True returns (messages, button clicks) instead."""
    c = cfg()
    sub = f"https://pubsub.googleapis.com/v1/projects/{c['project_id']}/subscriptions/{c['subscription']}"
    resp = _req(sub + ":pull", {"maxMessages": max_messages, "returnImmediately": True}, idempotent=True)
    out, clicks, acks = [], [], []
    for rm in resp.get("receivedMessages") or []:
        acks.append(rm.get("ackId"))
        try:
            ev = json.loads(base64.b64decode((rm.get("message") or {}).get("data") or "").decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        m = parse_event(ev)
        if m:
            out.append(m)
            continue
        k = parse_click(ev)
        if k:
            clicks.append(k)
        elif isinstance(ev, dict) and parse_added(ev):
            record_added(parse_added(ev))
        elif isinstance(ev, dict):
            _note_unparsed(ev)
    if acks:
        _req(sub + ":acknowledge", {"ackIds": acks}, idempotent=True)
    return (out, clicks) if with_clicks else out


def to_gchat(text: str) -> str:
    """Markdown -> Google Chat markup (Chat renders *bold* and _italic_, has no headers, and
    shows a literal ** as noise). Same conversion example's bridge applies to every reply."""
    t = str(text or "")
    t = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", t, flags=re.M)        # headers -> bold
    t = re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", t)                  # **b** -> *b*
    t = re.sub(r"__([^_\n]+)__", r"*\1*", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", t)  # [text](url) -> <url|text>
    t = re.sub(r"^\s*[-\u2022]\s+", "\u2022 ", t, flags=re.M)        # bullets
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def thread_of(message_name: str) -> str:
    """spaces/S/messages/ID.ID -> spaces/S/threads/ID ('' when the name is not a message)."""
    m = re.match(r"(spaces/[^/]+)/messages/([^/.]+)", str(message_name or ""))
    return f"{m.group(1)}/threads/{m.group(2)}" if m else ""


def get(message_name: str) -> dict:
    """One Chat message by name: {"text", "sender", "bot", "createTime", "thread"}; {} on any failure."""
    try:
        d = _req(f"https://chat.googleapis.com/v1/{message_name}", idempotent=True)
    except Exception:  # noqa: BLE001
        return {}
    s = d.get("sender") or {}
    return {"text": str(d.get("text") or d.get("formattedText") or (d.get("cardsV2") and "[card]") or ""),
            "sender": str(s.get("displayName") or s.get("name") or ""), "bot": str(s.get("type") or "") == "BOT",
            "createTime": str(d.get("createTime") or ""), "thread": str((d.get("thread") or {}).get("name") or "")}


# ------------------------------------------------------------------ tagging (ported from example's chat_app, 2026-10-26)
MEMBERS_CACHE = core.DATA / "gchat_members.json"     # {space: {users/ID: displayName}, "_ts": {space: t}}


def _is_dm(space: str) -> bool:
    """A DM with the app (the bridge records each person's DM space). Nobody is tagged in a DM."""
    return bool(space) and space in core.load(core.DATA / "gchat_spaces.json", {}).values()


def refresh_members(space: str, max_age_s: int = 3600) -> dict:
    """{users/ID: displayName} for the space's humans, via app auth. Cached an hour."""
    c = core.load(MEMBERS_CACHE, {})
    ts = (c.get("_ts") or {}).get(space, 0)
    if space in c and time.time() - float(ts) < max_age_s:
        return c[space]
    names = {}
    try:
        r = _req(f"https://chat.googleapis.com/v1/{space}/members?pageSize=100", idempotent=True)
        for m in r.get("memberships") or []:
            mem = m.get("member") or {}
            if mem.get("type") == "HUMAN" and mem.get("name"):
                names[mem["name"]] = mem.get("displayName") or mem["name"]
        c[space] = names
        c.setdefault("_ts", {})[space] = time.time()
        core.save(MEMBERS_CACHE, c)
    except Exception:  # noqa: BLE001
        names = c.get(space) or {}
    return names


def _tag_people(space: str = "") -> dict:
    """{users/ID: {name, never_tag}} for every person in people.json whose Chat id is known: the
    pinned chat_user (core.person_for pins it the first time they write), else the space's member
    whose display name starts with their name."""
    out, members = {}, None
    for key, u in (core.people().get("users") or {}).items():
        name = str(u.get("name") or key).strip()
        uid = str(u.get("chat_user") or "")
        if not uid.startswith("users/") and space:
            if members is None:
                members = refresh_members(space)
            hits = [i for i, dn in members.items() if str(dn).lower().split()[:1] == [name.lower().split()[0]]]
            uid = hits[0] if len(hits) == 1 else ""
        if name and re.fullmatch(r"users/\d{5,30}", uid):
            out[uid] = {"name": name, "never_tag": bool(u.get("never_tag"))}
    return out


def scrub_never_tag(text: str, space: str = "") -> str:
    """Strip every <users/ID> mention and plain "@First" of a never_tag person (people.json
    users.<key>.never_tag). No one is never_tag today; the hook is example's, kept for parity."""
    nt = {uid: p["name"] for uid, p in _tag_people(space).items() if p["never_tag"]}
    if not nt or not text:
        return text
    pats = []
    for uid, name in nt.items():
        pats += [re.escape(f"<{uid}>"), r"@" + re.escape(name) + r"\b", r"@" + re.escape(name.split()[0]) + r"\b"]
    rx = re.compile("|".join(pats), re.I)
    return re.sub(r"[ \t]{2,}", " ", rx.sub("", text)).strip() if rx.search(text) else text


def mentionize(text: str, space: str = "") -> str:
    """Turn a typed name into a REAL Google Chat tag, the same way example does (the owner 2026-10-02 on
    the property side: "you need to tag people when you address them @"). A real mention is
    <users/ID>; plain "@a team member" notifies nobody. Runs on every message the app posts (post / patch),
    so it does not depend on the model remembering the syntax.

    Converted, for every person in people.json (full name or first name):
      "@a team member" / "@owner example"           anywhere in the text
      "a team member, ..." / "owner: ..."         a name that OPENS the message or a line
    Left alone: possessives ("a team member's note"), names mid-sentence, and anything in a DM."""
    if not text or (space and _is_dm(space)):
        return text
    for uid, p in _tag_people(space).items():
        name = p["name"]
        forms = sorted({name, name.split()[0]}, key=len, reverse=True)
        alt = "|".join(re.escape(f) for f in forms)
        tag = f"<{uid}>"
        # "@Name" anywhere (never a possessive, never part of a longer word)
        text = re.sub(r"@(?:" + alt + r")(?![A-Za-z'’])", tag, text, flags=re.I)
        # a name that opens the message or a line, followed by , or :
        text = re.sub(r"(?m)^(\s*)(?:" + alt + r")(?=[,:]\s)", lambda m: m.group(1) + tag, text, flags=re.I)
    return scrub_never_tag(text, space)


def ensure_mention(text: str, space: str, user_id: str) -> str:
    """Make sure a message addressed to `user_id` in a group space tags them (a reply to whoever
    asked). Left as is when it already tags someone, or in a DM."""
    if not text or not str(user_id or "").startswith("users/") or (space and _is_dm(space)):
        return text
    text = mentionize(text, space)
    if "<users/" in text:
        return text
    return scrub_never_tag(f"<{user_id}> {text}", space)


_REPLY_OPT = "messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"


def _tk():
    import thread_keys
    return thread_keys


def _slug() -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(core.profile().get("name") or "agent").lower()).strip("-") or "agent"


def _with_thread(space: str, body: dict, thread: str = "", key: str = "") -> tuple:
    """(url, body) for one message create. A thread given is replied in. None given (the owner
    2026-11-04, "hundreds of chat threads"): the post is keyed by its SUBJECT (thread_keys) -- the
    first org-defined subject its words name, else this agent's thread for the day in that space.
    The first post under a key sends a one-line header that names the thread, so threads label
    themselves; later posts about the subject reply in it. `key` pins a subject the words would not
    show. Never in a DM; off with org.json chat_thread_keys=false."""
    url = f"https://chat.googleapis.com/v1/{space}/messages"
    body = dict(body)
    if thread:
        body["thread"] = {"name": thread}
        return url + "?" + _REPLY_OPT, body
    try:
        tk = _tk()
        if _is_dm(space) or not tk.enabled():
            return url, body
        ent = _keyed_thread(space, body, key)
    except Exception as e:  # noqa: BLE001
        print(f"[gchat] thread_keys: {type(e).__name__}: {str(e)[:120]} (posting unthreaded)", file=sys.stderr)
        return url, body
    if ent and ent.get("thread"):
        body["thread"] = {"name": ent["thread"]}
    elif ent and ent.get("key"):
        body["thread"] = {"threadKey": ent["key"]}
    else:
        return url, body
    return url + "?" + _REPLY_OPT, body


def _keyed_thread(space: str, body: dict, key: str = "") -> dict | None:
    """The registry entry whose thread this post belongs in, opening it (and posting its header)
    when the subject has none alive. The header carries thread.threadKey, so the key pins the
    thread even when the registry is later lost; the entry then holds the thread name."""
    tk = _tk()
    words = tk.body_words(body)
    slug = _slug()
    if key:
        base, days = tk.safe_key(key), 30
    else:
        base, days = tk.base_key(words, slug)
    reg = tk.Registry()
    ent = reg.entry(space, base, days)
    if ent:
        reg.touch(space, base)
        return ent
    title = tk.title_for(words, slug, str(core.profile().get("name") or ""))
    if key and tk.base_key(words, slug)[0].startswith("day-"):
        title = key                     # the words name no subject: the pinned key is the label
    ent = reg.open(space, base, title)
    try:
        r = _req(f"https://chat.googleapis.com/v1/{space}/messages?{_REPLY_OPT}",
                 {"text": title, "thread": {"threadKey": ent["key"]}}) or {}
        reg.bind(space, base, thread=str((r.get("thread") or {}).get("name") or ""), header=str(r.get("name") or ""))
    except Exception:  # noqa: BLE001
        pass                            # the message itself still carries the threadKey
    reg.touch(space, base)
    return reg.entry(space, base, days) or ent


def adopt_thread(space: str, thread: str, text: str = "") -> bool:
    """A person opened (or continued) a thread about a subject: the agent's own keyed posts about
    it follow them there, with no header."""
    space = space or str(thread or "").split("/threads/")[0]
    if not space or not thread or "/threads/" not in str(thread) or _is_dm(space):
        return False
    try:
        return bool(_tk().Registry().adopt(space, thread, text, _slug()))
    except Exception:  # noqa: BLE001
        return False


def thread_title(space: str, subject: str, title: str = "", mark: str | None = None) -> bool:
    """Edit the header line that names a subject's thread. `subject` is a key or any text that
    names one; `title` replaces the line; `mark` goes before it ("✅ done") and never stacks.
    Only a header the app posted can be edited (an adopted thread has none)."""
    tk = _tk()
    reg = tk.Registry()
    ents = reg.dump().get(space) or {}
    base = tk.safe_key(subject) if subject in ents else tk.base_key(subject, _slug())[0]
    ent = ents.get(base)
    if not ent or not ent.get("header"):
        return False
    base_title = title or ent.get("title") or ""
    mark = (mark if mark is not None else ent.get("mark") or "").strip()
    shown = f"{mark} {base_title}".strip()
    if not shown:
        return False
    try:
        _req(f"https://chat.googleapis.com/v1/{ent['header']}?updateMask=text", {"text": shown[:MAX_TEXT]}, method="PATCH")
    except Exception:  # noqa: BLE001
        return False
    reg.retitle(space, base, base_title, mark)
    return True


def _laid_out(text: str) -> tuple:
    """(opening, card) for a long text, (text, None) for anything else; a fault in the layout never stops a post."""
    try:
        import layout
        return layout.split(text)
    except Exception:  # noqa: BLE001
        return str(text or ""), None


def post(space: str, text: str, thread: str = "", key: str = "") -> str:
    """Post as the app. Returns the first message's resource name ('' when nothing was posted),
    so a placeholder can later be edited into the answer with patch(). No thread given: the post
    joins its subject's thread (_with_thread); `key` pins the subject. A long text goes up laid out (layout.py):
    its opening as the message, the rest in a card under it with each part folded behind Show more."""
    if not str(text or "").strip() or not space.startswith("spaces/"):
        return ""
    lead, card = _laid_out(text)
    if card:
        try:
            url, body = _with_thread(space, {"text": mentionize(to_gchat(lead), space)[:MAX_TEXT], "cardsV2": [card]}, thread, key)
            r = _req(url, body)
            return str(r.get("name") or "posted")
        except urllib.error.HTTPError as e:
            if e.code != 400:           # Chat refused the card itself: the same words go up as plain text, below
                raise
    text = mentionize(to_gchat(text), space)
    if not text:
        return ""
    first = ""
    for ch in [text[i:i + MAX_TEXT] for i in range(0, len(text), MAX_TEXT)]:
        url, body = _with_thread(space, {"text": ch}, thread, key)
        r = _req(url, body)
        first = first or str(r.get("name") or "posted")
        if not thread:              # the rest of a long text stays in the thread the first chunk opened
            thread = str((r.get("thread") or {}).get("name") or "") or thread
    return first


def post_body(space: str, body: dict, thread: str = "", key: str = "") -> str:
    """Post a ready-made message body (cards with buttons) as the app. Returns the message name."""
    if not isinstance(body, dict) or not space.startswith("spaces/"):
        return ""
    url, body = _with_thread(space, body, thread, key)
    r = _req(url, body)
    return str(r.get("name") or "")


def patch_body(name: str, body: dict) -> bool:
    """Replace a card message this app posted (buttons -> the answer)."""
    if not isinstance(body, dict) or not name.startswith("spaces/") or "/messages/" not in name:
        return False
    mask = ",".join(k for k in ("text", "cardsV2") if k in body)
    _req(f"https://chat.googleapis.com/v1/{name}?updateMask={mask}", body, method="PATCH")
    return True


def patch(name: str, text: str) -> bool:
    """Edit a message this app posted (the typing placeholder becomes the answer). Text past
    one message's limit continues in a new message in the same space. A long text is laid out as in post()."""
    if not str(text or "").strip() or not name.startswith("spaces/") or "/messages/" not in name:
        return False
    lead, card = _laid_out(text)
    if card:
        try:
            _req(f"https://chat.googleapis.com/v1/{name}?updateMask=text,cardsV2",
                 {"text": mentionize(to_gchat(lead), name.split("/messages/")[0])[:MAX_TEXT], "cardsV2": [card]}, method="PATCH")
            return True
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
    text = mentionize(to_gchat(text), name.split("/messages/")[0])
    if not text:
        return False
    _req(f"https://chat.googleapis.com/v1/{name}?updateMask=text", {"text": text[:MAX_TEXT]}, method="PATCH")
    if len(text) > MAX_TEXT:
        post(name.split("/messages/")[0], text[MAX_TEXT:])
    return True


def find_dm(user: str) -> str:
    """The DM space between this app and `user` ('users/<id>' or an email), once that person has
    added the app; '' when there is none yet. App auth (chat.bot) may call spaces.findDirectMessage,
    so an owner's first card can reach them before they ever write to the app."""
    u = user if str(user).startswith("users/") else "users/" + str(user)
    try:
        r = _req("https://chat.googleapis.com/v1/spaces:findDirectMessage?" + urllib.parse.urlencode({"name": u}))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ""
        raise
    name = str(r.get("name") or "")
    return name if name.startswith("spaces/") else ""


def delete(name: str) -> bool:
    """Take down a message this app posted (a typing placeholder that turned into silence)."""
    if not name.startswith("spaces/") or "/messages/" not in name:
        return False
    try:
        _req(f"https://chat.googleapis.com/v1/{name}", method="DELETE")
        return True
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    if not configured():
        print("NOT_CONFIGURED (see assistant/agent/GCHAT_SETUP.md)")
    elif a[:1] == ["--test"]:
        c = cfg()   # a pull that is never acknowledged: proves the Subscriber grant, consumes nothing (it redelivers)
        r = _req(f"https://pubsub.googleapis.com/v1/projects/{c['project_id']}/subscriptions/{c['subscription']}:pull",
                 {"maxMessages": 1, "returnImmediately": True}, idempotent=True)
        print(f"OK auth + subscription reachable (waiting={len(r.get('receivedMessages') or [])})")
    elif a[:1] == ["--say"] and "--space" in a:
        print("POSTED" if post(a[a.index("--space") + 1], a[1]) else "NOT_POSTED")
