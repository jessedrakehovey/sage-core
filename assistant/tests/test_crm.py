"""test_crm.py - the CRM contract (assistant/CRM_CONTRACT.md): the engine ships no CRM, reads any through crm.py,
keeps an organization's connector entries out of an export, and onboarding files one build when a CRM is named.
A fake connector only; no real service, person or organization.

  python assistant/tests/test_crm.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import crm  # noqa: E402
import engine  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {str(extra)[:300]}"))
    if not cond:
        FAILS.append(name)


TMP = Path(tempfile.mkdtemp(prefix="crm_test_"))
os.environ["CRM_SNAPSHOT_FILE"] = str(TMP / "none.json")

# ---------------------------------------------------------------- 1. no connector: quiet
crm._plugins = lambda: []
crm._ALIASES = None
check("no connector: the CRM is called 'the CRM'", crm.name() == "the CRM")
check("no connector, no snapshot: not connected, empty snapshot, nothing to check",
      not crm.connected() and crm.snapshot() == {} and crm.check() == [])
check("no connector: no message, no stage move, any non-empty thread id is fine",
      crm.message_exec("t1", "hi") is None and crm.stage_exec("t1", "Booked") is None
      and crm.apply_stage("t1", "Booked")[0] is False and crm.valid_thread("t1") and not crm.valid_thread(""))
check("no connector: the generic preview tag", crm.preview_tag() == crm.GENERIC_PREVIEW)
check("contract field names read straight", crm.status({"crm_status": "client"}) == "client" and crm.project_id({"crm_id": "p9"}) == "p9")

# ---------------------------------------------------------------- 2. a connector
snap = {"ts": "2026-10-29T12:00:00Z", "stages": [{"stage_id": "s1", "name": "Inquiry", "category": "lead", "order": 0}],
        "projects": [{"id": "p1", "name": "Pat Example", "stage_id": "s1", "status": "lead", "conv": "c1"}],
        "contacts": [], "old_forms": [{"id": "f1", "name": "Intake"}]}
(TMP / "acme.json").write_text(json.dumps(snap), encoding="utf-8")
fake = types.SimpleNamespace(
    CRM=True, SERVICE="AcmeCRM", SNAPSHOT_PATH=str(TMP / "acme.json"), PREVIEW_TAG="[ACME PREVIEW]",
    FIELD_ALIASES={"crm_status": ["acme_status"], "crm_id": ["acme_id"], "thread_id": ["conv"], "forms": ["old_forms"]},
    valid_thread=lambda s: s.startswith("c"),
    message_exec=lambda t, b, s="", r="": {"kind": "acme_message", "payload": {"conv": t, "text": b}},
    stage_exec=lambda t, st: {"kind": "acme_stage", "payload": {"conv": t, "stage": st}},
    apply_stage=lambda t, st, dry=False: (True, "moved"),
    booked_meeting=lambda t, who="": {"when": "Mon 10:00"} if t == "c1" else None,
    MESSAGE_KINDS={"acme_message"})
crm._plugins = lambda: [fake]
crm._ALIASES = None
del os.environ["CRM_SNAPSHOT_FILE"]
check("connector: its name, its snapshot path, connected", crm.name() == "AcmeCRM" and crm.connected() and crm.snapshot_path() == TMP / "acme.json")
check("connector: the snapshot follows the contract", crm.check() == [], crm.check())
check("connector: older field names read through its aliases",
      crm.status({"acme_status": "client"}) == "client" and crm.project_id({"acme_id": "x"}) == "x"
      and crm.thread_id(snap["projects"][0]) == "c1" and crm.forms() == snap["old_forms"])
check("connector: messages, stage moves and booked meetings are its own",
      crm.message_exec("c1", "hello")["kind"] == "acme_message" and crm.stage_exec("c1", "Booked")["payload"]["stage"] == "Booked"
      and crm.apply_stage("c1", "Booked") == (True, "moved") and crm.booked_meeting("c1")["when"] == "Mon 10:00"
      and crm.valid_thread("c1") and not crm.valid_thread("x1") and crm.preview_tag() == "[ACME PREVIEW]")
check("check: a project on an unknown stage is a problem",
      any("unknown stage" in p for p in crm.check(dict(snap, projects=[{"id": "p2", "stage_id": "zz"}]))))

# ---------------------------------------------------------------- 3. an export keeps the organization's connector home
engine.CONNECTOR_TERMS = ["acme", "ac_"]
cfg = {"_doc": "the AcmeCRM sync runs every 2 h", "jobs": [
    {"name": "ac-snapshot", "cmd": ["python", "assistant/scripts/ac_snapshot.py"]},
    {"name": "mail", "cmd": ["python", "assistant/scripts/mail.py"], "why": "reads AcmeCRM mail too"}],
    "gates": [{"id": "reply", "kinds": ["email_send", "ac_message"]}], "ac_flag": True}
out = json.loads(engine.strip_connector_json("assistant/spine_jobs.json", json.dumps(cfg)))
check("export: the connector's job and key leave, a shared job and a gate stay (only its connector bits go)",
      [j["name"] for j in out["jobs"]] == ["mail"] and "why" not in out["jobs"][0] and "ac_flag" not in out
      and out["gates"] == [{"id": "reply", "kinds": ["email_send"]}] and "_doc" not in out, out)
check("export: catalog reference may name any service", engine.strip_connector_json("assistant/catalog/acme.json", '{"name": "AcmeCRM"}') == '{"name": "AcmeCRM"}')
check("export: a file without connector terms is untouched", engine.strip_connector_json("assistant/x.json", '{"a": 1}') == '{"a": 1}')
engine.CONNECTOR_WORDS = [(__import__("re").compile(r"(?<![A-Za-z0-9])AcmeCRM(?![A-Za-z0-9])"), "the CRM")]
check("export: prose names the CRM generically", engine.neutral_connector_words("assistant/README.md", "Check AcmeCRM first.") == "Check the CRM first."
      and engine.neutral_connector_words("assistant/catalog/a.json", "AcmeCRM") == "AcmeCRM")

# ---------------------------------------------------------------- 4. onboarding: naming a CRM files one build
import onboarding  # noqa: E402
written = []
fake_write = lambda d, by="": (written.append(d), {"status": "applied", "path": "Wiki/x.md"})[1]  # noqa: E731
onboarding._crm_connected = lambda: False
s = {"discovery": {"crm": {"tool": "Zeta Pipeline", "catalog_id": "", "status": "uncatalogued", "how": ""}}}
p1 = onboarding.crm_build(s, write=fake_write)
p2 = onboarding.crm_build(s, write=fake_write)
check("onboarding: a named CRM with no connector files ONE build request against the contract",
      p1 == "Wiki/x.md" and p2 == "" and len(written) == 1 and "CRM_CONTRACT.md" in written[0]["body"]
      and "crm_zetapipeline.py" in written[0]["body"] and written[0]["title"].startswith("Build request"), written)
check("onboarding: skipped, or a connector already installed: no build",
      onboarding.crm_build({"discovery": {"crm": {"tool": "", "status": "skipped"}}}, write=fake_write) == ""
      and (setattr(onboarding, "_crm_connected", lambda: True) or onboarding.crm_build(
          {"discovery": {"crm": {"tool": "Other"}}}, write=fake_write) == "") and len(written) == 1)

print(f"\n{'ALL OK' if not FAILS else str(len(FAILS)) + ' failure(s)'}")
sys.exit(1 if FAILS else 0)
