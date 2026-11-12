"""Offline tests for assistant/agent/preconditions.py (Seed D, problem spaces: one chase chain as data, one port of
the preconditions check). The four verdicts; a step reads the record before it fires; days_since_<event> facts come
from the channel history and read UNKNOWN on an unstamped or stale record; the chain names the next due step in
plain words, never advances on UNKNOWN, reads an answered ask as moot, and the pack can register its own facts.
No network, no model, scratch files only.
   python assistant/tests/test_preconditions.py
"""
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "agent"))
import preconditions as pc  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


NOW = datetime(2026, 11, 1, 12, 0)
CHAIN = {"chain": "chase", "initial": "pending_ask",
         "operators": [
             {"id": "first_ask", "precond": [], "effect": "asked_once", "gated": True, "script": "draft_ask"},
             {"id": "re_ask", "precond": ["asked_once", "days_since_ask>=2"], "effect": "asked_twice", "gated": True, "script": "draft_ask"},
             {"id": "change_channel", "precond": ["asked_twice", "days_since_reask>=2"], "effect": "channel_changed", "gated": True, "script": "draft_ask"},
             {"id": "escalate", "precond": ["channel_changed", "days_since>=3"], "effect": "escalated", "gated": True, "script": "escalation.json"}]}

# --- the chain as data
ok(pc.validate(CHAIN) == [], "the plan's chain validates")
ok(pc.validate({"chain": "x", "initial": "s", "operators": [{"id": "a", "effect": "e"}, {"id": "a", "effect": "e"}]})
   == ["operator id 'a' repeats", "a: effect 'e' repeats"], "repeated ids and effects are named")
ok(pc.validate("nope") == ["the chain is not an object"] and "no operators" in pc.validate({"chain": "x", "initial": "s"}), "malformed chains")
tmp = Path(tempfile.mkdtemp(prefix="pc_"))
(tmp / "chain.json").write_text(json.dumps(CHAIN), encoding="utf-8")
ok(pc.load_chain(tmp / "chain.json")["chain"] == "chase", "load_chain reads the file")
(tmp / "bad.json").write_text(json.dumps({"chain": "x"}), encoding="utf-8")
try:
    pc.load_chain(tmp / "bad.json")
    ok(False, "load_chain raises on a bad chain")
except ValueError as e:
    ok("no initial state" in str(e), f"load_chain raises on a bad chain: {e}")

# --- a fresh record: first ask is due
fresh = {"as_of": "2026-11-01T11:00", "status": "pending"}
ex = pc.explain(CHAIN, fresh, NOW)
ok(ex["operator"]["id"] == "first_ask" and ex["verdict"] == pc.SATISFIED and ex["state"] == "pending_ask", f"nothing asked yet: first ask is due ({ex['line']})")
ok(ex["line"].startswith("chase chain says: first ask is due"), f"plain words: {ex['line']}")
ok(pc.applicable(CHAIN, fresh, NOW)["id"] == "first_ask", "applicable() returns the operator")

# --- asked once, yesterday: re-ask not due yet (days_since_ask>=2 open)
r1 = {"as_of": "2026-11-01T11:00", "status": "awaiting",
      "channel_history": [{"channel": "chat", "ts": "2026-10-31T09:00", "result": "no_reply"}]}
ex = pc.explain(CHAIN, r1, NOW)
ok(ex["operator"] is None and ex["verdict"] == pc.OPEN and ex["state"] == "asked_once", f"asked once a day ago: re-ask not due ({ex['line']})")
ok("re ask is not due yet" in ex["line"] and "short of days_since_ask >= 2" in ex["line"], f"the line says why: {ex['line']}")

# --- asked once, three days ago: re-ask is due, with the evidence in words
r2 = {"as_of": "2026-11-01T11:00", "status": "awaiting",
      "channel_history": [{"channel": "text", "ts": "2026-10-29T09:00", "result": "no_reply"}]}
ex = pc.explain(CHAIN, r2, NOW)
ok(ex["operator"]["id"] == "re_ask", "asked once three days ago: re-ask is due")
ok("asked 3 days ago on text, no reply" in ex["line"] and "last ask 3 days ago (meets days_since_ask >= 2)" in ex["line"], f"evidence in words: {ex['line']}")
v, ev = pc.evaluate(CHAIN["operators"][1], r2, NOW)
ok(v == pc.SATISFIED and len(ev) == 2, f"evaluate -> (satisfied, two evidence lines): {ev}")
v, ev = pc.evaluate(CHAIN["operators"][0], r2, NOW)
ok(v == pc.SATISFIED and ev[0].startswith("already asked once"), f"a step whose effect already holds is done by record: {ev}")

# --- asked twice on one channel, the second ask three days ago: change channel is due
r3 = {"as_of": "2026-11-01T11:00", "status": "awaiting",
      "channel_history": [{"channel": "chat", "ts": "2026-10-27T09:00", "result": "no_reply"},
                          {"channel": "chat", "ts": "2026-10-29T09:00", "result": "no_reply"}]}
ex = pc.explain(CHAIN, r3, NOW)
ok(ex["operator"]["id"] == "change_channel" and ex["state"] == "asked_twice", f"two unanswered asks on chat: change channel is due ({ex['line']})")
ok("two asks on chat went unanswered" in ex["line"], f"the stall is said in words: {ex['line']}")
ok(pc.events(r3)["reask"] == "2026-10-29T09:00" and pc.events(r3)["first_ask"] == "2026-10-27T09:00", "events: reask = the second unanswered ask")

# --- asked twice, the re-ask yesterday: change channel not due yet
r3b = {"as_of": "2026-11-01T11:00", "status": "awaiting",
       "channel_history": [{"channel": "chat", "ts": "2026-10-27T09:00", "result": "no_reply"},
                           {"channel": "chat", "ts": "2026-10-31T09:00", "result": "no_reply"}]}
ex = pc.explain(CHAIN, r3b, NOW)
ok(ex["operator"] is None and ex["verdict"] == pc.OPEN and "change channel is not due yet" in ex["line"], f"re-asked yesterday: wait ({ex['line']})")

# --- channel changed four days ago, still nothing: escalate is due
r4 = {"as_of": "2026-11-01T11:00", "status": "awaiting",
      "channel_history": [{"channel": "chat", "ts": "2026-10-20T09:00", "result": "no_reply"},
                          {"channel": "chat", "ts": "2026-10-22T09:00", "result": "no_reply"},
                          {"channel": "text", "ts": "2026-10-28T09:00", "result": "no_reply"}]}
ex = pc.explain(CHAIN, r4, NOW)
ok(ex["operator"]["id"] == "escalate" and ex["state"] == "channel_changed", f"asked on a second channel four days ago: escalate is due ({ex['line']})")
ok("asked again on text 4 days ago after the stall" in ex["line"], f"the channel change is read from the history: {ex['line']}")

# --- escalated: every step done, what is left is a decision
r5 = dict(r4, events={"escalated": "2026-10-30T09:00"})
ex = pc.explain(CHAIN, r5, NOW)
ok(ex["operator"] is None and ex["verdict"] == pc.SATISFIED and ex["state"] == "escalated" and "every step is done" in ex["line"], f"escalated: {ex['line']}")
r5b = dict(r4, facts=["escalated"])
ok(pc.explain(CHAIN, r5b, NOW)["state"] == "escalated", "a fact the writer asserts counts too")

# --- a reply after the last ask: moot, nothing to chase
r6 = {"as_of": "2026-11-01T11:00", "status": "awaiting",
      "channel_history": [{"channel": "chat", "ts": "2026-10-27T09:00", "result": "no_reply"},
                          {"channel": "chat", "ts": "2026-10-29T09:00", "result": "replied"}]}
ex = pc.explain(CHAIN, r6, NOW)
ok(ex["operator"] is None and ex["verdict"] == pc.MOOT and "nothing to do" in ex["line"], f"answered: moot ({ex['line']})")
ok(pc.check("asked_once", r6, NOW) == (pc.OPEN, "every ask on the record was answered"), "asked_once is open once the asks were answered")
r7 = dict(r3, status="answered")
ex = pc.explain(CHAIN, r7, NOW)
ok(ex["verdict"] == pc.MOOT and "the record is answered" in ex["line"], "a terminal status is moot whatever the history")
ok(pc.explain(CHAIN, dict(r3, status="answered"), NOW, terminal_statuses=("closed",))["operator"]["id"] == "change_channel",
   "a binding passes its own terminal statuses")

# --- UNKNOWN never advances: a stale or unstamped record
stale = dict(r2, as_of="2026-10-29T09:00")
ex = pc.explain(CHAIN, stale, NOW)
ok(ex["operator"] is None and ex["verdict"] == pc.UNKNOWN, "a record read three days ago cannot say whether the re-ask is due")
ok("UNKNOWN whether re ask is due" in ex["line"] and "over its 36 h limit" in ex["line"] and "Nothing advances" in ex["line"], f"the line says so: {ex['line']}")
ok(pc.explain(CHAIN, stale, NOW, max_age_hours=100)["operator"]["id"] == "re_ask", "a binding's longer limit reads the same record as fresh")
unst = {k: v for k, v in r2.items() if k != "as_of"}
ex = pc.explain(CHAIN, unst, NOW)
ok(ex["verdict"] == pc.UNKNOWN and "carries no as_of" in ex["line"], f"no as_of: UNKNOWN ({ex['line']})")
ok(pc.applicable(CHAIN, unst, NOW) is None, "applicable() is None on UNKNOWN")
ok(pc.explain(CHAIN, {"status": "pending"}, NOW)["operator"]["id"] == "first_ask", "a record with no asks needs no stamp: the first ask has no time fact")
noti = {"as_of": "2026-11-01T11:00", "status": "awaiting", "channel_history": [{"channel": "chat", "ts": "", "result": "no_reply"}]}
v, ev = pc.check("days_since_ask>=2", noti, NOW)
ok(v == pc.UNKNOWN and "carries no time" in ev, f"an unstamped ask reads UNKNOWN: {ev}")

# --- an older row's counters with a reply in the middle: three sends, the last two unanswered (the derived middle
#     entry has no stamp and must stay in the middle)
mid = {"as_of": "2026-11-01T11:00", "status": "awaiting", "sends": 3, "unanswered": 2,
       "first_sent": "2026-10-15T16:22", "last_sent": "2026-10-22T23:22", "last_reply_ts": "2026-10-16T00:12"}
ex = pc.explain(CHAIN, mid, NOW)
ok(ex["operator"]["id"] == "change_channel" and "two asks on chat went unanswered" in ex["line"], f"an unstamped middle entry keeps its place: {ex['line']}")

# --- relative stamps: a replay fixture keeps its age whatever day it runs
rel = {"as_of": "now-1h", "status": "awaiting",
       "channel_history": [{"channel": "chat", "ts": "now-5d", "result": "no_reply"}, {"channel": "chat", "ts": "now-3d", "result": "no_reply"}]}
ex = pc.explain(CHAIN, rel, NOW)
ok(ex["operator"]["id"] == "change_channel" and "second ask 3 days ago" in ex["line"], f"relative stamps resolve against now: {ex['line']}")
ok(pc.explain(CHAIN, dict(rel, as_of="now-40h"), NOW)["verdict"] == pc.UNKNOWN, "a relative as_of over the limit is stale")
ok(pc.record_age_hours({"as_of": "now"}, NOW) == 0.0, "'now' is the clock itself")

# --- an older row with only counters reads the same way (outreach derives the history)
old = {"as_of": "2026-11-01T11:00", "status": "awaiting", "sends": 2, "unanswered": 2,
       "first_sent": "2026-10-20T09:00", "last_sent": "2026-10-25T09:00"}
ex = pc.explain(CHAIN, old, NOW)
ok(ex["operator"]["id"] == "change_channel" and ex["state"] == "asked_twice", f"counters only: two unanswered sends read as asked twice ({ex['line']})")

# --- check(): the grammar
ok(pc.check("", r2, NOW) == (pc.SATISFIED, "no condition"), "an empty precondition is satisfied")
ok(pc.check("days_since_ask<2", r2, NOW)[0] == pc.OPEN and pc.check("days_since_ask>2", r2, NOW)[0] == pc.SATISFIED, "comparators")
ok(pc.check("days_since>=3", r2, NOW)[0] == pc.SATISFIED, "bare days_since = the latest entry")
ok(pc.check("days_since_reply>=1", r2, NOW) == (pc.UNKNOWN, "no reply on the record"), "a missing event reads UNKNOWN")
ok(pc.check("fog_index>=2", r2, NOW) == (pc.UNKNOWN, "no reader for 'fog_index'"), "an unregistered quantity reads UNKNOWN")
ok(pc.check("owner_signed", r2, NOW) == (pc.UNKNOWN, "no reader for 'owner_signed'"), "an unregistered fact reads UNKNOWN")
ok(pc.check("owner_signed", dict(r2, facts=["owner_signed"]), NOW) == (pc.SATISFIED, "the record says owner_signed"), "a fact the record asserts is satisfied without a reader")
ok(pc.check({"check": "nope"}, r2, NOW) == (pc.UNKNOWN, "no reader for 'nope'"), "a dict precondition with no reader")

# --- the pack registers its own facts
pc.register("quote_on_file", lambda rec, now, **kw: (pc.SATISFIED, "quote dated " + str(rec.get("quote_date"))) if rec.get("quote_date") else (pc.OPEN, "no quote"))
ok(pc.check("quote_on_file", {"quote_date": "2026-10-30"}, NOW) == (pc.SATISFIED, "quote dated 2026-10-30"), "a registered fact")
ok(pc.check({"check": "quote_on_file"}, {}, NOW) == (pc.OPEN, "no quote"), "the same fact as a dict spec")
pc.register("boom", lambda rec, now, **kw: 1 / 0)
ok(pc.check("boom", {}, NOW) == (pc.UNKNOWN, "boom failed: ZeroDivisionError"), "a reader that raises reads UNKNOWN")
pc.register("truthy", lambda rec, now, **kw: True)
ok(pc.check("truthy", {}, NOW) == (pc.SATISFIED, ""), "a bare True is satisfied")
pc.register_quantity("tries_", lambda rec, now, ch: (len([e for e in rec.get("channel_history") or [] if e.get("channel") == ch]), f"{ch} tries"))
ok(pc.check("tries_chat>=2", r3, NOW)[0] == pc.SATISFIED and pc.check("tries_text>=1", r3, NOW)[0] == pc.OPEN, "a prefix quantity with its argument")
ok(pc.evaluate({"id": "s", "precond": ["asked_once", "owner_signed"], "effect": "x"}, r2, NOW)[0] == pc.UNKNOWN, "one UNKNOWN precondition makes the step UNKNOWN")
ok(pc.evaluate({"id": "s", "precond": ["asked_once", "answered"], "effect": "x"}, r6, NOW)[0] == pc.MOOT, "moot outranks everything")

# --- garbage never raises
ok(pc.explain(CHAIN, None, NOW)["operator"]["id"] == "first_ask" and pc.explain(None, r2, NOW)["operator"] is None, "non-dict inputs pass through")
ok(pc.check("asked_once", {"channel_history": "nope"}, NOW)[0] == pc.OPEN, "a malformed history is an empty one")

# --- the CLI
(tmp / "rec.json").write_text(json.dumps(r3), encoding="utf-8")
import contextlib, io  # noqa: E401,E402
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = pc.main(["explain", str(tmp / "chain.json"), str(tmp / "rec.json"), "--now", NOW.strftime("%Y-%m-%dT%H:%M")])   # the fixtures are dated; a real clock would read them stale
out = json.loads(buf.getvalue())
ok(rc == 0 and out["operator"]["id"] == "change_channel", "explain CLI")

# --- the engine file names no organization (identity words from the engine's own classifier, never a literal list)
sys.path.insert(0, str(ROOT))
try:
    import engine as _eng
    ov = _eng.org_values()
    words = {str(w) for k in ("names", "words", "hosts", "domains") for w in (ov.get(k) or []) if len(str(w)) > 3}
    if len(str(ov.get("org_name") or "")) > 3:
        words.add(str(ov.get("org_name")))
except Exception:  # noqa: BLE001
    words = set()
txt = (ROOT / "agent" / "preconditions.py").read_text(encoding="utf-8").lower()
hits = sorted(w for w in words if str(w).lower() in txt)
ok(not hits, f"preconditions.py names no organization identity word: {hits[:3]}")
print(f"ALL {N} OK")
