"""Offline tests for the desk page's 'Lead funnel' tile (dashboard/build.py lead_funnel, 2026-10-20):
two fake quiz leads, a fake HoneyBook snapshot and a fake exec ticket log in a scratch folder.
    python assistant/tests/test_lead_funnel.py
"""
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "dashboard"))
import build  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="wv_funnel_"))
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


NOW = datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc)
leads = TMP / "leads"
leads.mkdir()
# lead A: texted 10 min after the form, meeting booked 2026-10-07 13:12 UTC (id timestamp) for 10-12 20:00, booked in HoneyBook
(leads / "a.json").write_text(json.dumps({
    "lead": {"name": "example example", "phone": "(555) 092-3292", "submitted_at": "2026-10-06T16:40:00Z"},
    "meeting": {"id": "d128e3d7b0a55c1c821de610", "start": "2026-10-12T20:00:00.000+00:00"}}), encoding="utf-8")
# lead B: texted 30 min after, meeting still ahead (booked 2026-10-20 15:22 UTC for 10-23 20:30), still a lead
(leads / "b.json").write_text(json.dumps({
    "lead": {"name": "example Birchell", "phone": "555-047-0647", "submitted_at": "2026-10-20T06:00:00Z"},
    "meeting": {"id": "ccb80fb7d3f1b55871708429", "start": "2026-10-23T20:30:00.000+00:00"}}), encoding="utf-8")
# an old lead (outside 90 days) and a junk file are ignored
(leads / "old.json").write_text(json.dumps({"lead": {"name": "Old Lead", "submitted_at": "2026-05-01T00:00:00Z"}}), encoding="utf-8")
(leads / "junk.json").write_text("not json", encoding="utf-8")

tickets = TMP / "exec_tickets.jsonl"
tickets.write_text("\n".join(json.dumps(t) for t in [
    {"id": "tk-1", "kind": "imessage_send", "status": "done", "payload": {"to": "+1555-092-3292"}, "ts": "", "ts_done": "2026-10-06T16:50:00Z"},
    {"id": "tk-2", "kind": "imessage_send", "status": "done", "payload": {"to": "+1555-047-0647"}, "ts": "", "ts_done": "2026-10-20T06:30:00Z"},
    {"id": "tk-3", "kind": "imessage_send", "status": "queued", "payload": {"to": "+1555-047-0647"}, "ts": "2026-10-20T06:05:00Z", "ts_done": ""},
    {"id": "tk-4", "kind": "imessage_send", "status": "done", "payload": {"to": "+1555-092-3292"}, "ts": "", "ts_done": "2026-09-01T00:00:00Z"},
]) + "\nbroken line\n", encoding="utf-8")

hb = {"stages": [{"stage_id": "s1", "name": "Inquiry", "category": "lead", "order": 0},
                 {"stage_id": "s6", "name": "Proposal Signed", "category": "booked", "order": 6},
                 {"stage_id": "s7", "name": "Retainer Paid", "category": "booked", "order": 7}],
      "projects": [{"name": "example example's Project", "stage_id": "s7"},
                   {"name": "example Birchell's Project", "stage_id": "s6"}],
      "meetings": []}

fn = build.lead_funnel(now=NOW, leads_dir=leads, hb=hb, tickets=tickets)
check("leads in = 2 (old + junk ignored)", fn["n"]["leads"] == 2, fn)
check("first text sent = 2 (queued and pre-lead texts ignored)", fn["n"]["texted"] == 2, fn)
check("meetings booked = 2", fn["n"]["booked"] == 2, fn)
check("meetings held = 1 (the other is still ahead)", fn["n"]["held"] == 1, fn)
check("signed + retainer paid = 1 (Proposal Signed alone does not count)", fn["n"]["signed"] == 1, fn)
check("median lead -> text = 20 min", fn["min_to_text"] == 20, fn)
want = ((datetime(2026, 10, 12, 20, tzinfo=timezone.utc) - datetime(2026, 10, 7, 13, 12, 20, tzinfo=timezone.utc)).total_seconds()
        + (datetime(2026, 10, 23, 20, 30, tzinfo=timezone.utc) - datetime(2026, 10, 20, 15, 22, 2, tzinfo=timezone.utc)).total_seconds()) / 2 / 86400
check("median booked -> meeting (days, from the HoneyBook id time)", abs(fn["days_lead_time"] - want) < 1e-6, fn)

html = build.funnel_html(fn)
check("tile shows the rate", "50% of previous" in html and "100% of previous" in html, html)
check("tile shows the medians", "20 min" in html and f"{want:.1f} d" in html, html)

empty = build.lead_funnel(now=NOW, leads_dir=TMP / "none", hb={}, tickets=TMP / "none.jsonl")
eh = build.funnel_html(empty)
check("no data -> zero counts, dashes for medians and rates", empty["n"]["leads"] == 0 and empty["min_to_text"] is None
      and eh.count("—") == 6, eh)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)} (scratch {TMP})")
sys.exit(1 if FAILS else 0)
