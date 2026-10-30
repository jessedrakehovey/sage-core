"""Offline tests for assistant/scripts/mail_attach.py and the evidence check in the doer's computer
lane (2026-10-15): Drive links out of a mail body, the footage-folder refusal, never-overwrite names,
evidence rows, and computer.verify_evidence / evidence_suffix against real scratch files. No network,
no model.   python assistant/tests/test_mail_attach.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_attach_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["DOER_STATUS"] = str(TMP / "doer_status.md")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "doer"))
import google_api  # noqa: E402
import mail_attach as ma  # noqa: E402
import computer  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# 1. Drive links in a body (Gmail turns big attachments into these)
body = ("OwnerFilm_MIX_SFX_V2.wav\n<https://drive.google.com/file/d/1EXAMPLEce94c7919588188b/view?usp=drive_web>\n"
        "again https://drive.google.com/file/d/1EXAMPLEce94c7919588188b/view and "
        "https://drive.google.com/open?id=1EXAMPLE4d1942600fc68a22 plus https://docs.google.com/document/d/1EXAMPLEd0667678514dfe27/edit")
ids = google_api.drive_links(body)
check("drive links parsed, deduped, in order", ids == ["1EXAMPLEce94c7919588188b", "1EXAMPLE4d1942600fc68a22",
                                                      "1EXAMPLEd0667678514dfe27"], ids)
check("no links -> empty", google_api.drive_links("nothing here") == [])

# 2. the footage-folder refusal and safe names
check("project folder is not footage", not ma.is_footage_folder(r"L:\projects\Kingsmere\_SoundMix_Corwin_v2"))
check("Audio/ Card N/ DCIM are footage", ma.is_footage_folder(r"D:\Wedding\Audio\H5") and ma.is_footage_folder("E:/Card 2/x")
      and ma.is_footage_folder(r"F:\DCIM\100"))
check("safe file names", ma._safe_name('a/b:c*"d?.wav') == "a_b_c__d_.wav" and ma._safe_name("") == "attachment")
folder = TMP / "out"
folder.mkdir()
(folder / "x.wav").write_bytes(b"old")
p2 = ma._free_path(folder, "x.wav")
check("never overwrite: next free name", p2.name == "x (2).wav" and (folder / "x.wav").read_bytes() == b"old")

# 3. evidence rows + the doer's code-side verification
ev = TMP / "evidence.json"
good = folder / "good.bin"
good.write_bytes(b"hello" * 1000)
rows = [{"saved": str(good), "bytes": good.stat().st_size, "sha256": hashlib.sha256(good.read_bytes()).hexdigest(), "source": "drive"}]
ma.add_evidence(ev, rows, "test note")
d = json.loads(ev.read_text(encoding="utf-8"))
check("evidence file written with file rows + note", d["files"][0]["path"] == str(good) and d["notes"] == ["test note"])
ma.add_evidence(ev, rows, "second")
check("evidence appends", len(json.loads(ev.read_text(encoding="utf-8"))["files"]) == 2)
out_dir = TMP / "ticket"
out_dir.mkdir()
frows = [{"path": str(good), "bytes": good.stat().st_size, "sha256": rows[0]["sha256"]}]
(out_dir / "evidence.json").write_text(json.dumps({"files": frows, "checks": [{"what": "project saved", "ok": True, "detail": "owner1 Beach"}]}), encoding="utf-8")
lines, ok, n = computer.verify_evidence(out_dir)
check("verified: file re-hashed, check relayed", ok and n == 2 and "verified" in lines[0] and lines[1].startswith("OK project saved"), lines)
suffix = computer.evidence_suffix(out_dir, "DOER_DONE")
check("suffix starts with Checked:", suffix.startswith(" Checked:") and "good.bin" in suffix, suffix)
bad_rows = [dict(frows[0], sha256="0" * 64), {"path": str(folder / "missing.bin"), "bytes": 3}]
(out_dir / "evidence.json").write_text(json.dumps({"files": bad_rows}), encoding="utf-8")
lines, ok, n = computer.verify_evidence(out_dir)
check("a wrong hash and a missing file are said plainly", not ok and "DIFFERS" in lines[0] and lines[1].startswith("MISSING"), lines)
check("suffix says something is off", computer.evidence_suffix(out_dir, "DOER_DONE").startswith(" Checked, and something is off:"))
(out_dir / "evidence.json").unlink()
check("no evidence file -> an unchecked claim, said so", "claim" in computer.evidence_suffix(out_dir, "DOER_DONE"))
check("no suffix on a checkpoint or a block", computer.evidence_suffix(out_dir, "DOER_BLOCKED") == "" and computer.evidence_suffix(out_dir, "DOER_CHECKPOINT") == "")

# 4. the doer's own learn module loads (not assistant/scripts/learn.py), from both lanes
import doer  # noqa: E402
L = doer._learn()
check("doer loads its own learn.py (after_run exists)", hasattr(L, "after_run") and hasattr(L, "harvest"))
check("it is the doer's file, not assistant/scripts/learn.py", str(getattr(L, "__file__", "")).replace("\\", "/").endswith("assistant/doer/learn.py"))

# 5. policy: the new tools are tier B from the doer's shell, editor scripts are scanned, a full interpreter path is scanned
import computer_policy as cp  # noqa: E402
HOME = Path.home().as_posix()                               # the policy's ~ is this machine's home, whoever runs it
check("mail_attach from the doer is tier B", cp.classify('python assistant/scripts/mail_attach.py owner1-personal 1a13da84cfd13326 --out "' + HOME + '/Videos/Sage Inbox/Corwin"')[0] == "B")
if (ROOT.parent / "editor" / "resolve_swap_audio.py").is_file():   # the organization's editor tool: only where it lives
    check("the Resolve swap with the Python312 path is tier B", cp.classify('"' + HOME + '/AppData/Local/Programs/Python/Python312/python.exe" editor/resolve_swap_audio.py --project "owner1 Beach" --old a.wav --new "L:/x/b.wav" --apply')[0] == "B")
check("a full-path interpreter still scans the script (missing one is refused)", cp.classify('"' + HOME + '/AppData/Local/Programs/Python/Python312/python.exe" editor/nope.py')[0] == "NEVER")
check("Sage Inbox is a write root", any("Sage Inbox" in r for r in cp._policy()["write_roots"]))
check("mail_attach passes the static scan", cp.scan_script((ROOT / "scripts" / "mail_attach.py").read_text(encoding="utf-8"))[0] == "B")
if (ROOT.parent / "editor" / "resolve_swap_audio.py").is_file():   # the organization's editor tool: only where it lives
    check("resolve_swap_audio passes the static scan", cp.scan_script((ROOT.parent / "editor" / "resolve_swap_audio.py").read_text(encoding="utf-8"))[0] == "B")
check("the computer prompt carries the evidence contract and both tools",
      all(x in (ROOT / "doer" / "computer_prompt.md").read_text(encoding="utf-8") for x in ("evidence.json", "mail_attach.py", "resolve_swap_audio.py")))

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)} (scratch {TMP})")
sys.exit(1 if FAILS else 0)
