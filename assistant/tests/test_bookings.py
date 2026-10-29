"""Offline tests for lead-vs-booked (2026-10-06): HoneyBook's workspace status decides what is a
booking; leads in a "booked" pipeline column are re-staged to inquiry, their follow-ups are held
(re-armable), calendar weddings are labelled, and only a BOOKED wedding on an away day posts a
clash card. Scratch dirs, no network.   python assistant/tests/test_bookings.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_book_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(HERE.parent / "data" / "rulings.json")
sys.path.insert(0, str(HERE.parent / "scripts"))
import alib  # noqa: E402
import cards  # noqa: E402
import followups as fu  # noqa: E402
import sense_honeybook as shb  # noqa: E402
import sense_local as sl  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# every sensor reads the ledger at VAULT/<state_file>: point both at a scratch vault
VAULT = TMP / "vault"
LEDGER = VAULT / "state.json"
CFG = dict(alib.config())
CFG["projects"] = dict(CFG["projects"], state_file="state.json")
for m in (shb, sl):
    m.VAULT = VAULT
    m.config = lambda: CFG
sl.CLASH_LATCH = TMP / "adata" / "clash_latch.json"

today = date.today()
d10 = (today + timedelta(days=4)).isoformat()
d40 = (today + timedelta(days=40)).isoformat()
alib.save_json(LEDGER, {"projects": {
    "lead-seeded": {"couple": "Belinda's Project", "wedding_date": d10, "stage": "booked", "honeybook_id": "P1", "seeded_by": "sense_honeybook"},
    "client-seeded": {"couple": "example", "wedding_date": d40, "stage": "booked", "honeybook_id": "P2", "seeded_by": "sense_honeybook"},
    "lead-handset": {"couple": "Hand Set", "wedding_date": d40, "stage": "booked", "honeybook_id": "P3"},
    "lead-confirmed": {"couple": "Confirmed", "wedding_date": d40, "stage": "booked", "honeybook_id": "P4",
                       "seeded_by": "sense_honeybook", "confirmed_by": "owner1"},
}})
snap = {"stages": [{"stage_id": "QS", "name": "Questionnaire Sent", "category": "booked"}],
        "projects": [{"id": "W1", "project_id": "P1", "name": "Belinda's Project", "date": d10, "stage_id": "QS", "status": "lead"},
                     {"id": "W2", "project_id": "P2", "name": "example", "date": d40, "stage_id": "QS", "status": "client"},
                     {"id": "W3", "project_id": "P3", "name": "Hand Set", "date": d40, "stage_id": "QS", "status": "lead_sent"},
                     {"id": "W4", "project_id": "P4", "name": "Confirmed", "date": d40, "stage_id": "QS", "status": "lead"},
                     {"id": "W5", "project_id": "P5", "name": "New Lead", "date": d40, "stage_id": "QS", "status": "lead"}]}
cat = {"QS": "booked"}

# 1. the status decides, never the column
check("client is booked", shb.is_booked(snap["projects"][1], cat))
check("lead in a booked column is not booked", not shb.is_booked(snap["projects"][0], cat))
check("old snapshot without status falls back to the column", shb.is_booked({"stage_id": "QS"}, cat))

# 2. sync re-stages only auto-seeded entries; hand-set and confirmed ones are reported, not touched
n = shb.sync_booking_status(snap)
led = alib.load_json(LEDGER, {})["projects"]
check("auto-seeded lead -> inquiry", led["lead-seeded"]["stage"] == "inquiry" and n == 1, led["lead-seeded"])
check("client stays booked", led["client-seeded"]["stage"] == "booked")
check("hand-set entry never re-staged", led["lead-handset"]["stage"] == "booked")
check("confirmed_by entry never re-staged", led["lead-confirmed"]["stage"] == "booked")
check("status mirrored on every entry", all(v.get("honeybook_status") for v in led.values()))
diffs = shb.project_drift(snap, cat, {"QS": "Questionnaire Sent"}, today)
txt = json.dumps(diffs)
check("drift asks about the hand-set lead", "Hand Set" in txt and "really booked" in txt, txt[:300])
check("drift leaves the re-staged entry alone", "Belinda" not in txt)
# promotion: the lead signs
snap["projects"][0]["status"] = "client"
shb.sync_booking_status(snap)
check("signed lead -> booked again", alib.load_json(LEDGER, {})["projects"]["lead-seeded"]["stage"] == "booked")
snap["projects"][0]["status"] = "lead"
shb.sync_booking_status(snap)

# 3. seeding: a new lead in a booked column is seeded as inquiry
check("new lead seeded as inquiry", shb.seed_project_state(snap, cat, {"QS": "Questionnaire Sent"}, today) == 1
      and any(v["couple"] == "New Lead" and v["stage"] == "inquiry" for v in alib.load_json(LEDGER, {})["projects"].values()))

# 4. hold, then re-arm when booked
wd = (today + timedelta(days=30)).isoformat()
base = {"unit": "u-hold", "couple": "X", "wedding_date": wd, "anchor": wd, "cancel": "c"}
fu.arm_chain("wedding-lifecycle", base, "test", steps=["week-of-confirm"])
fu.cancel_unit("wedding-lifecycle", "u-hold", "inquiry", hold=True)
st = [i for i in fu.load_state()["items"].values() if i.get("unit") == "u-hold"]
check("hold parks the chain as expired", st and all(i["status"] == "expired" for i in st), st)
r = fu.arm_chain("wedding-lifecycle", base, "test", steps=["week-of-confirm"])
check("a held chain re-arms once booked", r["week-of-confirm"].get("armed"), r)

# 5. calendar labels
sp = snap["projects"]
check("calendar: booked couple", sl.classify_booking({"date": d40, "title": "example wedding"}, sp)[0] == "booked")
check("calendar: lead couple is an inquiry", sl.classify_booking({"date": d10, "title": "Belinda wedding"}, sp)[0] == "inquiry")
check("calendar: a friend's wedding is unconfirmed",
      sl.classify_booking({"date": (today + timedelta(days=9)).isoformat(), "title": "Justin's wedding"}, sp)[0] == "unconfirmed")

# 6. clash: only the booked wedding on an away day gets a card; one card per wedding+date
away = [{"date": d10, "end_date": d10, "title": "Catalina hike", "all_day": True, "hours": 24, "owner": "owner1", "feed": "g"},
        {"date": d40, "end_date": d40, "title": "example wedding", "all_day": True, "hours": 24, "owner": "owner1", "feed": "g", "booking": "booked"},
        {"date": d40, "end_date": d40, "title": "Church", "all_day": False, "hours": 2, "owner": "owner1", "feed": "g"}]
check("an inquiry on a trip day is no clash; the shoot itself is not a trip; a short event is not away",
      sl.check_clashes(away, today) == 0)
away.append({"date": (today + timedelta(days=39)).isoformat(), "end_date": (today + timedelta(days=41)).isoformat(),
             "title": "Friend's wedding trip", "all_day": True, "hours": 72, "owner": "owner1", "feed": "g"})
check("booked weddings inside a multi-day trip post cards (HoneyBook client + owner-confirmed)", sl.check_clashes(away, today) == 2)
check("never twice", sl.check_clashes(away, today) == 0)
live = cards.pending()
check("the card asks who shoots, lane wedding",
      len(live) == 2 and all("Who shoots it?" in c["question"] and c["lane"] == "wedding" for c in live), live)
check("a hand-set booking HoneyBook calls a lead gets no clash card", not any("Hand Set" in c["question"] for c in live))

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)} (scratch {TMP})")
sys.exit(1 if FAILS else 0)
