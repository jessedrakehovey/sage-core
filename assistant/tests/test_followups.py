"""Offline tests for the Follow-Through engine + sentinels (scratch ASSISTANT_DATA)."""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_fu_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
sys.path.insert(0, str(HERE.parent / "scripts"))
import followups as fu  # noqa: E402
import tickets  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# 1. every shipped map step builds a valid item with sample vars
m = fu.load_sop_map()
sample = {"unit": "u1", "couple": "A & B", "wedding_date": "2026-11-14", "anchor": "2026-11-14", "what": "x",
          "amount": "$1", "who": "Bob", "who_email": "bob@example.com", "cancel": "c"}
bad = []
for sop, s in m["sops"].items():
    for step in s["steps"]:
        try:
            fu.item_from_map(sop, step, sample, "test")
        except Exception as e:
            bad.append(f"{sop}/{step}: {e}")
check("every map step builds", not bad, "; ".join(bad))

# 2. offsets resolve against the anchor; dedupe on re-arm
wd = (date.today() + timedelta(days=20)).isoformat()
r = fu.arm("wedding-lifecycle", "prep-checkin", {"unit": "p1", "couple": "A & B", "wedding_date": wd, "anchor": wd, "cancel": "c"}, "test")
check("armed with T-14d", r["armed"] and r["fire_at"].startswith((date.today() + timedelta(days=6)).isoformat()), r)
r2 = fu.arm("wedding-lifecycle", "prep-checkin", {"unit": "p1", "couple": "A & B", "wedding_date": wd, "anchor": wd, "cancel": "c"}, "test")
check("dedupe on re-arm", not r2["armed"] and r2.get("dup_of") == r["id"])

# 3. arm_chain skips steps already in the past
past = (date.today() - timedelta(days=40)).isoformat()
res = fu.arm_chain("wedding-lifecycle", {"unit": "p0", "couple": "Old", "wedding_date": past, "anchor": past, "cancel": "c"}, "test",
                   steps=["timeline-request", "prep-checkin", "edit-start"])
check("past steps not armed", all(not v["armed"] for v in res.values()), res)

# 4. tick fires a due item into the ticket queue, one-shot goes done
soon = fu.arm("personal", "reminder", {"unit": "r1", "what": "call mom", "anchor": (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")}, "test")
out = fu.tick()
check("due item fired", out["fired"] >= 1, out)
q = tickets._load()
check("ticket created for it", any(t["source"] == "followups" and t["kind"] == "followup" for t in q.values()))
st = fu.load_state()
check("one-shot marked done", st["items"][soon["id"]]["status"] == "done")

# 4b. a fired one-shot (now done) is NOT re-armed by the next sensor pass
again = fu.arm("personal", "reminder", {"unit": "r1", "what": "call mom", "anchor": (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")}, "test")
check("done one-shot not re-armed", not again["armed"] and again.get("status") == "done", again)

# 5. recurring re-arms with an advanced fire_at and count
rec = fu.arm("home", "maintenance", {"unit": "h1", "what": "filter", "cancel": "c"}, "test")
st = fu.load_state()
st["items"][rec["id"]]["fire_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
fu.save_state(st)
fu.tick()
it = fu.load_state()["items"][rec["id"]]
check("recurring re-armed", it["status"] == "waiting" and it["recur"]["count"] == 1 and fu.parse_iso(it["fire_at"]) > datetime.now(timezone.utc))

# 6. mail trigger matches a mail ticket added after the last scan
aw = fu.arm("personal", "awaiting-reply", {"unit": "w1", "who": "Bob", "who_email": "bob@example.com", "what": "quote", "anchor": date.today().isoformat()}, "test")
tickets.add_many([{"source": "mail:personal", "source_id": "<m1>", "lane": "personal", "kind": "mail", "title": "Re: quote",
                   "summary": "here it is", "payload": {"from": "Bob <bob@example.com>", "subject": "Re: quote"}}], "test")
out = fu.tick()
check("mail trigger matched", out["mail_matched"] == 1 and fu.load_state()["items"][aw["id"]]["status"] == "done", out)

# 7. expiry
ex = fu.arm("wedding-lifecycle", "thank-you", {"unit": "p2", "couple": "C", "wedding_date": past, "anchor": past, "cancel": "c"}, "test")
check("expired at arm time refused", not ex["armed"], ex)

# 8. sentinels: a stale staged ticket is pinged once, then latched
import sentinels
tickets.add_many([{"source": "t", "source_id": "old", "lane": "wedding", "kind": "x", "title": "old thing", "summary": "", "payload": {}}], "test")
db = tickets._load()
for t in db.values():
    if t["source_id"] == "old":
        t["status"], t["updated"] = "staged", "2026-01-01T00:00:00Z"
json.dump(db, open(TMP / "tickets.json", "w"), indent=1)
r = sentinels.run()
import cards as _cards
_waiting = [c for c in _cards.pending() if c.get("header", "").startswith("Waiting")]
check("sentinel: a stale staged ticket is ONE held card for the Monday digest, not a ticket or a mid-week line (2026-10-20)",
      r["pinged"] == 0 and not r.get("folded") and len(_waiting) == 1 and _waiting[0].get("held"), (r, _waiting))
r = sentinels.run()
check("sentinel latched", r["pinged"] == 0 and not r.get("folded"), r)

# 8b. a machinery finding is ONE card by code; while that card is open the finding is not carded again
# (DUPLICATE_ALARM), and once the card is gone the finding is fresh again (WP2, 2026-10-24)
import cards as _cards
feed = {"items": [{"key": "spine:xjob", "text": "spine job 'xjob' has failed 3 runs in a row: boom"}]}
_collect = sentinels.collect
sentinels.collect = lambda: list(feed["items"])
json.dump({}, open(TMP / "sentinels_latch.json", "w"))
r = sentinels.run()
check("machinery finding: one card by code, no ticket", r.get("cards") == 1 and r["pinged"] == 0, r)
open_cards = [c for c in _cards.pending() if c.get("header", "").startswith("Spine job xjob")]
check("the card names the job and its first button runs it", open_cards and open_cards[0]["options"][0].startswith("Run it now"), open_cards)
json.dump({}, open(TMP / "sentinels_latch.json", "w"))          # the latch ran out
r = sentinels.run()
check("open card: the same finding is not carded again", r.get("cards") == 0 and r["pinged"] == 0, r)
for f in (TMP / "cards" / "live").glob("q*.json"):
    f.unlink()                                                     # card answered / expired
json.dump({}, open(TMP / "sentinels_latch.json", "w"))
r = sentinels.run()
check("card gone: the finding is fresh again and carded once more", r.get("cards") == 1, r)
sentinels.collect = _collect

# 8c. a step reads the record before it fires (WP3, 2026-10-24): satisfied = done by record, moot = dismissed,
# open = fires with the read as its evidence
import preconditions
wd_pre = (date.today() + timedelta(days=25)).isoformat()     # T-30d was 5 days ago (due now); T-22d expiry is 3 days out
verdicts = {}
preconditions.check = lambda spec, unit: verdicts.get(unit, ("unknown", "no record"))
st_before = len(tickets._load())
a_sat = fu.arm("wedding-lifecycle", "questionnaire-reminder", {"unit": "pre_sat", "couple": "Sat Couple", "wedding_date": wd_pre, "anchor": wd_pre, "cancel": "c"}, "test")
a_open = fu.arm("wedding-lifecycle", "questionnaire-reminder", {"unit": "pre_open", "couple": "Open Couple", "wedding_date": wd_pre, "anchor": wd_pre, "cancel": "c"}, "test")
a_moot = fu.arm("wedding-lifecycle", "timeline-request", {"unit": "pre_moot", "couple": "Moot Couple", "wedding_date": wd_pre, "anchor": wd_pre, "cancel": "c"}, "test")
check("the shipped steps carry satisfied_when", fu.load_state()["items"][a_sat["id"]]["action"].get("satisfied_when", {}).get("check") == "questionnaire_answered")
verdicts.update({"pre_sat": ("satisfied", "the questionnaire on their HoneyBook project is completed"),
                 "pre_open": ("open", "their questionnaire file is sent and not completed"),
                 "pre_moot": ("moot", "the ledger says cancelled")})
import client_texts as _ct
_ct.auto_fire = lambda *a, **k: {"ok": False, "reason": "test: standing text off"}
fu.tick()
stt = fu.load_state()["items"]
check("a step the record already shows is done by record, no ticket", stt[a_sat["id"]]["status"] == "done" and stt[a_sat["id"]]["evidence"].startswith("by_record"), stt[a_sat["id"]])
check("a moot step (cancelled wedding) is dismissed", stt[a_moot["id"]]["status"] == "dismissed")
fired_t = [t for t in tickets._load().values() if (t.get("payload") or {}).get("followup") == a_open["id"]]
check("an open step fires, and its ticket carries what was read", fired_t and "record checked: their questionnaire file" in str(fired_t[0]["payload"].get("evidence") or ""), fired_t and fired_t[0]["payload"].get("evidence"))
check("no ticket for the satisfied or moot steps", not [t for t in tickets._load().values() if (t.get("payload") or {}).get("followup") in (a_sat["id"], a_moot["id"])])
import outcomes as _oc
rows = {r["ref"]: r for r in _oc.fold().values() if r.get("kind") == "followup"}
check("the ledger has a verified row for the by-record step and a claimed row for the fired one",
      rows.get(a_sat["id"], {}).get("state") == "verified" and rows.get(a_open["id"], {}).get("state") in ("claimed", "verifying"), {k: v.get("state") for k, v in rows.items()})

# 9. a bare-date anchor means 09:00 local that day, never the evening before
it = fu.item_from_map("personal", "reminder", {"unit": "tz", "what": "x", "anchor": "2026-11-14"}, "test")
local = fu.parse_iso(it["fire_at"]).astimezone()
check("date anchor fires 09:00 local on the day", (local.date().isoformat(), local.hour) == ("2026-11-14", 9), it["fire_at"])

# 10. a moved anchor moves a duty that has not fired; a fired / self-advanced one is left alone
wd2 = (date.today() + timedelta(days=40)).isoformat()
wd3 = (date.today() + timedelta(days=47)).isoformat()
v = {"unit": "mv", "couple": "M", "cancel": "c"}
a1 = fu.arm("wedding-lifecycle", "prep-checkin", dict(v, wedding_date=wd2, anchor=wd2), "test")
a2 = fu.arm("wedding-lifecycle", "prep-checkin", dict(v, wedding_date=wd3, anchor=wd3), "test")
moved = fu.load_state()["items"][a1["id"]]["fire_at"]
check("anchor move re-times the waiting duty", a2.get("moved") and moved != a1["fire_at"] and moved[:10] == (date.today() + timedelta(days=33)).isoformat(), (a2, moved))
a3 = fu.arm("wedding-lifecycle", "prep-checkin", dict(v, wedding_date=wd3, anchor=wd3), "test")
check("same anchor is a plain dup", not a3.get("moved") and a3.get("dup_of") == a1["id"], a3)
h1 = fu.arm("home", "maintenance", {"unit": "hvac", "what": "filter"}, "test")
h2 = fu.arm("home", "maintenance", {"unit": "hvac", "what": "filter"}, "test")
check("unanchored recurring duty is never re-timed", h1["armed"] and not h2.get("moved"), h2)

# 11. cancel_unit stops a cancelled wedding's chain and closes its open follow-up tickets
cv = {"unit": "gone", "couple": "Gone", "cancel": "c"}
pastwd = (date.today() - timedelta(days=1)).isoformat()
fu.arm_chain("wedding-lifecycle", dict(cv, wedding_date=wd2, anchor=wd2), "test", steps=["timeline-request", "prep-checkin", "edit-start"])
g = fu.arm("wedding-lifecycle", "ingest-gate", dict(cv, wedding_date=pastwd, anchor=(datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")), "test")
fu.tick()
tk = [t for t in tickets._load().values() if (t.get("payload") or {}).get("followup") == g["id"]]
check("recurring gate fired a ticket", len(tk) == 1 and tk[0]["status"] == "new", tk)
check("cancel instruction names the real id", "<id>" not in (tk[0]["payload"].get("instruction") or "x<id>") or g["id"] in tk[0]["payload"]["cancel"], tk[0]["payload"])
n = fu.cancel_unit("wedding-lifecycle", "gone", "project is cancelled")
left = [i for i in fu.load_state()["items"].values() if i.get("unit") == "gone" and i["status"] in ("waiting", "fired")]
check("cancel_unit dismissed the chain", n >= 4 and not left, (n, left))
check("cancel_unit closed the open ticket", tickets._load()[tk[0]["id"]]["status"] == "dismissed")
other = [i for i in fu.load_state()["items"].values() if i.get("unit") == "mv" and i["status"] == "waiting"]
check("cancel_unit left other weddings alone", len(other) == 1)

# 12. a busy queue defers the fire instead of losing the duty
d = fu.arm("personal", "reminder", {"unit": "busy", "what": "x", "anchor": (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")}, "test")
real_add = tickets.add_many
tickets.add_many = lambda items, owner: -1
out = fu.tick()
tickets.add_many = real_add
it = fu.load_state()["items"][d["id"]]
check("QUEUE_BUSY keeps the duty waiting", it["status"] == "waiting" and it["fire_count"] == 0 and out["fired"] == 0, (it["status"], out))
out = fu.tick()
check("deferred duty fires on the next tick", fu.load_state()["items"][d["id"]]["status"] == "done" and out["fired"] == 1, out)

# 13. one busy queue stops the tick's firing (no 20 s wait per item) and keeps every duty
past1 = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
b1 = fu.arm("personal", "reminder", {"unit": "bz1", "what": "one", "anchor": past1}, "test")
b2 = fu.arm("personal", "reminder", {"unit": "bz2", "what": "two", "anchor": past1}, "test")
calls = []
tickets.add_many = lambda items, owner: calls.append(1) or -1
out = fu.tick()
tickets.add_many = real_add
stt = fu.load_state()["items"]
check("a busy queue is tried once per tick, not once per duty", len(calls) == 1 and out["fired"] == 0, (len(calls), out))
check("both duties are still waiting", stt[b1["id"]]["status"] == "waiting" and stt[b2["id"]]["status"] == "waiting")
out = fu.tick()
check("both fire on the next tick", out["fired"] == 2, out)

# 14. a mail-triggered duty deferred by a busy queue still sees the mail next tick
mw = fu.arm("personal", "awaiting-reply", {"unit": "mw", "who": "Bob", "who_email": "bob@example.com", "what": "quote",
                                           "anchor": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")}, "test")
real_add([{"source": "mail:personal", "source_id": "m-bob-1", "lane": "personal", "kind": "mail", "title": "Re: quote",
           "summary": "here it is", "payload": {"from": "Bob <bob@example.com>", "subject": "Re: quote"}}], "test")
tickets.add_many = lambda items, owner: -1
out = fu.tick()
tickets.add_many = real_add
check("deferred mail match is not counted", out["mail_matched"] == 0 and fu.load_state()["items"][mw["id"]]["status"] == "waiting", out)
out = fu.tick()
check("the same mail still matches on the next tick", out["mail_matched"] == 1, out)

# 15. a live recurring duty follows the map's cap; an anchor moved into the past retires the stale duty
dv = {"unit": "cap", "couple": "Cap", "cancel": "c", "wedding_date": wd2, "anchor": wd2}
d1 = fu.arm("wedding-lifecycle", "delivery-check", dv, "test")
stc = fu.load_state()
stc["items"][d1["id"]]["recur"]["max"] = 8
fu.save_state(stc)
fu.arm("wedding-lifecycle", "delivery-check", dv, "test")
check("recur cap refreshed from the map", fu.load_state()["items"][d1["id"]]["recur"]["max"] == m["sops"]["wedding-lifecycle"]["steps"]["delivery-check"]["recur"]["max"])
t1 = fu.arm("wedding-lifecycle", "timeline-request", {"unit": "early", "couple": "E", "cancel": "c", "wedding_date": wd3, "anchor": wd3}, "test")
sooner = (date.today() + timedelta(days=5)).isoformat()      # T-30d is now 25 days in the past
fu.arm_chain("wedding-lifecycle", {"unit": "early", "couple": "E", "cancel": "c", "wedding_date": sooner, "anchor": sooner}, "test", steps=["timeline-request"])
check("a duty whose new time already passed is retired, not left at the old date", fu.load_state()["items"][t1["id"]]["status"] == "dismissed")

# 16. cancel_unit also closes the ticket of a one-shot that already fired, but never a claimed one
cu = {"unit": "cu", "couple": "CU", "cancel": "c"}
o1 = fu.arm("personal", "reminder", {"unit": "cu", "what": "x", "anchor": past1}, "test")
o2 = fu.arm("home", "delivery-arriving", {"unit": "cu", "what": "y", "anchor": past1}, "test")
fu.tick()
tk1 = next(t for t in tickets._load().values() if (t.get("payload") or {}).get("followup") == o1["id"])
tk2 = next(t for t in tickets._load().values() if (t.get("payload") or {}).get("followup") == o2["id"])
tickets._transition(tk2["id"], "claimed", "operator", "", {"new"})
fu.cancel_unit("personal", "cu", "gone")
fu.cancel_unit("home", "cu", "gone")
dbt = tickets._load()
check("fired one-shot's open ticket is closed", dbt[tk1["id"]]["status"] == "dismissed", dbt[tk1["id"]]["status"])
check("a ticket the operator has claimed is left alone", dbt[tk2["id"]]["status"] == "claimed", dbt[tk2["id"]]["status"])

# 17. a lead who booked a meeting gets no nudge at all (2026-10-08, Arden & Reese: "no email needed")
import lead_meetings as lm  # noqa: E402
import sense_honeybook as shb  # noqa: E402
real = datetime.now(timezone.utc)
soon_m = (real + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
SNAPM = {"projects": [{"id": "ws7", "project_id": "pj7", "name": "Arden and Reese"}],
         "meetings": [{"id": "m7", "title": "Intro call", "type": "meeting", "start": soon_m, "workspace_id": "ws7", "clients": []}]}
lm.SNAP = TMP / "hb_snap.json"
lm.SNAP.write_text(json.dumps(SNAPM), encoding="utf-8")
check("the snapshot meeting is found by project id", (lm.for_unit("pj7", "") or {}).get("id") == "m7")
check("and by the couple's project name", (lm.for_unit("other", "Arden and Reese") or {}).get("id") == "m7")
check("a lead without one: nothing", lm.for_unit("pj8", "Pat Lee") is None)
ago = (real - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
n1 = fu.arm("inquiry", "quote-followup-48h", {"unit": "pj7", "couple": "Arden and Reese", "anchor": ago}, "test")
n7 = fu.arm("inquiry", "quote-followup-7d", {"unit": "pj7", "couple": "Arden and Reese", "anchor": ago}, "test")
n2 = fu.arm("inquiry", "quote-followup-48h", {"unit": "pj8", "couple": "Pat Lee", "anchor": ago}, "test")


def fu_tickets():
    return {(t.get("payload") or {}).get("followup"): t for t in tickets._load().values()}


fu.tick()
tk, its = fu_tickets(), fu.load_state()["items"]
check("the booked lead's nudge makes no ticket", n1["id"] not in tk and its[n1["id"]]["status"] == "waiting"
      and its[n1["id"]]["meeting_hold"]["id"] == "m7", its[n1["id"]])
check("a lead without a meeting gets the plain nudge", n2["id"] in tk and "MEETING BOOKED" not in tk[n2["id"]]["payload"]["instruction"])
fu.tick(real + timedelta(days=5))       # past the 48h nudge's expiry and the 7d nudge's time, meeting still ahead
tk, its = fu_tickets(), fu.load_state()["items"]
check("held nudges neither fire nor expire while the meeting is ahead", n1["id"] not in tk and n7["id"] not in tk
      and its[n1["id"]]["status"] == its[n7["id"]]["status"] == "waiting", (its[n1["id"]]["status"], its[n7["id"]]["status"]))
lm.SNAP.write_text(json.dumps(dict(SNAPM, meetings=[])), encoding="utf-8")     # the meeting is over (no longer upcoming)
fu.tick(real + timedelta(days=2, hours=12))
check("the day after the meeting is left alone", n1["id"] not in fu_tickets())
fu.tick(real + timedelta(days=3, hours=1))
check("the first held nudge fires after the meeting", n1["id"] in fu_tickets())
fu.tick(real + timedelta(days=5))
tk, its = fu_tickets(), fu.load_state()["items"]
check("a meeting with no outcome: nudging resumes once", n1["id"] in tk and n7["id"] not in tk
      and its[n7["id"]]["status"] == "done", its[n7["id"]]["status"])
lm.SNAP.write_text(json.dumps(SNAPM), encoding="utf-8")
check("a quiet-lead check-in is skipped for a booked lead", shb._meeting_note("pj7", "Arden and Reese", SNAPM) != ""
      and shb._meeting_note("pj8", "Pat Lee", SNAPM) == "")
(TMP / "leads").mkdir(exist_ok=True)
(TMP / "leads" / "tok9.json").write_text(json.dumps({"lead": {"name": "example example"}, "meeting": {
    "id": "c9", "start": soon_m, "when": "Mon Oct 12, 1:00 PM PT", "type": "call"}}), encoding="utf-8")
check("a quiz lead's file meeting counts (Typeform token unit)", (lm.for_unit("tok9", "", {"projects": []}) or {}).get("id") == "c9")

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)} (scratch {TMP})")
sys.exit(1 if FAILS else 0)
