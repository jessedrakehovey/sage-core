"""org.py - what the agent knows about the organization it works for, learned in chat.

The code in assistant/agent carries no business facts; until now they lived only in prompt.md
and the owners' heads. This file is the one place an organization describes itself to its
agent, so the same agent can be stood up for any household or company and LEARN the rest:

    state/org.json
      name, what_we_do, where, people (role notes), lanes, services, duties, never, hours

Three ways it fills in:
  1. In chat, through the SUGGEST `org` field (actions.py): "we're a two-person bakery",
     "we use Square and Gmail", "every Monday send me the week's orders", "never refund without me".
  2. The onboarding interview: `next_questions()` lists what is still unknown; the bridge
     puts ONE of them into the model's context so the agent asks it naturally, one per
     conversation, never twice in a day. `interview_hint()` is that line.
  3. By hand: edit the JSON, or `python assistant/agent/org.py set name "the organization"`.

Every change is ledgered (core.log_change, undo op `org_restore`) and rendered into the
model's context by `render()` as ABOUT THIS ORGANIZATION. `services` double as the human
side of the connection registry: a service with a `url` gets a "Sign in" button on Mission
Control's Setup page (scripts/setup_actions.py) even when no code checks it yet.

  python assistant/agent/org.py                 -> the profile, rendered as the model sees it
  python assistant/agent/org.py questions       -> what the agent still needs to ask
  python assistant/agent/org.py set <field> <value>
  python assistant/agent/org.py service add "<name>" --url https://... --for "what it is used for" [--lane x]
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

ORG = core.STATE / "org.json"
ASKED = core.DATA / "org_asked.json"

FIELDS = ("name", "what_we_do", "where", "hours", "tone")
LIST_FIELDS = ("lanes", "services", "duties", "never", "people")
CAPS = {"lanes": 12, "services": 60, "duties": 40, "never": 30, "people": 40}

# The interview, in the order a new hire would ask. Each row: field, the question the agent
# should work into the conversation, and a test of whether it is answered.
QUESTIONS = [
    ("name", "What should I call the business (or household) I work for?", lambda o: bool(o.get("name"))),
    ("what_we_do", "In a sentence or two, what do you do, and for whom?", lambda o: len(o.get("what_we_do") or "") > 10),
    ("people", "Who is who? Who approves things, who do I answer to, and is there anyone I should never answer?",
     lambda o: len(o.get("people") or []) >= 1),
    ("lanes", "What are the main areas of your work I should keep apart (for example clients, money, admin, home)?",
     lambda o: len(o.get("lanes") or []) >= 1),
    ("services", "Which sites and services do you run the business from (email, calendar, CRM, bank, booking tool, anything you sign in to)?",
     lambda o: len(o.get("services") or []) >= 1),
    ("duties", "What should I do on a schedule without being asked: a morning summary, a weekly check, chasing something?",
     lambda o: len(o.get("duties") or []) >= 1),
    ("never", "What must never happen without your say-so, beyond sending messages, paying, or promising prices and dates?",
     lambda o: o.get("never_asked") or len(o.get("never") or []) >= 1),
    ("hours", "When do you want to hear from me, and when should I stay quiet?", lambda o: bool(o.get("hours"))),
]


def _blank() -> dict:
    return {"name": "", "what_we_do": "", "where": "", "hours": "", "tone": "",
            "people": [], "lanes": [], "services": [], "duties": [], "never": [], "updated": ""}


def load() -> dict:
    o = core.load(ORG, None)
    if not isinstance(o, dict):
        o = _blank()
    for k, v in _blank().items():
        o.setdefault(k, copy.deepcopy(v))
    return o


def save(o: dict) -> None:
    o["updated"] = core.stamp()
    core.save(ORG, o)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:40]


def _norm_item(field: str, item) -> dict | None:
    """One list entry, shaped. Strings are allowed shorthand."""
    if isinstance(item, str):
        item = {"name": item} if field in ("services", "lanes", "people") else {"text": item}
    if not isinstance(item, dict):
        return None
    d = {}
    if field == "lanes":
        d["key"] = _slug(item.get("key") or item.get("name") or "")
        d["name"] = str(item.get("name") or item.get("key") or "").strip()[:40]
        d["about"] = str(item.get("about") or item.get("description") or "").strip()[:200]
        return d if d["key"] else None
    if field == "services":
        d["id"] = _slug(item.get("id") or item.get("name") or "")
        d["name"] = str(item.get("name") or "").strip()[:60]
        d["url"] = str(item.get("url") or "").strip()[:200]
        d["for"] = str(item.get("for") or item.get("used_for") or "").strip()[:200]
        d["lane"] = _slug(item.get("lane") or "")
        d["account"] = str(item.get("account") or "").strip()[:80]       # a username or address, never a secret
        if re.search(r"(?i)\b(password|passcode|pin)\b\s*[:=]|\b\d{12,}\b", json.dumps(d)):
            return None
        return d if d["id"] else None
    if field == "people":
        d["name"] = str(item.get("name") or "").strip()[:60]
        d["role"] = str(item.get("role") or "").strip()[:80]
        d["about"] = str(item.get("about") or "").strip()[:200]
        return d if d["name"] else None
    if field == "duties":
        d["text"] = str(item.get("text") or item.get("duty") or "").strip()[:300]
        d["when"] = str(item.get("when") or "").strip()[:80]
        d["lane"] = _slug(item.get("lane") or "")
        return d if len(d["text"]) > 3 else None
    if field == "never":
        d["text"] = str(item.get("text") or "").strip()[:200]
        return d if len(d["text"]) > 3 else None
    return None


def _key(field: str, d: dict) -> str:
    return d.get("key") or d.get("id") or core._norm(d.get("name") or d.get("text") or "")


def apply(spec: dict, by: str = "") -> dict:
    """The SUGGEST `org` field: scalar fields set, list fields upserted (same key folds).
    -> {"status": applied|refused|nothing, "changed": [...], "change": cid}"""
    if not isinstance(spec, dict):
        return {"status": "refused", "reason": "org must be an object"}
    o = load()
    before = copy.deepcopy(o)
    changed = []
    for f in FIELDS:
        if f in spec and isinstance(spec[f], str):
            v = spec[f].strip()[:400]
            if core.is_protected(v):
                return {"status": "refused", "reason": "that touches the protected core"}
            if v and v != o.get(f):
                o[f] = v
                changed.append(f)
    for f in LIST_FIELDS:
        items = spec.get(f)
        if items is None:
            continue
        items = items if isinstance(items, list) else [items]
        for it in items[:10]:
            d = _norm_item(f, it)
            if not d:
                continue
            if f == "never" and core.is_protected(d["text"]) and not core.tightens(d["text"]):
                continue                                   # loosening a core rule is never an org fact
            k = _key(f, d)
            cur = o.setdefault(f, [])
            hit = next((i for i, x in enumerate(cur) if _key(f, x) == k), None)
            if hit is None:
                if len(cur) >= CAPS[f]:
                    continue
                d["added"] = time.strftime("%Y-%m-%d")
                cur.append(d)
                changed.append(f"{f}+{k}")
            else:
                merged = {**cur[hit], **{kk: vv for kk, vv in d.items() if vv}}
                if merged != cur[hit]:
                    cur[hit] = merged
                    changed.append(f"{f}~{k}")
    for f in ("remove_service", "remove_duty", "remove_never", "remove_lane"):
        if spec.get(f):
            lf = {"remove_service": "services", "remove_duty": "duties", "remove_never": "never", "remove_lane": "lanes"}[f]
            k = _slug(spec[f]) if lf in ("services", "lanes") else core._norm(str(spec[f]))
            kept = [x for x in o.get(lf, []) if _key(lf, x) != k and core._norm(x.get("text") or x.get("name") or "") != k]
            if len(kept) != len(o.get(lf, [])):
                o[lf] = kept
                changed.append(f"{lf}-{k}")
    if spec.get("never_asked") is True:
        o["never_asked"] = True
    if not changed:
        return {"status": "nothing", "changed": []}
    save(o)
    cid = core.log_change("org", "organization profile: " + ", ".join(changed)[:200],
                          {"op": "org_restore", "before": before}, by)
    return {"status": "applied", "changed": changed, "change": cid}


def restore(before: dict) -> bool:
    if not isinstance(before, dict):
        return False
    save(before)
    return True


def render() -> str:
    o = load()
    if not any(o.get(k) for k in FIELDS + LIST_FIELDS):
        return ("ABOUT THIS ORGANIZATION: nothing recorded yet. You are new here; learn it the way a new hire would "
                "(see Learning the organization in your instructions).")
    L = ["ABOUT THIS ORGANIZATION (what the owners have told you; keep it current with the `org` field):"]
    if o.get("name"):
        L.append(f"- Name: {o['name']}")
    if o.get("what_we_do"):
        L.append(f"- What we do: {o['what_we_do']}")
    if o.get("where"):
        L.append(f"- Where: {o['where']}")
    if o.get("hours"):
        L.append(f"- When to speak up / stay quiet: {o['hours']}")
    if o.get("tone"):
        L.append(f"- How they like to be talked to: {o['tone']}")
    if o.get("people"):
        L.append("- People: " + "; ".join(f"{p['name']} ({p['role']})" + (f": {p['about']}" if p.get("about") else "") for p in o["people"]))
    if o.get("lanes"):
        L.append("- Lanes: " + "; ".join(f"{x['key']}" + (f" = {x['about']}" if x.get("about") else "") for x in o["lanes"]))
    if o.get("services"):
        L.append("- Services and sites (the Setup page on Mission Control has a Sign in button for each): "
                 + "; ".join(f"{s['name']}" + (f" for {s['for']}" if s.get("for") else "") + (f" ({s['url']})" if s.get("url") else "")
                             for s in o["services"]))
    if o.get("duties"):
        L.append("- Standing duties: " + "; ".join(f"{d['text']}" + (f" ({d['when']})" if d.get("when") else "") for d in o["duties"]))
    if o.get("never"):
        L.append("- Never without an owner: " + "; ".join(n["text"] for n in o["never"]))
    return "\n".join(L)


def next_questions() -> list:
    o = load()
    return [{"field": f, "question": q} for f, q, done in QUESTIONS if not done(o)]


def _asked() -> dict:
    return core.load(ASKED, {}) or {}


def interview_hint(max_per_day: int = 3) -> str:
    """ONE onboarding question for the model to work into its next reply, or ''. A field is asked
    at most once a day, and at most `max_per_day` questions a day, so the interview feels like
    conversation, not a form."""
    qs = next_questions()
    if not qs:
        return ""
    asked = _asked()
    today = time.strftime("%Y-%m-%d")
    today_n = sum(1 for v in asked.values() if str(v).startswith(today))
    if today_n >= max_per_day:
        return ""
    for q in qs:
        if str(asked.get(q["field"], "")).startswith(today):
            continue
        asked[q["field"]] = core.stamp()
        core.save(ASKED, asked)
        return (f"ONBOARDING (you are still learning this organization; {len(qs)} things unknown): if the moment allows, "
                f"ask this in your own words, once, at the end of your reply: \"{q['question']}\" "
                f"Save the answer with the `org` field (`{q['field']}`). Never ask if they are busy with something urgent.")
    return ""


def status_text() -> str:
    o = load()
    qs = next_questions()
    head = f"{o.get('name') or '(no name yet)'}: {len(o.get('services') or [])} services, {len(o.get('duties') or [])} duties, " \
           f"{len(o.get('lanes') or [])} lanes, {len(o.get('never') or [])} never-rules."
    if qs:
        return head + " Still to learn: " + "; ".join(q["field"].replace("_", " ") for q in qs) + "."
    return head + " Onboarding complete."


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="show", choices=["show", "questions", "set", "service", "status", "json"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--url", default="")
    ap.add_argument("--for", dest="used_for", default="")
    ap.add_argument("--lane", default="")
    a = ap.parse_args()
    if a.cmd == "show":
        print(render())
    elif a.cmd == "json":
        print(json.dumps(load(), indent=1, ensure_ascii=False))
    elif a.cmd == "status":
        print(status_text())
    elif a.cmd == "questions":
        for q in next_questions():
            print(f"{q['field']:<12} {q['question']}")
    elif a.cmd == "set" and len(a.args) >= 2:
        print(apply({a.args[0]: " ".join(a.args[1:])}, by="cli"))
    elif a.cmd == "service" and len(a.args) >= 2 and a.args[0] == "add":
        print(apply({"services": [{"name": " ".join(a.args[1:]), "url": a.url, "for": a.used_for, "lane": a.lane}]}, by="cli"))
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
