"""Offline tests for triage_next.py (2026-10-18): every client input leads to its next action. Scratch ledger,
lead files, HoneyBook snapshot, cards and tickets; no model, no network, no HoneyBook. Replays example's two
10-18 texts (example's number, then her email) and the other fact shapes.
    python assistant/tests/test_triage_next.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_triage_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["WV_PROJECT_STATE"] = str(TMP / "project_state.json")
os.environ["WV_HB_SNAPSHOT"] = str(TMP / "honeybook.json")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
(TMP / "adata").mkdir(parents=True)
(TMP / "adata" / "leads").mkdir()
# the vocabulary is data: copy the live map into the scratch data dir
import shutil
shutil.copy(ROOT / "data" / "sop_map.json", TMP / "adata" / "sop_map.json")
WID, PID = "33d4c6374833902d47cb1229", "9ce6fbde7ad780639e5b729f"
json.dump({"projects": {"2026-11-07_example": {"couple": "example", "wedding_date": "2026-11-07", "stage": "booked", "honeybook_id": PID, "honeybook_status": "client"},
                        "2027-07-30_example-Ridders-Project": {"couple": "example example", "wedding_date": "2027-07-30", "stage": "inquiry", "honeybook_id": "09f7c73db9bbc65428283dbe", "phone": "+18055550101"}}},
          open(TMP / "project_state.json", "w", encoding="utf-8"))
json.dump({"projects": [{"id": WID, "project_id": PID, "name": "example", "date": "2026-11-07", "status": "client"},
                        {"id": "03ca993ad76ecf08ab4acf52", "project_id": "a54623a8cf2d3448fed85bcb", "name": "Blake Northcott's Project", "status": "lead_sent"}],
           "contacts": [{"id": "633efac59a74b6407d6c071e", "name": "example example", "email": "p133d8b@example.invalid", "phone": "555-031-6831", "workspace_ids": []}],
           "stages": []}, open(TMP / "honeybook.json", "w", encoding="utf-8"))
json.dump({"lead": {"name": "Blake Northcott", "partner": "Zion Copperfield", "email": "pcc42c4@example.invalid", "phone": "+1 (555) 057-8657", "date": "2027-06-12"}},
          open(TMP / "adata" / "leads" / "x4d9651b444f5acf0ee6fce87c89a579.json", "w", encoding="utf-8"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import cards  # noqa: E402
import exec_store as es  # noqa: E402
import followups  # noqa: E402
import tickets  # noqa: E402
import triage_next as tn  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def live_cards():
    return [c for c in cards._live() if c.get("status") == "open"]


# 1. who is it from
p = tn.party_for(handle="+1555-031-6831")
check("example's number -> the booked example unit with its workspace", p["kind"] == "booked" and p["unit"] == "2026-11-07_example" and p["workspace_id"] == WID, p)
check("Arden's number -> the quiz lead + his HoneyBook workspace", tn.party_for(handle="(555) 057-8657")["kind"] == "lead" and tn.party_for(handle="(555) 057-8657")["workspace_id"] == "03ca993ad76ecf08ab4acf52")
check("example by ledger phone", tn.party_for(handle="+18055550101")["unit"].startswith("2027-07-30"))
check("a stranger is unknown", tn.party_for(handle="+18055559999")["kind"] == "unknown")

# 2. facts: example's two texts (the number first, the email next; one person)
f1 = tn.facts_in("Here is example's number: (555) 018-0318", "+1555-031-6831", "example example")
check("a number that is not the sender's is a contact fact with the name", any(f["type"] == "contact" and f["name"] == "example" and f["phone"] == "(555) 018-0318" for f in f1), f1)
f2 = tn.facts_in("And email: example@example.com", "+1555-031-6831", "example example", prior=["Here is example's number: (555) 018-0318"])
c2 = next(f for f in f2 if f["type"] == "contact")
check("the email text folds the earlier number into one contact fact", c2["email"] == "example@example.com" and c2["phone"] == "(555) 018-0318" and c2["name"] == "example", c2)
check("a real message always carries the reply fact", any(f["type"] == "reply" for f in f2))
check("a pure ack carries nothing", tn.facts_in("Liked “Hey example”", "+1555-031-6831") == [] or all(f["type"] != "reply" for f in tn.facts_in("Thank you!!", "+1555-031-6831")))
check("copy on everything", any(f["type"] == "copy_on_everything" for f in tn.facts_in("Can you copy example on everything you send me?", "+1555-031-6831")))
check("a sent timeline is a file_received fact (WP3)", any(f["type"] == "file_received" for f in tn.facts_in("Here's our timeline for the day, attached!", "+1555-031-6831")))
check("a yes is a yes_to_proposal fact (WP3)", any(f["type"] == "yes_to_proposal" for f in tn.facts_in("We'd love to book you for the day!", "+1555-031-6831")))
pl = tn.plan(tn.facts_in("Here is the timeline, attached", "+1555-031-6831", "example example"), tn.party_for(handle="+1555-031-6831"))
check("file_received closes the timeline request and notes it", {a["do"] for a in pl} >= {"followups_close", "hb_note", "couple_page"} and next(a for a in pl if a["do"] == "followups_close").get("steps") == "timeline-request", pl)
check("questionnaire done", any(f["type"] == "questionnaire_done" for f in tn.facts_in("We filled out the questionnaire last night!", "+1555-031-6831")))
check("lost", any(f["type"] == "lost" for f in tn.facts_in("Thanks so much but we decided to go with another videographer.", "+1555-057-8657")))
check("date change", any(f["type"] == "date_change" for f in tn.facts_in("Quick update: we moved the wedding to May 2nd", "+1555-031-6831")))
check("the sender's own number is not a contact", not any(f["type"] == "contact" for f in tn.facts_in("call me at 555-031-6831", "+1555-031-6831")))

# 3. plan + act, dry: order and gates
r = tn.triage("And email: example@example.com", handle="+1555-031-6831", name="example example", prior=["Here is example's number: (555) 018-0318"], dry=True)
dos = [a["do"] for a in r["actions"]]
check("example's texts -> participant card + also_notify + couple page + reply, reply last",
      {"hb_add_participant", "also_notify", "couple_page", "reply_draft"} <= set(dos) and dos[-1] == "reply_draft", dos)
check("gates: auto / card / operator", {a["do"]: a["gate"] for a in r["actions"]}["hb_add_participant"] == "card" and {a["do"]: a["gate"] for a in r["actions"]}["also_notify"] == "auto"
      and {a["do"]: a["gate"] for a in r["actions"]}["reply_draft"] == "operator")
check("the chat line lists all of it", "also_notify" in r["line"] and "hb_add_participant" in r["line"] and "reply draft" in r["line"], r["line"])

# 4. act for real (scratch): the card is up, the ledger copies example, the page note is queued
r = tn.triage("And email: example@example.com", handle="+1555-031-6831", name="example example", prior=["Here is example's number: (555) 018-0318"])
st = {a["do"]: a for a in r["actions"]}
check("participant card posted", st["hb_add_participant"]["status"] == "carded", st["hb_add_participant"])
card = next((c for c in live_cards() if "example" in json.dumps(c)), None)
check("the card carries email + name verbatim and an hb_add_participant exec", card is not None and card["questions"][0]["exec"]["kind"] == "hb_add_participant"
      and "example@example.com" in card["questions"][0]["question"] and card["questions"][0]["exec"]["payload"]["workspace_id"] == WID, card)
led = json.load(open(TMP / "project_state.json", encoding="utf-8"))["projects"]["2026-11-07_example"]
check("ledger also_notify carries example", led.get("also_notify") == ["example@example.com"] and "(555) 018-0318" in (led.get("also_phones") or []), led)
q = json.load(open(TMP / "adata" / "triage_queue.json", encoding="utf-8"))
check("the couple-page note is queued for the spine", q["couple_notes"] and "example" in q["couple_notes"][0]["body"] and q["couple_notes"][0]["couple"] == "2026-11-07_example")
# the same text again: nothing doubled
n_cards = len(live_cards())
r2 = tn.triage("And email: example@example.com", handle="+1555-031-6831", name="example example", prior=["Here is example's number: (555) 018-0318"])
check("a second pass adds no second card (one subject, one card)", len(live_cards()) == n_cards)
check("also_notify does not double", json.load(open(TMP / "project_state.json", encoding="utf-8"))["projects"]["2026-11-07_example"]["also_notify"] == ["example@example.com"])
# approving the card mints the exec ticket with example's details
a = cards.answer(card["id"], "Add them (Recommended)")
t = es.fold().get(a.get("ticket") or "")
check("the tap mints hb_add_participant for the example workspace", t and t["kind"] == "hb_add_participant" and t["payload"]["email"] == "example@example.com" and t["payload"]["workspace_id"] == WID, a)
if t:
    es.set_status(t["id"], "cancelled", "test")

# 5. questionnaire done closes the reminders
followups.arm("wedding-lifecycle", "questionnaire-reminder-2", {"unit": "2026-11-07_example", "couple": "example", "wedding_date": "2026-11-07", "anchor": "2026-11-07"}, "test")
r = tn.triage("We filled out the questionnaire last night!", handle="+1555-031-6831", name="example example")
st = {a["do"]: a for a in r["actions"]}
check("questionnaire done -> reminders closed", st["followups_close"]["status"] == "done" and "closed 1" in st["followups_close"]["detail"], st.get("followups_close"))
check("questionnaire done -> internal HoneyBook note queued under the standing rule", st["hb_note"]["status"] == "done" and "note ticket" in st["hb_note"]["detail"], st.get("hb_note"))
check("...and the note ticket exists", any(x["kind"] == "hb_note" and x["status"] == "queued" for x in es.fold().values()))
for x in es.fold().values():
    if x["status"] == "queued":
        es.set_status(x["id"], "cancelled", "test")

# 6. a lost lead: card to archive, follow-ups stopped
followups.arm("inquiry", "quote-followup-7d", {"unit": "x4d9651b444f5acf0ee6fce87c89a579", "couple": "Blake Northcott", "anchor": "2026-10-20"}, "test")
r = tn.triage("Thank you for everything, we went another direction.", handle="+1555-057-8657", name="Blake Northcott")
st = {a["do"]: a for a in r["actions"]}
check("lost -> mark-lost card", st["hb_set_stage"]["status"] == "carded", st.get("hb_set_stage"))
lc = next((c for c in live_cards() if "mark lost" in json.dumps(c).lower()), None)
check("the lost card archives with a reason and the stage word on the card", lc and lc["questions"][0]["exec"]["payload"]["stage"] == "lost" and "lost" in lc["questions"][0]["question"].lower())
check("lost -> follow-ups stopped", st["followups_stop"]["status"] == "done" and "stopped 1" in st["followups_stop"]["detail"], st.get("followups_stop"))

# 7. a stranger: only the reply is left
r = tn.triage("Hi! Are you free Oct 3 2027 for a wedding in Ojai?", handle="+18055559999")
check("unknown sender -> reply only, nothing written", [a["do"] for a in r["actions"]] == ["reply_draft"], r["actions"])

# 8. the hook: tickets.add_many annotates the ticket
import phone_bridge  # noqa: E402
cards_before = len(live_cards())
tickets.add_many([{"source": "imessage", "source_id": "t1", "lane": "wedding", "kind": "imessage_in", "title": "Text from example",
                   "summary": "Can you also copy example on everything? example@example.com",
                   "payload": {"from": "+1555-031-6831", "name": "example example", "text": "Can you also copy example on everything? example@example.com", "thread": []}}], "test")
tk = next(t for t in tickets._load().values() if t["source_id"] == "t1")
tri = tk["payload"].get("triage") or {}
check("a new inbound ticket carries its triage block", tri.get("party", {}).get("unit") == "2026-11-07_example" and any(f["type"] in ("contact", "copy_on_everything") for f in tri.get("facts", [])), tri)
check("...with the actions and the one-line story", tri.get("actions") and tri.get("line"), tri.get("line"))
check("a non-inbound ticket is left alone", "triage" not in (tickets._load()[tickets.make_id("x", "y")]["payload"] if tickets.add_many([{"source": "x", "source_id": "y", "lane": "wedding", "kind": "shoot_prep", "title": "x", "summary": "", "payload": {}}], "test") >= 0 else {}))
os.environ["WV_NO_TRIAGE"] = "1"
tickets.add_many([{"source": "imessage", "source_id": "t2", "lane": "wedding", "kind": "imessage_in", "title": "x", "summary": "x", "payload": {"from": "+1555-031-6831", "text": "x", "thread": []}}], "test")
check("WV_NO_TRIAGE switches the hook off", "triage" not in next(t for t in tickets._load().values() if t["source_id"] == "t2")["payload"])
del os.environ["WV_NO_TRIAGE"]

# 9. the drain of queued couple notes is a no-op in dry mode and reports what waits
d = tn.drain_queue(dry=True)
check("drain dry leaves the queue", d["left"] >= 1 and d["filed"] == 0, d)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)} (scratch {TMP})")
sys.exit(1 if FAILS else 0)
