"""graduation.py - autonomy is earned per action class, offered never assumed (WP6, 2026-10-24).

The property OS's followups/graduation.py: an allowlisted action class with enough resolved taps, a high approve
rate, zero breaches and zero clawbacks is RECOMMENDED for autonomy and the owner flips it. Ported, scaled to this
volume (tracking.json -> graduation):

  classes      the exec kinds that may ever graduate (internal writes gated by a card today; list comes from
               the CRM connector's GRADUATION_CLASSES attr). Anything the protected core names is
               FOREVER_GATED here whatever the file says: outbound sends, money, the doer's steps, prices, dates.
  min_samples  resolved taps of that kind (default 10; the property uses 25 at its volume)
  approve_min  share approved (not Skip/Drop), default 0.90
  zero edits, zero failed outcomes, zero undone taps in the window

Eligible -> ONE `decide`-shaped card by code ("Make X automatic?": Make it automatic (Recommended) / Keep asking).
The tap (cards.sweep -> _act_on_graduation) writes `granted[kind]` here, ledgered; triage_next then queues that kind
as a standing action (approval ref "standing:graduation <kind> (owner tap <ts>)") and the outcome ledger verifies
it like anything else. A single failed outcome of a granted kind demotes it (`demote`) and says so once.

  python assistant/scripts/graduation.py status | tick [--dry-run] | demote <kind>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from alib import DATA, load_json  # noqa: E402
import crm  # noqa: E402

TRACKING = ROOT / "tracking.json"
STATE = DATA / "graduation.json"
# Engine-owned forever-gated kinds (outbound sends, money, browser agent, etc.); CRM-specific
# forever-gated kinds (e.g. outbound CRM messages) are merged in dynamically via crm.kinds().
FOREVER_GATED = {"email_send", "imessage_send", "ads_apply", "agent_run", "calendar_hold", "local", "tracking_fix"}
DEFAULTS = {"enabled": True, "min_samples": 10, "approve_min": 0.90, "window_days": 90,
            "classes": []}
SKIP = re.compile(r"^\s*(skip|drop|dismiss|ignore|not now|later|remind me|leave it|never mind|cancel|no\b)", re.I)


def _all_forever_gated() -> frozenset:
    """Engine-owned forever-gated kinds plus any the CRM connector adds."""
    return frozenset(FOREVER_GATED | crm.kinds("FOREVER_GATED"))


def _graduation_classes() -> list:
    """Exec kinds eligible for graduation: the engine's own (sheet_set) plus the CRM connector's."""
    return ["sheet_set"] + sorted(crm.kinds("GRADUATION_CLASSES") - {"sheet_set"})


def cfg() -> dict:
    t = load_json(TRACKING, {}) or {}
    g = t.get("graduation") if isinstance(t.get("graduation"), dict) else {}
    base_classes = g.get("classes") if g.get("classes") else _graduation_classes()
    out = {**DEFAULTS, **{k: v for k, v in g.items() if not str(k).startswith("_")}}
    out["classes"] = [c for c in base_classes if c not in _all_forever_gated()]
    return out


def _state() -> dict:
    s = load_json(STATE, {}) or {}
    s.setdefault("granted", {})
    s.setdefault("offered", {})
    s.setdefault("demoted", {})
    return s


def _save(s: dict) -> None:
    from alib import save_json
    save_json(STATE, s)


def granted(kind: str) -> bool:
    return bool(_state()["granted"].get(kind))


def stats(kind: str, window_days: int) -> dict:
    """Resolved taps on cards whose exec kind is `kind` in the window: approved / skipped / edited / undone, and
    failed outcomes of that kind."""
    import cards
    import outcomes
    since = time.time() - window_days * 86400
    answers = cards._answers_index()
    n = approved = skipped = edited = undone = 0
    try:
        files = list(cards.ARCHIVE.glob("q*.json"))
    except OSError:
        files = []
    for f in files:
        c = cards._read(f)
        if not isinstance(c, dict) or float(c.get("ts") or 0) < since:
            continue
        q = (c.get("questions") or [{}])[0]
        if str((q.get("exec") or {}).get("kind") or "") != kind:
            continue
        a = answers.get(c.get("id"))
        if not a:
            continue
        n += 1
        if a.get("undone"):
            undone += 1
        elif SKIP.match(str(a.get("option") or "")):
            skipped += 1
        else:
            approved += 1
            if a.get("revised"):
                edited += 1
    failed = sum(1 for r in outcomes.fold().values() if r.get("kind") == kind and r.get("state") in ("failed",)
                 and outcomes._ts(r.get("updated")) >= since)
    return {"kind": kind, "resolved": n, "approved": approved, "skipped": skipped, "edited": edited, "undone": undone, "failed_outcomes": failed,
            "approve_rate": round(approved / n, 2) if n else 0.0}


def eligible(kind: str, c: dict | None = None) -> tuple:
    c = c or cfg()
    if kind in _all_forever_gated() or kind not in c["classes"]:
        return False, "never graduates"
    s = stats(kind, int(c["window_days"]))
    if s["resolved"] < int(c["min_samples"]):
        return False, f"{s['resolved']} of {c['min_samples']} resolved taps"
    if s["approve_rate"] < float(c["approve_min"]):
        return False, f"approve rate {s['approve_rate']} under {c['approve_min']}"
    if s["edited"] or s["undone"] or s["failed_outcomes"]:
        return False, f"{s['edited']} edited, {s['undone']} undone, {s['failed_outcomes']} failed outcomes"
    return True, f"{s['approved']} untouched approvals in a row, nothing failed"


def offer(kind: str, why: str, dry: bool = False) -> dict:
    """ONE decide card by code. The tap flips the gate; code never flips it alone."""
    import cards
    _cn = crm.name()
    # CRM-specific kind descriptions: engine owns only sheet_set; connector kinds fall back to the kind name.
    names = {"sheet_set": "writing cells on the pricing sheet"}
    what = names.get(kind, kind.replace("_", " "))
    q = {"header": f"Make it automatic? {what}"[:60], "lane": "wedding", "audience": "internal", "urgent": False,
         "question": (f"You have approved {what} unchanged every time ({why}). Shall I do it without a card from now on? "
                      "It stays in the ledger and read back like today; one failure puts it back on a card, and you can undo this any time."),
         "options": [{"label": "Make it automatic (Recommended)", "description": "code queues it under your standing OK"},
                     {"label": "Keep asking", "description": "a card each time, as now"}],
         "graduation_class": kind, "details": "Graduation (tracking.json -> graduation). Never offered for sends, money, the doer's steps, prices or dates."}
    if dry:
        return {"posted": False, "dry": True, "card": q}
    r = cards.post([q], "graduation", lane="wedding")
    st = _state()
    st["offered"][kind] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "qid": r.get("qid", ""), "posted": bool(r.get("posted")), "why": why}
    _save(st)
    return r


def answer(kind: str, option: str, by: str = "owner1") -> dict:
    st = _state()
    opt = str(option or "").lower()
    if opt.startswith("make it automatic"):
        st["granted"][kind] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "by": by}
        st["demoted"].pop(kind, None)
        _save(st)
        _ledger("graduation_granted", kind=kind, by=by)
        return {"ok": True, "granted": True}
    st["offered"][kind] = {**(st["offered"].get(kind) or {}), "declined": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _save(st)
    _ledger("graduation_declined", kind=kind, by=by)
    return {"ok": True, "granted": False}


def demote(kind: str, why: str = "a failed outcome") -> bool:
    st = _state()
    if not st["granted"].get(kind):
        return False
    st["granted"].pop(kind, None)
    st["demoted"][kind] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "why": str(why)[:200]}
    _save(st)
    _ledger("graduation_demoted", kind=kind, why=str(why)[:120])
    try:
        import cards
        cards.say("graduation", "wedding", f"I have put {kind.replace('_', ' ')} back on a card: {why}.")
    except Exception:  # noqa: BLE001
        pass
    return True


def _ledger(event: str, **fields) -> None:
    try:
        import exec_store as es
        es.ledger(event, **fields)
    except Exception:  # noqa: BLE001
        pass


def tick(dry: bool = False) -> dict:
    """Daily: offer what became eligible (once per 30 days per kind), demote a granted kind with a fresh failure."""
    c = cfg()
    out = {"offered": [], "demoted": [], "eligible": [], "skipped": []}
    if not c.get("enabled", True):
        return out
    st = _state()
    import outcomes
    for kind in c["classes"]:
        if st["granted"].get(kind):
            since = outcomes._ts(st["granted"][kind].get("ts"))
            bad = [r for r in outcomes.fold().values() if r.get("kind") == kind and r.get("state") == "failed" and outcomes._ts(r.get("updated")) >= since]
            if bad:
                det = str((bad[-1].get("checks") or [{}])[-1].get("detail") or "a failed outcome")
                if not dry:
                    demote(kind, det[:120])
                out["demoted"].append(kind)
            continue
        ok, why = eligible(kind, c)
        if not ok:
            out["skipped"].append(f"{kind}: {why}")
            continue
        out["eligible"].append(kind)
        last = st["offered"].get(kind) or {}
        if last.get("ts") and time.time() - outcomes._ts(last["ts"]) < 30 * 86400:
            continue
        if not dry:
            offer(kind, why)
        out["offered"].append(kind)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("tick"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("demote"); p.add_argument("kind"); p.add_argument("--why", default="by hand")
    a = ap.parse_args()
    if a.cmd == "status":
        c = cfg()
        print(json.dumps({"granted": _state()["granted"], "stats": {k: stats(k, int(c["window_days"])) for k in c["classes"]}}, indent=1))
    elif a.cmd == "tick":
        r = tick(dry=a.dry_run)
        print("RESULT GRADUATION " + json.dumps(r))
    elif a.cmd == "demote":
        print("demoted" if demote(a.kind, a.why) else "not granted")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
