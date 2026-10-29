"""test_weekly_nudge_cards.py - the week-old items ride the Monday digest as cards (build request 2026-10-20).

On 2026-10-20 at 14:46 Sage posted mid-week: "three things have sat for a week: 18 wedding leads quiet over
21 days, $2,724 still outstanding in HoneyBook, and a few stuck personal items. Say 'nudge the cold leads' ..."
It counted leads the owner had dropped or skipped and asked him to type a set phrase. Now:

  1. cold leads: ONE held card a week by code (no leads_cold ticket for the operator), dropped / skipped /
     lost leads in Sage's memory and leads with a booked meeting left out
  2. its first button hands the drafting to Sage (an operator ticket); Leave keeps those leads off next week's card
  3. the outstanding balance: ONE held card a week, no ticket
  4. a ticket staged for days is a held card (sentinels), never a chase line in chat
  5. a week-old ask that already landed (a later done outcome, or memory) closes quietly, an expired card about a
     number memory flags as a scam closes quietly, and several wordings of one job ride ONE card (2026-10-26)
  6. an ask an owner closed (a Skip tap on its card, a never-raise rule, 'handling it himself') closes quietly
Scratch stores; no model, no Google, no Chat.   python assistant/tests/test_weekly_nudge_cards.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_weekly_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["RULINGS_FILE"] = str(HERE.parent / "data" / "rulings.json")
os.environ["CHAT_PUSH"] = "off"
os.environ["WV_NO_TRIAGE"] = "1"
os.environ["HB_SNAPSHOT_FILE"] = str(TMP / "vault" / "snap.json")
sys.path.insert(0, str(HERE.parent / "scripts"))
import alib  # noqa: E402
import cards  # noqa: E402
import sense_honeybook as shb  # noqa: E402
import sentinels  # noqa: E402
import tickets  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


VAULT = TMP / "vault"
CFG = dict(alib.config())
CFG["projects"] = dict(CFG["projects"], state_file="state.json")
CFG["honeybook"] = dict(CFG.get("honeybook") or {}, snapshot="snap.json")
shb.VAULT = VAULT
shb.config = lambda: CFG
alib.save_json(VAULT / "state.json", {"projects": {}})
cards.UNDO_S = 0                                   # harvest the tap at once

# Sage's memory, in the words it really holds (assistant/agent/state/memory.json, 2026-10-19)
Path(os.environ["AGENT_STATE"]).mkdir(parents=True, exist_ok=True)
(Path(os.environ["AGENT_STATE"]) / "memory.json").write_text(json.dumps({"facts": [
    {"fact": "On Oct 19, 2026 at 8:49 AM owner1 tapped 'Drop the lead' on the Emery nudge card. She is a dropped lead", "active": True},
    {"fact": "On Oct 18, 2026 owner1 said Arden and Reese went another direction. They are a lost lead: no nudges", "active": True},
    {"fact": "On Oct 19, 2026 owner1 tapped Skip on the card offering nudges to Indy Greystone (Jun 2027) and Avery Elmswood (Jan 2027).", "active": True},
    {"fact": "Zoe Park asked about drone coverage in September", "active": True},
]}), encoding="utf-8")

today = date.today()
old = (today - timedelta(days=40)).isoformat()
wd = (today + timedelta(days=300)).isoformat()
leads = [("P1", "Finley Larkspur's Project"), ("P2", "Arden and Reese"), ("P3", "Indy Greystone"), ("P4", "Avery Elmswood"),
         ("P5", "Zoe Park"), ("P6", "Mia Jessamine")]
snap = {"ts": (datetime.now(timezone.utc) - timedelta(hours=100)).strftime("%Y-%m-%dT%H:%M:%SZ"),   # stale: no live sync paths run
        "stages": [{"stage_id": "PS", "name": "Proposal Sent", "category": "lead"}],
        "projects": [{"id": f"W{pid}", "project_id": pid, "name": n, "date": wd, "stage_id": "PS", "status": "lead",
                      "created_at": old, "last_activity_at": old} for pid, n in leads],
        "money": {"not_paid_cents": 272400, "paid_cents": 1000000, "booked_ytd_cents": 2000000}}
alib.save_json(VAULT / "snap.json", snap)


def open_cards(header):
    return [c for c in cards.pending() if c.get("header") == header]


def card_json(c):
    """The whole stored card (pending() leaves out `details`)."""
    return (TMP / "cards" / "live" / f"{c['qid']}.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 1. cold leads: one held card, dropped / skipped left out
shb._MEM_CACHE.clear()
shb.sense_honeybook()
cold = open_cards("Cold leads")
check("cold leads: ONE card by code", len(cold) == 1, cold)
c = cold[0] if cold else {}
full = card_json(c) if c else ""
check("it is HELD for the Monday digest, never posted mid-week", c.get("held") is True and not c.get("urgent"), c)
check("dropped, skipped and lost leads are left out of the count",
      "2 wedding leads" in c.get("question", "") and "Zoe Park" in full and "Mia Jessamine" in full
      and not any(n in full for n in ("Emery", "Arden", "Greystone", "Kit")), c.get("question"))
check("buttons, not a set phrase to type", (c.get("options") or [""])[0].startswith("Draft the nudges") and "nudge the cold leads" not in full.lower(), c)
kinds = {t["kind"] for t in tickets._load().values()}
check("no leads_cold / balance_outstanding ticket for the operator to chat about", not kinds & {"leads_cold", "balance_outstanding"}, kinds)
bal = open_cards("Outstanding balance")
check("the outstanding balance: ONE held card", len(bal) == 1 and bal[0].get("held") and "$2,724" in bal[0]["question"], bal)

shb.sense_honeybook()
check("the next sensor run the same week posts nothing new", len(open_cards("Cold leads")) == 1 and len(open_cards("Outstanding balance")) == 1)

# ---------------------------------------------------------------- 2. the buttons act
r = cards.answer(c["qid"], "Draft the nudges (Recommended)")
cards.sweep()
req = [t for t in tickets._load().values() if t["kind"] == "owner1_request" and t["source"] == "honeybook-card"]
check("'Draft the nudges' hands the drafting to Sage as one operator ticket naming only the live leads",
      len(req) == 1 and "Zoe Park" in req[0]["payload"]["text"] and "Emery" not in req[0]["payload"]["text"], req)

r = cards.answer(bal[0]["qid"], "Leave it")
cards.sweep()
check("'Leave it' on the balance card makes no ticket", len([t for t in tickets._load().values() if t["source"] == "honeybook-card"]) == 1)

# Leave on a cold-leads card keeps those leads off next week's card
st = alib.load_json(shb.WEEKLY, {})
latch = alib.load_json(sentinels.LATCH, {})
latch[st["cold"]["key"]] = 1.0                    # what cards._act_on_sentinel writes on Leave
alib.save_json(sentinels.LATCH, latch)
st["cold"]["week"] = "w0000"                      # next week
alib.save_json(shb.WEEKLY, st)
snap["projects"].append({"id": "WP7", "project_id": "P7", "name": "Noah Kim", "date": wd, "stage_id": "PS", "status": "lead",
                         "created_at": old, "last_activity_at": old})
alib.save_json(VAULT / "snap.json", snap)
for f in list((TMP / "cards" / "live").glob("q*.json")) + list((TMP / "cards" / "superseded").glob("q*.json")):
    f.unlink()                                    # last week's cards are long answered
shb.sense_honeybook()
nxt = open_cards("Cold leads")
check("a lead set Left last week stays off; a new cold lead is on next week's card",
      len(nxt) == 1 and "1 wedding lead has" in nxt[0]["question"] and "Noah Kim" in card_json(nxt[0]) and "Zoe Park" not in card_json(nxt[0]), nxt)

# ---------------------------------------------------------------- 3. a ticket staged for days: a held card, not a chat line
tickets.add_many([{"source": "t", "source_id": "stuck", "lane": "personal", "kind": "x", "title": "Renew the car registration",
                   "summary": "", "payload": {}}], "test")
db = tickets._load()
tid = next(k for k, t in db.items() if t["source_id"] == "stuck")
db[tid]["status"], db[tid]["updated"] = "staged", "2026-01-01T00:00:00Z"
alib.save_json(TMP / "adata" / "tickets.json", db)
said = []
_say = cards.say
cards.say = lambda *a, **k: said.append(a)
r = sentinels.run()
cards.say = _say
w = [c for c in cards.pending() if c.get("header", "").startswith("Waiting")]
check("a week-old staged item is ONE held card with buttons, and nothing is said in chat",
      len(w) == 1 and w[0].get("held") and w[0]["options"][0].startswith("Hand it to Sage") and not said, (r, w, said))
cards.answer(w[0]["qid"], "Close it")
cards.sweep()
check("'Close it' closes the item", tickets._load()[tid]["status"] == "done", tickets._load()[tid]["status"])

# ---------------------------------------------------------------- 5. already done, a scam number, the same job twice (2026-10-26)
# On Oct 25-26 three 'add example to example and example's HoneyBook project' cards went up; example was added and read
# back on Oct 18 and the doer saw it in the activity feed on Oct 20. Another brought back an expired date card from
# the suspected-scam number +1 555-051-2351.
def stage(sid, title, kind="agent_run", created="2026-01-01T00:00:00Z", status="staged", note="", summary=""):
    tickets.add_many([{"source": "t", "source_id": sid, "lane": "wedding", "kind": kind, "title": title, "summary": summary,
                       "payload": {}}], "test")
    db = tickets._load()
    k = next(k for k, t in db.items() if t["source_id"] == sid)
    db[k].update(status=status, created=created, updated=created if status == "staged" else "2026-01-03T00:00:00Z")
    if note:
        db[k]["history"].append({"ts": db[k]["updated"], "by": "doer", "to": status, "note": note})
    alib.save_json(TMP / "adata" / "tickets.json", db)
    return k

t1 = stage("tay1", "owner1: add example to example and example HoneyBook project")
t2 = stage("tay2", "owner1: Retry, with a different route: add example as a client contact on example and example's HoneyBook project (wedding 20")
stage("tay-done", "owner1: In HoneyBook, add example (example@example.com, phone (555) 018-0318) as a client contact on example and Tayl",
      created="2026-01-02T00:00:00Z", status="done",
      note="DOER_DONE: I didn't add example again, because she looks like she's already on the project. The activity feed says "
           "'owner1 example added example to this workspace'.", summary="add example as a client contact on example and example's project")
t3 = stage("mem1", "owner1: send Marco and Lena the touch point call link")
t4 = stage("scam", "Card expired unanswered: Date from +1555-051-2351", kind="card_expired",
           summary='+1555-051-2351 (text, 10/19): "If payment is not received by 10/26/2026 your policy will be pro-rata canceled."')
d1 = stage("dup1", "owner1: rent a drone for Priya and Sam's wedding")
d2 = stage("dup2", "owner1: Retry, with a different route: rent a drone for Priya and Sam's wedding (aerials)", created="2026-01-01T06:00:00Z")
lone = stage("lone", "owner1: add example to the Ortiz HoneyBook project")       # not done anywhere: still carded
mem = json.loads((Path(os.environ["AGENT_STATE"]) / "memory.json").read_text(encoding="utf-8"))
mem["facts"] += [
    {"fact": "On Oct 22, 2026 the touch point call link was sent to Marco and Lena by email and read back in Sent Mail", "active": True},
    {"fact": "On Oct 15, 2026 a text from an unknown number, +1 555-051-2351, said the payment failed. It could be a scam.", "active": True},
    {"fact": "As of Oct 20, 2026 the Ortiz project is still not updated", "active": True}]
(Path(os.environ["AGENT_STATE"]) / "memory.json").write_text(json.dumps(mem), encoding="utf-8")
said = []
cards.say = lambda *a, **k: said.append(a)
r = sentinels.run()
cards.say = _say
db = tickets._load()
check("asks the doer already finished (a later DOER_DONE outcome) close quietly, with no card",
      db[t1]["status"] == "done" and db[t2]["status"] == "done" and "closed quietly" in db[t1]["history"][-1]["note"],
      [db[t1]["status"], db[t2]["status"]])
check("an ask memory records as done ('was sent ... read back') closes quietly", db[t3]["status"] == "done", db[t3]["status"])
check("an expired card about a number memory flags as a scam closes quietly", db[t4]["status"] == "done", db[t4]["status"])
w = [c for c in cards.pending() if c.get("header", "").startswith("Waiting")]
heads = " | ".join(c["header"] + " " + c["question"] for c in w)
check("no card names a finished ask or the scam number", "example HoneyBook project" not in heads and "Marco" not in heads
      and "1555-051-2351" not in heads and not said, heads)
drone = [c for c in w if "drone" in c["question"].lower()]
check("two wordings of one job (the same couple and change) ride ONE card", len(drone) == 1 and "asked 2 times" in drone[0]["question"], heads)
check("an ask nothing shows as done still gets its card", any("Ortiz" in c["question"] for c in w), heads)
cards.answer(drone[0]["qid"], "Close it")
cards.sweep()
db = tickets._load()
check("'Close it' on that card closes every wording of the job", db[d1]["status"] == "done" and db[d2]["status"] == "done",
      [db[d1]["status"], db[d2]["status"]])
check("re-running posts nothing new for the closed job", not [c for c in cards.pending() if "drone" in c.get("question", "").lower()]
      and (sentinels.run() or True) and not [c for c in cards.pending() if "drone" in c.get("question", "").lower()])

# ---------------------------------------------------------------- 6. an ask the owner closed himself (2026-10-26)
# The 08:15 sweep posted 'Waiting 10d: a team member: camera 2', which the owner skipped on Oct 16 and barred for good on Oct 23,
# and at 08:45 'Waiting 6d' for the insurance payment retry, which he said on Oct 17 he'd handle himself.
cam = stage("cam1", "Text from staff1 example: Hi owner1, I was thinking about this and I would be interested in  working as camera 2 on w",
            summary="I'll have accces to a canon C400 to use")
cam2 = stage("cam2", "owner1 answered 'Draft: open to it, let's talk' on: staff1: camera 2", kind="card_answer")
gl = stage("gl1", "owner1: owner1 wants the example-org General Liability policy (owner1 example DBA example-org) payment retried.",
           summary="A text from unknown number +1 555-051-2351 on Oct 15 said the payment failed and the payment method must be updated")
sk = cards.post([{"header": "Kayla: venue walk", "question": "Book a venue walk with Kayla?", "lane": "wedding",
                  "options": [{"label": "Book it (Recommended)", "description": ""}, {"label": "Skip", "description": ""}]}], "test")
cards.answer(sk["qid"], "Skip")
tap = stage("tap1", "owner1: book a venue walk with Kayla", note=f"card {sk['qid']}: venue walk")
staff1_other = stage("ian2", "owner1: send staff1 the contractor agreement for the example wedding")   # another a team member matter: still carded
lead = stage("cold", "19 cold leads (quiet > 21d)", summary="Indy Greystone's Project 26d (Proposal Sent); Avery Elmswood's Project 32d")
mem = json.loads((Path(os.environ["AGENT_STATE"]) / "memory.json").read_text(encoding="utf-8"))
mem["facts"] += [
    {"subject": "staff1 example", "added": "2026-10-16 15:00", "active": True,
     "fact": "On Oct 16, 2026 at 12:08 PM owner1 tapped Skip on the card about staff1's Oct 14 ask to work as camera 2 with his Canon C400"},
    {"subject": "General Liability insurance", "added": "2026-10-17 15:02", "active": True,
     "fact": "On Oct 17, 2026 owner1 said he'll check the 'payment failed, update by 10/26' text with the insurer himself and sign in "
             "there himself. Don't send the doer or chase it again unless he asks"},
    {"subject": "Cold lead outreach", "added": "2026-10-19 12:00", "active": True,
     "fact": "On Oct 19, 2026 owner1 tapped Skip on the card offering nudges to Indy Greystone (Jun 2027) and Avery Elmswood (Jan 2027)"}]
(Path(os.environ["AGENT_STATE"]) / "memory.json").write_text(json.dumps(mem), encoding="utf-8")
(Path(os.environ["AGENT_STATE"]) / "rules.json").write_text(json.dumps({"rules": [
    {"id": "r88", "added": "2026-10-23 12:18", "active": True, "why": "owner1: Please stop following up on staff1's camera offer",
     "rule": "Never bring up staff1's camera 2 (second shooter) offer again in any open-items list, digest or follow-up. owner1 has closed it"}]}),
    encoding="utf-8")
cards.say = lambda *a, **k: said.append(a)
sentinels.run()
cards.say = _say
db = tickets._load()
check("staff1's camera 2 offer (Skip tap + 'never bring it up' rule) closes quietly, both tickets",
      db[cam]["status"] == "done" and db[cam2]["status"] == "done" and "owner closed" in db[cam2]["history"][-1]["note"],
      [db[cam]["status"], db[cam2]["status"]])
check("the insurance payment retry owner1 is handling himself closes quietly", db[gl]["status"] == "done", db[gl]["status"])
check("an ask whose own card was answered Skip closes quietly", db[tap]["status"] == "done", db[tap]["status"])
w = [c for c in cards.pending() if c.get("header", "").startswith("Waiting")]
heads = " | ".join(c["header"] + " " + c["question"] for c in w)
check("no Waiting card for camera 2, the insurance payment or the skipped venue walk",
      "camera" not in heads and "Liability" not in heads and "Kayla" not in heads, heads)
check("another matter for the same person still gets its card", any("contractor agreement" in c["question"] for c in w), heads)
check("a Skip on two leads does not close the whole cold-leads item", db[lead]["status"] == "staged", db[lead]["status"])
fresh_ask = stage("cam3", "owner1: offer staff1 camera 2 on the example wedding", created="2026-10-24T18:00:00Z")
check("an owner's own new ask made after the rule is not closed by it",
      not sentinels._owner_closed(tickets._load()[fresh_ask], sentinels._memory_facts(), sentinels._rules()))

print()
print("ALL OK" if not FAILS else f"FAILURES: {', '.join(FAILS)} (scratch {TMP})")
sys.exit(1 if FAILS else 0)
