"""Offline tests for form_ingest.py: a completed intake form in the CRM snapshot lands on the client's page (once) and
in the ledger entry; a page that already has a hand-written form section is left alone; nothing matches -> recorded as
unmatched, no write; the organization's field map comes before the generic defaults.
   python assistant/tests/test_form_ingest.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_fi_"))
VAULT = TMP / "vault"
PAGES = VAULT / "Wiki" / "Clients"
PAGES.mkdir(parents=True)
(VAULT / "scripts" / "projects").mkdir(parents=True)
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["FORM_INGEST_VAULT"] = str(VAULT)
os.environ["CLIENT_PAGES_DIR"] = "Wiki/Clients"
os.environ["CRM_SNAPSHOT_FILE"] = str(TMP / "crm.json")
sys.path.insert(0, str(ROOT / "scripts"))
import form_ingest as fi  # noqa: E402

FAILS = 0


def ok(cond, msg):
    global FAILS
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILS += 1


P1 = "62f49ced42d7e43bd430722e"
P2 = "a2da146e444d6e97441289e1"
PAGE = PAGES / "2027-01-09 Test.md"
PAGE.write_text(f"---\ntype: client\ncreated: 2026-01-01\nupdated: 2026-01-01\ncrm_id: \"{P1}\"\n---\n\n# Test\n\n"
                "## Package\n\n- x\n\n## Story and notes\n\n<!-- sage:notes:begin -->\n### 2026-01-02, added by the agent\n\nold note\n<!-- sage:notes:end -->\n",
                encoding="utf-8")
(VAULT / "scripts" / "projects" / "project_state.json").write_text(json.dumps({"projects": {
    "2027-01-09_Test": {"client": "A & B", "crm_id": P1, "location": "TODO"}}}), encoding="utf-8")
(Path(os.environ["ASSISTANT_DATA"])).mkdir(parents=True, exist_ok=True)
fi.FIELD_MAP.write_text(json.dumps({"legacy_section_words": ["survey"], "client_name_keys": ["party"], "fields": [{"field": "site", "match": r"main (location|site).*address"},
                                               {"field": "lead_contact", "match": r"reached on the day"}]}), encoding="utf-8")
SNAP = {"forms": [
    {"id": "1dd18ca69d5cbbf78ef84226", "name": "Intake form", "workspace_id": "5a7aca84d1f916d389dc4b04", "project_id": P1,
     "published": True, "status": "completed", "sent_at": "2026-10-25T01:00:22Z",
     "answers": [{"q": "Main location name + address", "a": "Riverside Barn - 123 Example Street", "answered": True, "at": "2026-10-29T16:21:43Z"},
                 {"q": "How many guests are you expecting?", "a": "54", "answered": True, "at": "2026-10-29T16:23:48Z"},
                 {"q": "Event coordinator + website + phone number", "a": "D. G. - 805.555.0819", "answered": True, "at": None},
                 {"q": "Name + phone number other than the hosts that can be reached on the day", "a": "K. B. - 616.555.7071", "answered": True, "at": None},
                 {"q": "Do you have a hashtag?", "a": "", "answered": False, "at": None}]},
    {"id": "fb1ac596e56798aa1e8d1abf", "name": "Intake form", "workspace_id": "dfa13d542b350b6972e71703", "project_id": P2,
     "published": True, "status": "completed", "sent_at": "2026-04-20T16:05:18Z",
     "answers": [{"q": "Location", "a": "Somewhere", "answered": True, "at": None}]},
    {"id": "973e47cb04ff9d357ccf5079", "name": "Intake form", "workspace_id": "2754992c38ac3d54430e732b", "project_id": "e61b2086b27c3a396d127370",
     "published": True, "status": "viewed", "sent_at": "2025-10-14T17:25:27Z"},
]}
Path(os.environ["CRM_SNAPSHOT_FILE"]).write_text(json.dumps(SNAP), encoding="utf-8")

# 1. a dry run writes nothing
r = fi.run(dry=True, commit=False)
ok(r.get("dry") and "<!-- form:" not in PAGE.read_text(encoding="utf-8"), "dry run leaves the page alone")

# 2. the real run files the answers once, on the page and in the ledger
r = fi.run(dry=False, commit=False)
text = PAGE.read_text(encoding="utf-8")
ok("1dd18ca69d5cbbf78ef84226" in r["filed"], "the completed form is filed")
ok("<!-- form:1dd18ca69d5cbbf78ef84226:begin -->" in text and "### Intake form (completed 2026-10-29" in text,
   "the page carries the form block with its name and completion date")
ok("- **Main location name + address:** Riverside Barn - 123 Example Street" in text and "hashtag" not in text,
   "answered questions are listed; a blank answer is not")
ok(text.index("<!-- sage:notes:begin -->") < text.index("<!-- form:") < text.index("### 2026-01-02"),
   "the block sits at the top of the agent's notes, under Story and notes")
ok("updated: " + time.strftime("%Y-%m-%d") in text, "the front matter's updated date moves")
ps = json.loads((VAULT / "scripts" / "projects" / "project_state.json").read_text(encoding="utf-8"))["projects"]["2027-01-09_Test"]
ok(ps.get("site") == "Riverside Barn - 123 Example Street" and ps.get("location") == "Riverside Barn - 123 Example Street" and ps.get("headcount") == "54"
   and ps.get("coordinator", "").startswith("D. G.") and ps.get("lead_contact", "").startswith("K. B.") and ps.get("day_of_contact", "").startswith("K. B."),
   "the ledger entry gets the organization's fields (site, lead_contact) and the generic ones (location, headcount, coordinator, day_of_contact); a TODO is replaced" + " " + repr(ps))
ok(ps.get("form", {}).get("id") == "1dd18ca69d5cbbf78ef84226" and ps["form"].get("completed") == "2026-10-29",
   "the ledger entry records which form and when")
st = json.loads(fi.STATE.read_text(encoding="utf-8"))
ok("1dd18ca69d5cbbf78ef84226" in st["ingested"] and "fb1ac596e56798aa1e8d1abf" in st["unmatched"] and "973e47cb04ff9d357ccf5079" not in st["ingested"],
   "state: filed one, the client with no page is unmatched, a viewed (not completed) file is untouched")

# 3. idempotent: a second run changes nothing
before = PAGE.read_text(encoding="utf-8")
r2 = fi.run(dry=False, commit=False)
ok(r2["filed"] == [] and PAGE.read_text(encoding="utf-8") == before, "a second run files nothing and leaves the page as it was")

# 4. a page that already carries a hand-written form section is left alone, but counted as filed
PAGE2 = PAGES / "2027-02-02 Hand.md"
PAGE2.write_text(f"---\ntype: client\nupdated: 2026-01-01\ncrm_id: \"{P2}\"\n---\n\n# Hand\n\n## Story and notes\n\n"
                 "### Old survey (completed by the client, 2026-10-29)\n\n- **Location:** written by hand\n", encoding="utf-8")
fi.STATE.write_text(json.dumps({"ingested": {}, "unmatched": {}}), encoding="utf-8")
fi.run(dry=False, commit=False)
t2 = PAGE2.read_text(encoding="utf-8")
st = json.loads(fi.STATE.read_text(encoding="utf-8"))
ok("<!-- form:" not in t2 and "fb1ac596e56798aa1e8d1abf" in st["ingested"], "a hand-written section under the organization's old name for the form is not duplicated")

# 5. the generic field map reads plain wording; a pages folder comes from the setting
f = fi.fields([{"q": "Where is the event? Location name and address", "a": "Barn"}, {"q": "What time does it start?", "a": "3:30pm"},
               {"q": "Who can be reached on the day of the event?", "a": "K 616"}], fmap=[(k, __import__("re").compile(v, 2)) for k, v in fi.DEFAULT_FIELDS])
ok(f == {"location": "Barn", "start_time": "3:30pm", "day_of_contact": "K 616"}, "defaults: location, start time, day-of contact" + " " + repr(f))
ok(fi.pages_dir() == VAULT / "Wiki" / "Clients", "the client pages folder is the configured one")
ok("crm_id" in fi.id_keys() and "workspace_id" in fi.id_keys(), "ids are matched on the contract's names (plus the connector's own)")

print(f"\n{'ALL OK' if not FAILS else str(FAILS) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
