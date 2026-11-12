"""doer.py - Sage's hands: one on-demand browser agent per task (assistant/doer, 2026-10-05).

Sage (assistant/agent) is read-only. When an owner asks for something that must be DONE in an
account or on a site ("update my DMV address", "cancel that subscription", "check whether the
Edison bill is paid"), Sage queues an `agent_run` ticket and wakes this. Nothing loops: a
headless model turn (`claude -p`, config doer.model) is started for that one ticket, given the
shared signed-in browser (browser.py) through a private Playwright MCP, works the task under
assistant/doer/prompt.md + policy.json, and exits. Irreversible steps stop at a CHECKPOINT card
(report.py); the owner's tap mints an exec ticket and drain hands it to `--approved`, which
resumes the same model session to perform exactly the approved step. Results go back into the
owner's chat with Sage.

  python assistant/doer/doer.py --tick                 work ONE waiting agent_run ticket (spine job doer-tick; Sage wakes it too)
  python assistant/doer/doer.py --run <ticket id>      work that ticket
  python assistant/doer/doer.py --approved <payload.json> [--dry-run]   drain: the approved step
  python assistant/doer/doer.py --tick --dry-run       show the prompt the next ticket would get; no model
  python assistant/doer/doer.py --status

Safety shape (scripts/guardrails/check_gates.py target "doer"): explicit --allowedTools, a fixed
deny belt, --strict-mcp-config with only the doer's browser server, Bash limited to login_fill.py
(types vault logins the model never sees), report.py checkpoint and vault.py has. No mail, no
the CRM, no web fetch, no file writes outside assistant/out/doer. Everything it does on a site
is in one of three tiers (prompt.md); tier C never completes without an owner's tap.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # assistant/
VAULT = ROOT.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
from alib import config, load_json, save_json  # noqa: E402
import tickets  # noqa: E402
import browser  # noqa: E402

CFG = (config().get("doer") or {})
DDATA = Path(os.environ["DOER_DATA"]) if os.environ.get("DOER_DATA") else ROOT / "data" / "doer"
OUT = ROOT / "out" / "doer"
# the live status Sage reads to answer "where is it?" (code writes Task/Phase, the turn edits Now)
STATUS = Path(os.environ["DOER_STATUS"]) if os.environ.get("DOER_STATUS") else OUT / "status.md"
try:   # assistant/org.json (WP5)
    sys.path.insert(0, str(ROOT))
    import orgcfg as _orgcfg
    MC_URL = str(_orgcfg.get("mission_control_url") or "https://localhost:9443")
except Exception:  # noqa: BLE001
    MC_URL = "https://localhost:9443"


def _first_owner() -> str:
    try:
        import core
        return core.first_owner()
    except Exception:  # noqa: BLE001
        return "owner"


def _person_name(key: str) -> str:
    """A requester's first name from people.json (the prompt's {requester}); the key itself when unknown."""
    try:
        import core
        u = (core.people().get("users") or {}).get(str(key or "")) or {}
        return str(u.get("name") or "").split(" ")[0] or str(key or "an owner")
    except Exception:  # noqa: BLE001
        return str(key or "an owner")
SESSIONS = DDATA / "sessions.json"
BUDGET = DDATA / "budget.json"
LOCK = DDATA / ".doer.lock"
# tests set WV_LOG_DIR: a test run must never write fixture lines into the live log
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "doer-wv.log"
CLAUDE = os.environ.get("ASSISTANT_CLAUDE") or os.path.expandvars(r"%APPDATA%\npm\claude.cmd")
KIND = "agent_run"
MCP = "playwright-doer"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
MARK = re.compile(r"^\s*`?(DOER_(?:DONE|CHECKPOINT|BLOCKED|ANSWER)):\s*(.*?)`?\s*$")
REPORT_MAX = 1500       # a multi-line DOER_ANSWER / DOER_DONE keeps this much (chat + ticket note)


def parse_marker(text: str) -> tuple[str, str]:
    """The turn's last marker line -> (marker, arg). A DOER_ANSWER / DOER_DONE keeps the lines under it
    (up to REPORT_MAX): 2026-10-16 an answer listing the owner's options reached him cut at its first line."""
    lines = str(text or "").splitlines()
    for i in range(len(lines) - 1, -1, -1):
        m = MARK.match(lines[i])
        if not m:
            continue
        marker, arg = m.group(1), m.group(2).strip()
        if marker in ("DOER_ANSWER", "DOER_DONE"):
            rest = "\n".join(lines[i + 1:]).strip().strip("`").strip()
            if rest:
                arg = (arg + "\n" + rest).strip()
            if len(arg) > REPORT_MAX:
                arg = arg[:REPORT_MAX - 1].rstrip() + "…"
        return marker, arg
    return "", ""

# check_gates.py parses these two shapes to freeze/verify the allow/deny lists (target "doer").
ALLOWED = ",".join([
    "Read", "Glob", "Grep", "Edit(assistant/out/doer/**)",
    "Bash(python assistant/doer/login_fill.py:*)",
    "Bash(python assistant/doer/report.py checkpoint:*)",
    "Bash(python assistant/doer/report.py note:*)",           # the milestone note (notes.py, 2026-10-28)
    "Bash(python assistant/doer/report.py login:*)",          # the sign-in card: Enter login / I'll sign in myself (2026-11-03)
    "Bash(python assistant/doer/report.py evidence:*)",       # evidence.json written by code, no path to type (2026-11-10)
    "Bash(python assistant/doer/vault.py has:*)",
    f"mcp__{MCP}__browser_navigate", f"mcp__{MCP}__browser_navigate_back", f"mcp__{MCP}__browser_snapshot",
    f"mcp__{MCP}__browser_click", f"mcp__{MCP}__browser_type", f"mcp__{MCP}__browser_fill_form",
    f"mcp__{MCP}__browser_select_option", f"mcp__{MCP}__browser_press_key", f"mcp__{MCP}__browser_hover",
    f"mcp__{MCP}__browser_wait_for", f"mcp__{MCP}__browser_take_screenshot", f"mcp__{MCP}__browser_tabs",
    f"mcp__{MCP}__browser_handle_dialog", f"mcp__{MCP}__browser_find", f"mcp__{MCP}__browser_console_messages",
    f"mcp__{MCP}__browser_resize", f"mcp__{MCP}__browser_drag",
    # the forced click on a control that never counts as stable (CRM project pages, 2026-10-18:
    # three runs had no DOM-click route); prompt.md allows it only for that, after two timeouts
    f"mcp__{MCP}__browser_evaluate",
])
DENIED = ",".join([
    "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit", "Skill", "Write",
    f"mcp__{MCP}__browser_run_code_unsafe", f"mcp__{MCP}__browser_file_upload",
    f"mcp__{MCP}__browser_network_request", f"mcp__{MCP}__browser_install",
    "mcp__gmail__send_email", "mcp__gmail__delete_email", "mcp__gmail__batch_delete_emails",
    "mcp__example-mail-personal__send_email", "mcp__example-mail-personal__delete_email",
    "mcp__example-mail-films__send_email", "mcp__example-mail-films__delete_email",
    "mcp__playwright__browser_click", "mcp__playwright-cli__browser_click",
    "Edit(Wiki/**)", "Edit(Memory/**)", "Edit(Schema/**)", "Edit(scripts/**)", "Edit(.claude/**)",
    "Edit(assistant/scripts/**)", "Edit(assistant/doer/**)", "Edit(assistant/agent/**)", "Edit(assistant/data/**)",
    "Edit(assistant/config.json)", "Edit(assistant/prompts/**)",
    "Bash(python assistant/scripts/cards.py answer:*)", "Bash(python assistant/scripts/cards.py undo:*)",
    "Bash(python assistant/scripts/drain.py:*)", "Bash(python assistant/scripts/email_send.py:*)",
    "Bash(python assistant/scripts/exec_store.py:*)", "Bash(python assistant/doer/vault.py add:*)",
    "Bash(python assistant/doer/vault.py import-chrome:*)", "Bash(python assistant/doer/vault.py key:*)",
    "Bash(python assistant/doer/browser.py:*)", "Bash(python assistant/doer/doer.py:*)",
])


def _learn():
    """assistant/doer/learn.py by path. A bare `import learn` resolved to assistant/scripts/learn.py
    (the per-lane corrections ledger) whenever that one was already loaded, so every run logged
    "learn failed: AttributeError" and nothing was recorded or harvested (2026-10-12 to 10-15)."""
    import importlib.util
    mod = sys.modules.get("doer_learn")
    if mod is None:
        spec = importlib.util.spec_from_file_location("doer_learn", HERE / "learn.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["doer_learn"] = mod
        spec.loader.exec_module(mod)
    return mod


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ small state
def _budget_ok() -> bool:
    d = load_json(BUDGET, {}) or {}
    day = time.strftime("%Y-%m-%d")
    n = int(d.get(day, 0))
    if n >= int(CFG.get("runs_per_day_max", 20)):
        return False
    save_json(BUDGET, {day: n + 1})
    return True


def _session_get(tid: str) -> str:
    return str((load_json(SESSIONS, {}) or {}).get(tid) or "")


def _session_set(tid: str, sid: str) -> None:
    d = load_json(SESSIONS, {}) or {}
    if sid:
        d[tid] = sid
        if len(d) > 200:
            for k in list(d)[:-200]:
                d.pop(k, None)
        save_json(SESSIONS, d)


class _Single:
    """One doer at a time: one browser, one profile lease, one model turn."""

    def __enter__(self):
        DDATA.mkdir(parents=True, exist_ok=True)
        st = load_json(LOCK, {}) or {}
        if st and time.time() - float(st.get("ts", 0)) < int(CFG.get("turn_timeout_s", 900)) + 120:
            from drain import _pid_alive
            if _pid_alive(st.get("pid")):
                self.held = False
                return self
        save_json(LOCK, {"pid": os.getpid(), "ts": time.time()})
        self.held = True
        return self

    def __exit__(self, *exc):
        if self.held:
            try:
                LOCK.unlink()
            except OSError:
                pass


def set_status(t: dict, phase: str, now: str | None = None) -> None:
    """Rewrite the live status file. now=None keeps the turn's last Now line (what it was
    doing or waiting on when it stopped), so Sage can still say where it got to."""
    if now is None:
        try:
            now = next((ln[4:].strip() for ln in STATUS.read_text(encoding="utf-8").splitlines()
                        if ln.startswith("Now:")), "")
        except OSError:
            now = ""
    who = str((t.get("payload") or {}).get("from") or "an owner").capitalize()
    body = ("# Doer status (Sage reads this to answer \"where is it?\")\n"
            f"Task: {str(t.get('title') or t.get('summary') or 'task')[:120]} (ticket {t.get('id')}, asked by {who})\n"
            f"Phase: {' '.join(str(phase).split())[:400]}\n"
            f"Now: {' '.join(str(now or '-').split())[:300]}\n"
            f"Updated: {time.strftime('%Y-%m-%d %H:%M')}\n")
    try:
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(body, encoding="utf-8")
    except OSError as e:
        log(f"status write failed: {type(e).__name__}")
    _live("touch", str(t.get("id") or ""), " ".join(str(phase).split())[:200])


def _live(fn: str, *args):
    """The live card in chat (livecard.py, 2026-11-03): start / touch / finish, never a reason for a job to fail."""
    try:
        import livecard
        return getattr(livecard, fn)(*args)
    except Exception as e:  # noqa: BLE001
        log(f"live card {fn} failed: {type(e).__name__}: {str(e)[:120]}")
        return None


def _phase(mk: str, arg: str) -> str:
    if mk in ("DOER_DONE", "DOER_ANSWER"):
        return f"finished: {arg}"
    if mk == "DOER_CHECKPOINT":
        qid, _, step = arg.partition(" ")
        return f"waiting for an owner's tap on card {qid}: {step or arg}"
    if mk == "DOER_BLOCKED":
        return f"stopped, blocked: {arg}"
    return f"stopped without a result: {arg}"


def _retried_refusal(t: dict) -> bool:
    """True once a 'connector job' refusal has already been re-queued for this ticket (one retry, never a loop)."""
    return any("wrong refusal" in str(h.get("note") or "") for h in (t.get("history") or []))


def _asks_code(qid: str) -> bool:
    try:
        import cards
        c = next((c for c in cards._live() if c.get("id") == qid), None) or {}
        return bool((((c.get("questions") or [{}])[0].get("exec") or {}).get("payload") or {}).get("ask_code"))
    except Exception:  # noqa: BLE001
        return False


def _login_card(qid: str) -> dict:
    """The exec payload of a sign-in card (report.py login) when `qid` is one, else {}."""
    try:
        import cards
        c = cards._read(cards.LIVE / f"{qid}.json") or next((c for c in cards._live() if c.get("id") == qid), None) or {}
        p = (((c.get("questions") or [{}])[0].get("exec") or {}).get("payload") or {})
        return p if p.get("login") else {}
    except Exception:  # noqa: BLE001
        return {}


# ------------------------------------------------------------------ the model turn
def _agent_name() -> str:
    try:
        return str((load_json(ROOT / "agent" / "profile.json", {}) or {}).get("name") or "Sage")
    except Exception:
        return "Sage"


def _household() -> str:
    try:
        return str((load_json(ROOT / "agent" / "profile.json", {}) or {}).get("household") or "the household")
    except Exception:
        return "the household"


def build_prompt(t: dict, resume_block: str = "") -> str:
    tpl = (HERE / "prompt.md").read_text(encoding="utf-8")
    pol = load_json(HERE / "policy.json", {}) or {}
    p = t.get("payload") or {}
    who = _person_name(str(p.get("from") or ""))
    out_dir = (OUT / t["id"]).as_posix()
    return (tpl.replace("{agent_name}", _agent_name()).replace("{household}", _household())
            .replace("{requester}", who).replace("{task}", str(p.get("text") or t.get("summary") or t.get("title") or "").strip())
            .replace("{resume_block}", resume_block).replace("{out_dir}", out_dir).replace("{ticket}", t["id"])
            .replace("{vault}", VAULT.as_posix()).replace("{status_file}", STATUS.as_posix()).replace("{max_minutes}", str(pol.get("max_minutes", 15)))
            .replace("{{", "{").replace("}}", "}"))


def _mcp_config(out_dir: Path) -> Path:
    f = out_dir / "mcp.json"
    f.write_text(json.dumps({"mcpServers": {MCP: {"command": "npx", "args": ["-y", "@playwright/mcp@latest",
                                                                             "--cdp-endpoint", browser.cdp_url(),
                                                                             "--output-dir", str(out_dir)]}}}, indent=1),
                 encoding="utf-8")
    return f


def _settings(out_dir: Path) -> Path:
    """click_guard.py on the browser tools: 3 stuck clicks on one control end the job with the page
    state instead of running to max_turns (four CRM site runs, 2026-10-18). Counts reset per turn."""
    import click_guard
    click_guard.reset(out_dir)
    py, g, d = Path(sys.executable).as_posix(), (HERE / "click_guard.py").as_posix(), out_dir.as_posix()
    eg = (HERE / "egress.py").as_posix()
    cfg = {"hooks": {
        "PostToolUse": [{"matcher": f"mcp__{MCP}__browser_(click|evaluate)",
                         "hooks": [{"type": "command", "command": f'"{py}" "{g}" --hook post "{d}"', "timeout": 10}]}],
        "PreToolUse": [{"matcher": f"mcp__{MCP}__.*",
                        "hooks": [{"type": "command", "command": f'"{py}" "{g}" --hook pre "{d}"', "timeout": 10}]},
                       # the egress fence (egress.py, upgrade 14): a navigate to a host not on the doer's list is refused
                       # in allow_list mode; learn mode (the default) only notes the host
                       {"matcher": f"mcp__{MCP}__browser_navigate",
                        "hooks": [{"type": "command", "command": f'"{py}" "{eg}" --hook doer', "timeout": 10}]}],
    }}
    f = out_dir / "settings.json"
    f.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    return f


def _env(acct: str | None = None) -> tuple[dict, str]:
    """The same Claude account Sage's brain runs on (assistant/agent/account.py); `acct` = the fallback."""
    try:
        import account
        return account.env(acct)
    except Exception as e:  # noqa: BLE001
        return {**os.environ, "WV_HEADLESS": "1"}, f"default account ({type(e).__name__})"


def plain_error(raw: str) -> str:
    """One plain sentence for a run that ended without a result line. Never JSON, never a stack: the owner
    reads this in chat (the owner 2026-10-18, after a max-turns dump of {"spawned_by_subagents":0,...} reached him)."""
    s = str(raw or "")
    if "error_max_turns" in s or "max_turns" in s or "Reached max turns" in s:
        return "it ran out of steps before it finished (the step limit is set in config.json -> doer.max_turns); the page it was on would not take its clicks or kept changing"
    if "error_during_execution" in s or "is_error" in s or s.lstrip().startswith("{"):
        m = re.search(r'"errors?"\s*:\s*\[\s*"([^"]{3,160})', s)
        return "it stopped on an error" + (f": {m.group(1)}" if m else "") + "; nothing was changed as far as it reported"
    if "timeout" in s.lower():
        return "it ran past its time limit"
    # a cut tail of the CLI's result row (stderr + the last 400 chars) has no leading "{": its counters
    # (num_turns, uuid, killed/refused) are still never said
    if re.search(r'"(?:num_turns|uuid|session_id|subtype|duration_ms|total_cost_usd|\w*(?:killed|refused)\w*)"\s*:', s):
        return "it stopped before it finished, without a final line"
    return re.sub(r"\s+", " ", s)[:240] or "it ended without a final line"


LIMIT_CONTINUE = ("Your last turn on this job was cut off by the account's usage limit. Carry on from exactly where it "
                  "stopped: check the page first and do not redo a step that already happened.\n\n")

# ------------------------------------------------------------------ carrying on from the notes (2026-10-28)
# A run that ends on its step limit or its time limit is not a failure yet: the turn's milestone notes
# (notes.py, written with `report.py note`) are its checkpoint, and the job is continued from them, in the
# same session when the CLI returned one (the page and the cart are as it left them), else fresh with the
# notes in the prompt. Up to config doer.continues_max times; then the owner hears how far it got.
CARRY_ON = ("## CARRY ON\nYour last turn on this job stopped before it finished: {why}. This is try {n} of {max} on it; "
            "{place}.\n"
            "Your milestone notes so far (oldest first):\n{notes}\n"
            "Check the page first, then carry on from the LAST note: never redo a step a note says is done, and keep "
            "writing a note at each milestone (`python assistant/doer/report.py note --ticket {ticket} \"<line>\"`). If the "
            "job is bigger than one turn can hold, finish the piece in front of you, note it, and carry on; the notes are "
            "what the next try reads.\n")


def cut_off(r: dict) -> str:
    """'' when the turn ended on its own; else why it was cut: 'it ran out of steps' / 'it ran past its time limit'."""
    if r.get("ok") or r.get("marker"):
        return ""
    s = " ".join(str(r.get(k) or "") for k in ("error", "raw"))
    if "error_max_turns" in s or "max_turns" in s or "Reached max" in s or "ran out of steps" in s:
        return "it ran out of steps"
    if "timeout" in s.lower() or "time limit" in s.lower():
        return "it ran past its time limit"
    return ""


def carry_on(t: dict, r: dict, out_dir: Path, timeout: int, turn=None, builder=None, place: str = "") -> dict:
    """Continue a cut-off run from its notes, up to doer.continues_max times. The browser stays open between
    tries (the caller closes it). Sets r['continued'] to the number of extra tries made.
    `turn(prompt, out_dir, resume=, timeout=)` and `builder(t, resume_block=)` default to this lane's; the
    computer lane passes its own (computer.py), with `place` saying what is as it was left."""
    import notes
    turn = turn or run_turn
    builder = builder or build_prompt
    place = place or "the browser is still where you left it"
    n_max = int(CFG.get("continues_max", 3))
    n = 0
    tid = t["id"]
    while n < n_max:
        why = cut_off(r)
        if not why or _live("stop_requested", out_dir):                  # the owner's Stop: no further try
            break
        n += 1
        got = notes.last(tid)
        line = ("Still on it, picking up from where it stopped: " + got["text"]) if got else \
            "Still on it; it stopped partway without noting a milestone, so I'm taking that piece again."
        notes.record(tid, line, "progress", by="code")
        block = CARRY_ON.format(why=why, n=n + 1, max=n_max + 1, notes=notes.block(tid) or "(none yet)", ticket=tid, place=place)
        sid = str(r.get("session") or "")
        log(f"ticket {tid}: {why}; carrying on from the notes (try {n + 1} of {n_max + 1}, resume={'yes' if sid else 'no'})")
        set_status(t, f"carrying on from the notes, try {n + 1} of {n_max + 1}")
        r2 = turn(block, out_dir, resume=sid, timeout=timeout) if sid else turn(builder(t, resume_block=block), out_dir, timeout=timeout)
        if sid and not r2.get("ok") and not r2.get("marker") and "resume" in (r2.get("error") or "").lower():
            r2 = turn(builder(t, resume_block=block), out_dir, timeout=timeout)
        r = r2
    r["continued"] = n
    return r


def with_fallback(turn, prompt: str, out_dir: Path, resume: str = "", **kw) -> dict:
    """Run `turn`; on a usage limit, once more on profile.json `claude_account_fallback` (the owner 2026-10-26).
    A limited turn that already started a session is resumed there, so the browser work it did is not redone."""
    try:
        import account
        order = account.order()                       # the account with room first (reflex, 2026-10-27)
    except Exception:  # noqa: BLE001
        return turn(prompt, out_dir, resume=resume, **kw)
    r = None
    for i, acct in enumerate(order):
        if i == 0:
            r = turn(prompt, out_dir, resume=resume, acct=acct, **kw)
        else:
            sid = str(r.get("session") or resume or "")
            log(f"usage limit; running the turn once more on account {acct} (resume={'yes' if sid else 'no'})")
            r = turn(prompt if sid == resume else LIMIT_CONTINUE + prompt, out_dir, resume=sid, acct=acct, **kw)
        if r.get("ok") or not account.is_limit(r.get("error"), r.get("text"), r.get("raw")):
            return r
        account.note_limit(acct, str(r.get("error") or r.get("text") or r.get("raw") or ""))
    return r


def _apply_crm_tools(allowed: str, denied: str) -> tuple[str, str]:
    """Inject the CRM connector's write tool names into the deny belt at runtime so the engine lists none."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import alib
        write_tools = alib.plugin_set("MCP_WRITE_TOOLS")
        if write_tools:
            denied = denied + "," + ",".join(sorted(write_tools))
    except Exception:  # noqa: BLE001
        pass
    return allowed, denied


def run_turn(prompt: str, out_dir: Path, resume: str = "", timeout: int = 900) -> dict:
    """One headless model turn with the doer's browser -> {"ok", "text", "session", "error", "marker", "arg"}."""
    return with_fallback(_run_turn_on, prompt, out_dir, resume=resume, timeout=timeout)


def _run_turn_on(prompt: str, out_dir: Path, resume: str = "", timeout: int = 900, acct: str | None = None) -> dict:
    if not Path(CLAUDE).exists():
        return {"ok": False, "error": "claude CLI not found", "text": "", "session": "", "marker": "", "arg": ""}
    out_dir.mkdir(parents=True, exist_ok=True)
    _allowed, _denied = _apply_crm_tools(ALLOWED, DENIED)
    cmd = [CLAUDE, "-p", "--model", str(CFG.get("model", "sonnet")), "--output-format", "json",
           "--max-turns", str(CFG.get("max_turns", 80)),
           "--mcp-config", str(_mcp_config(out_dir)), "--strict-mcp-config",
           "--settings", str(_settings(out_dir)),
           "--allowedTools", _allowed, "--disallowedTools", _denied]
    if resume:
        cmd += ["--resume", resume]
    env, note = _env(acct)
    log(f"turn start model={CFG.get('model', 'sonnet')} resume={'yes' if resume else 'no'} {note}")
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, cwd=str(VAULT), env=env, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "text": "", "session": resume, "marker": "", "arg": ""}
    out = (r.stdout or "").strip()
    res = None
    for ln in reversed(out.splitlines() or [""]):
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        # a run that ended on its step limit or a crash has no "result" key, only is_error + subtype
        # (2026-10-18: the raw JSON of such a run went to the owner's chat verbatim); take it as the result row
        if isinstance(d, dict) and ("result" in d or d.get("is_error") or d.get("subtype")):
            res = d
            break
    if res is None:
        return {"ok": False, "error": plain_error(((r.stderr or "") + out)[-400:] or f"exit {r.returncode}"), "text": "", "session": resume, "marker": "", "arg": "",
                "raw": ((r.stderr or "") + out)[-400:]}
    if res.get("is_error") and not str(res.get("result") or "").strip():
        return {"ok": False, "error": plain_error(json.dumps(res)), "text": "", "session": str(res.get("session_id") or resume or ""), "marker": "", "arg": "",
                "raw": json.dumps(res)[-400:]}
    text = str(res.get("result") or "")
    marker, arg = parse_marker(text)
    return {"ok": not res.get("is_error"), "text": text, "session": str(res.get("session_id") or resume or ""),
            "error": text[:300] if res.get("is_error") else "", "marker": marker, "arg": arg}


# ------------------------------------------------------------------ talking back to the owner
# A failure line never tells the owner to open a terminal, run claude or type commands (nightly review,
# 2026-11-02): the owner reads chat on a phone. Such a sentence is dropped and one plain line says it needs the PC.
_PC_STEP = re.compile(r"\b(?:open|in|into|use|from)\s+(?:a|the|your)\s+(?:new\s+)?terminal\b|\bterminal (?:window|session)|command (?:prompt|line)|\bpowershell\b|\bcmd(?:\.exe)?\b|\bbash\b|"
                      r"\b(?:run|type|launch|start|open)\b[^.!?]{0,40}\bclaude\b|\bclaude (?:login|setup-token|--)|"
                      r"\b(?:run|type|paste|enter)\b[^.!?]{0,30}\bcommands?\b|\bpython\s+\S+\.py\b|\bschtasks\b|`[^`]+`", re.I)
PC_LINE = "That part is mine to sort out from here; nothing for you to do at a computer."


def pc_only(text: str) -> str:
    """The text without any sentence that tells the owner to use a terminal or type commands; if one was
    dropped, PC_LINE says plainly that it needs the PC. Text without such a step comes back unchanged."""
    s = str(text or "")
    try:                                   # the whole chat-first rule (agent/chatfirst.py, 2026-11-04), not only terminal words
        import chatfirst
        s = chatfirst.clean(s, PC_LINE)
    except Exception:  # noqa: BLE001
        pass
    if not _PC_STEP.search(s):
        return s
    parts = [x for x in re.split(r"(?<=[.!?:])\s+", s.strip()) if x]
    bad = [bool(_PC_STEP.search(x)) for x in parts] + [True]
    # a lead-in ("...from your phone:") goes with the step that followed it
    kept = [x for i, x in enumerate(parts) if not bad[i] and not (x.endswith(":") and bad[i + 1])]
    return " ".join(kept or [PC_LINE])     # the stock line only when nothing else is left (2026-11-10: it was on every line)


def deliver(t: dict, text: str) -> None:
    """Put the result in the requester's chat with Sage (the transcript the chat page shows,
    plus Google Chat when that is where they asked)."""
    text = pc_only(text)
    p = t.get("payload") or {}
    key = str(p.get("from") or _first_owner())
    try:
        import receipts
        convo = str(p.get("convo") or f"dm:{key}")
        # ONE line per job, in the thread or the DM it was asked in, edited in place by any later outcome
        # (2026-10-21; before this a DM ask's report landed top-level in the home space)
        receipts.say(str(t.get("id") or ""), convo, str(p.get("thread") or ""), text, space=str(p.get("space") or ""), channel="doer")
    except Exception as e:  # noqa: BLE001
        log(f"deliver failed: {type(e).__name__}: {str(e)[:160]}")
    try:                                    # Mission Control notification as well
        subprocess.run([sys.executable, str(ROOT / "scripts" / "notify.py")], input=text, capture_output=True, text=True,
                       encoding="utf-8", timeout=20, cwd=str(VAULT), creationflags=NO_WINDOW)
    except Exception:
        pass


def _outcome_doer(tid: str, marker: str, ev_ok: bool, ev_n: int, t: dict | None, where: str) -> None:
    """One outcome row per doer run (outcomes.py, 2026-10-24): DONE with checked evidence is verified, DONE with no
    evidence file is a claim the owner may be asked about, a mismatch is a failure. Never raises."""
    try:
        import outcomes
        p = (t or {}).get("payload") or {}
        outcomes.from_doer(str(tid), str(marker or ""), bool(ev_ok), int(ev_n or 0),
                           what=str(p.get("text") or (t or {}).get("title") or "")[:160], lane=str((t or {}).get("lane") or ""), where=where)
    except Exception as e:  # noqa: BLE001
        log(f"outcome row failed: {type(e).__name__}: {str(e)[:120]}")


def job_line(t: dict | None, n: int = 110) -> str:
    """The job in the owner's words, quoted, for any line that says it did not get done.
    the owner 2026-10-17: "sometimes Sage says it can't do something with no context on what it was"."""
    p = (t or {}).get("payload") or {}
    text = str(p.get("text") or (t or {}).get("summary") or "").split("\n\nHOW WE DID")[0]
    text = " ".join(text.split())
    if not text:
        return ""
    if len(text) > n:                 # cut at a word, never mid-name (voice.py, 2026-10-27: no "ownernam…")
        text = text[:n].rsplit(" ", 1)[0].rstrip(" ,;:(") + "…"
    return '"' + text + '"'


def portal(arg: str) -> str:
    """A blocked line becomes the owner's portal (the owner, 2026-10-20): the reason, then the link and the phone
    steps the model put after `DO IT YOURSELF:`, never a promise to retry."""
    why, _, how = str(arg or "").partition("DO IT YOURSELF:")
    why = why.replace("||", "").strip().rstrip(".")
    how = " ".join(how.split()).strip().rstrip(".")
    if not how:
        return why + "." if why else "it stopped without saying why."
    return f"{why}. The quickest way to finish it yourself, from your phone: {how}."


_OWNER_ONLY = re.compile(r"password|passcode|login saved|no login|sign in yourself|signed? in on your|verification code|one-time code|"
                         r"\b2fa\b|two-factor|authenticator|your tap|tap (?:approve|the card)|\bpay(?:ment|ing)?\b|card (?:number|details)|"
                         r"\bcvv\b|identity|id\.me|captcha|your (?:own )?(?:hand|phone)\b|only you can|stopped by the owner|sign-in card", re.I)
STOPPED = "stopped by the owner"


# A block that is only a missing sign-in (nightly review, 2026-10-17: such lines named vault.py / browser.py) is said as
# the chat route to fix it, /signin <site> or the 'save my <site> login' form, never the model's own steps. The Setup
# page the review asked for is the PC's (chatfirst.py, 2026-11-04), so chat is the place.
_SIGNED_OUT = re.compile(r"signed[ -]?out|not (?:signed|logged) in|logged out|sign-?in (?:expired|required|needed)|"
                         r"no login(?: saved)?|login (?:is )?missing|needs? (?:a |the )?(?:login|sign-?in)\b", re.I)
_HOST = re.compile(r"\b(?:https?://)?((?:[a-z0-9-]+\.)+(?!py\b|json\b|ps1\b|md\b)[a-z]{2,})\b", re.I)


def signin_line(arg: str) -> str:
    """The chat line for a sign-in block, or '' when `arg` is not one."""
    why = str(arg or "").partition("DO IT YOURSELF:")[0]
    if not _SIGNED_OUT.search(why):
        return ""
    m = _HOST.search(str(arg or ""))
    host = re.sub(r"^www\.", "", m.group(1).lower()) if m else ""
    site = host or "the site"
    return (f"I'm not signed in to {site}. Say '/signin {host or '<site>'}' here and I sign in from my vault; no login saved "
            f"for it yet? say 'save my {host or '<site>'} login' and the form comes up here.")


def _owner_only(arg: str) -> bool:
    """A block only the owner can clear: a credential, a code, a tap, money. Everything else is the agent's to build."""
    return bool(_OWNER_ONLY.search(str(arg or "")))


def _gap_for(t: dict, why: str) -> str:
    """A blocked or resultless run files the build for what was missing (capability.py), by code, and nothing is
    said in chat until the ask comes back and works (the owner, 2026-10-30: "stop, immediately build something, and
    not update me at all until it works"; "I hate seeing 'Still working', 'That didn't work'"). -> the build key."""
    try:
        import capability
        p = t.get("payload") or {}
        who = str(p.get("from") or _first_owner())
        ask = " ".join(str(p.get("text") or t.get("summary") or t.get("title") or "").split("\n\nHOW WE DID")[0].split())
        if not ask:
            return ""
        d = capability._load()
        g = capability._find(d, who, ask)
        if g and g.get("status") in ("building", "capped", "unfiled"):
            capability._save(d)
            return str(g.get("build") or "")
        if g and g.get("status") in ("built", "replayed"):
            g["quote"] = str(why)[:400]
            key = capability.file_build(d, g, by=who, previous=f"{g.get('build', '')} landed and the job still stopped: \"{str(why)[:200]}\"")
            capability._save(d)
            return key
        g = capability.open_gap(d, "capability", ask, str(why), who,
                                {"convo": str(p.get("convo") or f"dm:{who}"), "thread": str(p.get("thread") or ""), "space": str(p.get("space") or "")},
                                "doer blocked")
        g["ask_full"] = ask[:600]
        key = capability.file_build(d, g, by=who)
        capability._save(d)
        return key
    except Exception as e:  # noqa: BLE001
        log(f"gap for ticket {t.get('id')} failed: {type(e).__name__}: {str(e)[:120]}")
        return ""


def _card_has_screenshot(qid: str) -> bool:
    """True when the checkpoint card really carries a screenshot file (report.py put its name in the exec payload)."""
    try:
        import cards
        c = cards._read(cards.LIVE / f"{qid}.json") or {}
        q = (c.get("questions") or [{}])[0]
        return bool(((q.get("exec") or {}).get("payload") or {}).get("screenshot"))
    except Exception:  # noqa: BLE001
        return False


def _say(who: str, kind: str, arg: str, t: dict | None = None) -> str:
    name = _agent_name()
    job = job_line(t)
    on = f" on {job}" if job else ""
    if kind == "DOER_DONE":
        return f"Done: {arg}"
    if kind == "DOER_ANSWER":
        return arg
    if kind == "DOER_CHECKPOINT":
        qid, _, step = arg.partition(" ")
        lg = _login_card(qid)
        if lg:
            # the sign-in card (report.py login, 2026-11-03): the login goes into a dialog, never into this chat
            host = str(lg.get("host") or "the site")
            if step.startswith(REFUSED_STEP):
                said = step[len(REFUSED_STEP):].lstrip(": ").rstrip(". ")
                return (f"{host} did not accept that login{on}" + (f" ({said})" if said else "") + ". The card is back: Enter login "
                        "takes it again, straight to my vault and not into this chat, and I carry on from where I stopped.")
            if lg.get("human"):
                return (f"{host} wants a person at its sign-in{on} (a check only a person can pass). The card has 'I'll sign in myself': "
                        "Control PC opens my browser from your phone; tap Carry on when you are in.")
            return (f"I need the {host} login to carry on{on}. Tap Enter login on the card: it goes straight to my vault, not "
                    "into this chat, and I pick the job up from where I stopped.")
        ho = _handoff(t)
        if ho is not None and ho.get("qid") == qid:
            # the owner's own last step (report.py handoff): "open on your screen" only on code's read-back (2026-10-28)
            where = ("It is waiting in my browser; Control PC opens it from your phone." if ho.get("on_screen") else
                     "It is waiting in my browser (I could not confirm it is in front); Control PC opens it either way.")
            return (f"I got it ready and stopped before the last step, which is yours: {step or arg} {where} "
                    "Tap 'I finished it' on the card when it is done.")
        if _asks_code(qid):
            # a person asking for a code (voice.py, 2026-10-27); a code typed in this chat goes straight in (2026-10-25)
            s = " ".join(str(step or arg).split()).rstrip(".")
            s = re.sub(r"^(?:Type|Enter|Give me|Paste) (?:in )?(?:the |a )?", "", s)
            return f"I need the {s[0].lower() + s[1:] if s else 'code'}. Reply with it here and I'll carry on; no code? say so and I'll sign in another way."
        # "with a screenshot" only when one is attached (the owner 2026-10-29: "You didn't leave a screenshot ... you just
        # said you did"); the card is in this chat, and that is where he wants everything (2026-10-30), so no
        # pointer to Mission Control
        shot = " (with a screenshot)" if _card_has_screenshot(qid) else ""
        return f"I got it to the last step and stopped: {step or arg} The card{shot} is in this chat; tap Approve and I will finish."
    if kind == "DOER_BLOCKED":
        # only an owner-only step is said (a password, a code, a tap, a payment); any other block files its build
        # by code and stays quiet until the ask comes back and works (run_ticket, 2026-10-30)
        return f"One step needs you{on}: {signin_line(arg) or portal(arg)}"
    # plain words, never JSON (the owner 2026-10-18): what it was on, and why it stopped, in one line. A job that
    # left milestone notes says how far it got instead of "nothing was changed" (notes.py, 2026-10-28)
    got = {}
    try:
        import notes
        got = notes.last(str((t or {}).get("id") or ""), by="turn") if t else {}
    except Exception:  # noqa: BLE001
        got = {}
    tail = f" It got as far as: {got['text']}" if got else " Nothing was changed as far as it reported."
    return f"I couldn't finish{on}: {plain_error(arg) if arg else 'it ended without a final line'}.{tail}"


# ------------------------------------------------------------------ a site's sign-in window
# the owner, 2026-10-08: the CRM signed out in this browser -> an on-screen sign-in window that stays up until he
# signs in. Which site, which script and which pid file: config.json -> doer.signin_window (the organization's
# own; absent = no window). It shares this profile and CDP port.
_SIGNIN = dict(CFG.get("signin_window") or {})
SIGNIN = (VAULT / str(_SIGNIN.get("script"))) if _SIGNIN.get("script") else None
SIGNIN_PID = ROOT / "data" / str(_SIGNIN.get("pid_file") or "signin_doer.pid.json")


def _signin_window_open() -> bool:
    try:
        from profile_lease import pid_alive
        return pid_alive(int((load_json(SIGNIN_PID, {}) or {}).get("pid") or 0))
    except Exception:  # noqa: BLE001
        return False


def _handoff(t: dict | None, since: float = 0.0) -> dict | None:
    """report.py's read-back of a hand-off checkpoint for this ticket (handoff.json), or None."""
    tid = str((t or {}).get("id") or "")
    f = OUT / tid / "handoff.json"
    if not tid or not f.is_file():
        return None
    d = load_json(f, {}) or {}
    return d if float(d.get("epoch") or 0) >= since else None


def _close_browser(t: dict | None = None, since: float = 0.0) -> None:
    """Quit the browser after a run, unless it is the sign-in window (that one closes itself), a hand-off
    left it on the owner's screen (the cart or the form lives in this session; 2026-10-28), or a page is held open
    for an owner at a step the agent could not finish (leftopen.py, 2026-11-08): then only this run's own tabs close."""
    if (_handoff(t, since) or {}).get("on_screen"):
        log(f"ticket {(t or {}).get('id')}: hand-off, browser left open on screen")
        return
    if _signin_window_open():
        return
    if browser.held_tabs():
        try:
            browser.prune_tabs(keep=0)             # the job's own tabs go; the held page stays, back in front
            for tab in browser.held_tabs():
                urllib.request.urlopen(browser.cdp_url() + "/json/activate/" + tab, timeout=5).read()
                break
        except Exception:  # noqa: BLE001
            pass
        log(f"ticket {(t or {}).get('id')}: a page is held open for an owner, browser left up")
        return
    browser.close_browser()


# ------------------------------------------------------------------ a step it cannot finish: left open (2026-11-08)
# The owner: "if sage gets to a step it can't complete in browser it should leave the browser open for the user to do
# it themself unless the user selects a button to do it later. Have control pc and do later buttons presented in chat."
def _leftopen():
    try:
        import leftopen
        return leftopen
    except Exception as e:  # noqa: BLE001
        log(f"left open unavailable: {type(e).__name__}: {str(e)[:120]}")
        return None


def _signin_site_block(arg: str) -> bool:
    """The configured site's own sign-in window handles this block (_signin_check), not a held page."""
    site = str(_SIGNIN.get("site") or "")
    return bool(site and SIGNIN and re.search(re.escape(site), arg or "", re.I) and re.search(r"sign|log\s?in|session|auth", arg or "", re.I))


def _leave_open(r: dict | None, out_dir: Path, login: dict | None = None) -> bool:
    """True when the run stopped at a step only the owner can take in the browser: the page stays for them."""
    lo = _leftopen()
    if not r or not lo or not lo.on() or r.get("marker") != "DOER_BLOCKED":
        return False
    arg = str(r.get("arg") or "")
    if STOPPED in arg.lower() or _live("stop_requested", out_dir) or "connector job" in arg.lower():
        return False
    if login and LOGIN_REFUSED.search(arg):         # the sign-in card comes back instead (_relogin)
        return False
    return _owner_only(arg) and not _signin_site_block(arg)


def same_page_block(tid: str) -> str:
    """The prompt block for a step that carries on after the owner passed a sign-in check: the page that asked is
    still open (browser.hold_wait), and it is the only page their tap or code answers."""
    w = browser.waiting(tid)
    if not w:
        return ""
    return ("\n## THE SAME PAGE IS STILL OPEN\nThe page you stopped on is still open in this browser, exactly as you left "
            f"it ({w.get('url', '')}). The owner's tap or code answers THAT page and no other.\n"
            "- First `browser_tabs`: select the tab with that address, then `browser_snapshot`. Do NOT `browser_navigate`, "
            "reload, go back, or start the sign-in again: each of those sends the owner a NEW prompt and throws their answer away.\n"
            "- If the page has already moved on (signed in, or the next step), carry on with the job from there.\n"
            "- If it still waits, wait ON it: `browser_wait_for` up to 60 seconds at a time, a snapshot between, for up to "
            "5 minutes. Never click 'resend', 'send again' or 'try again' on it.\n"
            "- Still waiting after that: stop with DOER_BLOCKED, say the page is still open and waiting on their answer, "
            "and leave the page as it is.\n"
            "- Once you are in, finish EVERY part of the job in this same session before your last line.\n")


def _held_note(tid: str) -> str:
    """The prompt line for a job that starts while another job's page is held open for an owner."""
    lo = _leftopen()
    if not (lo and lo.holding(tid)) and not browser.waiting(except_tid=tid):
        return ""
    return ("\n## A TAB IS HELD FOR THE OWNER\nAnother job left a page open in this browser for the owner to finish "
            "themself. Work only in the new blank tab that is in front (browser_tabs: select it if needed); never navigate, "
            "click, type in or close the other tab.\n")


def _fresh_tab() -> None:
    """A tab of the job's own in front, when another job's page is held open (the turn must not land on that one)."""
    if not browser.held_tabs():
        return
    try:
        req = urllib.request.Request(browser.cdp_url() + "/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(req, timeout=8) as resp:
            tab = json.loads(resp.read().decode("utf-8", "replace"))
        urllib.request.urlopen(browser.cdp_url() + "/json/activate/" + str(tab.get("id")), timeout=5).read()
    except Exception as e:  # noqa: BLE001
        log(f"fresh tab failed: {type(e).__name__}")


def _signin_check(arg: str) -> bool:
    """A run blocked on the configured site's sign-in: probe its session now (detached); signed out -> the window opens."""
    site = str(_SIGNIN.get("site") or "")
    if not (site and SIGNIN and re.search(re.escape(site), arg or "", re.I) and re.search(r"sign|log\s?in|session|auth", arg or "", re.I)):
        return False
    from alib import VENV_PY
    py = VENV_PY.with_name("pythonw.exe") if os.name == "nt" else VENV_PY
    flags = 0x08000000 | 0x00000200 if os.name == "nt" else 0   # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP; never DETACHED_PROCESS: a venv launcher loses its console and the real python opens a visible one
    try:
        subprocess.Popen([str(py if py.exists() else sys.executable), str(SIGNIN), "--check", "doer"], cwd=str(VAULT),
                         creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as e:
        log(f"hb sign-in check failed to start: {e}")
        return False
    return True


# ------------------------------------------------------------------ tasks
def run_ticket(t: dict, dry: bool = False, follow_up: str = "", login: dict | None = None) -> str:
    """Work one agent_run ticket end to end. Returns the final marker line (or an error).
    follow_up: a block that resumes the task's own session (a code card answered 'No code came').
    login: the sign-in card's payload when this run carries on after a login was handed over; a login the site
    then refuses brings the sign-in card back (_relogin) instead of ending the job."""
    tid = t["id"]
    if not follow_up and not dry:
        _live("clear_stopped", tid)                                      # a fresh claim of the job forgets an old Stop
    if not dry and _leftopen():
        _leftopen().clear(tid)                                           # the job runs again: its held page is the job's own
    if str((t.get("payload") or {}).get("where") or "").lower() == "computer":     # hands on the PC (computer.py)
        import computer
        return computer.run_ticket(t, dry, follow_up)
    out_dir = OUT / tid
    out_dir.mkdir(parents=True, exist_ok=True)
    sid = _session_get(tid) if follow_up else ""
    prompt = follow_up if sid else build_prompt(t, resume_block=standing_block(_standing(t)) + follow_up)
    if not dry:
        prompt += _held_note(tid)
    if dry:
        print(prompt)
        return "DRY_RUN_OK"
    set_status(t, "starting: opening the browser", "")
    if not _budget_ok():
        set_status(t, "not started: today's limit of browser tasks is used up")
        tickets._transition(tid, "staged", "doer", "budget: today's doer runs are used up; retry tomorrow", {"claimed", "new"})
        deliver(t, f"I have used today's limit of browser tasks, so {job_line(t) or 'this job'} waits. I will pick it up tomorrow.")
        return "BUDGET"
    try:
        browser.open_browser()
    except RuntimeError as e:
        tickets._transition(tid, "staged", "doer", f"browser: {e}"[:400], {"claimed", "new"})
        set_status(t, f"not started: the browser would not open: {e}")
        deliver(t, f"I could not open my browser for {job_line(t) or 'this job'}: {e}")
        return f"ERROR {e}"
    started = time.time()
    _fresh_tab()
    _live("start", t, "Opening the browser")
    set_status(t, ("signing in and carrying on" if follow_up.startswith("## SIGNED IN") else "switching to another sign-in method") if follow_up
               else "working in the browser", "starting")
    r = None
    keep = False
    try:
        r = run_turn(prompt, out_dir, resume=sid, timeout=int(CFG.get("turn_timeout_s", 900)))
        if not r.get("ok") and sid and "resume" in (r.get("error") or "").lower():
            r = run_turn(build_prompt(t, resume_block=follow_up), out_dir, timeout=int(CFG.get("turn_timeout_s", 900)))
        r = carry_on(t, r, out_dir, int(CFG.get("turn_timeout_s", 900)))     # a cut-off run goes on from its notes
        keep = _leave_open(r, out_dir, login)     # a step only the owner can take: the page stays up for them
    finally:
        if not keep:
            _close_browser(t, started)
    _session_set(tid, r.get("session"))
    (out_dir / "result.json").write_text(json.dumps(r, indent=1, ensure_ascii=False), encoding="utf-8")
    who = str((t.get("payload") or {}).get("from") or _first_owner())
    mk, arg = r.get("marker"), r.get("arg", "")
    if mk == "DOER_BLOCKED" and login and not dry and LOGIN_REFUSED.search(str(arg or "")):
        again, why = _relogin(t, login, str(arg or ""))
        if again:                                    # asked again on a fresh card: the job waits there, it has not ended
            mk, arg = "DOER_CHECKPOINT", f"{again} {REFUSED_STEP}: {why}"
    ev_ok, ev_n = True, 0
    if mk in ("DOER_DONE", "DOER_ANSWER"):
        try:
            import computer
            _, ev_ok, ev_n = computer.verify_evidence(out_dir)      # the browser lane's evidence.json, re-checked by code (2026-10-24)
        except Exception:  # noqa: BLE001
            pass
        tag = (f" [evidence: {ev_n} checked, {'ok' if ev_ok else 'MISMATCH'}]" if ev_n else " [no evidence file]") if mk == "DOER_DONE" else ""
        tickets._transition(tid, "done", "doer", f"{mk}: {arg}"[:REPORT_MAX + 20] + tag, tickets.OPEN, note_max=REPORT_MAX + 120)
    elif mk == "DOER_CHECKPOINT":
        qid = arg.split(" ", 1)[0]
        tickets._transition(tid, "staged", "doer", f"card {qid}: checkpoint, waiting for the owner's tap", {"claimed", "new"})
    elif mk == "DOER_BLOCKED" and (STOPPED in arg.lower() or _live("stop_requested", out_dir)):
        # the owner tapped Stop on the live card (livecard.py): the job ends where it is, nothing is retried or built
        arg = STOPPED
        tickets._transition(tid, "staged", "doer", "stopped by the owner from the live card", {"claimed", "new"})
        _live("close_followups", tid, str((_live("stop_requested", out_dir) or {}).get("by") or "owner"))
    elif mk == "DOER_BLOCKED" and "connector job" in arg.lower() and not _retried_refusal(t):
        # 2026-10-20: the model refused four jobs as "a connector job" that no connector covered, and nothing
        # re-routed them. The prompt no longer asks for that refusal; one that slips through is queued once more.
        tickets._transition(tid, "new", "doer", "wrong refusal (connector job): no connector covers this; queued once more", {"claimed", "new"})
    elif mk == "DOER_BLOCKED":
        tickets._transition(tid, "staged", "doer", f"blocked: {arg}"[:500], {"claimed", "new"})
    else:
        arg = r.get("error") or "the turn ended without a final line"
        if r.get("continued") and cut_off(r):
            arg = f"{cut_off(r)} {int(r['continued']) + 1} times over, even carrying on from its notes"
        tickets._transition(tid, "staged", "doer", f"no result: {arg}"[:500], {"claimed", "new"})
    set_status(t, _phase(mk or "", arg))
    _outcome_doer(tid, mk or "", ev_ok, ev_n, t, "browser")
    say = _say(who, mk or "", arg, t)
    offered = ""
    if keep:
        # ONE message in the job's thread, the page left open, Control PC + Do later (leftopen.py, 2026-11-08)
        offered = _leftopen().offer(t, say, arg) if _leftopen() else ""
        if not offered:
            _close_browser(t, started)            # nowhere to offer it: closed and said the old way
    if mk == "DOER_DONE":
        try:
            import computer
            say += computer.evidence_suffix(out_dir, mk)             # "Checked: ..." or "no evidence file ... a claim I could not check"
        except Exception:  # noqa: BLE001
            pass
        say += _after_standing(t)
    if mk == "DOER_BLOCKED" and arg == STOPPED:
        say = f"Stopped{' ' + job_line(t) if job_line(t) else ''}, as you asked. Nothing more was changed."
    elif mk == "DOER_BLOCKED" and _signin_check(arg):
        say += " " + str(_SIGNIN.get("say") or "If the site is signed out, I sign back in from my vault; a login I am missing comes to you as a card here.")
    elif mk == "DOER_BLOCKED" and not _owner_only(arg):
        # not the owner's step: the missing piece is built now and the ask comes back by itself; no words in chat
        bk = _gap_for(t, arg)
        log(f"ticket {tid}: blocked, build filed {bk or '(none)'}; nothing said in chat")
        say = ""
    elif mk not in ("DOER_DONE", "DOER_ANSWER", "DOER_CHECKPOINT", "DOER_BLOCKED"):
        bk = _gap_for(t, arg)
        log(f"ticket {tid}: no result, build filed {bk or '(none)'}; nothing said in chat")
        say = ""
    try:
        learn = _learn()                                 # the self-learning engine (zero-LLM): record, harvest lesson.json, score
        if mk in ("DOER_DONE", "DOER_ANSWER"):
            say += learn.after_run(t, r, "browser", started)
        else:
            learn.after_run(t, r, "browser", started)
    except Exception as e:  # noqa: BLE001
        log(f"learn failed: {type(e).__name__}")
    if offered:
        say = offered                              # the left-open message is the job's report; nothing more is said
    elif say:
        deliver(t, say)
    else:
        _mc_note(f"{job_line(t) or 'a job'}: {mk or 'no result'}: {arg[:300]} (building the missing piece; nothing said in chat)")
    # the report itself is the message just posted; only a quiet stop (nothing said in chat) leaves its reason on the card
    _live("finish", tid, mk or "", "" if (say or mk == "DOER_CHECKPOINT") else plain_error(arg), not say)
    log(f"ticket {tid}: {mk or 'NO_MARKER'} {arg[:160]}")
    _handback(t, mk or "", arg)
    return f"{mk or 'NO_MARKER'}: {arg}"


def _handback(t: dict, mk: str, arg: str) -> None:
    """The step ended: hand the ASK back to the desk (agent/pickup.py), which reads this ending against what the owner
    asked for and does the next deed (the card with the link in it, the next step, a check-back on an upload). Until
    2026-11-10 a step's end was the work's end: a link sat in a report for an hour until the owner asked for the text
    again, and "not uploaded yet" was never looked at twice. A checkpoint (the owner's tap comes next), a job the owner
    stopped and a job put back in the queue are not endings. Never raises."""
    try:
        if mk == "DOER_CHECKPOINT" or str(arg or "") == STOPPED:
            return
        now = tickets._load().get(t.get("id")) or {}
        if now.get("status") in ("new", "claimed"):
            return                                        # queued once more, or already running again
        keep_path = list(sys.path)
        try:
            sys.path.append(str(ROOT / "agent"))
            import pickup
            r = pickup.on_job_end(t, mk, str(arg or ""))
        finally:
            sys.path[:] = keep_path                       # agent/ has modules named like scripts/ ones (followups.py)
        log(f"ticket {t.get('id')}: hand-back {'started ' + str(r.get('job')) if r.get('ok') else 'not started (' + str(r.get('reason')) + ')'}")
    except Exception as e:  # noqa: BLE001
        log(f"ticket {t.get('id')}: hand-back failed: {type(e).__name__}: {str(e)[:120]}")


def _mc_note(text: str) -> None:
    """A line for Mission Control only (never Chat): the record of a quiet stop."""
    try:
        subprocess.run([sys.executable, str(ROOT / "scripts" / "notify.py")], input=text, capture_output=True, text=True,
                       encoding="utf-8", timeout=20, cwd=str(VAULT), creationflags=NO_WINDOW)
    except Exception:  # noqa: BLE001
        pass


def run_approved(payload: dict, dry: bool = False) -> int:
    """drain.py: the owner approved payload.step on a checkpoint card. Resume the doer's own
    session and perform exactly that. Prints DOER_RESULT=<line>; exit 0 only on DOER_DONE."""
    tid = str(payload.get("ticket") or "")
    t = tickets._load().get(tid)
    if not t:
        print("DOER_RESULT=REFUSED unknown ticket")
        return 1
    if _live("stopped", tid):
        # the owner stopped this job: its follow-up step closes quietly (exit 0, so drain re-cards nothing)
        log(f"ticket {tid}: approved step skipped, the owner stopped this job")
        print("DOER_RESULT=DOER_DONE: nothing done, the owner stopped this job")
        return 0
    if str((t.get("payload") or {}).get("where") or "").lower() == "computer":     # the computer lane's sha-bound step
        import computer
        return computer.run_approved(payload, dry)
    if payload.get("login"):
        return run_login_resume(t, payload, dry)
    step, resume = str(payload.get("step") or ""), str(payload.get("resume") or "")
    code = str(payload.get("code") or "")
    vals = payload.get("values") or {}
    block = ("## APPROVED STEP\nThe owner tapped Approve on your checkpoint card. Perform exactly this now, then verify "
             f"it on screen and finish with DOER_DONE (never another checkpoint for the same step):\n- Step: {step}\n"
             + (f"- Values: {json.dumps(vals, ensure_ascii=False)}\n" if vals else "")
             + f"- How: {resume}\n"
             + (f"- The owner's verification code is {code}: run `python assistant/doer/login_fill.py --url <the page url> --code {code}` "
                "(never type it yourself) and carry on.\n" if code else "")
             + "If the site now shows something different from the card (a new total, a changed field), do NOT proceed: "
             "stop with DOER_BLOCKED and say what changed.\n")
    block += same_page_block(tid)
    sid = _session_get(tid)
    prompt = block if sid else build_prompt(t, resume_block=block)
    if dry:
        print("DRY_RUN_OK\n" + prompt[:600])
        return 0
    try:
        browser.open_browser()
    except RuntimeError as e:
        print(f"DOER_RESULT=NOT_STARTED browser: {e}")
        return 1
    t_run = time.time()
    _live("start", t, f"Doing the approved step: {step[:120]}")
    set_status(t, f"working on the approved step: {step}", "starting")
    out_dir = OUT / tid
    r = None
    keep = False
    try:
        r = run_turn(prompt, out_dir, resume=sid, timeout=int(CFG.get("approved_timeout_s", 480)))
        if not r.get("ok") and sid and "resume" in (r.get("error") or "").lower():
            r = run_turn(build_prompt(t, resume_block=block), out_dir, timeout=int(CFG.get("approved_timeout_s", 480)))
        keep = _leave_open(r, out_dir)            # the approved step hit a step only the owner can take: left open
    finally:
        browser.release_wait(tid, before=t_run)   # the page it waited on is done with (a NEW wait made this run stays)
        if not keep:
            _close_browser()
    _session_set(tid, r.get("session"))
    (out_dir / "approved_result.json").write_text(json.dumps(r, indent=1, ensure_ascii=False), encoding="utf-8")
    mk, arg = r.get("marker"), r.get("arg", "")
    who = str((t.get("payload") or {}).get("from") or _first_owner())
    set_status(t, _phase(mk or "", arg or r.get("error", "")))
    if mk == "DOER_DONE":
        tickets._transition(tid, "done", "doer", f"approved step done: {arg}"[:500], tickets.OPEN)
        say = _say(who, mk, arg, t)
        deliver(t, say)
        _live("finish", tid, mk, say)
        _handback(t, mk, arg)
        print(f"DOER_RESULT=DOER_DONE: {arg}")
        return 0
    say = _say(who, mk or "", arg or r.get("error", ""), t)
    offered = (_leftopen().offer(t, say, arg) if _leftopen() else "") if keep else ""
    if keep and not offered:
        _close_browser()
    if offered:
        tickets._transition(tid, "staged", "doer", f"left open for the owner: {arg}"[:500], {"claimed", "new", "staged"})
    else:
        deliver(t, say)
    _live("finish", tid, mk or "", say)
    _handback(t, mk or "", arg or r.get("error", ""))
    print(f"DOER_RESULT={mk or 'NO_MARKER'}: {arg or r.get('error', '')}")
    return 1


# ------------------------------------------------------------------ the sign-in card (2026-11-03)
# A login the vault does not have used to end the job ("no login saved for <host>"). Now report.py login posts ONE card
# in the job's thread: "Enter login" opens the vault dialog (the fields go to the vault, never into chat) and, saved,
# answers the card; "I'll sign in myself" puts the browser on the owner's screen (Chrome Remote Desktop shows it from a
# phone) and a Carry on card follows; "Stop" ends the job. Either approval mints the usual agent_run exec ticket with
# payload.login, and drain hands it here: the job carries on from its notes, in the same session.
LOGIN_RESUME = ("## SIGNED IN: CARRY ON\n{how} Go back to {url} (or where the job was), run EXACTLY "
                "`python assistant/doer/login_fill.py --url <the page url>` once if a sign-in form is still showing (it prints "
                "SIGNED_IN or ALREADY when the session is good), then carry on with the job from your LAST milestone note. "
                "Checkpoints, notes and the final line work as usual. If the site still refuses the login, stop with "
                "`DOER_BLOCKED: {host} still refuses the sign-in: <what the page says>`.\n")


def login_block(payload: dict) -> str:
    host, url = str(payload.get("host") or "the site"), str(payload.get("url") or "the sign-in page")
    how = (f"The owner signed in to {host} by hand in your browser and tapped Carry on." if payload.get("self") else
           f"The owner saved the {host} login in the vault.")
    return LOGIN_RESUME.format(how=how, url=url, host=host)


# A login the owner handed over and the site then refused (2026-11-04: a saved login came back "The username or
# password is incorrect"; the job ended there, with no way to enter it again and a false "unverified" alarm).
LOGIN_REFUSED = re.compile(r"refuses the sign-in|no login saved|wrong password|login is wrong|(?:password|login|email|username)[^.]{0,40}incorrect", re.I)
REFUSED_STEP = "the site refused that login"


def _relogin(t: dict, login: dict, arg: str) -> tuple:
    """The sign-in card again, in the job's thread, carrying what the site said -> (card id, what it said) or ('', '')."""
    try:
        import report
        why = re.sub(r"^.*?refuses the sign-in:?\s*", "", " ".join(str(arg or "").split()), flags=re.I).strip() or "the site refused it"
        line = report.login(t["id"], str(login.get("host") or ""), str(login.get("url") or ""), why[:220])
        if line.startswith("LOGIN="):
            return line.split("=", 1)[1].strip(), why[:220]
        log(f"ticket {t['id']}: the sign-in card did not come back: {line[:160]}")
    except Exception as e:  # noqa: BLE001
        log(f"ticket {t.get('id')}: the sign-in card did not come back: {type(e).__name__}: {str(e)[:120]}")
    return "", ""


def run_login_resume(t: dict, payload: dict, dry: bool = False) -> int:
    """drain -> --approved with payload.login: the job itself carries on (run_ticket with the resume block). The
    approved step was the sign-in, so anything but a refused login counts as DOER_DONE for the exec ticket; the job's
    own outcome reaches the owner through run_ticket as always."""
    tid = t["id"]
    if not dry:
        tickets._transition(tid, "claimed", "doer", "login handed over: carrying on with the job", {"staged", "claimed", "new", "done"})
    line = run_ticket(t, dry, follow_up=login_block(payload), login=payload)
    refused = line.startswith("DOER_BLOCKED") and LOGIN_REFUSED.search(line)       # refused AND the card could not come back
    if not dry and line.startswith("DOER_CHECKPOINT") and REFUSED_STEP in line:
        print("DOER_RESULT=DOER_DONE: the site refused that login; the sign-in card is back in the job's thread")
        return 0
    if dry or (not refused and not line.startswith(("ERROR", "BUDGET", "BUSY"))):
        print(f"DOER_RESULT=DOER_DONE: carried on after the sign-in ({line[:200]})")
        return 0
    print(f"DOER_RESULT={line[:300]}")
    return 1


def _login_self_due():
    """A sign-in card answered "I'll sign in myself" (report.py SELF) and not yet acted on -> (ticket, qid, payload) or None.
    Found from the CARD (its payload names the job), not from the job's last history line: a job that ended on a refused
    login, whose card the owner then reopened with Undo, still answers this tap (2026-11-04)."""
    import cards
    from report import SELF
    jobs = tickets._load()
    for qid, e in (cards._answers_index() or {}).items():
        if not e or e.get("undone") or str(e.get("option") or "") != SELF or time.time() - float(e.get("epoch") or 0) > 86400:
            continue
        p = _login_card(qid)
        t = jobs.get(str((p or {}).get("ticket") or ""))
        if not p or not t or t.get("kind") != KIND or t.get("status") not in ("staged", "done"):
            continue
        if _live("stopped", t["id"]) or (OUT / t["id"] / f"self_{qid}.json").is_file():   # the owner stopped it, or done once already
            continue
        return t, qid, p
    return None


def self_signin(t: dict, qid: str, payload: dict) -> str:
    """The owner will sign in by hand: the browser goes on their screen at the sign-in page, and a Carry on card
    follows in the thread (with the Remote Desktop link for a phone). Once per card."""
    import report
    tid = t["id"]
    (OUT / tid).mkdir(parents=True, exist_ok=True)
    save_json(OUT / tid / f"self_{qid}.json", {"epoch": time.time()})
    ok, detail = False, ""
    try:
        browser.open_browser(show=True)
        ok, detail = browser.show_on_screen()
        url = str(payload.get("url") or "")
        if url:
            browser.navigate(url)
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {str(e)[:80]}"
    save_json(OUT / tid / "handoff.json", {"on_screen": bool(ok), "detail": detail, "epoch": time.time(), "qid": qid})
    r = report.carry_on_card(tid, payload, on_screen=(ok, detail))
    log(f"ticket {tid}: owner signs in by hand at {payload.get('host')}: on_screen={ok} {detail[:80]} card={r}")
    return r


def _quiet(now=None) -> bool:
    start, end = (load_json(HERE / "policy.json", {}) or {}).get("quiet_hours", [21, 7])
    h = (now or time.localtime()).tm_hour
    return h >= int(start) or h < int(end)


def _claimable_now(t: dict) -> bool:
    """A pre-approved client send (payload.standing) never goes out during quiet hours: it waits."""
    return not ((t.get("payload") or {}).get("standing") and _quiet())


# The standing actions (an owner's standing rule pre-approves ONE tier C step, no card) are the organization's own:
# each plugin (config.json -> plugins.modules) maps DOER_STANDING = {action: the registered source that mints such a
# ticket} and words the step (doer_standing_block) and what follows it (doer_after_standing). None here = no standing step.
def _plugins():
    try:
        import alib
        return alib
    except Exception:  # noqa: BLE001
        return None


def _standing_sources() -> dict:
    return _plugins().plugin_map("DOER_STANDING") if _plugins() else {}


def _after_standing(t: dict) -> str:
    """What follows a standing step that went out (this organization: the "I just sent our collection" text,
    client_texts.py, 2026-10-17). Returns a line for the owner's chat; never raises."""
    st = _standing(t)
    if not st or not _plugins():
        return ""
    return str(_plugins().plugin_call("doer_after_standing", st) or "")


def _standing(t: dict) -> dict:
    """payload.standing is honoured only on a ticket minted by the registered source for that
    action (id = make_id(source, file id)). Anything else gets the normal tiers and checkpoints."""
    st = (t.get("payload") or {}).get("standing") or {}
    src = _standing_sources().get(str(st.get("action") or ""))
    if not src or t.get("source") != src or t.get("id") != tickets.make_id(src, str(st.get("file_id") or "")):
        return {}
    return st


def standing_block(st: dict) -> str:
    """The ONE tier C step an owner pre-approved by standing rule (no card), worded by the plugin that owns the
    action. Narrow on purpose: anything on screen that does not match falls back to the normal checkpoint."""
    if not st or st.get("action") not in _standing_sources() or not _plugins():
        return ""
    return str(_plugins().plugin_call("doer_standing_block", st) or "")


def _code_fallback_due():
    """A task waiting on a code card that the owner answered 'No code came' or 'Use instead: <method>'
    (report.py), past the undo window. Returns (ticket, option) or None. cards.sweep closes a task
    whose card was answered, so a 'done' straight after the checkpoint counts too."""
    import cards
    from report import NO_CODE, USE_INSTEAD
    idx = cards._answers_index()
    for t in tickets._load().values():
        hist = t.get("history") or [{}]
        last = hist[-2] if len(hist) > 1 and hist[-1].get("by") == "cards.sweep" and hist[-1].get("to") == "done" else hist[-1]
        if t.get("kind") != KIND or t.get("status") not in ("staged", "done") or last.get("by") != "doer" or last.get("to") != "staged":
            continue
        if _live("stopped", t["id"]):                                    # the owner stopped it: nothing carries it on
            continue
        m = re.match(r"card (\S+): checkpoint", str(last.get("note") or ""))
        e = idx.get(m.group(1)) if m else None
        if not e or e.get("undone") or time.time() - float(e.get("epoch") or 0) < cards.UNDO_S:
            continue
        opt = str(e.get("option") or "")
        if opt == NO_CODE or opt.startswith(USE_INSTEAD):
            return t, opt
    return None


def fallback_block(opt: str) -> str:
    """The resume note when the code never arrived: switch method, the owner's pick or the doer's own."""
    from report import USE_INSTEAD
    method = opt[len(USE_INSTEAD):].strip() if opt.startswith(USE_INSTEAD) else ""
    how = (f"choose exactly this one: {method}." if method else
           "pick one yourself, in this order: an authenticator app, a code by email, approve on another signed-in "
           "device; never the same text message again.")
    return ("## NO CODE: SIGN IN ANOTHER WAY\n"
            f"The owner answered your code card '{opt}': the code did not arrive, so do not wait for it. Go back to the "
            "sign-in's code step (run login_fill.py again if the page moved on), open the site's 'Try another way' / "
            f"'Use another method' link and {how}\n"
            "Then carry on as usual: a new code is a new code CHECKPOINT naming where it was sent (with its other_methods); "
            "'approve on your other device' is a CHECKPOINT (ask_code false) whose step says where to approve. "
            "If the site offers no other way, stop with DOER_BLOCKED and say so.\n")


def tick(dry: bool = False) -> str:
    with _Single() as s:
        if not s.held:
            return "BUSY another doer is running"
        due = _code_fallback_due()
        if due and (dry or tickets._transition(due[0]["id"], "claimed", "doer", f"code card answered '{due[1]}': "
                                               "switching sign-in method", {"staged", "done"})):
            return run_ticket(due[0], dry, follow_up=fallback_block(due[1]))
        own = _login_self_due()
        if own and not dry:
            return "SELF_SIGNIN " + self_signin(*own)
        if not dry:
            try:                                    # a sign-in page nobody answered in time: let go of it
                if browser.expire_waits() and not browser.held_tabs():
                    browser.close_browser()
            except Exception as e:  # noqa: BLE001
                log(f"expire waits failed: {type(e).__name__}: {str(e)[:100]}")
        lo = _leftopen() if not dry else None
        if lo:
            try:
                did = lo.sweep(quiet=_quiet())      # a Do later that is due comes back with the same two buttons
            except Exception as e:  # noqa: BLE001
                did = [f"sweep failed: {type(e).__name__}: {str(e)[:100]}"]
            for line in did:
                log("left open: " + line)
            if any(x.endswith("brought back") for x in did):
                return "LEFT_OPEN " + "; ".join(did)
        got = tickets.claim(limit=1, by="doer", kind=KIND, where=_claimable_now)
        if not got:
            return "IDLE"
        return run_ticket(got[0], dry)


SCRATCH_STORE_VARS = ("ASSISTANT_DATA", "AGENT_DATA", "AGENT_STATE", "ASSISTANT_CARDS")


def scratch_store() -> str:
    """The name of the env var that points the data store at a scratch dir (a test), or ''."""
    return next((v for v in SCRATCH_STORE_VARS if os.environ.get(v)), "")


def wake() -> str:
    """Start a detached tick now (Sage calls this the moment it queues a task).

    Never from a scratch data store: a test that queues a fixture `do` into its temp tickets.json
    and wakes the doer hands that env to a detached process, which then works the FIXTURE in the
    real browser and reports to the owners' real Google Chat ('Rent a lens for Nov 7, the 35mm'
    posted 7 times, 2026-10-15/16, every time the builder ran test_agent.py)."""
    var = scratch_store()
    if var:
        return f"NOT_WOKEN {var} points at a scratch store (a test); nothing runs against it"
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    flags = 0x08000000 | 0x00000200 if os.name == "nt" else 0   # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP; never DETACHED_PROCESS: a venv launcher loses its console and the real python opens a visible one
    subprocess.Popen([str(pyw if pyw.exists() else exe), str(Path(__file__).resolve()), "--tick"], cwd=str(VAULT),
                     creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "WOKEN"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tick", action="store_true")
    ap.add_argument("--run", default="")
    ap.add_argument("--approved", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--wake", action="store_true")
    a = ap.parse_args()
    if a.approved:
        try:
            payload = json.loads(Path(a.approved).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"DOER_RESULT=REFUSED bad payload file ({type(e).__name__})")
            return 1
        return run_approved(payload, a.dry_run)
    if a.run:
        t = tickets._load().get(a.run)
        if not t or t.get("kind") != KIND:
            print("no such agent_run ticket")
            return 1
        tickets._transition(a.run, "claimed", "doer", "", {"new", "claimed", "staged"})
        print(run_ticket(t, a.dry_run))
        return 0
    if a.tick:
        print(tick(a.dry_run))
        return 0
    if a.wake:
        print(wake())
        return 0
    waiting = tickets.pending(kind=KIND)
    print(json.dumps({"waiting": waiting, "budget": load_json(BUDGET, {}), "browser": browser.status(),
                      "sessions": len(load_json(SESSIONS, {}) or {}), "log": str(LOG),
                      "live": STATUS.read_text(encoding="utf-8") if STATUS.is_file() else ""}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
