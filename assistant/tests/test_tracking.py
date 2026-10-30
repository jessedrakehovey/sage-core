"""Offline tests for the tracking layer (assistant/scripts/tracking/): drift detectors, the chaser's state
machine, the advice line, the weekly card, and the tracking_fix exec kind. Scratch ASSISTANT_DATA; no network."""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_trk_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")   # cards.post below must never touch the live card store
os.environ["WV_NO_TRIAGE"] = "1"
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent / "scripts" / "tracking"))
import common as C  # noqa: E402
import advice  # noqa: E402
import chase  # noqa: E402
import drift  # noqa: E402
import weekly  # noqa: E402
import exec_store as es  # noqa: E402
import cards  # noqa: E402

FAILS = []
NOW = datetime(2026, 10, 16, 11, 0)   # a Friday


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ------------------------------------------------------------------ config
c = C.cfg()
check("tracking.json loads with defaults filled", c["chase"]["max_follow_ups"] == 3 and c["weekly"]["readback_day"] == "fri")

# ------------------------------------------------------------------ drift: ledger vs editor
ledger = {"projects": {
    "2026-10-03_Sloane": {"couple": "Sloane Underhill 's Project", "wedding_date": "2026-06-20", "stage": "booked", "crm_status": "client"},
    "2026-10-10_Belinda": {"couple": "belinda's Project", "wedding_date": "2026-10-10", "stage": "inquiry", "crm_status": "lead"},
    "2026-10-03_Sara": {"couple": "Ellis Merriweather's Project", "wedding_date": "2026-10-03", "stage": "pre-shoot", "crm_status": "client"},
    "2025-01-01_Old": {"couple": "Old Couple", "wedding_date": "2025-01-01", "stage": "booked", "crm_status": "client"},
}}
d = {"enabled": True, "ledger": str(TMP / "ps.json"), "event_grace_days": 2, "post_event_stage": "ingest", "max_days_past": 120}
fs = drift.ledger_stage(d, NOW, ledger=ledger, folders=["Sloane_Bram", "_selftest"])
keys = {f["key"]: f for f in fs}
check("editor project -> ledger says edit", "ledger:2026-10-03_Sloane:edit" in keys and keys["ledger:2026-10-03_Sloane:edit"]["fix"]["stage"] == "edit", list(keys))
check("booked couple, wedding passed -> ingest", "ledger:2026-10-03_Sara:ingest" in keys)
check("a lead whose date passed is not ours", not any(k.startswith("ledger:2026-10-10_Belinda") for k in keys))
check("a wedding older than max_days_past is history", not any("Old" in k for k in keys))
check("every ledger fix carries a desc", all(f["fix"].get("desc") for f in fs))

# ------------------------------------------------------------------ drift: CRM snapshot
snap = {"stages": [{"stage_id": "s1", "name": "Questionnaire Sent", "category": "booked", "order": 8},
                   {"stage_id": "s2", "name": "Say Hello", "category": "lead", "order": 1},
                   {"stage_id": "s3", "name": "Completed", "category": "booked", "order": 18}],
        "projects": [{"id": "p1", "name": "Drew Everhart's Project", "date": "2026-09-26", "stage_id": "s1", "status": "client", "stage_moved_at": "2026-07-26"},
                     {"id": "p2", "name": "Olivia's Project", "date": "2027-05-29", "stage_id": "s2", "status": "client", "stage_moved_at": "2026-01-19"},
                     {"id": "p3", "name": "Done Couple", "date": "2026-09-01", "stage_id": "s3", "status": "client"},
                     {"id": "p4", "name": "Lead Couple", "date": "2026-09-01", "stage_id": "s1", "status": "lead"},
                     {"id": "p5", "name": "Archived", "date": "2026-09-01", "stage_id": "s1", "status": "archived"}]}
fs = drift.crm_stage({"enabled": True, "event_grace_days": 2, "post_event_min_order": 13, "max_days_past": 120}, NOW, snap=snap)
keys = {f["key"] for f in fs}
check("wedding passed, HB pre-event stage -> manual finding", "crm:p1:post" in keys)
check("client in a lead stage -> finding", "crm:p2:lead" in keys)
check("Completed / lead / archived are not findings", not ({"crm:p3:post", "crm:p4:post", "crm:p5:post"} & keys), keys)
check("CRM findings carry a crm_stage fix (2026-10-18: the tap moves the stage through the connector)",
      all((f.get("fix") or {}).get("op") == "crm_stage" and (f["fix"].get("thread_id") == f["key"].split(":")[1]) and f["fix"].get("desc") for f in fs), [f.get("fix") for f in fs])
check("fix targets: lead-stage client -> Proposal Signed, past wedding -> File Management",
      {f["key"]: f["fix"]["stage"] for f in fs}.get("crm:p2:lead") == "Proposal Signed" and {f["key"]: f["fix"]["stage"] for f in fs}.get("crm:p1:post") == "File Management")
import crm as _crm_drift
class _DriftConn:
    CRM = True; SERVICE = "test-crm"
    @staticmethod
    def apply_stage(tid, stage, dry=False): return (True, "DRY_RUN_OK") if dry else (True, "OK")
_saved_drift_plugins = _crm_drift._plugins
_crm_drift._plugins = lambda: [_DriftConn()]
_crm_drift._ALIASES = None
check("drift.apply dry-runs a crm_stage fix through crm.apply_stage",
      (lambda r: r and r[0]["op"] == "crm_stage" and r[0]["ok"] and r[0]["dry"])(
          drift.apply([{"op": "crm_stage", "key": "crm:p1:post", "thread_id": "03ca993ad76ecf08ab4acf52", "stage": "File Management", "desc": "d"}], dry=True)))
_crm_drift._plugins = _saved_drift_plugins; _crm_drift._ALIASES = None
check("drift.apply refuses a crm_stage fix without a stage", not drift.apply([{"op": "crm_stage", "thread_id": "03ca993ad76ecf08ab4acf52", "desc": "d"}], dry=True)[0]["ok"])

# ------------------------------------------------------------------ drift: reminders still armed
fu_state = {"items": {"fu_1": {"id": "fu_1", "sop": "money", "step": "bill-due", "status": "waiting", "lane": "money",
                               "subject": "SoCal Edison bill $120.44 due 2026-10-15", "unit": "socal-edison", "created": "2026-10-01T10:00:00Z"},
                      "fu_2": {"id": "fu_2", "sop": "money", "step": "bill-due", "status": "waiting", "lane": "money",
                               "subject": "Verizon bill due 2026-10-20", "unit": "verizon", "created": "2026-10-01T10:00:00Z"}}}
tix = {"t1": {"id": "t1", "kind": "mail", "lane": "money", "title": "SoCal Edison: payment received", "summary": "Thanks, your payment of $120.44 was received.", "created": "2026-10-12T09:00:00Z"},
       "t2": {"id": "t2", "kind": "mail", "lane": "money", "title": "Verizon: your bill is ready", "summary": "Amount due $88", "created": "2026-10-12T09:00:00Z"},
       "t3": {"id": "t3", "kind": "mail", "lane": "money", "title": "SoCal Edison payment received", "summary": "old", "created": "2026-09-20T09:00:00Z"}}
fs = drift.armed_reminder({"enabled": True, "sops": ["money", "home"], "settled_words": ["payment received", "was paid"]}, NOW, fu_state=fu_state, tickets=tix)
check("paid bill -> close the reminder", len(fs) == 1 and fs[0]["fix"] == {"op": "followup_done", "id": "fu_1", "desc": fs[0]["fix"]["desc"]}, fs)
check("a bill merely 'ready' stays armed", not any(f["key"] == "fu:fu_2" for f in fs))

# ------------------------------------------------------------------ drift: past calendar events
cal = {"events": [{"uid": "e1", "title": "Coffee with Marcus about the Ojai venue", "date": "2026-10-14", "time": "10:00", "owner": "owner1", "all_day": False},
                  {"uid": "e2", "title": "Lensrentals delivery", "date": "2026-10-14", "time": "08:00", "owner": "owner1", "all_day": False},
                  {"uid": "e3", "title": "Dentist", "date": "2026-10-14", "time": "15:00", "owner": "owner6", "all_day": False},
                  {"uid": "e4", "title": "Trip", "date": "2026-10-14", "time": None, "owner": "owner1", "all_day": True},
                  {"uid": "e5", "title": "Glenrock wedding", "date": "2026-10-14", "time": "14:00", "owner": "owner1", "all_day": False, "booking": "booked"},
                  {"uid": "e6", "title": "Call with Teo", "date": "2026-10-14", "time": "16:00", "owner": "owner1", "all_day": False}]}
fs = drift.past_event({"enabled": True, "min_days_past": 1, "max_days_past": 4, "skip_title_words": ["delivery"]}, NOW, cal=cal, seen={},
                      mentions=["Had the call with Teo, he wants the 8 min film"])
keys = {f["key"]: f for f in fs}
check("a timed event nobody mentioned since -> ask", "event:e1" in keys and keys["event:e1"]["manual"] == "ask")
check("skip words, all-day, weddings and mentioned events are left alone", not ({"event:e2", "event:e4", "event:e5", "event:e6"} & set(keys)), set(keys))
check("owner6's event lands in her lane", keys.get("event:e3", {}).get("lane") == "owner6")

# ------------------------------------------------------------------ advice + score
line = advice.line({"kind": "ticket", "lane": "wedding", "title": "x", "age_days": 7, "amount": "$2,724", "waiting_on": "a client", "why": "staged for you"})
check("advice line: tap, why, waiting, days, money", line.startswith("💡") and "waited 7 days" in line and "$2,724" in line and "a client is waiting" in line and len(line) <= advice.MAX, line)
check("advice line is one line and ends with a period", "\n" not in line and line.endswith("."))
hi = advice.score({"kind": "card", "lane": "wedding", "age_days": 5, "waiting_on": "a client"}, c)
lo = advice.score({"kind": "ticket", "lane": "personal", "age_days": 5}, c)
check("a client-facing wedding card outranks a personal ticket", hi > lo, (hi, lo))

# ------------------------------------------------------------------ chase: candidates + state machine
old = time.time() - 3 * 86400
live_cards = [{"id": "qold", "ts": old, "status": "open", "questions": [{"header": "Text reply: staff1", "lane": "wedding", "audience": "client", "question": "Send?"}]},
              {"id": "qnew", "ts": time.time() - 3600, "status": "open", "questions": [{"header": "Fresh", "lane": "wedding", "question": "x"}]},
              {"id": "qstuck", "ts": old, "status": "open", "questions": [{"header": "Stuck items", "lane": "personal", "question": "x"}]}]
tickets = {"tk1": {"id": "tk1", "status": "staged", "lane": "wedding", "kind": "edit_gate", "title": "Sloane edit waiting at review", "summary": "", "updated": (NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")},
           "tk2": {"id": "tk2", "status": "staged", "lane": "personal", "kind": "imessage_in", "title": "Text from owner6", "summary": "", "updated": (NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")},
           "tk3": {"id": "tk3", "status": "staged", "lane": "personal", "kind": "stuck_work", "title": "3 stuck", "summary": "", "updated": (NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")},
           "tk4": {"id": "tk4", "status": "staged", "lane": "money", "kind": "mail", "title": "Insurance renewal", "summary": "", "updated": (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}}
fus = {"items": {"f1": {"id": "f1", "status": "fired", "lane": "home", "subject": "Filter change", "fired_at": (NOW - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ"), "action": {"instruction": "Change the filter"}}}}
cands = chase.candidates(NOW, c, cards=live_cards, tickets=tickets, fus=fus)
keys = [x["key"] for x in cands]
check("stale card, stale ticket, fired follow-up are candidates", {"card:qold", "ticket:tk1", "fu:f1"} <= set(keys), keys)
check("fresh card / fresh ticket are not", "card:qnew" not in keys and "ticket:tk4" not in keys)
check("personal texts, sentinel digests and the Stuck items card are never chased", not ({"ticket:tk2", "ticket:tk3", "card:qstuck"} & set(keys)), keys)
check("ranked: the client-facing card first", keys[0] == "card:qold", keys)

ch = c["chase"]
st = {"items": {}, "lane_day": {}}
x = chase.pick(st, cands, "wedding", NOW, ch)
check("pick: a fresh item when nothing is awaiting", x and x["key"] == "card:qold")
st["items"]["card:qold"] = {"kind": "card", "lane": "wedding", "title": "staff1", "sends": 1, "status": "awaiting", "qid": "n1", "last_sent_epoch": time.time() - 2 * 3600}
x = chase.pick(st, cands, "wedding", NOW, ch)
check("pick: an item asked 2 h ago is not re-asked; the next fresh one comes", x and x["key"] == "ticket:tk1", x and x["key"])
st["items"]["card:qold"]["last_sent_epoch"] = time.time() - 30 * 3600
x = chase.pick(st, cands, "wedding", NOW, ch)
check("pick: after reask_hours the overdue one beats a fresh one", x and x["key"] == "card:qold")
st["items"]["card:qold"]["sends"] = 3
x = chase.pick(st, cands, "wedding", NOW, ch)
check("pick: at max_follow_ups it folds and the next fresh one is asked", st["items"]["card:qold"]["status"] == "folded" and x and x["key"] == "ticket:tk1")
fl = chase.folded_line(st, NOW, ch)
check("folded: one quiet line for the owner, no buttons, names the item", list(fl) == ["owner1"] and "asked 3 times" in fl["owner1"] and "staff1" in fl["owner1"], fl)
st["fold_day"] = {"owner1": NOW.strftime("%Y-%m-%d")}
check("folded: the line goes once a day", chase.folded_line(st, NOW, ch) == {})
st["items"]["card:qold"]["qid"] = "nq"
chase.harvest(st, answers=[{"qid": "nq", "option": "Remind me tomorrow", "ts": "2026-10-16T10:00:00"}])
check("folded: a tap on the old card still counts and resets the count", st["items"]["card:qold"]["status"] == "snoozed" and st["items"]["card:qold"]["sends"] == 0)
st["items"]["card:qold"].update({"status": "folded", "sends": 3})
st["items"]["card:qold"]["parked_until"] = (NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
x = chase.pick(st, cands, "wedding", NOW, ch)
check("pick: a parked item comes back after park_days with a fresh count", st["items"]["card:qold"]["status"] == "awaiting" and st["items"]["card:qold"]["sends"] == 0)

# harvest: taps on our own cards
st = {"items": {"ticket:tk1": {"kind": "ticket", "lane": "wedding", "sends": 1, "status": "awaiting", "qid": "qa"},
                "fu:f1": {"kind": "followup", "lane": "home", "sends": 1, "status": "awaiting", "qid": "qb"},
                "card:qold": {"kind": "card", "lane": "wedding", "sends": 1, "status": "awaiting", "qid": "qc"}}, "lane_day": {}}
n = chase.harvest(st, answers=[{"qid": "qa", "option": "Handled (Recommended)", "ts": "2026-10-16T10:00:00"},
                               {"qid": "qb", "option": "Remind me tomorrow", "ts": "2026-10-16T10:00:00"},
                               {"qid": "qc", "option": "Drop it", "ts": "2026-10-16T10:00:00"}])
check("harvest: Handled / Remind me / Drop it land in the ledger", n == 3 and st["items"]["ticket:tk1"]["status"] == "answered"
      and st["items"]["fu:f1"]["status"] == "snoozed" and st["items"]["fu:f1"].get("snooze_until") and st["items"]["card:qold"]["status"] == "dropped", st)
check("harvest is idempotent", chase.harvest(st, answers=[{"qid": "qa", "option": "Handled (Recommended)", "ts": "x"}]) == 0)

# the card a chase posts
q = chase._question(cands[0], {"sends": 1})
check("nudge card: header, lane, advice, chase_of, four buttons led by a recommended action",
      q["header"].startswith("Nudge") and q["lane"] == "wedding" and q["advice"].startswith("💡") and len(q["options"]) == 4
      and q["options"][0]["label"] == chase.OPT_REPOST and q["chase_of"] == "card:qold" and q["question"].startswith("Still waiting"), q)

# the button grammar (cards.NEEDS_ACTION, 2026-10-17 evening): every chase card has a button the
# system DOES, so it really posts (Handled / Remind me / Drop alone were refused)
for x in cands:
    r = cards.post([chase._question(x, {"sends": 0})], owner="tracking.chase", lane=x["lane"])
    check(f"a {x['kind']} chase card posts through cards.post", r.get("posted") and r.get("qid"), r)
    if r.get("qid"):
        cards.close_card(r["qid"], "test")
fu = next(x for x in cands if x["kind"] == "followup")
check("a follow-up's chase offers Hand it to Sage", chase._question(fu, {})["options"][0]["label"] == chase.OPT_HAND)
check("the old three buttons alone would be refused (why this changed)",
      not cards.post([dict(chase._question(fu, {}), options=chase._question(fu, {})["options"][1:])], owner="tracking.chase", lane="home").get("posted"))

# the action taps: Hand it to Sage queues one operator ticket; Handled still lands
import tickets as _tk  # noqa: E402
st = {"items": {"fu:f1": {"kind": "followup", "lane": "home", "title": "Filter change", "sends": 1, "status": "awaiting", "qid": "qh"}}, "lane_day": {}}
chase.harvest(st, answers=[{"qid": "qh", "option": chase.OPT_HAND, "ts": "2026-10-16T10:00:00"}])
handed = [t for t in _tk._load().values() if t.get("source") == "tracking.chase"]
check("Hand it to Sage: answered + one operator ticket carrying chase_of", st["items"]["fu:f1"]["status"] == "answered"
      and len(handed) == 1 and handed[0]["payload"].get("chase_of") == "fu:f1" and handed[0]["status"] == "new", handed)

# card mode: a refused chase card is not lost; it rides the daily line
_orig_post, _orig_line, _seen = chase._post, chase._post_line, []
chase._post = lambda q, lane, dry: ""
chase._post_line = lambda owner, text, dry: (_seen.append(text), True)[1]
_orig_good = chase.rhythm.good_time
chase.rhythm.good_time = lambda now: True
_cfg = C.cfg
C.cfg = lambda: dict(_cfg(), chase=dict(_cfg()["chase"], nudge="card"))
_orig_cands = chase.candidates
chase.candidates = lambda now, c=None: cands
C.save_state("chase", {"items": {}, "lane_day": {}})
r = chase.tick(now=NOW)
check("card mode: a refused chase falls back to the daily line", _seen and any(p.get("line") for p in r["posted"]), r)
chase._post, chase._post_line, chase.rhythm.good_time, C.cfg, chase.candidates = _orig_post, _orig_line, _orig_good, _cfg, _orig_cands

# one chase per lane per day, posting gated by the window (dry run posts nothing)
C.save_state("chase", {"items": {}, "lane_day": {}})
r = chase.tick(now=NOW.replace(hour=3), dry=False)
check("tick at 03:00 posts nothing (quiet hours)", r["posted"] == [] , r)

# ------------------------------------------------------------------ weekly: readback + hygiene card
C.save_state("drift", {"ts": "x", "findings": [
    {"key": "ledger:k:edit", "kind": "drift", "lane": "wedding", "title": "Sloane: editing has started", "text": "The editor has a project for Sloane but the ledger still says 'booked'.",
     "why": "start the delivery clock", "fix": {"op": "ledger_stage", "ledger": str(TMP / "ps.json"), "key": "k", "stage": "edit", "desc": "Sloane: ledger stage booked -> edit"}, "age_days": 10},
    {"key": "crm:p1:post", "kind": "drift", "lane": "wedding", "title": "Avery: wedding was 9/26", "text": "Avery's wedding was 9/26; the CRM still shows 'Questionnaire Sent'.", "why": "move it along", "fix": None, "manual": "crm", "age_days": 20},
    {"key": "event:e1", "kind": "drift", "lane": "personal", "title": "Coffee with Marcus (10/14)", "text": "'Coffee with Marcus' was on the calendar 10/14 and nothing came up since.", "why": "anything to follow up?", "fix": None, "manual": "ask", "age_days": 2},
]})
text = weekly.readback_text(NOW)
check("readback: moved / stalled / records lines", text.startswith("Week in review") and "Moved:" in text and "Stalled:" in text and "3 records lag" in text, text)
card = weekly.hygiene_card(drift.cached(), 12)
check("hygiene card: one question, numbered rows, approve mints tracking_fix with every fix", card and card["question"].count("\n") == 3 and card["exec"]["kind"] == "tracking_fix"
      and len(card["exec"]["payload"]["fixes"]) == 1 and card["options"][0]["label"].startswith("Fix 1 record") and card["advice"].startswith("💡"), card)
ex, err = cards._sanitize_exec(card)
check("cards accepts the hygiene card's exec (fix desc verbatim in the question)", ex and not err, err)
bad = json.loads(json.dumps(card)); bad["exec"]["payload"]["fixes"][0]["desc"] = "something else"
ex, err = cards._sanitize_exec(bad)
check("a fix whose words are not in the question is refused", err and "EXEC_VERBATIM" in err, err)
check("tracking_fix is a local, never-outbound kind", es.KIND_LANES.get("tracking_fix") == "local" and "tracking_fix" not in es.OUTBOUND_KINDS)
check("tracking_fix needs fixes", es.validate_payload("tracking_fix", {"fixes": [{"op": "x"}]}) == "" and es.validate_payload("tracking_fix", {}) != "")
only_manual = weekly.hygiene_card([f for f in drift.cached() if not f.get("fix")], 12)
check("no fixable rows -> a chat line (say), no card, no exec (owner1 2026-10-17: a 'Noted' button is not a decision)",
      only_manual and "exec" not in only_manual and only_manual.get("say") and not only_manual["options"])

# weekly gating
C.save_state("weekly", {})
r = weekly.tick(now=NOW.replace(hour=10), dry=True)
check("weekly: too early on Friday", r.get("skipped") == "too early", r)
r = weekly.tick(now=datetime(2026, 10, 15, 16, 0), dry=True)
check("weekly: not on Thursday", r.get("skipped") == "not the day", r)

# ------------------------------------------------------------------ apply (the write behind an approved ticket)
ps = TMP / "ps.json"
C.save_json(ps, {"projects": {"k": {"couple": "Sloane", "stage": "booked"}}})
fu_path = TMP / "followups" / "state.json"
C.save_json(fu_path, {"items": {"fu_1": {"id": "fu_1", "sop": "money", "step": "bill-due", "status": "waiting", "subject": "SoCal Edison"}}})
res = drift.apply([{"op": "ledger_stage", "ledger": str(ps), "key": "k", "stage": "edit", "desc": "d"}, {"op": "followup_done", "id": "fu_1", "desc": "d"}, {"op": "nope"}], dry=True)
check("apply --dry-run changes nothing", C.load_json(ps, {})["projects"]["k"]["stage"] == "booked" and res[0]["ok"] and res[1]["ok"] and not res[2]["ok"], res)
res = drift.apply([{"op": "ledger_stage", "ledger": str(ps), "key": "k", "stage": "edit", "desc": "d"}, {"op": "followup_done", "id": "fu_1", "desc": "d"}])
check("apply writes the ledger stage and closes the follow-up", C.load_json(ps, {})["projects"]["k"]["stage"] == "edit" and C.load_json(fu_path, {})["items"]["fu_1"]["status"] == "done", res)
check("fixed keys are latched so they do not come back", "ledger:k" in C.state("drift_latch", {}) and "fu:fu_1" in C.state("drift_latch", {}))

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)} (scratch {TMP})")
sys.exit(1 if FAILS else 0)
