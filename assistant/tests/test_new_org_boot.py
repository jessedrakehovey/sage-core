"""WP5 (2026-10-24): one command stands up another organization, and it boots.
new_org.py scaffolds "Acme Bakery" into a temp folder; the new tree's own scripts run (the spine lists its jobs,
the ledger and the cards answer, the follow-ups tick against a copied SOP map), the code reads Acme's identities
from its org.json, and nothing of this organization is left in the new tree's data, prompts, tasks or wrappers.
   python assistant/tests/test_new_org_boot.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VAULT = ROOT.parent
sys.path.insert(0, str(ROOT))
import engine as _engine  # noqa: E402
TEMPLATE = _engine.is_template(VAULT)                        # the exported engine: the source organization is the placeholder
TMP = Path(tempfile.mkdtemp(prefix="wv_neworg_"))
OUT = TMP / "acme-os"
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(args, cwd=None, env_extra=None, timeout=120):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ASSISTANT_", "AGENT_", "DOER_"))}
    env["WV_LOG_DIR"] = str(TMP / "logs")
    env["CHAT_PUSH"] = "off"
    env.update(env_extra or {})
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=str(cwd or OUT), env=env, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# 1. scaffold
rc, out = run([str(ROOT / "new_org.py"), "Acme Bakery", "--agent", "Pip", "--owner", "Ana Ruiz:ana@acme.test", "--owner", "example Ruiz:example@acme.test",
               "--out", str(OUT), "--what", "a bakery with wholesale accounts"], cwd=VAULT, timeout=300)
check("new_org.py scaffolds Acme", rc == 0 and (OUT / "assistant" / "agent" / "profile.json").exists(), out[-600:])
org = json.loads((OUT / "assistant" / "org.json").read_text(encoding="utf-8"))
check("org.json carries Acme's identities, phones and task names",
      org["send_identities"] == ["ana@acme.test", "example@acme.test"] and "ana-iphone" in org["phone_accounts"] and org["default_phone_account"] == "ana-iphone"
      and org["watched_tasks"][0] == "Acme Bakery Day Spine (assistant)", org)
for name in ("sop_map.json", "comms_law.json", "client_texts.json", "rulings.json"):
    check(f"data/{name} travels", (OUT / "assistant" / "data" / name).exists())
check("spine_jobs.json travels", (OUT / "assistant" / "spine_jobs.json").exists())
check("the agent's skills, learnings and tests do not", not (OUT / "assistant" / "agent" / "skills").exists() and not (OUT / "assistant" / "tests").exists())
xml = (OUT / "assistant" / "tasks" / "DaySpine.xml").read_text(encoding="utf-8")
check("task XMLs point at the new root with the new org's task names", str(OUT) in xml and "Acme Bakery Day Spine" in xml and (TEMPLATE or "example-org" not in xml))
ps = (OUT / "assistant" / "scripts" / "spine.ps1").read_text(encoding="utf-8")
check("the PowerShell wrappers point at the new root", str(OUT) in ps and (TEMPLATE or "example-org" not in ps))

# 2. the code reads Acme's values
rc, out = run(["-c", "import sys; sys.path.insert(0, 'assistant/scripts'); import exec_store, phone_bridge, chat_push, sentinels; "
                     "print(exec_store.ALLOWED_FROM, exec_store.ALLOWED_PHONE_ACCOUNTS, phone_bridge.DEFAULT_ACCOUNT, chat_push.MC_URL, sentinels.WATCHED_TASKS[0])"])
check("exec_store / phone_bridge / chat_push / sentinels resolve Acme's org.json", rc == 0 and "ana@acme.test" in out and "ana-iphone" in out
      and (TEMPLATE or "example" not in out) and "Acme Bakery Day Spine" in out, out[-400:])

# 3. the new tree boots
rc, out = run(["assistant/scripts/spine.py", "--list"])
check("the Day Spine lists its jobs in the new tree", rc == 0 and "cards-sweep" in out and "outcomes-settle" in out, out[-300:])
rc, out = run(["assistant/scripts/outcomes.py", "open"])
check("the outcome ledger answers", rc == 0 and "Nothing waiting" in out, out[-200:])
rc, out = run(["assistant/scripts/cards.py", "pending", "--json"])
check("the cards store answers", rc == 0 and out.strip().endswith("]"), out[-200:])
rc, out = run(["assistant/scripts/followups.py", "tick"])
check("the follow-ups tick against the copied SOP map (no FATAL)", rc == 0 and "FATAL" not in out, out[-300:])
rc, out = run(["assistant/scripts/deadman.py", "--status"])
check("the deadman reads the new tree", rc == 0 and '"spine"' in out, out[-200:])
rc, out = run(["assistant/scripts/sentinels.py", "--dry-run"])
check("the sentinels run dry in the new tree", rc == 0, out[-300:])

# 4. nothing of this organization in the new tree's data, prompts, tasks and wrappers
words = re.compile(r"example|owner1|\bowner6\b|tailnet|example-org", re.I)     # identifiers like the owner_request are code, not the organization
infra = re.compile(r"example|tailnet|example-org", re.I)
hits = []
for f in (OUT / "assistant").rglob("*"):
    if f.suffix.lower() in (".json", ".md", ".ps1", ".xml") and f.is_file():
        rx = infra if f.suffix.lower() in (".ps1", ".xml") else words        # wrappers carry the PC's own user path
        for i, ln in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if rx.search(ln.replace(str(OUT), "")):
                hits.append(f"{f.relative_to(OUT)}:{i}: {ln.strip()[:80]}")
check("no owner, organization, host or folder name of this organization in the new tree's data / prompts / tasks / wrappers", TEMPLATE or not hits, "\n".join(hits[:12]))

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f"({OUT})")
sys.exit(1 if FAILS else 0)
