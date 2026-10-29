"""Offline tests for the lifecycle layer (assistant/scripts/tracking/): chains (rulings 4), the world model
(11), the priority brain + Eisenhower lens (10) and the evening day plan (6). Scratch ASSISTANT_DATA; no
network, no cards posted, no model."""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_life_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent / "scripts" / "tracking"))
import common as C  # noqa: E402
import advice  # noqa: E402
import chains  # noqa: E402
import dayplan  # noqa: E402
import entities  # noqa: E402
import priority  # noqa: E402
import weekly  # noqa: E402
import cards  # noqa: E402

FAILS = []
NOW = datetime(2026, 10, 15, 19, 30)     # a Thursday evening


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


CFG = chains.cfg()
check("chains.json loads: four chains, ask defaults", set(CFG["chains"]) == {"shoot", "edit", "lead", "money"} and CFG["ask"]["max_asks"] == 3)
check("every gate has a label and an evidence list", all(isinstance(g.get("evidence"), list) and g.get("label")
      for ch in CFG["chains"].values() for g in (ch.get("gates") or []) + [g for s in (ch.get("schedule") or {}).values() for g in s["gates"]]))

# ------------------------------------------------------------------ a small world
HB = {"stages": [{"stage_id": "s0", "name": "Inquiry", "category": "lead", "order": 0}, {"stage_id": "s1", "name": "Say Hello", "category": "lead", "order": 1},
                 {"stage_id": "s4", "name": "Proposal Sent", "category": "lead", "order": 4}, {"stage_id": "s8", "name": "Questionnaire Sent", "category": "booked", "order": 8}],
      "projects": [{"id": "L1", "name": "Indy Greystone's Project", "date": "2027-06-19", "stage_id": "s4", "status": "lead_sent", "stage_moved_at": "2026-09-20T10:00:00", "last_activity_at": "2026-10-01"},
                   {"id": "L2", "name": "Fresh Lead's Project", "date": "2027-05-01", "stage_id": "s0", "status": "lead", "stage_moved_at": "2026-10-14T10:00:00", "last_activity_at": "2026-10-14"},
                   {"id": "L3", "name": "ZZ Test Lead", "date": "2027-05-01", "stage_id": "s0", "status": "lead", "stage_moved_at": "2026-10-14", "last_activity_at": "2026-10-14"},
                   {"id": "L4", "name": "Cold Lead's Project", "date": "2027-05-01", "stage_id": "s1", "status": "lead", "stage_moved_at": "2026-05-01", "last_activity_at": "2026-05-02"},
                   {"id": "P1", "name": "example", "date": "2026-10-24", "stage_id": "s8", "status": "client", "stage_moved_at": "2026-09-07"}]}
LEDGER = {"projects": {"2026-10-24_example": {"couple": "example", "wedding_date": "2026-10-24", "stage": "booked", "honeybook_status": "client", "honeybook_id": "P1"},
                       "2026-09-05_example": {"couple": "example Underhill's Project", "wedding_date": "2026-09-05", "stage": "edit", "honeybook_status": "client"},
                       "2026-10-30_Lead": {"couple": "Some Lead", "wedding_date": "2026-10-30", "stage": "inquiry", "honeybook_status": "lead"}}}
EDITOR = {"example_example": {"wedding": {"project": "example", "music": {"song": "x"}}, "done": ["brief", "transcribe", "slots"]}}
CAL = [{"date": "2026-10-16", "time": "09:00", "title": "UPS package for example's shoot (signature)", "owner": "owner1", "all_day": False, "uid": "e1"},
       {"date": "2026-10-16", "time": "11:00", "title": "Phone call with Will", "owner": "owner1", "all_day": False, "uid": "e2"},
       {"date": "2026-10-16", "time": "14:00", "title": "Dentist", "owner": "owner6", "all_day": False, "uid": "e3"},
       {"date": "2026-10-16", "time": None, "title": "example: signature couples shoot", "owner": "owner1", "all_day": True, "uid": "e4"},
       {"date": "2026-10-24", "time": "14:00", "title": "example wedding - owner6 second shooter", "owner": "owner6", "all_day": False, "uid": "e5"}]
FUS = [{"id": "f1", "sop": "wedding-lifecycle", "step": "timeline-request", "status": "done", "unit": "2026-10-24_example", "subject": "example - timeline"},
       {"id": "f2", "sop": "money", "step": "bill-due", "status": "waiting", "lane": "money", "subject": "Verizon bill $88.00 due 2026-10-16", "fire_at": "2026-10-13T17:00:00"},
       {"id": "f3", "sop": "personal", "step": "awaiting-reply", "status": "waiting", "lane": "personal", "subject": "Will - reply about the trip", "fire_at": "2026-10-16T09:00:00"}]
CHAT = [{"ts": "2026-10-10 10:00", "who": "owner1", "text": "Licensed the song for example on Musicbed"},
        {"ts": "2026-10-12 10:00", "who": "Sage", "text": "example first cut done"}]       # Sage's own words never count
REG = {"money": [{"name": "Car insurance", "kind": "insurance", "amount": 640, "due": "2026-10-20"},
                 {"name": "Q3", "kind": "quarterly", "due": "2027-01-15", "piece": "bookkeeping", "done": True}],
       "people": [{"name": "Finley Pennywhistle", "relation": "mother-in-law", "birthday": "10-28", "last_contact": "2026-10-01"},
                  {"name": "Casey Juniper", "relation": "friend", "last_contact": "2026-07-01", "promised": [{"what": "send the drone footage", "by": "2026-10-10"}]}],
       "trips": [{"name": "Solvang shoot trip", "start": "2026-10-17", "pieces": [{"piece": "rentals", "booked": True}, {"piece": "lodging", "booked": False}]}]}


def world(now=NOW, state=None, **kw):
    base = dict(ledger=LEDGER, hb=HB, editor=EDITOR, calendar=CAL, followups=FUS, tickets=[], execs=[], chat=CHAT, registers=REG,
                state=state if state is not None else {"gates": {}, "lane_day": {}})
    base.update(kw)
    return chains.World(now=now, **base)


# ------------------------------------------------------------------ chains: entities
w = world()
leads = chains.entities_for("lead", CFG["chains"]["lead"], w)
check("lead chain: real leads only (no ZZ test, no 45-day-quiet lead)", {e["title"] for e in leads} == {"Indy Greystone", "Fresh Lead"}, [e["title"] for e in leads])
shoot = chains.entities_for("shoot", CFG["chains"]["shoot"], w)
check("shoot chain: the booked couple inside 60 days, not the lead, not the past wedding", [e["title"] for e in shoot] == ["example"], shoot)
edit = chains.entities_for("edit", CFG["chains"]["edit"], w)
check("edit chain: the wedding that passed", [e["title"] for e in edit] == ["example Underhill"], edit)
money = chains.entities_for("money", CFG["chains"]["money"], w)
check("money chain: nothing inside 60 days of 10/15 (next quarterly is 1/15)", money == [], money)
w2 = world(now=datetime(2026, 12, 20, 10, 0))
money = chains.entities_for("money", CFG["chains"]["money"], w2)
check("money chain: on 12/20 the 1/15 quarterly is in play", len(money) == 1 and money[0]["anchor"] == date(2027, 1, 15), money)

# ------------------------------------------------------------------ chains: the walk
rows = chains.gaps(w, CFG)
by = {(r["chain"], r["title"], r["gate"]): r for r in rows}
check("lead at Proposal Sent: reply / call / proposal done by HoneyBook order", all(by[("lead", "Indy Greystone", g)]["status"] == "done" for g in ("reply", "call", "proposal")))
erin = by[("lead", "Indy Greystone", "contract")]
check("contract up next, 14-day window from the stage move, overdue -> ASK", erin["status"] == "ask" and erin["due"] == "2026-10-04" and erin["ask_due"].startswith("overdue"), erin)
check("the retainer behind it waits (one open gate per entity)", by[("lead", "Indy Greystone", "retainer")]["status"] == "later")
fresh = by[("lead", "Fresh Lead", "reply")]
check("a brand-new inquiry: reply due in 1 day, inside lead_days -> ASK", fresh["status"] == "ask" and fresh["due"] == "2026-10-15", fresh)
check("shoot: timeline done by the follow-up, second shooter done by owner6's calendar", by[("shoot", "example", "timeline")]["status"] == "done"
      and by[("shoot", "example", "second_shooter")]["status"] == "done" and "calendar" in by[("shoot", "example", "second_shooter")]["why"], by[("shoot", "example", "second_shooter")])
rent = by[("shoot", "example", "rentals")]
check("shoot: rentals up next at T-9; offsets -14/-7/-3 -> asked at the last crossed offset (T-14)", rent["status"] == "ask" and rent["ask_due"] == "T-14", rent)
check("shoot: a dry-run card recommends 'Book it for me' for a bookable gate, 4 options, advice line, the gate's own question",
      (lambda q: q["options"][0]["label"] == "Book it for me (Recommended)" and len(q["options"]) == 4 and q["advice"].startswith("💡") and q["question"].endswith("Book it?"))
      (chains._question(rent, [r for r in rows if r["chain"] == "shoot"], CFG["chains"]["shoot"], CFG["chains"]["shoot"]["gates"][2], CFG["ask"])))
q = chains._question(erin, [r for r in rows if r["title"] == "Indy Greystone"], CFG["chains"]["lead"], CFG["chains"]["lead"]["gates"][3], CFG["ask"])
check("a non-bookable gate recommends Booked and asks its own question", q["options"][0]["label"] == "Booked (Recommended)" and q["question"].endswith("Has it come in?"), q)
ex, err = cards._sanitize_exec(q)
check("the chain card has no exec (never a send) and passes the card sanitizer", not ex and not err, err)
check("edit: ingested + music + first cut proved by the editor / chat; review_sent up next, not yet asked (due 10/17, lead 7 -> ask)",
      by[("edit", "example Underhill", "ingested")]["status"] == "done" and by[("edit", "example Underhill", "music")]["status"] == "done"
      and by[("edit", "example Underhill", "first_cut")]["status"] == "done" and by[("edit", "example Underhill", "review_sent")]["status"] == "ask",
      {k[2]: (v["status"], v["why"]) for k, v in by.items() if k[0] == "edit"})
check("Sage's own chat lines never count as evidence", "said so" not in by[("edit", "example Underhill", "first_cut")]["why"])

# offsets: at T-7 the rentals gate is asked again only if T-14 was used
st = {"gates": {"shoot:2026-10-24_example:rentals": {"status": "asked", "asks": 1, "last_ask": "2026-10-10 09:00", "offsets_asked": [-14]}}, "lane_day": {}}
w3 = world(now=datetime(2026, 10, 17, 10, 0), state=st)
r = {(x["chain"], x["gate"]): x for x in chains.gaps(w3, CFG)}[("shoot", "rentals")]
check("offsets: T-7 crossed and not yet used -> asked again", r["status"] == "ask" and r["ask_due"] == "T-7", r)
st["gates"]["shoot:2026-10-24_example:rentals"]["offsets_asked"] = [-14, -7]
r = {(x["chain"], x["gate"]): x for x in chains.gaps(world(now=datetime(2026, 10, 18, 10, 0), state=st), CFG)}[("shoot", "rentals")]
check("offsets: T-7 used, T-3 not crossed -> up_next, no ask", r["status"] == "up_next" and not r.get("ask_due"), r)
st["gates"]["shoot:2026-10-24_example:rentals"]["asks"] = 3
r = {(x["chain"], x["gate"]): x for x in chains.gaps(world(now=datetime(2026, 10, 22, 10, 0), state=st), CFG)}[("shoot", "rentals")]
check("max_asks reached: never asked again", r["status"] == "up_next", r)

# the owner's word
st = {"gates": {"shoot:2026-10-24_example:rentals": {"status": "done", "ts": "2026-10-15T10:00:00"}}, "lane_day": {}}
r = {(x["chain"], x["gate"]): x for x in chains.gaps(world(state=st), CFG)}
check("'Booked' marks the gate done and the walk moves on", r[("shoot", "rentals")]["status"] == "done" and r[("shoot", "travel")]["status"] == "ask", r[("shoot", "travel")])
st = {"gates": {"shoot:2026-10-24_example:rentals": {"status": "dropped"}}, "lane_day": {}}
r = {(x["chain"], x["gate"]): x for x in chains.gaps(world(state=st), CFG)}
check("'Drop' latches the gate off and the walk moves on", r[("shoot", "rentals")]["status"] == "dropped" and r[("shoot", "travel")]["status"] == "ask")
st = {"gates": {"lead:L1:contract": {"status": "dropped", "ts": "2026-10-15T10:00:00"}}, "lane_day": {}}
r = {(x["chain"], x["entity"], x["gate"]): x for x in chains.gaps(world(state=st), CFG)}
check("a lead's dropped contract latches the lead: no retainer card behind it", r[("lead", "L1", "contract")].get("latched")
      and r[("lead", "L1", "retainer")]["status"] == "later", r[("lead", "L1", "retainer")])
held = []
chains._hold_lead_followups(list(r.values()), st, cancel=lambda sop, unit, **k: held.append((sop, unit, k.get("keep_steps"))))
chains._hold_lead_followups(list(r.values()), st, cancel=lambda sop, unit, **k: held.append((sop, unit)))
check("the latch stops that lead's proposal nudges once per drop", held == [("inquiry", "L1", ("reply-sla",))], held)
hb_moved = dict(HB, projects=[dict(HB["projects"][0], last_activity_at="2026-10-15T12:00:00")] + HB["projects"][1:])
r = {(x["chain"], x["entity"], x["gate"]): x for x in chains.gaps(world(state=st, hb=hb_moved), CFG)}
check("HoneyBook activity after the drop releases the latch", not r[("lead", "L1", "contract")].get("latched")
      and r[("lead", "L1", "retainer")]["status"] in ("up_next", "ask"), r[("lead", "L1", "retainer")])
st = {"gates": {"shoot:2026-10-24_example:rentals": {"status": "booking", "ticket": "t1"}}, "lane_day": {}}
r = {(x["chain"], x["gate"]): x for x in chains.gaps(world(state=st), CFG)}
check("'Book it for me' holds the chain at that gate while the ticket is out", r[("shoot", "rentals")]["status"] == "booking" and r[("shoot", "travel")]["status"] == "later")

# harvest
minted = []
st = {"gates": {"a:1:g": {"status": "asked", "qid": "q1", "asks": 1, "lane": "wedding", "label": "x", "title": "A"},
                "a:2:g": {"status": "asked", "qid": "q2", "asks": 1, "lane": "wedding", "label": "y", "title": "B"},
                "a:3:g": {"status": "asked", "qid": "q3", "asks": 1}, "a:4:g": {"status": "asked", "qid": "q4", "asks": 1}}, "lane_day": {}}
n = chains.harvest(st, answers=[{"qid": "q1", "option": "Booked (Recommended)", "ts": "2026-10-15T10:00:00"},
                                {"qid": "q2", "option": "Book it for me", "ts": "2026-10-15T10:00:00"},
                                {"qid": "q3", "option": "Not yet", "ts": "2026-10-15T10:00:00", "text": "next week"},
                                {"qid": "q4", "option": "Drop", "ts": "2026-10-15T10:00:00"}],
                   opts=CFG["ask"]["options"], mint=lambda k, g: minted.append(k) or "tk1")
check("harvest: Booked / Book it for me / Not yet / Drop land in the ledger; Book it mints one ticket", n == 4 and st["gates"]["a:1:g"]["status"] == "done"
      and st["gates"]["a:2:g"]["status"] == "booking" and st["gates"]["a:2:g"]["ticket"] == "tk1" and minted == ["a:2:g"]
      and st["gates"]["a:3:g"]["status"] == "not_yet" and st["gates"]["a:3:g"]["answer_text"] == "next week" and st["gates"]["a:4:g"]["status"] == "dropped", st)
check("harvest is idempotent", chains.harvest(st, answers=[{"qid": "q1", "option": "Booked"}], opts=CFG["ask"]["options"]) == 0)
check("a dismissed booking ticket reopens the gate", (lambda s: chains._booking_still_open(s, {"tk1": {"status": "dismissed"}}) == 1 and s["gates"]["a:2:g"]["status"] == "not_yet")(st))

# tick: one ask per entity, max_per_lane_per_day, dry run posts nothing, quiet hours
C.save_state("chains", {"gates": {}, "lane_day": {}})
out = chains.tick(now=NOW.replace(hour=10), dry=True, w=world(now=NOW.replace(hour=10)), c=CFG)
check("tick (dry): at most max_per_lane_per_day cards per lane", out["asks"] >= 3 and len(out["posted"]) == CFG["ask"]["max_per_lane_per_day"], out)
check("tick (dry) writes no state", C.state("chains", {}).get("gates") == {})

# stale leads (2026-10-29): Lane Calloway-Brook carded "book a call?" 265 days after the step was due
hb_stale = dict(HB, projects=HB["projects"] + [{"id": "L9", "name": "Lane Calloway-Brook's Project", "date": "2027-08-01", "stage_id": "s1", "status": "lead",
                                               "stage_moved_at": "2026-01-20T10:00:00", "last_activity_at": "2026-10-01"}])
ws = world(now=NOW.replace(hour=10), hb=hb_stale)
r = {(x["entity"], x["gate"]): x for x in chains.gaps(ws, CFG) if x["chain"] == "lead"}
zk = next(x for k, x in r.items() if k[0] == "L9" and x["status"] not in ("done", "later"))
check("a lead whose step was due more than 60 days ago is stale, not asked", zk["status"] == "stale" and "days ago" in zk["why"], zk)
check("Erin (due 11 days ago) is still asked", r[("L1", "contract")]["status"] == "ask", r[("L1", "contract")])
check("a stale lead is not an open loop in the world model (no top-5 'new lead')",
      not any(e["id"].endswith(":L9") for e in entities.from_chains(chains.gaps(ws, CFG), ws.now)))
check("no HoneyBook activity in 60 days is stale too", chains._stale_lead(CFG["chains"]["lead"], {"hb": {"last_activity_at": "2026-08-01"}},
      date(2026, 10, 10), {}, ws).startswith("no HoneyBook activity"))
check("Keep on the archive card turns the stale check off", chains._stale_lead(CFG["chains"]["lead"], {"hb": {}}, date(2026, 1, 1), {"stale_kept": "x"}, ws) == "")
posted_q = []
_orig_post = chains._post
chains._post = lambda q, dry: posted_q.append(q) or f"qs{len(posted_q)}"
try:
    st = {"gates": {}, "lane_day": {}}
    chains.tick(now=ws.now, w=world(now=ws.now, hb=hb_stale, state=st), c=CFG)
    arch = [q for q in posted_q if q["header"].startswith("Archive this lead?")]
    check("tick: ONE 'Archive this lead?' card for the stale lead, no booking card for it",
          len(arch) == 1 and "Lane Calloway-Brook" in arch[0]["header"] and not any(q["header"].startswith("Next step") and "Lane Calloway-Brook" in q["header"] for q in posted_q), posted_q)
    check("the archive card is not urgent (held for Monday's digest)", not arch[0].get("urgent")
          and not any(arch[0]["header"].startswith(h) for h in C.cfg().get("cards", {}).get("urgent_header_prefixes", [])))
    n0 = len(posted_q)
    chains.tick(now=ws.now + timedelta(days=8), w=world(now=ws.now + timedelta(days=8), hb=hb_stale, state=st), c=CFG)
    check("the archive card is filed once, never again", not any(q["header"].startswith("Archive") for q in posted_q[n0:]), posted_q[n0:])
    key = next(k for k, g in st["gates"].items() if g.get("stale_qid"))
    chains.harvest(st, answers=[{"qid": st["gates"][key]["stale_qid"], "option": "Archive (Recommended)", "ts": "2026-10-16T09:00:00"}], opts=CFG["ask"]["options"])
    check("Archive drops the lead's gate (latched until HoneyBook moves)", st["gates"][key]["status"] == "dropped", st["gates"][key])
finally:
    chains._post = _orig_post

# ------------------------------------------------------------------ the world model
model = entities.build(world(), CFG and C.cfg(), wiki=[{"name": "Morgan Ironwood", "birthday": "", "updated": "2026-10-08", "lead": False},
                                                       {"name": "example example and Dean (lead)", "birthday": "", "updated": "2026-10-08", "lead": True}])
rows = model["rows"]
kinds = {r["kind"] for r in rows}
check("world model: a row per lead / couple / edit / obligation / person / trip / appointment", {"lead", "couple", "edit", "obligation", "person", "trip", "appointment"} <= kinds, kinds)
byid = {r["id"]: r for r in rows}
check("lead row: stage, next action, deadline, days overdue", byid["lead:L1"]["next_action"] == "book the signed contract" and byid["lead:L1"]["days_overdue"] == 11
      and byid["lead:L1"]["stage"] == "the proposal", byid["lead:L1"])
check("money: the Verizon follow-up ($88, due 10/16) and the insurance register entry ($640, hard)",
      byid["fu:f2"]["amount"] == 88.0 and byid["fu:f2"]["deadline"] == "2026-10-16" and byid["reg:Car insurance"]["hard"] and byid["reg:Car insurance"]["amount"] == 640, [r for r in rows if r["kind"] == "obligation"])
check("people: Jon's promised item is overdue; Finley's birthday inside 30 days; Will (wiki, contact 7 d ago) has no open loop",
      "promised" in byid["person:Casey Juniper"]["next_action"] and byid["person:Casey Juniper"]["days_overdue"] == 5
      and "birthday" in byid["person:Finley Pennywhistle"]["next_action"] and "person:Morgan Ironwood" not in byid, [r for r in rows if r["kind"] == "person"])
check("trip: the unbooked piece is the next action, due 3 days before", byid["trip:Solvang shoot trip"]["next_action"] == "book lodging" and byid["trip:Solvang shoot trip"]["deadline"] == "2026-10-14")
check("appointments: owner6's dentist is hers; the all-day shoot is one row", byid["event:e3"]["owner"] == "owner6" and byid["event:e3"]["lane"] == "owner6" and "event:e4" in byid)
check("every row is scored with a quadrant", all(r.get("quadrant") in ("DO", "SCHEDULE", "DELEGATE", "DELETE") and "score" in r for r in rows))
check("ranked: the overdue $ lead with a client waiting is on top", rows[0]["id"] == "lead:L1", rows[0])
made = entities.init()
check("init writes the three registers with their shape documented", len(made) == 3 and all((chains.REGISTERS / f"{n}.json").exists() for n in ("money", "people", "trips"))
      and C.load_json(chains.REGISTERS / "money.json", {}).get("_doc"))
check("init never overwrites a filled register", entities.init() == [])
m = entities.run(world())
check("run writes entities.json + chain_gaps.json", C.state("entities", {}).get("rows") and C.state("chain_gaps", {}).get("rows"))

# ------------------------------------------------------------------ the priority brain
p = priority.pcfg()
client = priority.importance({"lane": "wedding", "amount": 8000, "waiting_kind": "client"}, p, {"wedding": 3})
admin = priority.importance({"lane": "wedding", "waiting_kind": "admin"}, p, {"wedding": 3})
check("importance: dollars + who waits (client > admin)", client > admin and round(client, 1) == round(3 * (1 + 3.903 + 3), 1), (client, admin))
check("a hard date adds hard_bonus", priority.importance({"hard": True}, p) == 1 + p["hard_bonus"])
check("urgency: 1.0 at the deadline, 3.0 deeply overdue, 0.5 far out", priority.urgency({"days_overdue": 0}, NOW, p) == 1.0 and priority.urgency({"days_overdue": 40}, NOW, p) == 3.0
      and priority.urgency({"deadline": "2026-12-01"}, NOW, p) == 0.5)
check("quadrants: DO / SCHEDULE / DELEGATE / DELETE", priority.quadrant(10, 1.5) == "DO" and priority.quadrant(10, 0.5) == "SCHEDULE"
      and priority.quadrant(1, 1.5) == "DELEGATE" and priority.quadrant(1, 0.5) == "DELETE" and priority.quadrant(1, 0.5, hard=True) == "SCHEDULE")
simple = advice.score({"kind": "card", "lane": "wedding", "age_days": 2})
brain = advice.score({"kind": "card", "lane": "wedding", "age_days": 2, "deadline": "2026-10-10", "waiting_kind": "client", "amount": 5000})
check("advice.score keeps the simple form without a deadline and hands over to the brain with one", simple < 20 and brain > 40, (simple, brain))
q2 = priority.q2_lines([{"quadrant": "SCHEDULE", "title": "example", "next_action": "book the second shooter", "deadline": "2026-10-20", "importance": 12},
                        {"quadrant": "SCHEDULE", "title": "Far", "next_action": "x", "deadline": "2027-03-01", "importance": 12},
                        {"quadrant": "DO", "title": "Erin", "next_action": "contract", "deadline": "2026-10-04", "importance": 12}], NOW)
check("Q2 lines: only SCHEDULE items inside the horizon, with the date", q2 == ["example: book the second shooter (by 10/20, 5 days)."], q2)
C.save_state("entities", {"rows": [{"quadrant": "SCHEDULE", "title": "example", "next_action": "book the second shooter", "deadline": "2026-10-20", "importance": 12}]})
C.save_state("drift", {"findings": []})
text = weekly.readback_text(datetime(2026, 10, 16, 16, 0))
check("the Friday readback carries the Q2 nudge line (a line, never a card)", "Worth scheduling this week" in text and "example: book the second shooter" in text, text)

# ------------------------------------------------------------------ the day plan
D = dayplan.dcfg()
day = datetime(2026, 10, 16)
ents = [{"kind": "obligation", "title": "Verizon bill", "next_action": "pay it", "deadline": "2026-10-16", "owner": "owner1"},
        {"kind": "appointment", "title": "x", "next_action": "y", "deadline": "2026-10-16", "owner": "owner1"}]
its = dayplan.items_for(day, cal=CAL, entities=ents, gaps=[{"due": "2026-10-16", "status": "up_next", "title": "example", "label": "the review link"}],
                        fus=FUS, chase_state={"c1": {"status": "snoozed", "snooze_until": "2026-10-16 08:00", "title": "Insurance renewal", "owner": "owner1"}},
                        hb_tasks=[{"status": "pending", "due_date": "2026-10-16", "description": "Send Custom Packages Guide", "project": "Finley Larkspur's Project"}], d=D)
kinds = [x["kind"] for x in its]
check("items: timed events + calls + deadlines + reminders + chases + HB tasks; all-day listed but not counted",
      kinds.count("event") == 3 and "call" in kinds and kinds.count("deadline") == 3 and "followup" in kinds and "chase" in kinds
      and sum(1 for x in its if x["counts"]) == len(its) - 1, its)
check("timed items first, in time order", [x["text"][:9] for x in its[:3]] == ["UPS packa", "reminder:", "Phone cal"], [x["text"] for x in its[:4]])
msg = dayplan.message(day, its, D)
check("message: tomorrow's blocks, 'Also due', owner6's own line", msg.startswith("Tomorrow, Friday 10/16:") and "Morning: 9am UPS package" in msg and "Afternoon:" not in msg
      and "Also due:" in msg and "owner6: 2pm Dentist" in msg and "Verizon bill" in msg, msg)
joh_free = [x for x in its if x["owner"] != "owner6"]
check("no owner6 line on a day she has nothing", "owner6:" not in dayplan.message(day, joh_free, D))
C.save_state("dayplan", {})
r = dayplan.tick(now=NOW.replace(hour=18), dry=True, items=its, d=D)
check("tick: before plan_at nothing happens", r.get("skipped") == "too early", r)
r = dayplan.tick(now=NOW, dry=True, items=its, d=D)
check("tick at 19:30 with 3+ timed items: ONE message", r["posted"] and r["timed"] >= 3 and r["text"].startswith("Tomorrow"), r)
r = dayplan.tick(now=NOW, dry=True, items=its[:2], d=D)
check("tick with fewer than min_timed_items: silence", r.get("silent") and not r["posted"], r)
r = dayplan.tick(now=NOW, dry=False, items=[], d=D)
r2 = dayplan.tick(now=NOW.replace(hour=21), dry=False, items=its, d=D)
check("once per target day (state written on the first tick, even a silent one)", r.get("silent") and r2.get("skipped") == "done for the day", (r, r2))

# ------------------------------------------------------------------ live records (read-only smoke)
try:
    live = chains.gaps()
    check("live: chains walk the real records without error", isinstance(live, list))
except Exception as e:  # noqa: BLE001
    check("live: chains walk the real records without error", False, f"{type(e).__name__}: {e}")

print(f"\n{len(FAILS)} failure(s)" + (": " + ", ".join(FAILS) if FAILS else ""))
sys.exit(1 if FAILS else 0)
