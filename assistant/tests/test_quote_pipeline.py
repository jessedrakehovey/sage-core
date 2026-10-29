"""Offline tests for the lead -> priced quote path: pricing/engine.py parsing + flags,
pricing/hb_draft.py rendering, hb_exec's safety check. No network: the config is the snapshot on
disk and travel is passed in. Prices themselves are pinned against the 2026-09-30 sheet snapshot
so an accidental change to the arithmetic shows up here.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT = HERE.parent.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_quote_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(VAULT / "pricing"))
import engine  # noqa: E402
import hb_exec  # noqa: E402
import sense_typeform as st  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


CFG = json.loads((VAULT / "pricing" / "sheet_config_2026-09-30.json").read_text(encoding="utf-8"))
R = engine.REF
TODAY = datetime.date(2026, 10, 2)
TRAV = {"mode": "drive", "miles": 40.0, "in_ca": True, "resolved": "example"}


def ans(k, t, v):
    return {"field": {"ref": R[k] + "-xxxx"}, "type": t, t: v}


def item(hours="8", budget="$4,000 - $6,000", adds=("Aerial Cinematography/Drone - scale", "Second videographer"),
         date="2027-06-12", city="example", name="Pat O'Brien"):
    a = [ans("name", "text", name), ans("partner", "text", "Sam"), ans("email", "email", "pat@example.com"),
         ans("date", "date", date + "T00:00:00Z"), ans("hours", "choice", {"label": hours}),
         ans("budget", "choice", {"label": budget}), ans("additions", "choices", {"labels": list(adds)}),
         ans("venue_country", "text", "United States"), ans("film", "choice", {"label": "A feature (9-11 minutes)"}),
         ans("vows", "choice", {"label": "One Powerful Moment"})]
    if city:
        a += [ans("venue_city", "text", city), ans("venue_state", "text", "CA")]
    return {"token": "tok1", "submitted_at": "2026-10-01T10:00:00Z", "answers": a}


def q_for(**kw):
    lead = engine.parse_response(item(**kw))
    return lead, engine.quote(lead, CFG, TODAY, TRAV if lead.venue else {"mode": "unknown"})


# 1. a clean lead: no flags, and the arithmetic is pinned
lead, q = q_for()
prices = {t["name"]: t["price"] for t in q["tiers"]}
check("clean lead is ready with no flags", q["status"] == "ready" and q["flags"] == [], q["flags"])
check("prices pinned to the 2026-09-30 sheet", prices == {"Keepsake": 3846, "Classic": 4737, "Signature": 7569, "Legacy": 9285}, prices)
_, q5 = q_for(hours="5")
check("5 hours pinned", {t["name"]: t["price"] for t in q5["tiers"]}["Keepsake"] == 3213)

# 1b. no slow-season or weekday discounts (the owner 2026-10-16): example & Dean, Fri 2027-07-30,
# Signature was ($4,700 + $26 travel) x 0.95 July x 1.05 Friday = $4,714; floored it is $4,962
mx = engine.multipliers(datetime.date(2027, 7, 30), CFG, TODAY)
check("July is no longer discounted", mx["season"] == 1.0 and mx["total"] == 1.05, mx)
sig = next(p for p in CFG["packages"] if p["package_name"] == "Signature")
check("example & Dean's Signature quote is $4,962", round((sig["base_price"] + 26) * mx["total"]) == 4962, sig["base_price"])
days = [TODAY + datetime.timedelta(days=n) for n in range(400)]
check("no season or weekday multiplier lowers a quote",
      all(min(engine.multipliers(d, CFG, TODAY)[k] for k in ("season", "day")) >= 1 for d in days))
# backtest against the sheet's raw multipliers: only Mon-Thu and Jan/Feb/Jul/Aug dates move
moved, kept = set(), 0
for d in days[30::3] + [TODAY + datetime.timedelta(days=n) for n in range(430, 760, 3)]:
    q = engine.quote(engine.parse_response(item(date=d.isoformat())), CFG, TODAY, TRAV)
    raw = round(CFG["seasonMultipliers"][str(d.month)]["multiplier"]
                * CFG["dayMultipliers"][str((d.weekday() + 1) % 7)]["multiplier"] * q["multipliers"]["notice"], 4)
    b = lambda t: t["breakdown"]  # noqa: E731
    old = [round((b(t)["base"] + b(t)["coverage"] + b(t)["addons"] + b(t)["logistics"]) * raw) for t in q["tiers"]]
    if [t["price"] for t in q["tiers"]] != old:
        moved.add(d.month in (1, 2, 7, 8) or d.weekday() < 4)
    else:
        kept += 1
check("backtest: only Mon-Thu / Jan, Feb, Jul, Aug quotes change", moved == {True} and kept > 0, (moved, kept))

# 2. odd answers become flags, never a silent guess and never a crash
lead, q = q_for(hours="10-12 hours")
check("a range label is its first number, not 1012", lead.hours == 10, lead.hours)
lead, q = q_for(hours="")
check("missing hours is flagged", q["status"] == "needs_review" and any("coverage hours" in f for f in q["flags"]), q["flags"])
lead, q = q_for(budget="$5,000 - $8,000")
check("an unmapped budget label is flagged", any("budget answer not recognised" in f for f in q["flags"]), q["flags"])
lead, q = q_for(adds=("First look",))
check("an unknown addition is flagged", any("addition not recognised" in f for f in q["flags"]), q["flags"])
lead, q = q_for(adds=("None of these",))
check("'none' is not flagged", q["flags"] == [], q["flags"])
lead, q = q_for(city=None)
check("a country-only venue is no venue", lead.venue == "" and any("no venue given" in f for f in q["flags"]), (lead.venue, q["flags"]))
it = item()
it["answers"] += [{"field": {"ref": R["style"] + "-x"}, "type": "choice", "choice": None}, {"field": {}, "type": "text", "text": "x"}]
check("a null choice / an answer with no ref does not crash", bool(engine.parse_response(it)))
lead, q = q_for(date="June 12th")
check("an unreadable date is flagged", any("could not be read" in f for f in q["flags"]), q["flags"])
q = engine.quote(engine.parse_response(item()), dict(CFG, _source="offline snapshot x.json"), TODAY, dict(TRAV, approx=True))
check("offline prices and an approximate venue are flagged", any("offline snapshot" in f for f in q["flags"]) and any("approximate" in f for f in q["flags"]), q["flags"])
try:
    engine.quote(engine.parse_response(item()), dict(CFG, packages=[dict(p, is_active=False) for p in CFG["packages"]]), TODAY, TRAV)
    check("no active packages raises a clear error", False)
except ValueError as e:
    check("no active packages raises a clear error", "no active packages" in str(e), e)

# 3. the HoneyBook draft program
lead = engine.parse_response(item(name='Pat "Paddy" O\'Brien \\ x'))
lead.venue = "the sendero (ojai), CA"
q = engine.quote(lead, CFG, TODAY, TRAV)
q["answers"] = lead.answers
code, intent = hb_exec.build_code("hb_proposal_draft", {"response_token": "tok1", "quote": q})
check("draft renders with quotes / apostrophe / backslash in the name", "createSmartFile" in code and "not published" in intent)
check("a venue like 'sendero (' does not trip the send/delete check", True)
check("a failed pricing step reports HB_FAILED, an existing file reports SKIPPED", "NOT priced" in code and "':SKIPPED_EXISTING'" in code)
check("no template call can delete, publish or send", not __import__("re").search(r"\.(delete|remove|destroy|publish|send)[A-Za-z]*\(", code))
q2 = json.loads(json.dumps(q))
q2["lead"]["name"] = "x </code> y"
try:
    hb_exec.build_code("hb_proposal_draft", {"response_token": "tok1", "quote": q2})
    check("a name that would break the transport wrapper is refused", False)
except ValueError:
    check("a name that would break the transport wrapper is refused", True)
q3 = json.loads(json.dumps(q))
q3["lead"]["venue"] = ""
c3, _ = hb_exec.build_code("hb_proposal_draft", {"response_token": "tok1", "quote": q3})
check("retainer follows the venue rule", "Math.round(2000 * 100 / div)" in code and "Math.round(1000 * 100 / div)" in c3)
# 2026-10-18: HoneyBook now stores money as sent and reads it as CENTS ($8,114 sent as 8114 showed $81.14)
check("prices and retainer go in cents and are read back", "cents: Math.round(x.price * 100)" in code
      and "const setPrices" in code and "retainer did not save" in code and "price_per_unit: x.price" not in code)

# 3b. travel edge cases keep the price they had before
lead_zip = engine.parse_response({"token": "t", "submitted_at": "", "answers": [
    ans("name", "text", "A B"), ans("email", "email", "a@example.com"), ans("venue_state", "text", "CA"), ans("venue_zip", "text", "93023")]})
check("a state + zip venue is still a venue", lead_zip.venue == "CA, 93023", lead_zip.venue)
lead_st = engine.parse_response({"token": "t", "submitted_at": "", "answers": [ans("name", "text", "A B"), ans("venue_state", "text", "CA")]})
check("a state-only venue is not", lead_st.venue == "")
fl = engine.logistics_cost({"mode": "flight_domestic", "miles": None, "in_ca": False}, CFG)
check("a flight venue with no driving route keeps its flight + hotel", fl["flight"] > 0 and fl["hotel"] > 0, fl)

# 4. availability never claims a date is open without looking
line, flag = st.availability(None)
check("no date: nothing claimed", line == "no wedding date given" and flag is None)

# 5. a lead who booked a meeting in HoneyBook (lead_meetings.py, 2026-10-08: example & Dean, Mon Oct 12 1 PM)
import lead_meetings as lm  # noqa: E402
NOW = datetime.datetime(2026, 10, 8, 17, 0, tzinfo=datetime.timezone.utc)
MEET = {"id": "ci1", "title": "Intro call", "type": "phone_call", "start": "2026-10-12T20:00:00Z", "workspace_id": "ws9",
        "clients": [{"name": "example R", "email": "p51b3a3@example.invalid", "phone": None}], "hosts": ["staff1 example"]}
LEAD = {"lead": {"name": "example example", "partner": "Dean", "email": "p51b3a3@example.invalid", "phone": "555-092-3292"}}
check("an old snapshot without meetings changes nothing", lm.usable({"projects": []}) is None
      and lm.usable({"meetings": [{"_error": "x"}]}) is None)
m = lm.match(LEAD, [MEET], [], NOW)
check("matched by email, time in Pacific", m and m["when"] == "Mon Oct 12, 1:00 PM PT" and m["with"] == ["staff1 example"], m)
other = dict(MEET, clients=[{"name": "Someone Else", "email": "x@y.com"}], title="Call", workspace_id="ws9")
check("matched through the HoneyBook project name", lm.match(LEAD, [other], [{"id": "ws9", "name": "example example's Project"}], NOW))
check("a stranger's meeting is not theirs", lm.match(LEAD, [other], [{"id": "ws9", "name": "Pat Lee"}], NOW) is None)
check("a past meeting is not the meeting", lm.match(LEAD, [dict(MEET, start="2026-10-01T20:00:00Z")], [], NOW) is None)
posted = []
q5 = json.loads(json.dumps(LEAD))
check("found -> lead record changes", lm.apply(q5, [MEET], [], NOW, post=lambda c: posted.append(c) or "q1"))
check("the note tells the drafter to confirm, not schedule", "Mon Oct 12, 1:00 PM PT" in q5["meeting_note"]
      and "do not ask them to schedule" in q5["meeting_note"] and q5["meeting_card"] == "q1")
ex = posted[0]["exec"] if posted else {}
check("one calendar card, approve label is an option", len(posted) == 1 and ex["kind"] == "calendar_hold"
      and ex["payload"]["date"] == "2026-10-12" and ex["payload"]["time"] == "13:00"
      and ex["approve_label"] in [o["label"] for o in posted[0]["options"]], posted)
import exec_store  # noqa: E402
check("the calendar payload passes the exec queue's key check", not exec_store.validate_payload("calendar_hold", ex.get("payload") or {}))
check("the same meeting again: no second card", not lm.apply(q5, [MEET], [], NOW, post=lambda c: posted.append(c) or "q2") and len(posted) == 1)
check("a cancelled upcoming meeting is cleared", lm.apply(q5, [], [], NOW) and q5["meeting"] is None and "no longer" in q5["meeting_note"])
q5["meeting"], q5["meeting_note"] = m, lm.note({"meeting": m})
subj, body = st.brief(dict(q, **{"meeting": m, "meeting_note": q5["meeting_note"]}), "now")
check("the brief carries the meeting", "MEETING BOOKED" in body and "Mon Oct 12" in body)
check("no meeting: the brief is unchanged", "MEETING BOOKED" not in st.brief(q, "now")[1])
(TMP / "leads").mkdir(exist_ok=True)
(TMP / "leads" / "tokM.json").write_text(json.dumps(LEAD), encoding="utf-8")
res = lm.sweep({"meetings": [MEET], "projects": []}, NOW, post=lambda c: "q3")
saved = json.loads((TMP / "leads" / "tokM.json").read_text(encoding="utf-8"))
check("sweep writes the meeting onto the lead file", "lead_meetings_changed=1" in res and saved["meeting"]["id"] == "ci1", res)
check("sweep with unread meetings touches nothing", lm.sweep({"projects": []}, NOW) == "meetings=not-read")

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)} (scratch {TMP})")
sys.exit(1 if FAILS else 0)
