"""settings.py - every setting an owner may change, in one registry, for the Sage app's Settings screen.

The app's window (assistant/app.py) had set-up steps and a home screen and no way to change anything afterwards: who the
agent answers, what a team member may do, when decisions arrive, how hard it works. Those all existed, as fields in three
files. This module is the one door to them:

  REGISTRY   one row per setting: where it lives (file + dotted path), its type, its bounds and the words an owner reads.
             A row is only added for a field some engine code READS (the reader is named on the row); a setting nothing
             reads is a lie on a screen. A new setting is a new row here, never a new screen.
  people     the organization's structure: who the agent answers, each person's role, what they handle and who they
             report to; and the roles themselves (what a role may do, which lanes and which folders of notes it sees).
             Only an owner approves: `approve`, `teach` and `undo` cannot be given to another role from here.
  features   what the agent handles (the same gate the first chat question answers: onboarding.apply_features).
  grants     the standing yeses an owner gave on a card: listed, and revocable. Nothing here GIVES one.
  locked     the guardrails, shown and not changeable: a send needs an owner's tap, the undo window, quiet hours,
             look-only money. They are code, not settings.

Every change is validated, written in place (the file keeps its layout and line endings), appended to
data/agent/settings_changes.jsonl with what it was before, and entered in the agent's own change ledger
(core.log_change, undo op `settings_undo`), so "what changed" and "undo" in chat cover a change made in the window.

  python assistant/settings.py                      # every setting and its value, as the window draws it (JSON)
  python assistant/settings.py set <key> <json>     # one setting
  python assistant/settings.py call                 # one {"op": ...} request as JSON on stdin (what the app sends)
  python assistant/settings.py undo <id>
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ["SAGE_SETTINGS_ROOT"]) if os.environ.get("SAGE_SETTINGS_ROOT") else HERE
for _p in (HERE, HERE / "agent", HERE / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import orgcfg  # noqa: E402,F401   (applies this pack's own state folder to the environment)
import core  # noqa: E402

FILES = {"config": ROOT / "config.json", "profile": ROOT / "agent" / "profile.json"}
LEDGER = core.DATA / "settings_changes.jsonl"
BY = "the Sage app"
DAYS = [("mon", "Monday"), ("tue", "Tuesday"), ("wed", "Wednesday"), ("thu", "Thursday"), ("fri", "Friday"), ("sat", "Saturday"), ("sun", "Sunday")]
MODELS = [("haiku", "Fastest"), ("sonnet", "Balanced"), ("opus", "Most capable")]
QUIET = (21, 7)                                   # scripts/exec_store.py QUIET_START / QUIET_END: the send gate's own hours


def _hours(lo: int, hi: int) -> list:
    return [(h, time.strftime("%I:%M %p", time.strptime(f"{h}:00", "%H:%M")).lstrip("0")) for h in range(lo, hi + 1)]


GROUPS = [
    ("org", "Organization", "What the agent knows about who it works for. It reads these before every answer."),
    ("people", "People and roles", "Who the agent answers, what each person may do, and who reports to whom."),
    ("speaks", "When it speaks", "How decisions and reminders reach you."),
    ("handles", "What it handles", "The parts of your work the agent looks after. Turn one off and it stops asking about it."),
    ("work", "How it works", "Which Claude model it thinks with, and its daily limits."),
    ("computer", "On this computer", "How it behaves when it uses a browser on this PC."),
    ("guard", "Guardrails", "Rules enforced by code. They are shown here so you can see them; they cannot be switched off."),
]

# key, group, label, help, type, source file, dotted path, default (the READER's own default), extras, reader
REGISTRY = [
    dict(key="org.what_we_do", group="org", label="What you do", type="text", src="org", path="what_we_do", max=400,
         help="One or two lines, as you would tell a new hire.", reader="agent/org.py render"),
    dict(key="org.where", group="org", label="Where you work", type="text", src="org", path="where", max=400,
         help="Your town, your service area, or \"online\".", reader="agent/org.py render"),
    dict(key="org.hours", group="org", label="Your working hours", type="text", src="org", path="hours", max=400,
         help="When to speak up and when to leave you alone, in your own words.", reader="agent/org.py render"),
    dict(key="org.tone", group="org", label="How you like to be talked to", type="text", src="org", path="tone", max=400,
         help="Short and plain? Warm? First names? It follows this in every reply to you.", reader="agent/org.py render"),

    dict(key="cards.delivery", group="speaks", label="Decisions that can wait", type="choice", src="config", path="cards.delivery", default="now",
         choices=[("weekly", "Together, once a week"), ("now", "One at a time, as they come up")],
         help="Urgent ones, like a new lead, always reach you at once.", reader="scripts/cards.py delivery_policy"),
    dict(key="cards.digest_day", group="speaks", label="Weekly list arrives on", type="choice", src="config", path="cards.digest_day", default="mon",
         choices=DAYS, show_if=("cards.delivery", "weekly"), reader="scripts/cards.py delivery_policy"),
    dict(key="cards.digest_hour", group="speaks", label="At", type="choice", src="config", path="cards.digest_hour", default=8,
         choices=_hours(QUIET[1], QUIET[0] - 1), show_if=("cards.delivery", "weekly"), reader="scripts/cards.py delivery_policy"),
    dict(key="cards.carry_over_weeks", group="speaks", label="Keep an unanswered decision for", type="choice", src="config", path="cards.carry_over_weeks", default=1,
         choices=[(0, "That week only"), (1, "One more week"), (2, "Two more weeks"), (4, "Four more weeks")], show_if=("cards.delivery", "weekly"),
         help="After that it is dropped, not asked again.", reader="scripts/cards.py delivery_policy"),
    dict(key="cards.say_empty", group="speaks", label="Tell me when there is nothing to decide", type="bool", src="config", path="cards.say_empty", default=True,
         show_if=("cards.delivery", "weekly"), reader="scripts/cards.py delivery_policy"),
    dict(key="exec.quiet_holds_approved", group="speaks", label="Hold what I approve at night until the morning", type="bool", src="config",
         path="exec.quiet_holds_approved", default=False,
         help=f"Off: a message you approve yourself between {QUIET[0] - 12} PM and {QUIET[1]} AM goes when you tap. Nothing goes out by itself in those hours either way.",
         reader="scripts/exec_store.py quiet_exempt"),
    dict(key="followups.window", group="speaks", label="Follow-ups and check-ins go out between", type="window", src="profile", path="followups.window",
         default=["08:00", "20:30"], help="A check-in that falls outside these hours waits for the next morning.", reader="agent/followups.py _window"),
    dict(key="followups.weekdays_only", group="speaks", label="Follow-ups on weekdays only", type="bool", src="profile", path="followups.weekdays_only",
         default=False, reader="agent/followups.py _weekdays_only"),
    dict(key="reminders.events", group="speaks", label="Remind me before calendar events", type="bool", src="config", path="reminders.events", default=True,
         reader="agent/event_reminders.py"),
    dict(key="reminders.events_lead_min", group="speaks", label="How long before", type="choice", src="config", path="reminders.events_lead_min", default=45,
         choices=[(15, "15 minutes"), (30, "30 minutes"), (45, "45 minutes"), (60, "1 hour"), (120, "2 hours")], show_if=("reminders.events", True),
         reader="agent/event_reminders.py"),

    dict(key="model", group="work", label="Everyday answers", type="choice", src="profile", path="model", default="sonnet", choices=MODELS,
         help="The Claude model behind a quick question in chat.", reader="agent/brain.py"),
    dict(key="desk.model", group="work", label="Real work", type="choice", src="profile", path="desk.model", default="opus", choices=MODELS,
         help="The model behind a task it carries out for you: research, a draft, a fix.", reader="agent/desk.py cfg"),
    dict(key="dream_model", group="work", label="The nightly review", type="choice", src="profile", path="dream_model", default="opus", choices=MODELS,
         help="Each night it rereads the day and saves what it learned.", reader="agent/dream.py"),
    dict(key="asks_per_day_max", group="work", label="Messages it answers in a day, at most", type="int", src="profile", path="asks_per_day_max", default=150,
         min=20, max=2000, step=10, help="A ceiling on your Claude usage. Past it, the agent says so and waits for tomorrow.", reader="agent/brain.py"),
    dict(key="routines.max_active", group="work", label="Standing routines, at most", type="int", src="profile", path="routines.max_active", default=25,
         min=1, max=100, reader="agent/routines.py"),
    dict(key="routines.llm_runs_per_day_max", group="work", label="Routine runs that need thinking, per day", type="int", src="profile",
         path="routines.llm_runs_per_day_max", default=12, min=0, max=100, reader="agent/routines.py"),
    dict(key="capacity.mode", group="work", label="When work piles up", type="choice", src="profile", path="capacity.mode", default="auto",
         choices=[("auto", "Open extra help by itself"), ("suggest", "Ask me first"), ("off", "Never open extra help")],
         help="Every hour it measures what is waiting. Extra help is a second pass over one kind of work, or a specialist for it.",
         reader="agent/capacity.py cfg"),
    dict(key="capacity.max_lanes", group="work", label="Extra passes at once, at most", type="int", src="profile", path="capacity.max_lanes", default=2,
         min=0, max=6, show_unless=("capacity.mode", "off"), reader="agent/capacity.py"),
    dict(key="capacity.max_specialists", group="work", label="Specialists at once, at most", type="int", src="profile", path="capacity.max_specialists",
         default=4, min=0, max=12, show_unless=("capacity.mode", "off"), reader="agent/capacity.py"),

    dict(key="doer.in_front", group="computer", label="Show its browser while it works", type="bool", src="config", path="doer.in_front", default=True,
         help="Off: the browser works out of sight. Either way it stays out of the way while someone is using the PC.", reader="doer/browser.py"),
    dict(key="doer.clear_screen_idle_s", group="computer", label="Someone is \"using the PC\" if they touched it in the last", type="choice", src="config",
         path="doer.clear_screen_idle_s", default=300, choices=[(60, "1 minute"), (300, "5 minutes"), (900, "15 minutes")], show_if=("doer.in_front", True),
         reader="doer/browser.py"),
    dict(key="doer.keep_open", group="computer", label="Keep its browser signed in between jobs", type="bool", src="config", path="doer.keep_open", default=True,
         help="Off: it closes the browser after each job and opens a new one for the next, which is slower.", reader="doer/browser.py"),
    dict(key="doer.live_card", group="computer", label="Show progress in chat while it works on a site", type="bool", src="config", path="doer.live_card",
         default=True, reader="doer/livecard.py"),
    dict(key="doer.runs_per_day_max", group="computer", label="Browser jobs in a day, at most", type="int", src="config", path="doer.runs_per_day_max",
         default=20, min=1, max=200, reader="doer/doer.py"),
]
BY_KEY = {r["key"]: r for r in REGISTRY}

LOCKED = [
    ("Nothing goes out without an owner's tap", "The card shows the whole message, and the tap is tied to that exact text."),
    ("Every approved send waits 35 seconds", "Long enough to take it back. A scheduled one can be undone until the minute it leaves."),
    (f"Nothing goes out by itself between {QUIET[0] - 12} PM and {QUIET[1]} AM", "Anything scheduled or standing waits for the morning. What you approve yourself at night is yours to hold or not, under When it speaks."),
    ("Money is look-only", "It reads balances and transactions. It cannot pay, transfer or sign in to a bank."),
    ("Only an owner approves", "A team member's ask becomes a card for the owners. No role but owner can approve, teach or undo."),
]

CAN_WORDS = {"ask": "Ask it questions", "note": "Add a note to the records", "task": "Hand it a task (an owner approves anything that goes out)"}
GRANTABLE = tuple(CAN_WORDS)                      # what a role other than owner may be given from here
OWNER_CAN = ["ask", "teach", "note", "schedule", "task", "undo", "approve"]


# ------------------------------------------------------------------ JSON, changed in place
def _ws(t: str, i: int) -> int:
    while i < len(t) and t[i] in " \t\r\n":
        i += 1
    return i


def _str_end(t: str, i: int) -> int:
    i += 1
    while t[i] != '"':
        i += 2 if t[i] == "\\" else 1
    return i + 1


def _val_end(t: str, i: int) -> int:
    c = t[i]
    if c == '"':
        return _str_end(t, i)
    if c in "{[":
        depth = 0
        while True:
            c = t[i]
            if c == '"':
                i = _str_end(t, i)
                continue
            if c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
    while i < len(t) and t[i] not in ",}] \t\r\n":
        i += 1
    return i


def _members(t: str, i: int):
    """The members of the object opening at t[i]: (key, value start, value end)."""
    i = _ws(t, i + 1)
    while t[i] != "}":
        ke = _str_end(t, i)
        key = json.loads(t[i:ke])
        vs = _ws(t, _ws(t, ke) + 1)
        ve = _val_end(t, vs)
        yield key, vs, ve
        i = _ws(t, ve)
        if t[i] == ",":
            i = _ws(t, i + 1)


def _locate(t: str, parts: list):
    """(start, end) of the value at the path; (object start, None) when only the last key is missing; None when a parent is."""
    i = _ws(t, 0)
    for n, part in enumerate(parts):
        if i >= len(t) or t[i] != "{":
            return None
        hit = next(((vs, ve) for k, vs, ve in _members(t, i) if k == part), None)
        last = n == len(parts) - 1
        if hit is None:
            return (i, None) if last else None
        if last:
            return hit
        i = hit[0]
    return None


def _dig(d, parts: list, default=None):
    for p in parts:
        if not isinstance(d, dict) or p not in d:
            return default
        d = d[p]
    return d


def _put(d: dict, parts: list, value) -> None:
    for p in parts[:-1]:
        if not isinstance(d.get(p), dict):
            d[p] = {}
        d = d[p]
    d[parts[-1]] = value


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError):
        return {}


def json_set(path: Path, dotted: str, value) -> None:
    """Set one value in a JSON file and leave the rest of the file exactly as it was written (layout, comments kept as
    `_doc` fields, line endings, a byte-order mark). A file that cannot be edited in place is rewritten whole instead; a
    result that does not parse to exactly the intended data is never written."""
    parts = dotted.split(".")
    raw = path.read_bytes() if path.exists() else b"{}"
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    want = json.loads(text)
    _put(want, parts, value)
    nl = "\r\n" if "\r\n" in text else "\n"
    lit = json.dumps(value, ensure_ascii=False)
    new = None
    try:
        loc = _locate(text, parts)
        if loc and loc[1] is not None:
            new = text[:loc[0]] + lit + text[loc[1]:]
        elif loc:
            j = _ws(text, loc[0] + 1)
            member = json.dumps(parts[-1]) + ": " + lit
            if text[j] == "}":
                new = text[:loc[0] + 1] + member + text[j:]
            else:
                line = text.rfind("\n", 0, j) + 1
                pad = text[line:j] if not text[line:j].strip() else None
                new = text[:j] + member + "," + (nl + pad if pad is not None else " ") + text[j:]
        if new is not None and json.loads(new) != want:
            new = None
    except (IndexError, ValueError):
        new = None
    if new is None:
        new = json.dumps(want, indent=1, ensure_ascii=False).replace("\n", nl) + nl
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes((b"\xef\xbb\xbf" if bom else b"") + new.encode("utf-8"))
    for i in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.25 * (i + 1))
    os.replace(tmp, path)


# ------------------------------------------------------------------ the ledger
def _rows() -> list:
    return core.rows(LEDGER)


def _log(summary: str, undo: dict, by: str, cid: str = "") -> str:
    """One row here and one in the agent's own ledger (unless the change already made its own there: `cid`)."""
    sid = "s" + str(len(_rows()) + 1)
    if not cid:
        try:
            cid = core.log_change("settings", summary, {"op": "settings_undo", "id": sid}, by or BY)
        except Exception:  # noqa: BLE001   the settings ledger is the record either way
            pass
    core.append(LEDGER, {"id": sid, "ts": core.stamp(), "summary": summary[:300], "undo": undo, "by": by or BY, "change": cid})
    return sid


def changes(n: int = 12) -> list:
    rows = _rows()
    undone = {(r.get("undo") or {}).get("of") for r in rows if (r.get("undo") or {}).get("of")}
    out = [{"id": r["id"], "when": r.get("ts", ""), "summary": r.get("summary", ""), "by": r.get("by", ""), "undone": r["id"] in undone}
           for r in rows if not (r.get("undo") or {}).get("of")]
    return out[-n:][::-1]


def undo(sid: str, by: str = "", ledger: bool = True) -> dict:
    """Put one change back. ledger=False when the agent's own ledger is the caller (an undo asked for in chat): it writes
    its own undo row, and this one must not ask it to undo again."""
    rows = _rows()
    row = next((r for r in rows if r.get("id") == sid), None)
    if not row or (row.get("undo") or {}).get("of"):
        return {"ok": False, "message": "That change was not found."}
    if any((r.get("undo") or {}).get("of") == sid for r in rows):
        return {"ok": False, "message": "That change was already undone."}
    u = row.get("undo") or {}
    kind = u.get("kind")
    if kind == "json":
        json_set(FILES[u["src"]], u["path"], u["before"])
    elif kind == "people":
        core.save(core.PEOPLE, u["before"])
        if isinstance(u.get("org"), dict):
            import org
            org.restore(u["org"])
    elif kind == "core":
        line = core.undo(u.get("cid", ""), by=by or BY)
        if not line.startswith("Undone"):
            return {"ok": False, "message": line}
        ledger = False                            # the agent's ledger just recorded that undo itself
    elif kind == "features":
        import onboarding
        if isinstance(u.get("before"), list):
            onboarding.apply_features(u["before"])
        else:                                     # the gate had never been answered: every feature back on, the question open again
            s = onboarding.apply_features([k for k, *_ in onboarding.FEATURES])
            s.pop("features", None)
            onboarding._save(onboarding.STATE, s)
    else:
        return {"ok": False, "message": "That change cannot be undone from here."}
    core.append(LEDGER, {"id": "s" + str(len(rows) + 1), "ts": core.stamp(), "summary": "undid: " + row.get("summary", "")[:200],
                         "undo": {"of": sid}, "by": by or BY})
    if ledger and row.get("change"):              # the agent's "what changed" shows the undo too
        try:
            core.append(core.CHANGES, {"id": "c" + str(len(core.rows(core.CHANGES)) + 1), "ts": core.stamp(), "kind": "undo",
                                       "summary": f"undid {row['change']}: {row.get('summary', '')[:200]}", "undo": {"of": row["change"]}, "by": by or BY})
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "message": "Put back: " + row.get("summary", "")}


# ------------------------------------------------------------------ plain settings
def _org() -> dict:
    import org
    return org.load()


def value_of(row: dict, cache: dict | None = None):
    cache = cache if cache is not None else {}
    src = row["src"]
    if src not in cache:
        cache[src] = _org() if src == "org" else _read(FILES[src])
    v = _dig(cache[src], row["path"].split("."), None)
    return row.get("default", "") if v is None else v


def _clean(row: dict, value):
    """The value as it will be stored, or ValueError with the sentence the window shows."""
    t = row["type"]
    if t == "bool":
        if not isinstance(value, bool):
            raise ValueError("Choose on or off.")
        return value
    if t == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise ValueError("That needs to be a whole number.")
        if not row["min"] <= int(value) <= row["max"]:
            raise ValueError(f"Choose a number from {row['min']} to {row['max']}.")
        return int(value)
    if t == "choice":
        allowed = [c[0] for c in row["choices"]]
        hit = next((a for a in allowed if a == value and type(a) is type(value)), None)
        if hit is None:
            hit = next((a for a in allowed if str(a) == str(value)), None)
        if hit is None:
            raise ValueError("That is not one of the choices.")
        return hit
    if t == "window":
        if not (isinstance(value, list) and len(value) == 2 and all(isinstance(x, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", x) for x in value)):
            raise ValueError("Give a start and an end time.")
        if value[0] >= value[1]:
            raise ValueError("The end needs to be later than the start.")
        if value[0] < f"{QUIET[1]:02d}:00" or value[1] > f"{QUIET[0]:02d}:00":
            raise ValueError(f"Keep it between {QUIET[1]}:00 AM and {QUIET[0] - 12}:00 PM. Nothing goes out outside those hours.")
        return list(value)
    if t == "text":
        if not isinstance(value, str):
            raise ValueError("That needs to be text.")
        return value.strip()[:int(row.get("max", 400))]
    raise ValueError("That setting cannot be changed here.")


def _say(row: dict, v) -> str:
    if row["type"] == "bool":
        return "on" if v else "off"
    if row["type"] == "choice":
        return str(next((c[1] for c in row["choices"] if c[0] == v), v))
    if row["type"] == "window":
        return " to ".join(v) if isinstance(v, list) else str(v)
    return str(v)


def set_value(key: str, value, by: str = "") -> dict:
    row = BY_KEY.get(str(key))
    if not row:
        return {"ok": False, "message": "There is no such setting."}
    try:
        value = _clean(row, value)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    before = value_of(row)
    if before == value:
        return {"ok": True, "message": "", "unchanged": True}
    if row["src"] == "org":
        import org
        if not value:
            return {"ok": False, "message": "Write a few words, or leave it as it is."}
        r = org.apply({row["path"]: value}, by=by or BY)
        if r.get("status") != "applied":
            return {"ok": False, "message": "That was not saved: " + str(r.get("reason") or "nothing changed") + "."}
        sid = _log(f"{row['label']}: {value[:120]}", {"kind": "core", "cid": r.get("change", "")}, by, cid=str(r.get("change") or ""))
    else:
        json_set(FILES[row["src"]], row["path"], value)
        sid = _log(f"{row['label']}: {_say(row, value)} (was {_say(row, before)})", {"kind": "json", "src": row["src"], "path": row["path"], "before": before}, by)
    return {"ok": True, "message": "Saved.", "id": sid}


# ------------------------------------------------------------------ people and roles
def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:30]


def _people() -> dict:
    d = core.load(core.PEOPLE, {"roles": {}, "users": {}})
    d.setdefault("roles", {})
    d.setdefault("users", {})
    return d


def _owner_count(d: dict) -> int:
    return sum(1 for u in d["users"].values() if str(u.get("role") or "") == "owner")


def _note(d: dict, key: str) -> str:
    u = d["users"][key]
    boss = (d["users"].get(str(u.get("reports_to") or "")) or {}).get("name", "")
    return "; ".join(x for x in (str(u.get("title") or "").strip(), f"reports to {boss}" if boss else "") if x)


def _sync_org(d: dict, removed: str = "") -> dict:
    """The organization's own notes carry the same structure (name, role, what they handle, who they report to), because
    that is what the agent reads before it answers. -> the notes as they were, for undo."""
    import org
    o = org.load()
    before = copy.deepcopy(o)
    notes = [p for p in (o.get("people") or []) if core._norm(p.get("name") or "") != core._norm(removed)] if removed else list(o.get("people") or [])
    for key, u in d["users"].items():
        hit = next((p for p in notes if core._norm(p.get("name") or "") == core._norm(u.get("name") or "")), None)
        about = _note(d, key)
        if hit is None:
            notes.append({"name": str(u.get("name") or "")[:60], "role": str(u.get("role") or ""), "about": about[:200], "added": time.strftime("%Y-%m-%d")})
        else:
            hit["role"] = str(u.get("role") or "")
            if about or u.get("title") is not None or u.get("reports_to") is not None:
                hit["about"] = about[:200] or hit.get("about", "")
    if notes != (o.get("people") or []):
        o["people"] = notes[:org.CAPS["people"]]
        org.save(o)
    return before


def _reports_loop(d: dict, key: str, boss: str) -> bool:
    seen = {key}
    while boss:
        if boss in seen:
            return True
        seen.add(boss)
        boss = str((d["users"].get(boss) or {}).get("reports_to") or "")
    return False


def person_save(body: dict, by: str = "") -> dict:
    d = _people()
    before = copy.deepcopy(d)
    key = str(body.get("key") or "").strip()
    name = str(body.get("name") or "").strip()[:60]
    email = str(body.get("email") or "").strip().lower()[:120]
    role = str(body.get("role") or "").strip()
    title = str(body.get("title") or "").strip()[:120]
    boss = str(body.get("reports_to") or "").strip()
    if key and key not in d["users"]:
        return {"ok": False, "message": "That person is no longer on the list."}
    if not name:
        return {"ok": False, "message": "A name is needed."}
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return {"ok": False, "message": "That email address does not look right."}
    if role not in d["roles"]:
        return {"ok": False, "message": "Choose a role."}
    if core.is_protected(title):
        return {"ok": False, "message": "That description touches a guardrail. Describe what they handle in plain words."}
    new = not key
    if new:
        if not email:
            return {"ok": False, "message": "An email address is needed. It is how the agent knows who is writing."}
        key = base = _slug(name.split()[0]) or "person"
        n = 2
        while key in d["users"]:
            key, n = f"{base}{n}", n + 1
    taken = next((u.get("name") for k, u in d["users"].items() if k != key and email and email in [str(e).lower() for e in u.get("emails") or []]), None)
    if taken:
        return {"ok": False, "message": f"That email address already belongs to {taken}."}
    u = d["users"].setdefault(key, {"name": name, "role": role, "emails": [], "chat_user": "", "added": time.strftime("%Y-%m-%d")})
    if not new and str(u.get("role")) == "owner" and role != "owner" and _owner_count(d) <= 1:
        return {"ok": False, "message": "There has to be at least one owner. Make someone else an owner first."}
    if boss and (boss not in d["users"] or boss == key or _reports_loop(d, key, boss)):
        return {"ok": False, "message": "Choose someone else for them to report to."}
    u["name"], u["role"] = name, role
    if email:
        u["emails"] = [email] + [e for e in (u.get("emails") or []) if str(e).lower() != email]
    for f, v in (("title", title), ("reports_to", boss)):
        if v:
            u[f] = v
        else:
            u.pop(f, None)
    if d == before:
        return {"ok": True, "message": "", "unchanged": True}
    core.save(core.PEOPLE, d)
    org_before = _sync_org(d)
    sid = _log(f"{'added' if new else 'changed'} {name} ({role})", {"kind": "people", "before": before, "org": org_before}, by)
    return {"ok": True, "message": f"{name} was added. The agent answers them from their next message." if new else "Saved.", "id": sid, "key": key}


def person_remove(body: dict, by: str = "") -> dict:
    d = _people()
    before = copy.deepcopy(d)
    key = str(body.get("key") or "")
    u = d["users"].get(key)
    if not u:
        return {"ok": False, "message": "That person is no longer on the list."}
    if str(u.get("role")) == "owner" and _owner_count(d) <= 1:
        return {"ok": False, "message": "There has to be at least one owner."}
    del d["users"][key]
    for other in d["users"].values():
        if other.get("reports_to") == key:
            other.pop("reports_to", None)
    core.save(core.PEOPLE, d)
    org_before = _sync_org(d, removed=str(u.get("name") or ""))
    sid = _log(f"removed {u.get('name')} ({u.get('role')})", {"kind": "people", "before": before, "org": org_before}, by)
    return {"ok": True, "message": f"{u.get('name')} was removed. The agent no longer answers them.", "id": sid}


def lanes() -> list:
    cfg = _read(FILES["config"])
    out = [str(x) for x in (cfg.get("lanes") or []) if isinstance(x, str)]
    for x in (_org().get("lanes") or []):
        if x.get("key") and x["key"] not in out:
            out.append(x["key"])
    return out


def folders() -> list:
    """The folders of notes a role can be limited to: the top folders of the wiki."""
    wiki = core.VAULT / "Wiki"
    try:
        return sorted(f"Wiki/{p.name}/" for p in wiki.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))
    except OSError:
        return []


def role_save(body: dict, by: str = "") -> dict:
    d = _people()
    before = copy.deepcopy(d)
    name = _slug(body.get("name") or "")
    if not name:
        return {"ok": False, "message": "The role needs a name."}
    if name == "owner":
        return {"ok": False, "message": "The owner role cannot be changed. Owners see everything and are the only ones who approve."}
    can = [c for c in GRANTABLE if c in (body.get("can") or [])]
    extra = [c for c in (body.get("can") or []) if c not in GRANTABLE]
    if extra:
        return {"ok": False, "message": "Only an owner can approve, teach or undo. Those cannot be given to another role."}
    known_lanes, known_folders = lanes(), folders()
    role = dict(d["roles"].get(name) or {})
    role["can"] = can
    for f, picked, known in (("lanes", body.get("lanes"), known_lanes), ("paths", body.get("paths"), known_folders)):
        if picked is None:
            continue
        keep = [x for x in picked if x in known or x in (role.get(f) or [])]
        if keep:
            role[f] = keep
        else:
            role.pop(f, None)
    about = str(body.get("about") or "").strip()[:200]
    if about and core.is_protected(about):
        return {"ok": False, "message": "That description touches a guardrail. Describe the role in plain words."}
    if about:
        role["about"] = about
    elif "about" in body:
        role.pop("about", None)
    d["roles"][name] = role
    if d == before:
        return {"ok": True, "message": "", "unchanged": True}
    core.save(core.PEOPLE, d)
    sid = _log(f"role {name}: may " + (", ".join(can) or "do nothing") + (f"; sees {', '.join(role.get('lanes') or [])}" if role.get("lanes") else "")
               + (f"; searches {', '.join(p.split('/')[-2] for p in role['paths'])}" if role.get("paths") else ""),
               {"kind": "people", "before": before}, by)
    return {"ok": True, "message": "Saved.", "id": sid, "name": name}


def role_remove(body: dict, by: str = "") -> dict:
    d = _people()
    before = copy.deepcopy(d)
    name = str(body.get("name") or "")
    if name in ("owner", "staff", "guest") or name not in d["roles"]:
        return {"ok": False, "message": "That role stays."}
    if any(str(u.get("role")) == name for u in d["users"].values()):
        return {"ok": False, "message": "Someone still has that role. Give them another one first."}
    del d["roles"][name]
    core.save(core.PEOPLE, d)
    sid = _log(f"removed the role {name}", {"kind": "people", "before": before}, by)
    return {"ok": True, "message": "Removed.", "id": sid}


def people_view() -> dict:
    d = _people()
    users = [{"key": k, "name": str(u.get("name") or k), "role": str(u.get("role") or ""), "email": str((u.get("emails") or [""])[0]),
              "title": str(u.get("title") or ""), "reports_to": str(u.get("reports_to") or ""), "in_chat": bool(u.get("chat_user"))}
             for k, u in d["users"].items()]
    users.sort(key=lambda u: (u["role"] != "owner", u["name"].lower()))
    roles = []
    for name, r in d["roles"].items():
        owner = name == "owner"
        roles.append({"name": name, "locked": owner, "builtin": name in ("owner", "staff", "guest"),
                      "can": list(r.get("can") or []), "lanes": list(r.get("lanes") or []), "paths": list(r.get("paths") or []),
                      "about": "Sees everything. Approves what goes out, teaches the agent and can undo its changes." if owner else str(r.get("about") or ""),
                      "members": sum(1 for u in users if u["role"] == name)})
    return {"users": users, "roles": roles, "can_words": [{"key": k, "label": v} for k, v in CAN_WORDS.items()], "lanes": lanes(), "folders": folders()}


# ------------------------------------------------------------------ what it handles, standing yeses
def _module_on(mod: str) -> bool:
    """An optional module is on when its scheduled jobs are (a new organization's start off; `sage module on` and the
    feature gate switch them); with no job list to read, when the organization lists it."""
    import engine
    names = set((engine.MODULES.get(mod) or {}).get("jobs") or [])
    jobs = [j for j in (_read(ROOT / "spine_jobs.json").get("jobs") or []) if isinstance(j, dict) and j.get("name") in names]
    return any(j.get("enabled", True) for j in jobs) if jobs else mod in (_read(ROOT / "org.json").get("modules") or [])


def _features_on() -> dict:
    """Which features are on. Once the gate is answered: what was wanted. Before that nothing is gated, but a feature that
    is an optional module (money) is only on when that module is running."""
    import onboarding
    st = onboarding.features_state()
    return {k: (k in st["wanted"]) if st is not None else (_module_on(mod) if mod else True) for k, _what, _fns, mod, _words in onboarding.FEATURES}


def features_view() -> list:
    try:
        import onboarding
        on = _features_on()
        return [{"key": k, "label": what[:1].upper() + what[1:], "on": on[k]} for k, what, *_ in onboarding.FEATURES]
    except Exception:  # noqa: BLE001
        return []


def feature_set(body: dict, by: str = "") -> dict:
    import onboarding
    key, on = str(body.get("key") or ""), body.get("on") is True
    keys = [k for k, *_ in onboarding.FEATURES]
    if key not in keys:
        return {"ok": False, "message": "There is no such feature."}
    st = onboarding.features_state()
    now = _features_on()
    wanted = [k for k in keys if now[k]]
    if (key in wanted) == on:
        return {"ok": True, "message": "", "unchanged": True}
    new = [k for k in keys if (k in wanted and k != key) or (k == key and on)]
    onboarding.apply_features(new)
    label = next(what for k, what, *_ in onboarding.FEATURES if k == key)
    sid = _log(f"{'handles' if on else 'no longer handles'} {label.split(' (')[0].split(':')[0]}", {"kind": "features", "before": None if st is None else wanted}, by)
    return {"ok": True, "message": "Saved.", "id": sid}


def grants_view() -> list:
    try:
        import policy
        return [{"id": g["id"], "what": str(g.get("header") or ""), "answer": str(g.get("option") or ""), "lane": str(g.get("lane") or ""),
                 "by": str(g.get("by") or ""), "since": str(g.get("ts") or "")[:10], "until": str(g.get("until") or "")} for g in policy.active_grants()]
    except Exception:  # noqa: BLE001
        return []


def grant_revoke(body: dict, by: str = "") -> dict:
    import policy
    gid = str(body.get("id") or "")
    g = next((x for x in policy.active_grants() if x.get("id") == gid), None)
    if not g or not policy.revoke(gid, by=by or BY, why="taken back in settings"):
        return {"ok": False, "message": "That standing yes is no longer active."}
    core.append(LEDGER, {"id": "s" + str(len(_rows()) + 1), "ts": core.stamp(), "summary": f"took back the standing yes for '{g.get('header')}'",
                         "undo": {"kind": "none"}, "by": by or BY})
    return {"ok": True, "message": "Taken back. The agent asks you again from now on."}


# ------------------------------------------------------------------ the whole screen
def snapshot() -> dict:
    cache: dict = {}
    values = {r["key"]: value_of(r, cache) for r in REGISTRY}
    groups = []
    for gid, title, blurb in GROUPS:
        items = []
        for r in REGISTRY:
            if r["group"] != gid:
                continue
            it = {"key": r["key"], "label": r["label"], "help": r.get("help", ""), "type": r["type"], "value": values[r["key"]]}
            for f in ("min", "max", "step", "show_if", "show_unless"):
                if f in r:
                    it[f] = list(r[f]) if isinstance(r[f], tuple) else r[f]
            if r["type"] == "choice":
                it["choices"] = [{"value": c[0], "label": c[1]} for c in r["choices"]]
            items.append(it)
        groups.append({"id": gid, "title": title, "blurb": blurb, "items": items})
    o = _org()
    return {"ok": True, "groups": groups, "org": str(o.get("name") or ""), "agent": str(_read(FILES["profile"]).get("name") or ""),
            "people": people_view(), "features": features_view(), "grants": grants_view(),
            "locked": [{"title": t, "detail": w} for t, w in LOCKED], "changes": changes()}


OPS = {"get": lambda b, by: snapshot(), "set": lambda b, by: set_value(b.get("key"), b.get("value"), by), "undo": lambda b, by: undo(str(b.get("id") or ""), by),
       "person_save": person_save, "person_remove": person_remove, "role_save": role_save, "role_remove": role_remove,
       "feature_set": feature_set, "grant_revoke": grant_revoke}


def call(body: dict) -> dict:
    fn = OPS.get(str((body or {}).get("op") or "get"))
    if fn is None:
        return {"ok": False, "message": "no such step"}
    try:
        return fn(body or {}, str((body or {}).get("by") or ""))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"That did not save ({type(e).__name__}: {str(e)[:160]})."}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    if a[:1] == ["call"]:
        try:
            body = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            body = {}
        out = call(body if isinstance(body, dict) else {})
    elif a[:1] == ["set"] and len(a) == 3:
        try:
            v = json.loads(a[2])
        except ValueError:
            v = a[2]
        out = set_value(a[1], v)
    elif a[:1] == ["undo"] and len(a) == 2:
        out = undo(a[1])
    else:
        out = snapshot()
    print(json.dumps(out, ensure_ascii=False, indent=None if a[:1] == ["call"] else 1))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
