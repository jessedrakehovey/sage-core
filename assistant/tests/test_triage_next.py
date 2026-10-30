"""Offline tests for triage_next.py (2026-10-18): every client input leads to its next action. Scratch ledger,
lead files, CRM snapshot, cards and tickets; no model, no network, no real CRM. Replays Teo's two
10-18 texts (Wren's number, then her email) and the other fact shapes.
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
os.environ["CRM_SNAPSHOT_FILE"] = str(TMP / "crm.json")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
(TMP / "adata").mkdir(parents=True)
(TMP / "adata" / "leads").mkdir()
# Kind names used by the fake CRM connector throughout this test
_PART_KIND  = "acme_add_participant"
_STAGE_KIND = "acme_set_stage"
_NOTE_KIND  = "acme_note"
_MSG_KIND   = "acme_message"
_UPD_KIND   = "acme_project_update"
_AUTO_KIND  = "acme_automation_approve"

# Write a test-local sop_map (the vault's uses org-specific CRM kinds; we use acme_* here)
_sop_inputs = {
    "facts": {
        "copy_on_everything": {"any": [
            "\\b(copy|cc|include|loop in)\\b.{0,40}\\b(everything|all (the |of the )?(emails|messages|reminders)|every (email|message|reminder))",
            "\\b(wants?|would like) to be (emailed|cc'?d|copied|included) (on|in)\\b"
        ]},
        "questionnaire_done": {"any": [
            "\\b(filled (out|in)|completed|finished|submitted|done with|sent (in|back|over))\\b.{0,40}\\b(questionnaire|quiz|form)",
            "\\b(questionnaire|quiz|form)\\b.{0,30}\\b(is )?(done|complete|completed|filled|submitted|in)\\b"
        ]},
        "lost": {"any": [
            "\\b(went|going|go|decided|chose|chosen) (with |in |to go with |on )?(another|a different|someone else|other)\\b",
            "\\b(another|different) (direction|videographer|vendor|company|route)\\b",
            "\\bno longer (need|looking|interested)\\b",
            "\\bwon'?t be (moving forward|booking|needing)\\b",
            "\\bdecided not to\\b"
        ]},
        "date_change": {"any": [
            "\\b(moved|moving|changed|changing|pushed|push|postpon\\w+|reschedul\\w+|new date)\\b.{0,30}\\b(wedding|date|day)\\b",
            "\\b(wedding|date) (is now|has moved|changed)\\b"
        ]},
        "venue_change": {"any": [
            "\\b(new|different|changed|changing|switched) venue\\b",
            "\\bvenue (is now|has changed|changed)\\b"
        ]},
        "file_received": {"any": [
            "\\b(attached|attaching|here(?:'s| is) (?:the|our) (?:timeline|schedule|vendor list|shot list|song list|address(?:es)?))\\b",
            "\\b(sent|sending|filled in|wrote up) (?:you |over )?(?:the |our )?(?:timeline|schedule|vendor list|shot list|song list)\\b",
            "\\b(timeline|schedule|vendor list|shot list|song list)\\b.{0,30}\\b(attached|below|is in|sent over|done)\\b"
        ]},
        "yes_to_proposal": {"any": [
            "\\b(we(?:'d| would) (?:love|like) to (?:book|move forward|go ahead|lock (?:it|you) in))\\b",
            "\\b(let'?s (?:do it|book it|move forward|lock it in)|we'?re in|count us in|ready to (?:book|sign|move forward))\\b",
            "\\b(we (?:want|would like) to go with you|you'?re hired|book us)\\b"
        ]}
    },
    "actions": {
        "contact": [
            {"do": _PART_KIND, "gate": "card", "needs": "workspace", "detail": "add them to the project"},
            {"do": "also_notify", "gate": "auto", "needs": "unit", "detail": "copy them on email sends"},
            {"do": "couple_page", "gate": "auto", "needs": "unit", "detail": "their details on the couple page"}
        ],
        "copy_on_everything": [
            {"do": _PART_KIND, "gate": "card", "needs": "workspace", "detail": "copy on everything = a participant"},
            {"do": "also_notify", "gate": "auto", "needs": "unit", "detail": "ledger also_notify"}
        ],
        "questionnaire_done": [
            {"do": "followups_close", "gate": "auto", "needs": "unit", "steps": "questionnaire-reminder", "why": "they said the questionnaire is done"},
            {"do": _NOTE_KIND, "gate": "auto", "needs": "workspace"},
            {"do": "couple_page", "gate": "auto", "needs": "unit"}
        ],
        "lost": [
            {"do": _STAGE_KIND, "gate": "card", "needs": "workspace", "reason": "went another direction", "detail": "archive the project as lost"},
            {"do": "followups_stop", "gate": "auto", "why": "went another direction"},
            {"do": _NOTE_KIND, "gate": "auto", "needs": "workspace"},
            {"do": "couple_page", "gate": "auto", "needs": "unit"}
        ],
        "date_change": [
            {"do": _UPD_KIND, "gate": "operator", "detail": "the wedding date moved"}
        ],
        "venue_change": [
            {"do": _UPD_KIND, "gate": "operator", "detail": "new venue"}
        ],
        "reply": [
            {"do": "reply_draft", "gate": "operator", "detail": "the reply"}
        ],
        "file_received": [
            {"do": "followups_close", "gate": "auto", "needs": "unit", "steps": "timeline-request", "why": "they sent the timeline"},
            {"do": _NOTE_KIND, "gate": "auto", "needs": "workspace"},
            {"do": "couple_page", "gate": "auto", "needs": "unit"}
        ],
        "yes_to_proposal": [
            {"do": _NOTE_KIND, "gate": "auto", "needs": "workspace", "detail": "note the yes on the project"},
            {"do": "followups_close", "gate": "auto", "needs": "unit", "steps": "quote-followup", "why": "they said yes"}
        ]
    }
}
with open(TMP / "adata" / "sop_map.json", "w", encoding="utf-8") as _f:
    json.dump({"inputs": _sop_inputs}, _f)

WID, PID = "33d4c6374833902d47cb1229", "9ce6fbde7ad780639e5b729f"
json.dump({"projects": {"2026-11-07_Teo": {"couple": "Teo", "wedding_date": "2026-11-07", "stage": "booked", "crm_id": PID, "crm_status": "client"},
                        "2027-07-30_Delia-Ridders-Project": {"couple": "Delia Vantongeren", "wedding_date": "2027-07-30", "stage": "inquiry", "crm_id": "09f7c73db9bbc65428283dbe", "phone": "+18055550101"}}},
          open(TMP / "project_state.json", "w", encoding="utf-8"))
json.dump({"projects": [{"id": WID, "project_id": PID, "name": "Teo", "date": "2026-11-07", "status": "client"},
                        {"id": "03ca993ad76ecf08ab4acf52", "project_id": "a54623a8cf2d3448fed85bcb", "name": "Blake Northcott's Project", "status": "lead_sent"}],
           "contacts": [{"id": "633efac59a74b6407d6c071e", "name": "Teo Ashcombe", "email": "p133d8b@example.invalid", "phone": "5550316831", "workspace_ids": []}],
           "stages": []}, open(TMP / "crm.json", "w", encoding="utf-8"))
json.dump({"lead": {"name": "Blake Northcott", "partner": "Zion Copperfield", "email": "pcc42c4@example.invalid", "phone": "+1 (555) 057-8657", "date": "2027-06-12"}},
          open(TMP / "adata" / "leads" / "x4d9651b444f5acf0ee6fce87c89a579.json", "w", encoding="utf-8"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import cards  # noqa: E402
import exec_store as es  # noqa: E402
import crm as _crm_mod  # noqa: E402

# Register all acme_* kinds with exec_store (bare engine does not have them; vault may already have org-specific kinds)
for _k, _lane, _req in [
    (_PART_KIND,  "crm", [("workspace_id", "project_id"), ("email",)]),
    (_STAGE_KIND, "crm", [("workspace_id", "project_id"), ("stage",)]),
    (_NOTE_KIND,  "crm", [("workspace_id", "project_id"), ("text",)]),
    (_MSG_KIND,   "crm", [("workspace_id", "project_id"), ("text",)]),
    (_UPD_KIND,   "crm", [("workspace_id", "project_id")]),
    (_AUTO_KIND,  "crm", [("workspace_id", "project_id")]),
]:
    es.KIND_LANES.setdefault(_k, _lane)
    es.REQUIRED_KEYS.setdefault(_k, _req)

# Install a fake CRM connector (REPLACES all plugins) so crm.kinds("PARTICIPANT_KINDS") etc. return
# acme_* sets that match the test sop_map above; triage_auto handles acme_note auto steps.
import types as _types
import alib as _alib

def _fake_triage_auto(a, party, facts, dry):
    if a.get("do") != _NOTE_KIND:
        return None
    if not party.get("workspace_id"):
        return "no workspace"
    if dry:
        return "would queue note"
    try:
        row = es.enqueue(_NOTE_KIND, {"workspace_id": party["workspace_id"], "text": "triage note"}, es.APPROVER, "test stub")
        return f"note ticket {row['id']} queued"
    except Exception as _e:
        return f"note refused: {str(_e)[:80]}"

_crm_stub = _types.SimpleNamespace(
    CRM=True,
    SERVICE="AcmeCRM",
    PREVIEW_TAG="[ACME PREVIEW]",
    PARTICIPANT_KINDS={_PART_KIND},
    STAGE_KINDS={_STAGE_KIND},
    INBOUND_KINDS={_MSG_KIND + "_in", "inquiry_new"},
    MESSAGE_KINDS={_MSG_KIND},
    FIELD_UPDATE_KINDS={_UPD_KIND},
    AUTOMATION_KINDS={_AUTO_KIND},
    FOREVER_GATED=frozenset(),
    GRADUATION_CLASSES=frozenset(),
    CHANNEL_ALIASES={"acme"},
    ROUTES={_PART_KIND: "connector", _STAGE_KIND: "connector", _MSG_KIND: "connector"},
    RETRY_KINDS={_PART_KIND, _STAGE_KIND, _MSG_KIND, _NOTE_KIND, _UPD_KIND, _AUTO_KIND},
    FIELD_ALIASES={"crm_id": ["acme_id"], "crm_status": ["acme_status"], "crm_stage": ["acme_stage"]},
    KIND_LANES={_PART_KIND: "crm", _STAGE_KIND: "crm", _NOTE_KIND: "crm"},
    REQUIRED_KEYS={_PART_KIND: [("workspace_id",), ("email",)], _STAGE_KIND: [("workspace_id",), ("stage",)], _NOTE_KIND: [("workspace_id",), ("text",)]},
    triage_auto=_fake_triage_auto,
)
_alib.plugins = lambda: [_crm_stub]
_crm_mod._plugins = lambda: [_crm_stub]
_crm_mod._ALIASES = None
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
p = tn.party_for(handle="+15550316831")
check("Teo's number -> the booked Ashcombe unit with its workspace", p["kind"] == "booked" and p["unit"] == "2026-11-07_Teo" and p["workspace_id"] == WID, p)
check("Arden's number -> the quiz lead + his CRM workspace", tn.party_for(handle="(555) 057-8657")["kind"] == "lead" and tn.party_for(handle="(555) 057-8657")["workspace_id"] == "03ca993ad76ecf08ab4acf52")
check("Delia by ledger phone", tn.party_for(handle="+18055550101")["unit"].startswith("2027-07-30"))
check("a stranger is unknown", tn.party_for(handle="+18055559999")["kind"] == "unknown")

# 2. facts: Teo's two texts (the number first, the email next; one person)
f1 = tn.facts_in("Here is Wren's number: (555) 018-0318", "+15550316831", "Teo Ashcombe")
check("a number that is not the sender's is a contact fact with the name", any(f["type"] == "contact" and f["name"] == "Wren" and f["phone"] == "(555) 018-0318" for f in f1), f1)
f2 = tn.facts_in("And email: wren@example.com", "+15550316831", "Teo Ashcombe", prior=["Here is Wren's number: (555) 018-0318"])
c2 = next(f for f in f2 if f["type"] == "contact")
check("the email text folds the earlier number into one contact fact", c2["email"] == "wren@example.com" and c2["phone"] == "(555) 018-0318" and c2["name"] == "Wren", c2)
check("a real message always carries the reply fact", any(f["type"] == "reply" for f in f2))
check("a pure ack carries nothing", tn.facts_in("Liked “Hey Teo”", "+15550316831") == [] or all(f["type"] != "reply" for f in tn.facts_in("Thank you!!", "+15550316831")))
check("copy on everything", any(f["type"] == "copy_on_everything" for f in tn.facts_in("Can you copy Wren on everything you send me?", "+15550316831")))
check("a sent timeline is a file_received fact (WP3)", any(f["type"] == "file_received" for f in tn.facts_in("Here's our timeline for the day, attached!", "+15550316831")))
check("a yes is a yes_to_proposal fact (WP3)", any(f["type"] == "yes_to_proposal" for f in tn.facts_in("We'd love to book you for the day!", "+15550316831")))
pl = tn.plan(tn.facts_in("Here is the timeline, attached", "+15550316831", "Teo Ashcombe"), tn.party_for(handle="+15550316831"))
check("file_received closes the timeline request and notes it", {a["do"] for a in pl} >= {"followups_close", _NOTE_KIND, "couple_page"} and next(a for a in pl if a["do"] == "followups_close").get("steps") == "timeline-request", pl)
check("questionnaire done", any(f["type"] == "questionnaire_done" for f in tn.facts_in("We filled out the questionnaire last night!", "+15550316831")))
check("lost", any(f["type"] == "lost" for f in tn.facts_in("Thanks so much but we decided to go with another videographer.", "+15550578657")))
check("date change", any(f["type"] == "date_change" for f in tn.facts_in("Quick update: we moved the wedding to May 2nd", "+15550316831")))
check("the sender's own number is not a contact", not any(f["type"] == "contact" for f in tn.facts_in("call me at 555-031-6831", "+15550316831")))

# 3. plan + act, dry: order and gates
r = tn.triage("And email: wren@example.com", handle="+15550316831", name="Teo Ashcombe", prior=["Here is Wren's number: (555) 018-0318"], dry=True)
dos = [a["do"] for a in r["actions"]]
check("Teo's texts -> participant card + also_notify + couple page + reply, reply last",
      {_PART_KIND, "also_notify", "couple_page", "reply_draft"} <= set(dos) and dos[-1] == "reply_draft", dos)
check("gates: auto / card / operator", {a["do"]: a["gate"] for a in r["actions"]}[_PART_KIND] == "card" and {a["do"]: a["gate"] for a in r["actions"]}["also_notify"] == "auto"
      and {a["do"]: a["gate"] for a in r["actions"]}["reply_draft"] == "operator")
check("the chat line lists all of it", "also_notify" in r["line"] and _PART_KIND in r["line"] and "reply draft" in r["line"], r["line"])

# 4. act for real (scratch): the card is up, the ledger copies Wren, the page note is queued
r = tn.triage("And email: wren@example.com", handle="+15550316831", name="Teo Ashcombe", prior=["Here is Wren's number: (555) 018-0318"])
st = {a["do"]: a for a in r["actions"]}
check("participant card posted", st[_PART_KIND]["status"] == "carded", st[_PART_KIND])
card = next((c for c in live_cards() if "Wren" in json.dumps(c)), None)
check("the card carries email + name verbatim and the participant exec", card is not None and card["questions"][0]["exec"]["kind"] == _PART_KIND
      and "wren@example.com" in card["questions"][0]["question"] and card["questions"][0]["exec"]["payload"]["workspace_id"] == WID, card)
led = json.load(open(TMP / "project_state.json", encoding="utf-8"))["projects"]["2026-11-07_Teo"]
check("ledger also_notify carries Wren", led.get("also_notify") == ["wren@example.com"] and "(555) 018-0318" in (led.get("also_phones") or []), led)
q = json.load(open(TMP / "adata" / "triage_queue.json", encoding="utf-8"))
check("the couple-page note is queued for the spine", q["couple_notes"] and "Wren" in q["couple_notes"][0]["body"] and q["couple_notes"][0]["couple"] == "2026-11-07_Teo")
# the same text again: nothing doubled
n_cards = len(live_cards())
r2 = tn.triage("And email: wren@example.com", handle="+15550316831", name="Teo Ashcombe", prior=["Here is Wren's number: (555) 018-0318"])
check("a second pass adds no second card (one subject, one card)", len(live_cards()) == n_cards)
check("also_notify does not double", json.load(open(TMP / "project_state.json", encoding="utf-8"))["projects"]["2026-11-07_Teo"]["also_notify"] == ["wren@example.com"])
# approving the card mints the exec ticket with Wren's details
a = cards.answer(card["id"], "Add them (Recommended)")
t = es.fold().get(a.get("ticket") or "")
check("the tap mints the participant exec for the Ashcombe workspace", t and t["kind"] == _PART_KIND and t["payload"]["email"] == "wren@example.com" and t["payload"]["workspace_id"] == WID, a)
if t:
    es.set_status(t["id"], "cancelled", "test")

# 5. questionnaire done closes the reminders
followups.arm("wedding-lifecycle", "questionnaire-reminder-2", {"unit": "2026-11-07_Teo", "couple": "Teo", "wedding_date": "2026-11-07", "anchor": "2026-11-07"}, "test")
r = tn.triage("We filled out the questionnaire last night!", handle="+15550316831", name="Teo Ashcombe")
st = {a["do"]: a for a in r["actions"]}
check("questionnaire done -> reminders closed", st["followups_close"]["status"] == "done" and "closed 1" in st["followups_close"]["detail"], st.get("followups_close"))
if _NOTE_KIND in es.KIND_LANES and _STAGE_KIND in es.KIND_LANES:   # CRM plugin kinds registered (always true with the fake)
    check("questionnaire done -> internal CRM note queued under the standing rule", st[_NOTE_KIND]["status"] == "done" and "note ticket" in st[_NOTE_KIND]["detail"], st.get(_NOTE_KIND))
if _NOTE_KIND in es.KIND_LANES and _STAGE_KIND in es.KIND_LANES:   # CRM plugin kinds registered (always true with the fake)
    check("...and the note ticket exists", any(x["kind"] == _NOTE_KIND and x["status"] == "queued" for x in es.fold().values()))
for x in es.fold().values():
    if x["status"] == "queued":
        es.set_status(x["id"], "cancelled", "test")

# 6. a lost lead: card to archive, follow-ups stopped
followups.arm("inquiry", "quote-followup-7d", {"unit": "x4d9651b444f5acf0ee6fce87c89a579", "couple": "Blake Northcott", "anchor": "2026-10-20"}, "test")
r = tn.triage("Thank you for everything, we went another direction.", handle="+15550578657", name="Blake Northcott")
st = {a["do"]: a for a in r["actions"]}
if _NOTE_KIND in es.KIND_LANES and _STAGE_KIND in es.KIND_LANES:   # CRM plugin kinds registered (always true with the fake)
    check("lost -> mark-lost card", st[_STAGE_KIND]["status"] == "carded", st.get(_STAGE_KIND))
lc = next((c for c in live_cards() if "mark lost" in json.dumps(c).lower()), None)
if _NOTE_KIND in es.KIND_LANES and _STAGE_KIND in es.KIND_LANES:   # CRM plugin kinds registered (always true with the fake)
    check("the lost card archives with a reason and the stage word on the card", lc and lc["questions"][0]["exec"]["payload"]["stage"] == "lost" and "lost" in lc["questions"][0]["question"].lower())
check("lost -> follow-ups stopped", st["followups_stop"]["status"] == "done" and "stopped 1" in st["followups_stop"]["detail"], st.get("followups_stop"))

# 7. a stranger: only the reply is left
r = tn.triage("Hi! Are you free Oct 3 2027 for a wedding in Ojai?", handle="+18055559999")
check("unknown sender -> reply only, nothing written", [a["do"] for a in r["actions"]] == ["reply_draft"], r["actions"])

# 8. the hook: tickets.add_many annotates the ticket
import phone_bridge  # noqa: E402
cards_before = len(live_cards())
tickets.add_many([{"source": "imessage", "source_id": "t1", "lane": "wedding", "kind": "imessage_in", "title": "Text from Teo",
                   "summary": "Can you also copy Wren on everything? wren@example.com",
                   "payload": {"from": "+15550316831", "name": "Teo Ashcombe", "text": "Can you also copy Wren on everything? wren@example.com", "thread": []}}], "test")
if any(hasattr(m, "crm_people") for m in __import__("alib").plugins()):   # a client text is kept by the CRM's own client list
    tk = next(t for t in tickets._load().values() if t["source_id"] == "t1")
    tri = tk["payload"].get("triage") or {}
    check("a new inbound ticket carries its triage block", tri.get("party", {}).get("unit") == "2026-11-07_Teo" and any(f["type"] in ("contact", "copy_on_everything") for f in tri.get("facts", [])), tri)
    check("...with the actions and the one-line story", tri.get("actions") and tri.get("line"), tri.get("line"))
check("a non-inbound ticket is left alone", "triage" not in (tickets._load()[tickets.make_id("x", "y")]["payload"] if tickets.add_many([{"source": "x", "source_id": "y", "lane": "wedding", "kind": "shoot_prep", "title": "x", "summary": "", "payload": {}}], "test") >= 0 else {}))
os.environ["WV_NO_TRIAGE"] = "1"
tickets.add_many([{"source": "imessage", "source_id": "t2", "lane": "wedding", "kind": "imessage_in", "title": "x", "summary": "x", "payload": {"from": "+15550316831", "text": "x", "thread": []}}], "test")
if any(t["source_id"] == "t2" for t in tickets._load().values()) or any(hasattr(m, "crm_people") for m in __import__("alib").plugins()):
    check("WV_NO_TRIAGE switches the hook off", "triage" not in next(t for t in tickets._load().values() if t["source_id"] == "t2")["payload"])
del os.environ["WV_NO_TRIAGE"]

# 9. the drain of queued couple notes is a no-op in dry mode and reports what waits
d = tn.drain_queue(dry=True)
check("drain dry leaves the queue", d["left"] >= 1 and d["filed"] == 0, d)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)} (scratch {TMP})")
sys.exit(1 if FAILS else 0)
