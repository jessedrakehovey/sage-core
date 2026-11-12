"""Offline tests for the bank login purge (doer/vault_purge.py, 2026-10-30): the owner's "Promptly purge all bank related
log-in info you have unless it is through something like plaid" is one registered job (jobs.json vault_purge_banks)
whose tap deletes every bank entry (Recycle Bin too), keeps Plaid / Venmo / PayPal, reports plain copies by path
only and ends on one chat line. Scratch vault with a test key; no keyring, no model, no Chat.
   python assistant/tests/test_vault_purge.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_vault_purge_"))
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["DOER_VAULT_KEY"] = "test-key"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "doer"))
import jobs  # noqa: E402
import vault  # noqa: E402
import vault_purge as vp  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


# the registered job: a fixed command, no values, the terms in config
j = jobs.find("purge bank logins")
ok(j and j["key"] == vp.JOB and jobs.build_cmd(j, {}) == ["python", "assistant/doer/vault_purge.py"], "the purge is a registered job found by its words")
match, keep = vp.terms()
ok("chase" in match and "wells fargo" in match and "plaid" in keep and "venmo" in keep and "paypal" in keep, "match and keep come from jobs.json")
ok(vp.purge().startswith("Nothing removed: there is no login vault"), "no vault: nothing removed, said plainly")

kp = vault._open(create=True)
g = vault._group(kp)
kp.add_entry(g, "Chase", "studio1", "chase-secret-1", url="https://secure.chase.com/web/auth")
kp.add_entry(g, "Chase Jo", "JoCeleste123", "chase-secret-2", url="https://chase.com")
kp.add_entry(g, "Wells Fargo", "", "wf-secret-3", url="https://connect.secure.wellsfargo.com")
kp.add_entry(g, "Venmo", "owner", "venmo-secret", url="https://venmo.com")
kp.add_entry(g, "PayPal", "owner", "paypal-secret", url="https://www.paypal.com")
kp.add_entry(g, "Plaid bank link", "owner", "plaid-secret", url="https://plaid.com")
kp.add_entry(g, "Google", "owner", "g-secret", url="https://accounts.google.com")
kp.add_entry(g, "Citywide Rentals", "rent", "city-secret", url="https://citywide.example")
old = kp.add_entry(g, "Capital One", "oldcap", "cap-secret-4", url="https://capitalone.com")
kp.trash_entry(old)
kp.save()

dry = vp.purge(dry=True)
ok(all(t in dry for t in ("Chase", "Chase Jo", "Wells Fargo", "Capital One")) and "Venmo" not in dry and "Plaid" not in dry
   and "Citywide" not in dry and "secret" not in dry, f"dry run lists the bank titles only ({dry})")
ok(len(vault._open().entries) == 9, "a dry run changes nothing")

files = TMP / "assistant"
(files / "doer").mkdir(parents=True)
(files / "doer" / "tools.json").write_text('{"chase": {"user": "studio1"}}', encoding="utf-8")
(files / "learnings.md").write_text("venmo login is owner", encoding="utf-8")
line = vp.purge(root=files)
print("   ", line)
kp = vault._open()
left = sorted(e.title for e in kp.entries)
ok(left == ["Citywide Rentals", "Google", "PayPal", "Plaid bank link", "Venmo"], f"bank logins gone, Recycle Bin too, the rest kept ({left})")
ok(not kp.find_entries(title="Chase", first=True) and not kp.find_entries(title="Wells Fargo", first=True)
   and not (kp.recyclebin_group and kp.recyclebin_group.entries), "a vault search for the removed titles finds nothing")
ok("Removed 4 bank logins" in line and "Chase, Chase Jo" in line and "Wells Fargo" in line and "1 of them from its Recycle Bin" in line
   and "5 logins left" in line and "now finds nothing" in line, "the chat line says what went, what is left and the check")
ok("doer/tools.json" in line and "learnings.md" not in line and "studio1" not in line and "secret" not in line,
   "a plain copy is reported by path, never by value")
ok("\n" not in line, "one line for chat")
again = vp.purge(root=files)
ok(again.startswith("No bank logins in the vault") and "nothing changed" in again, f"a second tap changes nothing ({again})")
ok("vault_purge_banks" in (ROOT / "README.md").read_text(encoding="utf-8"), "the README names the job")
print(f"\nall {N} checks passed")
