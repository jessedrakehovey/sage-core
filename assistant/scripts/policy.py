"""policy.py - ONE answer to "may the agent do this without a tap?", and the grants an owner's tap can leave behind.

Borrowed from the Grok Bot / Muse teardown (Wiki/Builder, 2026-11-08, upgrades 1-3). Until this module the
ask/allow decision was spread over cards.py (its guards), drain.py (FOREVER_GATED), graduation.py (earned trust),
rulings.py (standing answers), desk_policy.py and the standing-exception switches in config.json. Each still does
its own job; this module is the one place that answers the question in a FIXED order, and the one place a card's
"reason" / "caution" / "always rule" lines come from (by code, never written by the model):

  1. forever gated    an outbound send, money, the browser agent, a credential, a record write of a gated kind:
                      always a tap, and a tap there is ever only "once" (no grant can cover it)
  2. untrusted trigger the action was set off by inbound material the machine could not vouch for (an unauthenticated
                      mail, a line carrying the injection marker): a tap, whatever else says yes
  3. owner ask-first   an owner's own rule that says "always ask" / "never without asking" on this subject: a tap
  4. owner allow       a standing grant from a tap ("always" / "until <date>" / "this task"), or an action class that
                      graduated (graduation.py): the card is answered by code, ledgered, undoable
  5. default           a tap

GRANTS (upgrade 1). A tap on a card may carry a scope: once (the default), this_task (the rest of this thread / job,
24 h at most), until (a date), always. A grant is a row in data/grants.json keyed on the card's header + lane (+ exec
kind), written through the change ledger (core.log_change, undo op grant_revoke) so /undo takes it back, and
mirrored into the audit ledger. A card the policy answers under a grant is still archived and still harvested, so
the operator carries the decision out exactly as if the owner had tapped; the answer is signed "policy:<grant id>".

EXPIRY BY ORIGIN (upgrade 3). A card an owner asked for in chat never expires; one a routine or the machinery raised
on its own expires in ROUTINE_EXPIRES_S when it was delivered at once (a held card keeps the digest's own rules).
What the model is told after a decline or such an expiry is ONE fixed line (NO_RETRY_LINE), never a paraphrase.

Zero LLM; nothing here sends. Engine code: no organization names (the owners come from people.json).
  python assistant/scripts/policy.py grants | revoke <gid> | decide "<header>" [--lane L] [--kind K]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from alib import DATA, load_json, save_json  # noqa: E402

GRANTS_FILE = DATA / "grants.json"
SCOPES = ("once", "this_task", "until", "always")
THIS_TASK_MAX_H = 24
UNTIL_DEFAULT_DAYS = 7
ROUTINE_EXPIRES_S = 30 * 60              # a routine-born card delivered at once dies in half an hour unanswered
CHAT_EXPIRES_S = 365 * 86400             # a card an owner asked for in chat waits for them
ORIGINS = ("chat", "routine", "system", "operator", "inbound")
NO_RETRY_LINE = ("The owner did not approve this. Do not retry it, do not re-ask it and do not reword it; "
                 "if the facts change later, that is a new decision.")
# money MOVING, not money mentioned: filing an invoice or reading a balance is not a payment
MONEY = re.compile(r"\b(pay|pays|paid|payment|transfer|wire|refund|charge|withdraw|purchase|buy|card number|routing number)\b|\bpay the\b", re.I)
CREDENTIAL = re.compile(r"\b(password|passcode|login|sign.?in|2fa|verification code|api key|token)\b", re.I)
ASK_FIRST = re.compile(r"\b(always ask|ask (?:me )?(?:first|before)|never (?:send|do|post|book|pay|text|email|reply|act)[^.]{0,40}without|"
                       r"needs? (?:my|an owner'?s|a) (?:tap|ok|approval)|check with (?:me|us) first|do not (?:send|act|post) without)\b", re.I)
_RECOMMENDED = re.compile(r"\((?:recommended|rec\.?)[^)]*\)", re.I)


# ------------------------------------------------------------------ helpers
def _hkey(h) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(h or "").lower()).strip()


def key_for(q: dict, lane: str = "") -> str:
    """The grant key of a card: its header (ids and numbers stripped), its lane and its exec kind. Two cards about
    the same decision under the same header share it; a different recipient or subject under a template header
    does NOT widen a grant to everyone (the subject is part of the key when the poster names one)."""
    hdr = re.sub(r"\b[0-9a-f]{8,}\b|\d{4,}", "", _hkey(q.get("header")))
    hdr = re.sub(r"\s+", " ", hdr).strip()
    subj = str(q.get("subject") or q.get("chain_key") or "").strip().lower()
    kind = str((q.get("exec") or {}).get("kind") or "")
    return "|".join([hdr, str(q.get("lane") or lane or "personal"), kind, subj])


def forever_gated(kind: str) -> bool:
    if not kind:
        return False
    try:
        import graduation
        return kind in graduation._all_forever_gated()
    except Exception:  # noqa: BLE001
        return kind in {"email_send", "imessage_send", "ads_apply", "agent_run", "local", "tracking_fix"}


def recommended_option(q: dict) -> str:
    """The label a grant answers with: the exec approve label, else the option marked (Recommended), else the first."""
    ex = q.get("exec") or {}
    if ex:
        return str(ex.get("approve_label") or "Approve")
    opts = [str(o.get("label") or "") for o in (q.get("options") or []) if isinstance(o, dict) and o.get("label")]
    rec = [o for o in opts if _RECOMMENDED.search(o)]
    return (rec or opts or [""])[0]


def _is_no(label: str) -> bool:
    return bool(re.match(r"^\s*(skip|drop|dismiss|ignore|no\b|not now|later|leave|cancel|never mind|keep asking|decline)", str(label or ""), re.I))


def grant_scopes(q: dict, lane: str = "") -> list:
    """Which grant scopes a card may offer. A forever-gated action offers only `once`; so does a card whose words touch
    money or credentials even when its kind is not gated (a decision card about a payment)."""
    kind = str((q.get("exec") or {}).get("kind") or "")
    text = f"{q.get('header') or ''} {q.get('question') or ''}"
    if forever_gated(kind) or MONEY.search(text) or CREDENTIAL.search(text):
        return ["once"]
    return list(SCOPES)


# ------------------------------------------------------------------ grants
def _grants() -> dict:
    d = load_json(GRANTS_FILE, {}) or {}
    d.setdefault("grants", [])
    return d


def active_grants(now: float | None = None) -> list:
    now = now or time.time()
    out = []
    for g in _grants()["grants"]:
        if not g.get("active", True):
            continue
        if g.get("until_ts") and float(g["until_ts"]) < now:
            continue
        out.append(g)
    return out


def find_grant(key: str, task: str = "", now: float | None = None) -> dict | None:
    for g in active_grants(now):
        if g.get("key") != key:
            continue
        if g.get("scope") == "this_task" and str(g.get("task") or "") != str(task or ""):
            continue
        return g
    return None


def _parse_until(until: str, now: float) -> float:
    s = str(until or "").strip()
    if not s:
        return now + UNTIL_DEFAULT_DAYS * 86400
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            t = time.mktime(time.strptime(s, fmt))
            return t + (86400 - 60 if fmt == "%Y-%m-%d" else 0)      # a date means through that day
        except ValueError:
            continue
    m = re.fullmatch(r"(\d+)\s*(d|day|days|h|hour|hours|w|week|weeks)", s, re.I)
    if m:
        n, u = int(m.group(1)), m.group(2).lower()[0]
        return now + n * {"d": 86400, "h": 3600, "w": 7 * 86400}[u]
    raise ValueError(f"could not read the date {s!r} (use YYYY-MM-DD, '3 days' or '2 weeks')")


def grant(q: dict, lane: str, scope: str, by: str, option: str = "", until: str = "", task: str = "", qid: str = "") -> dict:
    """Write one grant from an owner's tap. -> {"ok", "id", "reason", "until"}. `once` writes nothing."""
    scope = str(scope or "once").lower()
    if scope not in SCOPES:
        return {"ok": False, "reason": f"unknown scope {scope!r}"}
    if scope == "once":
        return {"ok": True, "id": "", "scope": "once"}
    if scope not in grant_scopes(q, lane):
        return {"ok": False, "reason": "this kind of action always needs a tap; nothing standing can cover it"}
    option = option or recommended_option(q)
    if _is_no(option):
        return {"ok": False, "reason": "a standing grant is a standing YES; a no is already final (nothing re-asks it)"}
    now = time.time()
    until_ts = 0.0
    if scope == "until":
        try:
            until_ts = _parse_until(until, now)
        except ValueError as e:
            return {"ok": False, "reason": str(e)}
    elif scope == "this_task":
        if not task:
            return {"ok": False, "reason": "no task or thread to tie this to; use 'until' or 'always'"}
        until_ts = now + THIS_TASK_MAX_H * 3600
    key = key_for(q, lane)
    d = _grants()
    for g in d["grants"]:                                   # one active grant per key: the new one replaces
        if g.get("active", True) and g.get("key") == key and (scope != "this_task" or g.get("task") == task):
            g["active"] = False
            g["replaced"] = time.strftime("%Y-%m-%d %H:%M")
    gid = "g" + uuid.uuid4().hex[:8]
    row = {"id": gid, "key": key, "scope": scope, "option": option, "by": str(by or "")[:60], "ts": time.strftime("%Y-%m-%d %H:%M"),
           "header": str(q.get("header") or "")[:80], "lane": str(q.get("lane") or lane or "personal"),
           "kind": str((q.get("exec") or {}).get("kind") or ""), "card": str(qid or ""), "active": True,
           **({"until_ts": until_ts, "until": time.strftime("%Y-%m-%d %H:%M", time.localtime(until_ts))} if until_ts else {}),
           **({"task": str(task)} if task else {})}
    d["grants"].append(row)
    save_json(GRANTS_FILE, d)
    _ledger("grant", id=gid, key=key, scope=scope, by=row["by"], until=row.get("until", ""))
    _change("grant", f"standing yes ({scope}{', until ' + row['until'] if row.get('until') else ''}): "
                     f"'{row['header']}' in {row['lane']} -> {option}", {"op": "grant_revoke", "id": gid}, by)
    return {"ok": True, "id": gid, "scope": scope, "until": row.get("until", ""), "option": option}


def revoke(gid: str, by: str = "", why: str = "") -> bool:
    d = _grants()
    for g in d["grants"]:
        if g.get("id") == gid and g.get("active", True):
            g["active"] = False
            g["revoked"] = time.strftime("%Y-%m-%d %H:%M") + (" " + why if why else "")
            save_json(GRANTS_FILE, d)
            _ledger("grant_revoke", id=gid, by=str(by or "")[:60], why=str(why or "")[:120])
            return True
    return False


def render_grants() -> str:
    gs = active_grants()
    if not gs:
        return ""
    lines = ["STANDING GRANTS (an owner's tap that stands; the policy answers these cards itself):"]
    for g in gs:
        when = f" until {g['until']}" if g.get("until") else (" for this task" if g.get("scope") == "this_task" else "")
        lines.append(f"- [{g['id']}] '{g.get('header')}' in {g.get('lane')}: {g.get('option')}{when} (from {g.get('by')}, {g.get('ts', '')[:10]})")
    return "\n".join(lines)


# ------------------------------------------------------------------ the decision
def ask_first_rule(text: str, lane: str = "") -> dict | None:
    """An owner's own rule (agent/state/rules.json or a lane lesson) that says to ASK on this subject."""
    try:
        import rulings
        hits = rulings.find_agent_rules(str(text or ""), lane=lane)
    except Exception:  # noqa: BLE001
        hits = []
    for h in hits[:3]:
        if ASK_FIRST.search(str(h.get("rule") or "")):
            return h
    return None


def untrusted_trigger(trigger: dict | None) -> str:
    """'' when the trigger is fine; else why it is not: an inbound mail that failed authentication, a line that
    carries the injection marker, inbound material with no verified sender."""
    if not isinstance(trigger, dict):
        return ""
    if trigger.get("marker"):
        return "the material that set this off carried an instruction marker"
    if trigger.get("kind") in ("mail", "email") and trigger.get("authenticated") is False:
        return "the mail that set this off failed sender checks (SPF/DKIM)"
    if trigger.get("untrusted"):
        return "set off by material the machine could not vouch for"
    return ""


def decide(q: dict, lane: str = "", origin: str = "", trigger: dict | None = None, task: str = "") -> dict:
    """-> {"verdict": "ask"|"allow", "why", "source", "grant", "option"}. Never raises."""
    kind = str((q.get("exec") or {}).get("kind") or "")
    text = f"{q.get('header') or ''}\n{q.get('question') or ''}"
    lane = str(q.get("lane") or lane or "personal")
    if forever_gated(kind):
        return {"verdict": "ask", "why": "this kind of action always needs your tap", "source": "forever_gated", "grant": "", "option": ""}
    if MONEY.search(text) or CREDENTIAL.search(text):
        return {"verdict": "ask", "why": "money or a login is involved; always your tap", "source": "protected_words", "grant": "", "option": ""}
    bad = untrusted_trigger(trigger)
    if bad:
        return {"verdict": "ask", "why": bad, "source": "untrusted_trigger", "grant": "", "option": ""}
    rule = ask_first_rule(text, lane)
    if rule:
        return {"verdict": "ask", "why": f"your rule {rule.get('id')}: {str(rule.get('rule') or '')[:120]}", "source": "owner_rule", "grant": "", "option": ""}
    g = find_grant(key_for(q, lane), task=task)
    if g:
        return {"verdict": "allow", "why": f"your standing yes from {g.get('ts', '')[:10]}" + (f" (until {g['until']})" if g.get("until") else ""),
                "source": "grant", "grant": g["id"], "option": str(g.get("option") or recommended_option(q))}
    if kind:
        try:
            import graduation
            if graduation.granted(kind):
                return {"verdict": "allow", "why": f"{kind.replace('_', ' ')} is automatic (you made it so)", "source": "graduation",
                        "grant": "", "option": recommended_option(q)}
        except Exception:  # noqa: BLE001
            pass
    why = "nothing standing covers this" if origin != "chat" else "you asked for this card"
    return {"verdict": "ask", "why": why, "source": "default", "grant": "", "option": ""}


def caution_for(q: dict) -> str:
    kind = str((q.get("exec") or {}).get("kind") or "")
    text = f"{q.get('header') or ''} {q.get('question') or ''}"
    if kind in ("email_send", "imessage_send") or re.search(r"\b(send|text|email|reply)\b", str(q.get("header") or ""), re.I):
        return "Goes to someone outside the house; cannot be unsent once it lands."
    if kind == "agent_run":
        return "Acts in the browser on your accounts."
    if MONEY.search(text):
        return "Money moves; check the amount and who gets it."
    if CREDENTIAL.search(text):
        return "A login is involved; nothing is typed in chat."
    try:
        import graduation
        if kind in graduation._all_forever_gated():
            return "A write to a live record."
    except Exception:  # noqa: BLE001
        pass
    return ""


def annotate(q: dict, lane: str = "", origin: str = "") -> dict:
    """Stamp the lines every card carries by code: reason (why it asks), caution, the rule an 'always' would make,
    the scopes a tap may carry, and its origin. Idempotent; never raises."""
    try:
        lane = str(q.get("lane") or lane or "personal")
        d = decide(q, lane, origin)
        scopes = grant_scopes(q, lane)
        q["grants"] = scopes
        q["origin"] = origin if origin in ORIGINS else str(q.get("origin") or "operator")
        q.setdefault("reason", d["why"] if d["verdict"] == "ask" else d["why"])
        c = caution_for(q)
        if c:
            q.setdefault("caution", c)
        if "always" in scopes:
            opt = _RECOMMENDED.sub("", recommended_option(q)).strip() or "the recommended choice"
            q.setdefault("always_rule", f"From now on, '{str(q.get('header') or '').strip()}' in {lane}: {opt}, without asking.")
    except Exception:  # noqa: BLE001
        pass
    return q


def expires_for(origin: str, has_exec: bool, default_s: float) -> float:
    """The wait for a card delivered at once, by origin (a held card keeps the digest's rules)."""
    if origin == "chat":
        return float(CHAT_EXPIRES_S)
    if origin == "routine":
        return float(ROUTINE_EXPIRES_S)
    return float(default_s)


# ------------------------------------------------------------------ ledgers
def _ledger(event: str, **fields) -> None:
    try:
        import exec_store as es
        es.ledger(event, **fields)
    except Exception:  # noqa: BLE001
        pass


def _change(kind: str, summary: str, undo: dict, by: str) -> str:
    try:
        sys.path.insert(0, str(HERE.parent / "agent"))
        import core
        return core.log_change(kind, summary, undo, by)
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("grants")
    r = sub.add_parser("revoke"); r.add_argument("gid"); r.add_argument("--why", default="")
    dp = sub.add_parser("decide"); dp.add_argument("header"); dp.add_argument("--lane", default=""); dp.add_argument("--kind", default="")
    a = ap.parse_args()
    if a.cmd == "grants":
        print(render_grants() or "(no standing grants)")
    elif a.cmd == "revoke":
        print("revoked" if revoke(a.gid, by="cli", why=a.why) else "no such active grant")
    elif a.cmd == "decide":
        q = {"header": a.header, "question": a.header, "lane": a.lane}
        if a.kind:
            q["exec"] = {"kind": a.kind}
        print(json.dumps(decide(q, a.lane), indent=1))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
