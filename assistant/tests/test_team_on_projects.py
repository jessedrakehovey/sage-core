"""team_on_projects.py: the owner's standing rule (2026-10-24) puts a team member on every open HoneyBook project.
Offline: a fake snapshot + a scratch data dir; the tickets it mints are real exec_store rows in the scratch."""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_top_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
SNAP = TMP / "honeybook.json"
os.environ["HB_SNAPSHOT"] = str(SNAP)
sys.path.insert(0, str(HERE.parent / "scripts"))
import exec_store as es  # noqa: E402
import team_on_projects as top  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# anchored to the real clock: the exec ledger stamps a failed ticket with the wall clock, and the "not re-minted
# today / tried again tomorrow" checks compare that stamp to NOW (a fixed 2026-10-24 went red on 2026-10-25)
NOW = datetime.now().replace(hour=14, minute=0, second=0, microsecond=0)
D = lambda days: (NOW + timedelta(days=days)).strftime("%Y-%m-%d")  # noqa: E731
T = lambda days: (NOW + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
staff1 = "ce9827d493d83824f4ef5036"
W = lambda n: f"{n:024x}"  # noqa: E731
SNAP.write_text(json.dumps({"projects": [
    {"id": W(1), "project_id": W(101), "name": "example", "date": D(14), "status": "client", "last_activity_at": T(-4)},
    {"id": W(2), "project_id": W(102), "name": "Old booked 2025", "date": D(-510), "status": "client", "last_activity_at": T(-480)},
    {"id": W(3), "project_id": W(103), "name": "Fresh lead", "date": None, "status": "lead", "last_activity_at": T(-1), "created_at": T(-1)},
    {"id": W(4), "project_id": W(104), "name": "Archived", "date": D(69), "status": "archived"},
    {"id": W(5), "project_id": W(105), "name": "Quiet lead", "date": None, "status": "lead_sent", "last_activity_at": T(-145)},
    {"id": W(6), "project_id": W(106), "name": "Next year", "date": D(193), "status": "lead_sent", "last_activity_at": T(-296)},
]}), encoding="utf-8")

top.cfg = lambda: {"enabled": True, "members": [{"name": "staff1 example", "user_id": staff1}], "statuses": ["lead", "lead_sent", "client"],
                   "backfill_activity_days": 60, "per_run": 2, "approval_ref": "standing:team-on-projects (test)"}

open_ = top.open_projects(json.loads(SNAP.read_text(encoding="utf-8")), ("lead", "lead_sent", "client"), 60, NOW)
check("open projects = future date or recent activity, never archived/stale", sorted(p["name"] for p in open_) == ["example", "Fresh lead", "Next year"], [p["name"] for p in open_])

r = top.run(dry=True, now=NOW)
check("dry run mints nothing but counts", r["minted"] == 2 and not list(es.fold()), r)

r = top.run(now=NOW)
rows = [t for t in es.fold().values() if t.get("kind") == "hb_add_team_member"]
check("per_run caps a pass at 2 tickets", r["minted"] == 2 and len(rows) == 2, (r, len(rows)))
check("tickets carry the standing approval ref and staff1's user id", all(t["approved_by"] == "owner1" and t["approval_ref"].startswith("standing:team-on-projects") and t["payload"]["user_id"] == staff1 for t in rows))
r = top.run(now=NOW)
rows = [t for t in es.fold().values() if t.get("kind") == "hb_add_team_member"]
check("the next pass mints only the third and skips the queued pairs", r["minted"] == 1 and r["skipped"] == 2 and len(rows) == 3, r)
r = top.run(now=NOW)
check("nothing left: no new tickets", r["minted"] == 0 and len([t for t in es.fold().values() if t.get("kind") == "hb_add_team_member"]) == 3, r)

# a failed ticket is retried the next day, not the same day
tid = rows[0]["id"]
es.set_status(tid, "exception", "PERMANENT: boom")
r = top.run(now=NOW)
check("a failure today is not re-minted today", r["minted"] == 0 and r["failed_waiting"] == 1, r)
from datetime import timedelta
r = top.run(now=NOW + timedelta(days=1))
check("the next day it is tried once more", r["minted"] == 1, r)
st = json.loads(top.STATE.read_text(encoding="utf-8"))
check("the pair ledger records tries", any(v.get("tries") == 2 for v in st["pairs"].values()), st)

# switch off
top.cfg = lambda: {"enabled": False}
check("enabled:false mints nothing", top.run(now=NOW)["minted"] == 0)

# the hb_exec template for the kind
sys.path.insert(0, str(HERE.parent / "scripts"))
import hb_exec  # noqa: E402
code, intent = hb_exec.build_code("hb_add_team_member", {"workspace_id": W(1), "user_id": staff1, "name": "staff1 example"})
check("template adds by user id, idempotent, reads back, no contact, no delete",
      "addWorkspaceMember" in code and "listWorkspaceMembers" in code and "createContact" not in code and "delete" not in code.lower() and "MEMBER=" in code)
try:
    hb_exec.build_code("hb_add_team_member", {"workspace_id": W(1), "user_id": "staff1"})
    check("a bad user id is refused", False)
except ValueError:
    check("a bad user id is refused", True)
check("validate_payload needs a 24-hex user id", "user_id" in es.validate_payload("hb_add_team_member", {"workspace_id": W(1), "user_id": "x"}))

print("\nALL PASS" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
