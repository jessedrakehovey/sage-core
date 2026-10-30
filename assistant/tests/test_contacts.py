"""test_contacts.py - iPhone .vcf import -> contacts book -> lookups, wiki note, phone_bridge
names, and the Mission Control upload door. Scratch dirs only.

  python assistant/tests/test_contacts.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="contacts_"))
os.environ["ASSISTANT_DATA"] = str(SCRATCH / "data")
os.environ["ASSISTANT_CARDS"] = str(SCRATCH / "data" / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(SCRATCH / "learnings")
os.environ["WV_CONTACTS_WIKI"] = str(SCRATCH / "Contacts.md")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import contacts  # noqa: E402
import phone_bridge as pb  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


VCF = "\r\n".join([
    "BEGIN:VCARD", "VERSION:3.0", "PRODID:-//Apple Inc.//iPhone OS 18.0//EN",
    "N:Pennywhistle;Finley;;;", "FN:Finley Pennywhistle",
    "item1.TEL;type=pref:(805) 555-0142", "item1.X-ABLabel:_$!<Mobile>!$_",
    "EMAIL;type=INTERNET;type=HOME:Finley@Example.com",
    "BDAY;X-APPLE-OMIT-YEAR=1604:1604-03-23",
    "item2.X-ABRELATEDNAMES:owner6 example", "item2.X-ABLabel:_$!<Daughter>!$_",
    "PHOTO;ENCODING=b;TYPE=JPEG:/9j/4AAQSkZJRgABAQAAAQABAAD",
    " /2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMU",
    "ADR;type=HOME:;;123 Example Street;Brackwater;CA;93030;USA",
    "END:VCARD",
    "BEGIN:VCARD", "VERSION:3.0", "N:;;;;", "ORG:Port Alder Florist;", "TEL;type=WORK:+1 805-555-0199",
    "END:VCARD",
    "BEGIN:VCARD", "VERSION:3.0", "N:Brightwater;Hollis;;;", "FN:Hollis Brightwater", "NICKNAME:Jer",
    "TEL;type=CELL:8055550942", "BDAY:1990-05-10", "END:VCARD", ""])

r = contacts.import_text(VCF, "owner1")
check("import: three people", r.get("ok") and r["count"] == 3, r)
book = contacts.load()["books"]["owner1"]["people"]
finley = next(p for p in book if p["name"] == "Finley Pennywhistle")
check("apple item label resolves after its TEL line", finley["phones"][0]["label"] == "mobile", finley["phones"])
check("no-year birthday", finley["birthday"] == "03-23", finley["birthday"])
check("related name with label", finley["related"] == [{"name": "owner6 example", "label": "daughter"}], finley["related"])
check("photo and street address dropped", "Secret" not in json.dumps(book) and "9j" not in json.dumps(book))
check("company-only card is named by company", any(p["name"] == "Port Alder Florist" for p in book))
check("lookup by any phone format", contacts.name_for("+18055550142") == "Finley Pennywhistle")
check("lookup by email, case-insensitive", contacts.name_for("finley@example.COM") == "Finley Pennywhistle")
check("unknown number -> empty", contacts.name_for("+15555550000") == "")
check("search by nickname", [p["name"] for p in contacts.search("jer")] == ["Hollis Brightwater"])

note = (SCRATCH / "Contacts.md").read_text(encoding="utf-8")
check("wiki note written with frontmatter + rows", note.startswith("---\ntype: reference") and "| Finley Pennywhistle |" in note
      and "(805) 555-0142 mobile" in note and "daughter: owner6 example" in note)
check("wiki note keeps addresses out", "Secret St" not in note)

# owner's book merges into one wiki row per person
contacts.import_text("BEGIN:VCARD\nVERSION:3.0\nFN:Finley Pennywhistle\nTEL:805-555-7777\nEND:VCARD\n", "owner6")
note = (SCRATCH / "Contacts.md").read_text(encoding="utf-8")
row = next(l for l in note.splitlines() if l.startswith("| Finley Pennywhistle |"))
check("both phones merge into one row", note.count("| Finley Pennywhistle |") == 1 and "owner1, owner6" in row.lower() and "555-7777" in row, row)
# re-import replaces, not appends
contacts.import_text("BEGIN:VCARD\nVERSION:3.0\nFN:Finley Pennywhistle\nTEL:805-555-8888\nEND:VCARD\n", "owner6")
check("re-import replaces that owner's book", contacts.name_for("8055557777") == "" and contacts.name_for("8055558888") == "Finley Pennywhistle")
check("garbage refused", not contacts.import_text("hello", "owner1")["ok"])

# phone_bridge fills the name the Shortcut never sends
# (Hollis is a wedding vendor here: a text from anyone else is not ticketed at all, text_gate.py 2026-10-11)
import text_gate  # noqa: E402
text_gate.business_numbers = lambda: {"8055550942"}
pb.inbound("+18055550942", "running late!", account="owner6-iphone")
check("inbound text gets the contact's name", pb.thread("8055550942")["name"] == "Hollis Brightwater")
tk = (SCRATCH / "data" / "tickets.json").read_text(encoding="utf-8")
check("ticket title names the sender", "Text from Hollis Brightwater" in tk)

# Mission Control door
import mc_server  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402
srv = ThreadingHTTPServer(("127.0.0.1", 0), mc_server.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"


def post(path, data, headers):
    req = urllib.request.Request(base + path, data=data.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


code, _ = post("/api/contacts/import?owner=owner1", VCF, {"Content-Type": "text/vcard"})
check("upload without the page's header refused", code == 403, code)
code, _ = post("/api/contacts/import?owner=owner1", VCF, {"X-example-Contacts": "1", "Origin": "https://evil.example"})
check("cross-origin upload refused", code == 403, code)
code, j = post("/api/contacts/import?owner=owner1", VCF, {"X-example-Contacts": "1"})
check("page upload imports", code == 200 and j.get("count") == 3, (code, j))
with urllib.request.urlopen(base + "/contacts") as resp:
    page = resp.read().decode()
check("contacts page renders with the import count", "owner1: 3 contacts" in page.lower() and "Export" in page)
srv.shutdown()

# CRM clients are known too (the owner 2026-10-07: a client's text was 'not a saved contact')
_snap = SCRATCH / "crm.json"
_snap.write_text(json.dumps({"contacts": [{"id": "c1", "name": "Emery Marchetti", "email": "emery@example.com", "phone": "(805) 555-0311"},
                                          {"id": "c2", "name": "", "email": "x@y.z"}, {"_error": "nope"}]}), encoding="utf-8")
os.environ["CRM_SNAPSHOT_FILE"] = str(_snap)   # the CRM plugin (crm_people) reads it per call
import alib  # noqa: E402
import crm as _crm_contacts  # noqa: E402
if any(hasattr(m, "crm_people") for m in alib.plugins()):   # the CRM's own people: only where its plugin is installed
    check("crm: a client's phone resolves to their name", contacts.name_for("+1 805-555-0311") == "Emery Marchetti")
    check("crm: a client's email resolves too", contacts.name_for("Emery@Example.com") == "Emery Marchetti")
    check("crm: nameless or error rows are ignored", contacts.name_for("x@y.z") == "" and len(contacts.crm_people()) == 1)
    check("crm: the match is marked as coming from the CRM", (contacts.lookup("+1 805-555-0311") or {}).get("source") == _crm_contacts.name().lower())

# Organization-free so it ships in sage-core (owner ask 2026-10-28: "Make assistant/scripts/contacts.py
# organization-free so it can ship in sage-core: line 49: identity: name owner."): the owners come from
# people.json only; no people.json is one generic book; core_sync finds no identity or niche line in the file.
_pj = json.loads((contacts.ROOT / "agent" / "people.json").read_text(encoding="utf-8-sig"))
check("owners: read from people.json, in order",
      contacts.OWNERS == tuple(k for k, u in _pj["users"].items() if u.get("role") == "owner"), contacts.OWNERS)
_root = contacts.ROOT
contacts.ROOT = SCRATCH / "no_pack"
check("owners: no people.json -> one generic book", contacts._owner_keys() == ("owner",), contacts._owner_keys())
contacts.ROOT = _root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core_sync  # noqa: E402
_vault = Path(__file__).resolve().parents[2]
_cls = core_sync.pick_engine(_vault, "auto")
_cfg = core_sync.config(_vault, _cls)
_rel = "assistant/scripts/contacts.py"
_flags = core_sync.flags_of(_rel, (_vault / _rel).read_text(encoding="utf-8"), _cls(_vault, _cfg).identity(),
                            core_sync.niche_patterns(_cfg), core_sync.allow_patterns(_cfg))
check("core_sync: contacts.py carries no identity or niche line (it ships)", not _flags, _flags)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
