"""Offline tests for assistant/agent/truth.py (the agent's binding of provenance.py, Seed B): the brief's facts carry
as_of + source, a stale fact / a sentinel hit / two disagreeing sources read as UNKNOWN in the FACTS AS OF block
(never as the stale value), UTC stamps are read as local time, a store that cannot be read is UNKNOWN (never a silent
gap), every sentinel in the pack names its logged mistake, the provenance packets' fixtures come out UNKNOWN /
CONFLICT through the same block, and the brain's context carries it. No network, no model, scratch files only.
   python assistant/tests/test_truth.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VAULT = ROOT.parent
PACK = ROOT / "data"
OWN_PACK = (PACK / "sentinels.json").exists() and (PACK / "provenance_rules.json").exists()
if not OWN_PACK:
    # a template checkout carries no organization's pack: bind a scratch one of the same shape
    _scratch = Path(tempfile.mkdtemp(prefix="truth_pack_"))
    (_scratch / "provenance_rules.json").write_text(json.dumps({"max_age_hours": {
        "calendar": 12, "mail": 12, "crm_snapshot": 26, "money_ledger": 48, "task_store": 6, "exec_ledger": None, "memory": None}}), encoding="utf-8")
    (_scratch / "sentinels.json").write_text(json.dumps({"sentinels": [
        {"name": "account_not_connected", "when": {"kind": ["calendar", "mail"], "field": ["status"], "value_in": ["NOT_CONFIGURED", None, ""]}, "why": "w"},
        {"name": "one_mailbox_absent", "when": {"kind": ["mail"], "field": ["found", "exists"], "value_in": [False, 0, None], "context": {"all_accounts": False}}, "why": "w"},
        {"name": "send_absent_from_exec_ledger", "when": {"kind": ["exec_ledger"], "field": ["sent"], "value_in": [False, 0, None]}, "why": "w"},
        {"name": "crm_meeting_absent", "when": {"kind": ["crm_snapshot"], "field": ["meeting_booked", "meetings"], "value_in": [False, 0, None]}, "why": "w"}]}),
        encoding="utf-8")
    os.environ["ASSISTANT_DATA"] = str(_scratch)
sys.path.insert(0, str(ROOT / "agent"))
import truth  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


NOW = datetime(2026, 11, 1, 9, 0, 0)

# ---- stale: a value older than its kind's limit is never asserted
[f] = truth.from_rows([{"fact": "client A location", "value": "Old Mill Barn", "as_of": "now-60h", "source": "CRM snapshot",
                        "kind": "crm_snapshot"}], NOW)
ok(f["unknown"] and f["stale"] and truth.provenance.known(f) is None, "a 60 h CRM snapshot (limit 26 h) is stale -> UNKNOWN")
block = truth.render([f], NOW)
line = [ln for ln in block.splitlines() if ln.startswith("- client A location")][0]
ok("UNKNOWN" in line and "last known 'Old Mill Barn'" in line, "the brief line says UNKNOWN and keeps the value only as last known")
ok("location 'Old Mill Barn' (" not in block, "the stale value is never written in the asserted form")
[g] = truth.from_rows([{"fact": "client A location", "value": "Old Mill Barn", "as_of": "now-2h", "source": "CRM snapshot",
                        "kind": "crm_snapshot"}], NOW)
ok(not g["unknown"] and "client A location 'Old Mill Barn' (CRM snapshot 07:00)" in truth.render([g], NOW),
   "a fresh fact is stated with its source and time")

# ---- every fact carries as_of + source
for x in (f, g):
    ok(x["as_of"] and x["source"], f"{x['fact']} carries as_of + source")

# ---- UTC stamps are local time (the engine reads a stamp as local)
utc_30h = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S.123Z")
z = truth.fact("CRM projects", 78, utc_30h, "CRM snapshot", "crm_snapshot")
# naive local wall time: across a DST change the age is off by the shift (an hour), never by the UTC offset
ok(z["stale"] and 28.5 < z["age_h"] < 31.5,f"a Z stamp 30 h ago reads 30 h old (got {z['age_h']}), stale over 26 h")
ok(truth.local_stamp("2026-11-01") == "2026-11-01" and truth.local_stamp("now-3h") == "now-3h", "plain and relative stamps pass through")

# ---- sentinels from the pack, each a logged mistake
nc = truth.fact("mailbox owner2", "NOT_CONFIGURED", "now", "mail check", "mail", field="status")
ok(nc["unknown"] and nc["sentinel"] == "account_not_connected", "a mailbox that is not connected is UNKNOWN, never empty")
ok(not truth.fact("mailbox owner1", "ok matched=8 new=0", "now", "mail check", "mail", field="status")["unknown"], "a connected mailbox reads")
one = truth.fact("planner timeline in mail", False, "now", "mail search (one account)", "mail", field="found", context={"all_accounts": False})
both = truth.fact("planner timeline in mail", False, "now", "mail search (every account)", "mail", field="found", context={"all_accounts": True})
ok(one["unknown"] and one["sentinel"] == "one_mailbox_absent" and not both["unknown"], "not found in ONE mailbox is UNKNOWN; in every mailbox it reads")
sent = truth.fact("film link email sent", False, "now", "exec ledger", "exec_ledger", field="sent")
ok(sent["unknown"] and sent["sentinel"] == "send_absent_from_exec_ledger", "no executor record of a send is UNKNOWN, never 'not sent'")
mt = truth.fact("client has a meeting", False, "now", "CRM snapshot", "crm_snapshot", field="meeting_booked")
ok(mt["unknown"] and mt["sentinel"] == "crm_meeting_absent", "no meeting in the CRM snapshot is UNKNOWN before a call is offered")
ok("UNKNOWN: sentinel account_not_connected" in truth.render([nc], NOW), "a sentinel hit is said in plain words in the block")

mistakes_md = VAULT / "Schema" / "mistakes.md"
if OWN_PACK and mistakes_md.exists():
    sents = json.loads((PACK / "sentinels.json").read_text(encoding="utf-8"))["sentinels"]
    mistakes = mistakes_md.read_text(encoding="utf-8")
    for s in sents:
        date = str(s.get("mistake", ""))[:10]
        ok(s.get("why") and date and f"**Date:** {date}" in mistakes, f"sentinel {s['name']} has a why and names a logged mistake ({date})")
    rules = json.loads((PACK / "provenance_rules.json").read_text(encoding="utf-8"))["max_age_hours"]
    ok(all(k in rules for k in ("calendar", "mail", "crm_snapshot", "money_ledger", "task_store")) and rules["memory"] is None,
       "the pack's rules cover the calendar, mail, CRM, money and task stores; memory is durable")
else:
    print("skip: no organization pack here (template checkout); the pack checks run in the organization's folder")

# ---- conflict: two fresh sources that disagree are kept side by side
[c] = truth.from_rows([
    {"fact": "client A event date", "value": "2027-06-12", "as_of": "now-2h", "source": "CRM snapshot", "kind": "crm_snapshot"},
    {"fact": "client A event date", "value": "2027-06-19", "as_of": "now-1h", "source": "mail check", "kind": "mail"}], NOW)
cl = truth.render([c], NOW)
ok(c["conflict"] and c["value"] is None and len(c["candidates"]) == 2, "two fresh sources that disagree merge into one CONFLICT, no value")
ok("UNKNOWN: conflict:" in cl and "'2027-06-12'" in cl and "'2027-06-19'" in cl and "CRM snapshot" in cl and "mail check" in cl,
   "the block names both values and both sources; it never picks one")
[a] = truth.from_rows([
    {"fact": "date", "value": "2027-06-12", "as_of": "now-2h", "source": "CRM snapshot", "kind": "crm_snapshot"},
    {"fact": "date", "value": "2027-06-12", "as_of": "now-1h", "source": "mail check", "kind": "mail"}], NOW)
ok(not a["unknown"] and a["value"] == "2027-06-12", "two sources that agree read as known")

# ---- the stores: a missing store is UNKNOWN, an `each` dict is one fact per entry
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "status.json"
    p.write_text(json.dumps({"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "accounts": {"owner1": "ok matched=3 new=0", "owner2": "NOT_CONFIGURED"}}), encoding="utf-8")
    spec = {"sources": [{"fact": "mailbox", "path": str(p), "as_of": "ts", "each": "accounts", "kind": "mail", "source": "mail check"},
                        {"fact": "calendar events", "path": str(Path(td) / "gone.json"), "as_of": "ts", "value": "events",
                         "count": True, "kind": "calendar", "source": "calendar feeds"}]}
    fs = {x["fact"]: x for x in truth.from_rows(truth.store_rows(spec))}
    ok(not fs["mailbox owner1"]["unknown"] and fs["mailbox owner2"]["unknown"], "each account is its own fact; the unconnected one is UNKNOWN")
    ok(fs["calendar events"]["unknown"] and fs["calendar events"]["value"] is None, "a store that cannot be read is UNKNOWN, never a silent gap")

# ---- the provenance packets: their fixtures come out UNKNOWN / CONFLICT through the same block
packets = sorted((VAULT / "scripts" / "evals" / "packets").glob("provenance-*.json"))
ok(len(packets) >= 2 or not OWN_PACK, f"the provenance packets exist ({len(packets)})")
for pk in packets:
    d = json.loads(pk.read_text(encoding="utf-8"))
    facts = truth.from_rows((d.get("fixtures") or {}).get("brief_facts") or [], NOW)
    blk = truth.render(facts, NOW)
    ok(facts and all(x["unknown"] for x in facts) and "UNKNOWN" in blk, f"packet {pk.stem}: every fixture fact reads UNKNOWN in the brief")
    ok(d.get("message") and d.get("tested_rule") == "provenance", f"packet {pk.stem}: well-formed for --check and names its rule")

# ---- the brain's context carries the block; the binding names no organization
import brain  # noqa: E402
ok("FACTS AS OF" in brain.context_block() or not truth.brief_facts(), "the brain's context carries FACTS AS OF (when the pack names stores)")
src = (ROOT / "agent" / "truth.py").read_text(encoding="utf-8").lower()
import core  # noqa: E402
names = [str(u.get("name") or "").lower() for u in (core.people().get("users") or {}).values()] + [str(json.loads((ROOT / "org.json").read_text(encoding="utf-8-sig")).get("name") or "").lower() if (ROOT / "org.json").exists() else ""]
ok(not any(n and n in src for n in names), "truth.py names no owner and not the organization")

print(f"ALL {N} OK")
