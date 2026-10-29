"""computer.py - the doer's hands on this PC (assistant/doer). Built 2026-10-11.

the owner: "fully functional Jarvis on the go, full control of computer and connections, safe,
self skill building". The browser doer (doer.py) works in Chromium; this lane works on the
machine itself: files and drives, scripts, local programs (ffmpeg, Resolve's scripting API,
the vault's own tools), connection checks, small pieces of code written on the spot. Same
shape as the browser lane: one agent_run ticket (payload.where = "computer"), one headless
model turn, three tiers, a checkpoint card before anything irreversible, exit. Nothing loops.

What is different, and why it is safe to give it a shell:
  * The tier is decided by CODE. computer_policy.py runs as a PreToolUse hook on every Bash,
    Write and Edit call of the turn (passed with --settings, outside the model's reach): tier A/B
    runs, tier C is refused with "post a checkpoint", NEVER is refused for good. The approved
    step is sha-bound: drain -> doer.py --approved -> run_approved sets DOER_APPROVED_SHA and that
    ONE exact command may run once.
  * Files are written only under assistant/out/doer/<ticket>/ and assistant/doer/tools/.
  * No MCP servers at all (--strict-mcp-config with an empty config), no PowerShell tool (one shell
    to police; `powershell -Command` inside Bash is scanned as text), no web, no nested agents.
  * Footage paths, credentials, the approval chain, the registry, scheduled tasks and the
    property business are NEVER, whatever the task says.

Self-built tools: a job that will come back is saved as `assistant/doer/tools/<slug>.py` with a
header (# tool / # triggers / # tier / # usage). `index()` rebuilds tools.json from the headers
(zero-LLM); the next turn sees the list and reuses them; Sage writes a `skill` from the report.
Every saved tool went through the scan when it was written (hook on Write) and again each time it
runs (hook on Bash). Tools are git-tracked so an owner can read or revert any of them.

  python assistant/doer/computer.py --status
  python assistant/doer/computer.py --index           # rebuild tools.json from the tool headers
  python assistant/doer/computer.py --run <ticket> [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VAULT = ROOT.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(HERE))
from alib import config, load_json  # noqa: E402
import tickets  # noqa: E402
import computer_policy as cpol  # noqa: E402

CFG = (config().get("doer") or {})
CCFG = (CFG.get("computer") or {})
OUT = ROOT / "out" / "doer"
TOOLS = HERE / "tools"
TOOLS_INDEX = TOOLS / "tools.json"
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "doer-computer-wv.log"
CLAUDE = os.path.expandvars(r"%APPDATA%\npm\claude.cmd")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
WHERE = "computer"

# check_gates.py parses these two shapes (target "doer-computer"). Bash is open on purpose: every
# call goes through the computer_policy.py hook (fail-closed), which is where the belt is.
ALLOWED = ",".join([
    "Read", "Glob", "Grep", "Bash",
    "Edit(assistant/out/doer/**)", "Edit(assistant/doer/tools/**)",
    "Write(assistant/out/doer/**)", "Write(assistant/doer/tools/**)",
])
DENIED = ",".join([
    "PowerShell", "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit", "Skill", "KillShell",
    "mcp__gmail__send_email", "mcp__gmail__delete_email", "mcp__gmail__batch_delete_emails",
    "mcp__example-mail-personal__send_email", "mcp__example-mail-personal__delete_email",
    "mcp__example-mail-films__send_email", "mcp__example-mail-films__delete_email",
    "mcp__honeybook__take_action", "mcp__playwright__browser_click", "mcp__playwright-cli__browser_click",
    "mcp__playwright-doer__browser_click",
    "Edit(Wiki/**)", "Edit(Memory/**)", "Edit(Schema/**)", "Edit(Raw/**)", "Edit(scripts/**)", "Edit(.claude/**)",
    "Edit(assistant/scripts/**)", "Edit(assistant/doer/*.py)", "Edit(assistant/doer/*.json)", "Edit(assistant/doer/*.md)",
    "Edit(assistant/agent/**)", "Edit(assistant/data/**)", "Edit(assistant/config.json)", "Edit(assistant/prompts/**)",
    "Write(Wiki/**)", "Write(Memory/**)", "Write(Schema/**)", "Write(Raw/**)", "Write(scripts/**)", "Write(.claude/**)",
    "Write(assistant/scripts/**)", "Write(assistant/doer/*.py)", "Write(assistant/doer/*.json)", "Write(assistant/doer/*.md)",
    "Write(assistant/agent/**)", "Write(assistant/data/**)", "Write(assistant/config.json)", "Write(assistant/prompts/**)",
    "Bash(python assistant/scripts/cards.py answer:*)", "Bash(python assistant/scripts/cards.py undo:*)",
    "Bash(python assistant/scripts/drain.py:*)", "Bash(python assistant/scripts/email_send.py:*)",
    "Bash(python assistant/scripts/exec_store.py:*)", "Bash(python assistant/doer/vault.py:*)",
    "Bash(python assistant/doer/browser.py:*)", "Bash(python assistant/doer/doer.py:*)",
    "Bash(python assistant/doer/computer.py:*)", "Bash(python assistant/doer/login_fill.py:*)",
])


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _doer():
    import doer  # noqa: WPS433  lazy: doer imports this module for dispatch
    return doer


# ------------------------------------------------------------------ self-built tools
_HDR = re.compile(r"^#\s*(tool|triggers|tier|usage|needs)\s*:\s*(.+)$", re.I | re.M)


def index() -> dict:
    """tools.json from the headers of assistant/doer/tools/*.py (zero-LLM, idempotent)."""
    out = {}
    TOOLS.mkdir(parents=True, exist_ok=True)
    for p in sorted(TOOLS.glob("*.py")):
        try:
            head = "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[:40])
        except OSError:
            continue
        meta = {k.lower(): v.strip() for k, v in _HDR.findall(head)}
        if "tool" not in meta:
            continue
        tier, _ = cpol.scan_script(p.read_text(encoding="utf-8", errors="replace"))
        out[p.stem] = {"name": meta["tool"][:80], "triggers": [t.strip() for t in meta.get("triggers", "").split(",") if t.strip()][:12],
                       "tier": meta.get("tier", tier)[:5], "scan": tier, "usage": meta.get("usage", f"python assistant/doer/tools/{p.name}")[:200],
                       "needs": meta.get("needs", "")[:120], "file": f"assistant/doer/tools/{p.name}",
                       "sha": hashlib.sha256(p.read_bytes()).hexdigest()[:12], "updated": time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime))}
    TOOLS_INDEX.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    return out


def tools_block(task: str = "") -> str:
    idx = load_json(TOOLS_INDEX, {}) if TOOLS_INDEX.is_file() else {}
    if not idx:
        return "(none yet - the first one you save starts the library)"
    words = set(re.findall(r"[a-z0-9]{3,}", (task or "").lower()))
    rows = []
    for slug, t in idx.items():
        hit = any(w in words for trig in t.get("triggers", []) for w in re.findall(r"[a-z0-9]{3,}", trig.lower()))
        rows.append((0 if hit else 1, f"- {'* ' if hit else ''}{t['name']} (tier {t.get('tier', '?')}): `{t['usage']}`"
                     + (f" - needs {t['needs']}" if t.get("needs") else "")))
    rows.sort()
    return "\n".join(r for _, r in rows[:25]) + ("\n(* = matches this task; read the file before running it)" if any(k == 0 for k, _ in rows) else "")


# ------------------------------------------------------------------ the turn
def build_prompt(t: dict, resume_block: str = "") -> str:
    d = _doer()
    tpl = (HERE / "computer_prompt.md").read_text(encoding="utf-8")
    pol = cpol._policy()
    p = t.get("payload") or {}
    who = {"owner1": "owner1", "owner6": "owner6"}.get(str(p.get("from") or ""), str(p.get("from") or "an owner"))
    task = str(p.get("text") or t.get("summary") or t.get("title") or "").strip()
    out_dir = (OUT / t["id"]).as_posix()
    return (tpl.replace("{agent_name}", d._agent_name()).replace("{household}", d._household())
            .replace("{requester}", who).replace("{task}", task)
            .replace("{resume_block}", resume_block).replace("{out_dir}", out_dir).replace("{ticket}", t["id"])
            .replace("{vault}", VAULT.as_posix()).replace("{status_file}", d.STATUS.as_posix())
            .replace("{max_minutes}", str(pol.get("max_minutes", 15)))
            .replace("{write_roots}", ", ".join(pol.get("write_roots", [])))
            .replace("{tools}", tools_block(task))
            .replace("{{", "{").replace("}}", "}"))


def _settings(out_dir: Path) -> Path:
    """The hook that makes the policy law for this turn. Written per run, outside the model's
    write roots? No: out_dir IS writable by the model, so the file is re-written right before the
    turn and its sha is logged; a changed settings file would also still be subject to the
    ALLOWED/DENIED lists and the project hooks."""
    py = Path(sys.executable).as_posix()
    hook = f'"{py}" "{(HERE / "computer_policy.py").as_posix()}" --hook'
    cfg = {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": hook, "timeout": 20}]},
        {"matcher": "Write|Edit|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": hook, "timeout": 20}]},
    ]}}
    f = out_dir / "settings.json"
    f.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    log(f"settings {f} sha={hashlib.sha256(f.read_bytes()).hexdigest()[:12]}")
    return f


def _mcp_config(out_dir: Path) -> Path:
    f = out_dir / "mcp.json"
    f.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    return f


def run_turn(prompt: str, out_dir: Path, resume: str = "", timeout: int = 900, env_extra: dict | None = None) -> dict:
    """One computer-use turn; a usage limit runs it once more on the fallback account (doer.with_fallback)."""
    return _doer().with_fallback(_run_turn_on, prompt, out_dir, resume=resume, timeout=timeout, env_extra=env_extra)


def _run_turn_on(prompt: str, out_dir: Path, resume: str = "", timeout: int = 900, env_extra: dict | None = None,
                 acct: str | None = None) -> dict:
    d = _doer()
    if not Path(CLAUDE).exists():
        return {"ok": False, "error": "claude CLI not found", "text": "", "session": "", "marker": "", "arg": ""}
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [CLAUDE, "-p", "--model", str(CCFG.get("model", CFG.get("model", "sonnet"))), "--output-format", "json",
           "--max-turns", str(CCFG.get("max_turns", CFG.get("max_turns", 80))),
           "--mcp-config", str(_mcp_config(out_dir)), "--strict-mcp-config",
           "--settings", str(_settings(out_dir)),
           "--allowedTools", ALLOWED, "--disallowedTools", DENIED]
    if resume:
        cmd += ["--resume", resume]
    env, note = d._env(acct)
    env = {**env, **(env_extra or {})}
    env.setdefault("WV_LOG_DIR", str(LOG.parent))
    log(f"turn start model={CCFG.get('model', CFG.get('model', 'sonnet'))} resume={'yes' if resume else 'no'} {note}")
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, cwd=str(VAULT), env=env, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "text": "", "session": resume, "marker": "", "arg": ""}
    out = (r.stdout or "").strip()
    res = None
    for ln in reversed(out.splitlines() or [""]):
        try:
            v = json.loads(ln)
        except ValueError:
            continue
        if isinstance(v, dict) and "result" in v:
            res = v
            break
    if res is None:
        return {"ok": False, "error": ((r.stderr or "") + out)[-400:] or f"exit {r.returncode}", "text": "", "session": resume, "marker": "", "arg": ""}
    text = str(res.get("result") or "")
    marker, arg = d.parse_marker(text)
    return {"ok": not res.get("is_error"), "text": text, "session": str(res.get("session_id") or resume or ""),
            "error": text[:300] if res.get("is_error") else "", "marker": marker, "arg": arg}


def is_computer(t: dict) -> bool:
    return str(((t or {}).get("payload") or {}).get("where") or "").lower() == WHERE


def verify_evidence(out_dir: Path, max_hash_bytes: int = 4 << 30) -> tuple:
    """Check the turn's evidence.json against the machine (zero-LLM). the owner, 2026-10-15: "when it has
    evidence of completion report back". -> (lines for the owner, all_ok, n_checked). A file row is
    re-stat'ed (and re-hashed when a sha256 is given and the file is under max_hash_bytes); a check
    row is reported as the tool wrote it. A missing file or a wrong hash is said plainly."""
    p = Path(out_dir) / "evidence.json"
    if not p.is_file():
        return [], True, 0
    try:
        ev = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["evidence.json could not be read"], False, 0
    lines, ok, n = [], True, 0
    for f in (ev.get("files") or [])[:40]:
        n += 1
        path = Path(str(f.get("path") or ""))
        if not path.is_file():
            lines.append(f"MISSING {path}")
            ok = False
            continue
        size = path.stat().st_size
        want = int(f.get("bytes") or 0)
        bits = [f"{size:,} bytes"]
        if want and size != want:
            bits.append(f"expected {want:,}")
            ok = False
        sha = str(f.get("sha256") or "")
        if sha and size <= max_hash_bytes:
            h = hashlib.sha256()
            try:
                with path.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() == sha:
                    bits.append("sha256 " + sha[:12] + " verified")
                else:
                    bits.append("sha256 DIFFERS from the tool's")
                    ok = False
            except OSError:
                bits.append("could not hash")
                ok = False
        lines.append(f"{path} ({', '.join(bits)})")
    for c in (ev.get("checks") or [])[:40]:
        n += 1
        good = bool(c.get("ok"))
        ok = ok and good
        lines.append(f"{'OK' if good else 'FAILED'} {c.get('what')}" + (f": {c.get('detail')}" if c.get("detail") else ""))
    return lines, ok, n


def evidence_suffix(out_dir: Path, marker: str) -> str:
    """The owner-facing evidence block under a DOER_DONE line; a DONE with no evidence says so."""
    if marker != "DOER_DONE":
        return ""
    lines, ok, n = verify_evidence(out_dir)
    if n == 0 and not lines:
        return " (No evidence file was written for this one, so take it as a claim I could not check.)"
    head = " Checked:" if ok else " Checked, and something is off:"
    return head + "\n- " + "\n- ".join(lines[:12]) + (f"\n- ...and {len(lines) - 12} more" if len(lines) > 12 else "")


def _finish(t: dict, r: dict, label: str, started: float = 0.0) -> str:
    d = _doer()
    tid = t["id"]
    d._session_set(tid, r.get("session"))
    (OUT / tid / f"{label}.json").write_text(json.dumps(r, indent=1, ensure_ascii=False), encoding="utf-8")
    who = str((t.get("payload") or {}).get("from") or "owner1")
    mk, arg = r.get("marker"), r.get("arg", "")
    if mk in ("DOER_DONE", "DOER_ANSWER"):
        _, ev_ok, ev_n = verify_evidence(OUT / tid)
        tickets._transition(tid, "done", "doer", f"{mk}: {arg}"[:d.REPORT_MAX + 20] + (f" [evidence: {ev_n} checked, {'ok' if ev_ok else 'MISMATCH'}]" if ev_n else " [no evidence file]"),
                            tickets.OPEN, note_max=d.REPORT_MAX + 120)
    elif mk == "DOER_CHECKPOINT":
        qid = arg.split(" ", 1)[0]
        tickets._transition(tid, "staged", "doer", f"card {qid}: checkpoint, waiting for the owner's tap", {"claimed", "new"})
    elif mk == "DOER_BLOCKED":
        tickets._transition(tid, "staged", "doer", f"blocked: {arg}"[:500], {"claimed", "new"})
    else:
        arg = r.get("error") or "the turn ended without a final line"
        if r.get("continued") and d.cut_off(r):
            arg = f"{d.cut_off(r)} {int(r['continued']) + 1} times over, even carrying on from its notes"
        tickets._transition(tid, "staged", "doer", f"no result: {arg}"[:500], {"claimed", "new"})
    d.set_status(t, d._phase(mk or "", arg))
    if hasattr(d, "_outcome_doer"):
        ev_ok, ev_n = (verify_evidence(OUT / tid)[1:] if mk in ("DOER_DONE", "DOER_ANSWER") else (True, 0))
        d._outcome_doer(tid, mk or "", ev_ok, ev_n, t, "computer")
    try:
        index()
    except Exception as e:  # noqa: BLE001
        log(f"index failed: {type(e).__name__}")
    learned = ""
    try:
        learn = _doer()._learn()
        learned = learn.after_run(t, r, WHERE, started, label)
    except Exception as e:  # noqa: BLE001
        log(f"learn failed: {type(e).__name__}")
    d.deliver(t, d._say(who, mk or "", arg, t) + evidence_suffix(OUT / tid, mk or "") + (learned if mk in ("DOER_DONE", "DOER_ANSWER") else ""))
    log(f"ticket {tid}: {mk or 'NO_MARKER'} {arg[:160]}")
    return f"{mk or 'NO_MARKER'}: {arg}"


def run_ticket(t: dict, dry: bool = False, follow_up: str = "") -> str:
    """Work one computer agent_run ticket end to end (called by doer.run_ticket when
    payload.where == computer). Returns the final marker line."""
    d = _doer()
    tid = t["id"]
    out_dir = OUT / tid
    out_dir.mkdir(parents=True, exist_ok=True)
    sid = d._session_get(tid) if follow_up else ""
    prompt = follow_up if sid else build_prompt(t, resume_block=follow_up)
    if dry:
        print(prompt)
        return "DRY_RUN_OK"
    if not d._budget_ok():
        d.set_status(t, "not started: today's limit of doer tasks is used up")
        tickets._transition(tid, "staged", "doer", "budget: today's doer runs are used up; retry tomorrow", {"claimed", "new"})
        d.deliver(t, f"I have used today's limit of tasks, so {d.job_line(t) or 'this job'} waits. I will pick it up tomorrow, or raise doer.runs_per_day_max.")
        return "BUDGET"
    d.set_status(t, "working on the PC", "starting")
    started = time.time()
    timeout = int(CCFG.get("turn_timeout_s", CFG.get("turn_timeout_s", 900)))
    r = run_turn(prompt, out_dir, resume=sid, timeout=timeout, env_extra={"DOER_COMPUTER_TICKET": tid})
    if not r.get("ok") and sid and "resume" in (r.get("error") or "").lower():
        r = run_turn(build_prompt(t, resume_block=follow_up), out_dir, timeout=timeout, env_extra={"DOER_COMPUTER_TICKET": tid})
    # a run cut off by its step limit or time limit goes on from its milestone notes (doer.carry_on, 2026-10-28)
    r = d.carry_on(t, r, out_dir, timeout,
                   turn=lambda p, o, resume="", timeout=timeout: run_turn(p, o, resume=resume, timeout=timeout, env_extra={"DOER_COMPUTER_TICKET": tid}),
                   builder=build_prompt, place="the files, folders and tools are as you left them")
    return _finish(t, r, "result", started)


def approved_block(payload: dict) -> str:
    step, resume = str(payload.get("step") or ""), str(payload.get("resume") or "")
    command = str(payload.get("command") or "")
    vals = payload.get("values") or {}
    return ("## APPROVED STEP\nThe owner tapped Approve on your checkpoint card. Perform exactly this now, verify the "
            f"result, and finish with DOER_DONE (never another checkpoint for the same step):\n- Step: {step}\n"
            + (f"- Run EXACTLY this command, character for character, once: `{command}`\n" if command else "")
            + (f"- Values: {json.dumps(vals, ensure_ascii=False)}\n" if vals else "")
            + f"- How: {resume}\n"
            + "The policy hook lets that one command through once; a different command at tier C is refused. If the "
            "machine now shows something different from the card (the file is gone, the size changed), do NOT proceed: "
            "stop with DOER_BLOCKED and say what changed.\n")


def run_approved(payload: dict, dry: bool = False) -> int:
    """drain.py -> doer.py --approved -> here when the ticket is a computer task. The exact
    approved command is unlocked by sha, once. Prints DOER_RESULT=<line>; exit 0 only on DOER_DONE."""
    d = _doer()
    tid = str(payload.get("ticket") or "")
    t = tickets._load().get(tid)
    if not t:
        print("DOER_RESULT=REFUSED unknown ticket")
        return 1
    command = str(payload.get("command") or "")
    block = approved_block(payload)
    sid = d._session_get(tid)
    prompt = block if sid else build_prompt(t, resume_block=block)
    if dry:
        print("DRY_RUN_OK\n" + prompt[:600])
        return 0
    out_dir = OUT / tid
    out_dir.mkdir(parents=True, exist_ok=True)
    used = out_dir / f"approved_{cpol.sha(command)[:12]}.used"
    env_extra = {"DOER_COMPUTER_TICKET": tid}
    if command:
        env_extra.update({"DOER_APPROVED_SHA": cpol.sha(command), "DOER_APPROVED_USED": str(used)})
    d.set_status(t, f"working on the approved step: {payload.get('step')}", "starting")
    started = time.time()
    r = run_turn(prompt, out_dir, resume=sid, timeout=int(CCFG.get("approved_timeout_s", CFG.get("approved_timeout_s", 480))), env_extra=env_extra)
    if not r.get("ok") and sid and "resume" in (r.get("error") or "").lower():
        r = run_turn(build_prompt(t, resume_block=block), out_dir, timeout=int(CCFG.get("approved_timeout_s", CFG.get("approved_timeout_s", 480))), env_extra=env_extra)
    d._session_set(tid, r.get("session"))
    (out_dir / "approved_result.json").write_text(json.dumps(r, indent=1, ensure_ascii=False), encoding="utf-8")
    mk, arg = r.get("marker"), r.get("arg", "")
    who = str((t.get("payload") or {}).get("from") or "owner1")
    d.set_status(t, d._phase(mk or "", arg or r.get("error", "")))
    learned = ""
    try:
        learn = _doer()._learn()
        learned = learn.after_run(t, r, WHERE, started, "approved")
    except Exception as e:  # noqa: BLE001
        log(f"learn failed: {type(e).__name__}")
    if mk == "DOER_DONE":
        tickets._transition(tid, "done", "doer", f"approved step done: {arg}"[:500], tickets.OPEN)
        d.deliver(t, d._say(who, mk, arg, t) + evidence_suffix(out_dir, mk or "") + learned)
        print(f"DOER_RESULT=DOER_DONE: {arg}")
        return 0
    d.deliver(t, d._say(who, mk or "", arg or r.get("error", ""), t))
    print(f"DOER_RESULT={mk or 'NO_MARKER'}: {arg or r.get('error', '')}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--run", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.index:
        idx = index()
        print(json.dumps(idx, indent=1, ensure_ascii=False))
        return 0
    if a.run:
        t = tickets._load().get(a.run)
        if not t or t.get("kind") != "agent_run" or not is_computer(t):
            print("no such computer agent_run ticket")
            return 1
        tickets._transition(a.run, "claimed", "doer", "", {"new", "claimed", "staged"})
        print(run_ticket(t, a.dry_run))
        return 0
    waiting = [{"id": t.get("id"), "status": t.get("status"), "title": t.get("title")} for t in tickets._load().values()
               if t.get("kind") == "agent_run" and is_computer(t) and t.get("status") in tickets.OPEN]
    print(json.dumps({"waiting": waiting, "tools": load_json(TOOLS_INDEX, {}) if TOOLS_INDEX.is_file() else {},
                      "policy": str(cpol.POLICY_FILE), "log": str(LOG)}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
