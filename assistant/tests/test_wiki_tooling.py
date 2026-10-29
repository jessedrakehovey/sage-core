"""The wiki stack on its own (2026-10-29): wiki_tool.py index / lint / search / audit / hook-pending against a temp vault built
from the GENERIC schema the engine ships (assistant/templates/Schema), session_capture.py appending one daily-log block
(and never twice), and semantic.py failing closed when fastembed / numpy are missing. Nothing here touches this vault.
   python assistant/tests/test_wiki_tooling.py
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # assistant/
VAULT = ROOT.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_wikitool_"))
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(args, stdin="", env_extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ("WV_HEADLESS", "WV_DREAM_LOG")}
    env.update(env_extra or {})
    r = subprocess.run([sys.executable] + args, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=str(TMP), env=env, timeout=120)
    return r.returncode, r.stdout or "", r.stderr or ""


# ---------------------------------------------------------------- the temp vault: generic schema, two good notes, one bad, two raw sources
(TMP / "scripts").mkdir()
for name in ("wiki_tool.py", "semantic.py", "session_capture.py"):
    shutil.copy2(VAULT / "scripts" / name, TMP / "scripts" / name)
shutil.copytree(ROOT / "templates" / "Schema", TMP / "Schema")
schema = (TMP / "Schema" / "schema.md").read_text(encoding="utf-8")
try:        # this organization's own niche vocabulary (assistant/core_sync.json -> niche_terms): none of it may be in the generic contract
    NICHE = [str(w).lower() for w in json.loads((ROOT / "core_sync.json").read_text(encoding="utf-8-sig")).get("niche_terms") or []]
except (OSError, ValueError):
    NICHE = []
leaks = [w for w in NICHE if w in schema.lower()]
check("the generic schema carries machine-read types, tags and routing and no word of this organization's niche vocabulary",
      all(f"```{b}\n" in schema for b in ("types", "tags", "routing")) and not leaks, str(leaks[:5]))
for d in ("Wiki/_meta", "Wiki/Operations", "Wiki/Clients", "Wiki/People", "Wiki/Personal", "Raw/Uploads", "Memory/Daily_Logs"):
    (TMP / d).mkdir(parents=True)
(TMP / "Raw" / "Uploads" / "inquiry process.md").write_text("We answer within two hours.\n", encoding="utf-8")
(TMP / "Raw" / "Uploads" / "price list.pdf").write_bytes(b"%PDF-1.4 fake\n")
(TMP / "Wiki" / "Operations" / "How We Answer an Inquiry.md").write_text(
    "---\ntype: workflow\narea: operations\ntags: [inquiry, procedure]\ncreated: 2026-10-01\nupdated: 2026-10-01\n"
    "sources:\n  - Raw/Uploads/inquiry process.md\nstatus: verified\n---\n\n# How We Answer an Inquiry\n\n"
    "Every inquiry gets a reply within two hours, drafted by the agent and tapped by an owner. Returning clients such as [[Acme Co]] get the short form.\n",
    encoding="utf-8")
(TMP / "Wiki" / "Clients" / "Acme Co.md").write_text(
    "---\ntype: client\narea: clients\ntags: [client-comms]\ncreated: 2026-10-02\nupdated: 2026-10-02\nsources: []\nstatus: draft\n---\n\n# Acme Co\n\n"
    "A wholesale account since 2026. Orders on the first Monday of the month.\n", encoding="utf-8")
(TMP / "Wiki" / "Personal" / "Bad Note.md").write_text(
    "---\ntype: unicorn\narea: personal\ntags: [personal]\ncreated: 2026-10-03\nupdated: 2026-10-03\nsources: []\nstatus: draft\n---\n\n# Bad Note\n\nA type the generic schema does not know.\n",
    encoding="utf-8")

# ---------------------------------------------------------------- index / lint / search / audit
rc, out, err = run(["scripts/wiki_tool.py", "index"])
idx = (TMP / "Wiki" / "_meta" / "index.md").read_text(encoding="utf-8")
check("index rebuilds Wiki/_meta/index.md grouped by area", rc == 0 and "## operations" in idx and "## clients" in idx and "[[Acme Co]]" in idx and "3 notes" in out, out + err)
rc, out, err = run(["scripts/wiki_tool.py", "lint"])
check("lint reads the generic schema: the unknown type is the one error, exit 1", rc == 1 and "1 errors" in out and "type 'unicorn' not in" in out, out + err)
rc, out, err = run(["scripts/wiki_tool.py", "lint", "Wiki/Operations/How We Answer an Inquiry.md"])
check("lint <path> checks only that note (the bad note elsewhere does not fail it), exit 0", rc == 0 and "1 notes checked" in out and "0 errors" in out, out + err)
rc, out, err = run(["scripts/wiki_tool.py", "lint", "Wiki/Personal/Bad Note.md"])
check("lint <path> on the bad note alone still fails", rc == 1 and "1 errors" in out, out + err)
(TMP / "Wiki" / "Personal" / "Bad Note.md").unlink()
rc, out, err = run(["scripts/wiki_tool.py", "lint"])
check("with the bad note gone the wiki lints clean; client profiles are leaves (no orphan warning)",
      rc == 0 and "0 errors" in out and "Acme Co.md: orphan" not in out, out + err)
rc, out, err = run(["scripts/wiki_tool.py", "search", "inquiry reply"])
check("search ranks the workflow first", rc == 0 and "Wiki/Operations/How We Answer an Inquiry.md" in out.strip().splitlines()[0], out + err)
rc, out, err = run(["scripts/wiki_tool.py", "audit"])
src = (TMP / "Wiki" / "_meta" / "sources.md").read_text(encoding="utf-8")
rq = (TMP / "Wiki" / "_meta" / "review-queue.md").read_text(encoding="utf-8")
check("audit: sources manifest (the cited .md ingested, the .pdf pending), index, lint PASS, review queue with the one draft",
      rc == 0 and "audit: PASS" in out and "1 raw sources pending" in out and "| Raw/Uploads/inquiry process.md | OK |" in src
      and "| Raw/Uploads/price list.pdf | **pending** |" in src and "[[Acme Co]]" in rq and "How We Answer" not in rq, out + err)
rc, out, err = run(["scripts/wiki_tool.py", "log", "test run"])
check("log prepends an entry to Wiki/_meta/log.md", rc == 0 and "test run" in (TMP / "Wiki" / "_meta" / "log.md").read_text(encoding="utf-8"))

# ---------------------------------------------------------------- hook-pending: the nightly ingest is promised only when a dreaming job is wired
rc, out, err = run(["scripts/wiki_tool.py", "hook-pending"], env_extra={"WV_DREAM_LOG": str(TMP / "no-such.log")})
ctx = (json.loads(out).get("hookSpecificOutput") or {}).get("additionalContext", "")
check("hook-pending with no dreaming job wired says the backlog does NOT drain on its own", rc == 0 and "1 Raw/ source" in ctx and "does NOT drain" in ctx, out + err)
(TMP / "assistant").mkdir()
(TMP / "assistant" / "org.json").write_text(json.dumps({"name": "Acme", "watched_tasks": ["Acme Dreaming (Memory consolidation)"]}), encoding="utf-8")
rc, out, err = run(["scripts/wiki_tool.py", "hook-pending"], env_extra={"WV_DREAM_LOG": str(TMP / "no-such.log")})
ctx = (json.loads(out).get("hookSpecificOutput") or {}).get("additionalContext", "")
check("hook-pending with a dreaming task among org.json watched_tasks promises the nightly ingest", rc == 0 and "drains on its own" in ctx, out + err)
(TMP / "Wiki" / "Operations" / "Price List.md").write_text(
    "---\ntype: reference\narea: operations\ntags: [pricing]\ncreated: 2026-10-04\nupdated: 2026-10-04\nsources:\n  - Raw/Uploads/price list.pdf\nstatus: draft\n---\n\n# Price List\n\nSee [[How We Answer an Inquiry]].\n",
    encoding="utf-8")
rc, out, err = run(["scripts/wiki_tool.py", "hook-pending"])
check("hook-pending is silent once every raw source is cited", rc == 0 and json.loads(out) == {"suppressOutput": True}, out + err)

# ---------------------------------------------------------------- session_capture: one block per session, appended, never twice
tr = TMP / "transcript.jsonl"
rows = [{"type": "user", "message": {"role": "user", "content": "Remember that Acme orders on the first Monday"}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}},
        {"type": "user", "message": {"role": "user", "content": "<system-reminder>noise</system-reminder>"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Noted: first Monday."}]}}]
tr.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
payload = json.dumps({"transcript_path": str(tr), "session_id": "abcdef1234567890", "cwd": str(TMP)})
rc, out, err = run(["scripts/session_capture.py"], stdin=payload)
day = TMP / "Memory" / "Daily_Logs" / f"{datetime.date.today():%Y-%m-%d}.md"
txt = day.read_text(encoding="utf-8") if day.exists() else ""
check("session_capture appends the day's log block: the human prompt, not the tool result or the reminder, plus the closing note",
      rc == 0 and day.exists() and "session abcdef12" in txt and "- Remember that Acme orders on the first Monday" in txt
      and "tool_result" not in txt and "noise" not in txt and "> Noted: first Monday." in txt, out + err + txt[-300:])
rc, out, err = run(["scripts/session_capture.py"], stdin=payload)
check("the same session is never appended twice", rc == 0 and day.read_text(encoding="utf-8") == txt)
rc, out, err = run(["scripts/session_capture.py"], stdin=payload.replace("abcdef1234567890", "fedcba0987654321"), env_extra={"WV_HEADLESS": "1"})
check("a headless run (WV_HEADLESS=1) writes nothing", rc == 0 and day.read_text(encoding="utf-8") == txt)

# ---------------------------------------------------------------- semantic.py without fastembed / numpy: one line, exit 2, the hook silent
nofe = TMP / "no_embeddings"
nofe.mkdir()
for mod in ("fastembed", "numpy"):
    (nofe / f"{mod}.py").write_text(f'raise ImportError("No module named {mod} (test stub)")\n', encoding="utf-8")
env = {"PYTHONPATH": str(nofe), "PYTHONSAFEPATH": "1"}
rc, out, err = run(["scripts/semantic.py", "search", "first monday orders"], env_extra=env)
check("semantic search without the packages fails closed: exit 2, the install line, no traceback",
      rc == 2 and "requirements-semantic.txt" in err and "wiki_tool.py search" in err and "Traceback" not in err, f"rc={rc} {out[-200:]} {err[-300:]}")
rc, out, err = run(["scripts/semantic.py", "build"], env_extra=env)
check("semantic build without the packages: the same one line", rc == 2 and "requirements-semantic.txt" in err and "Traceback" not in err, err[-300:])
rc, out, err = run(["scripts/semantic.py", "hook"], stdin=json.dumps({"prompt": "what does Acme order and when do they order it"}), env_extra=env)
check("the UserPromptSubmit hook stays silent (valid JSON, exit 0) without the packages", rc == 0 and json.loads(out) == {"suppressOutput": True} and "Traceback" not in err, out + err)
rc, out, err = run(["scripts/semantic.py", "status"], env_extra=env)
check("status answers without an index and without the packages", rc == 0 and "no index built yet" in out, out + err)

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f"({TMP})")
if not FAILS:
    shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILS else 0)
