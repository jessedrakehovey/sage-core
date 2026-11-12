"""desk_policy.py - the code-enforced belt of the desk (assistant/agent/desk.py).

The desk is a full Claude Code turn with write tools, started for a WORK ask an owner typed in chat.
It may read anything, edit code, write notes and post cards. What it may never do is decided HERE,
in a PreToolUse hook on every Bash, Write and Edit call of the turn (passed with --settings, outside
the model's reach), never by the prompt:

  * run a send path or the executor (mail, texts, the CRM composer, drain, exec tickets): a message
    leaves only on an owner's tap on a card;
  * answer, undo or reopen a card for the owner;
  * touch credentials, tokens, the doer's vault or the identity files;
  * push, reset or mass-stage git; register scheduled tasks; format or wipe anything;
  * drive a browser (the doer is the one gate target allowed to act in a browser);
  * start a nested agent, or edit its own belt, the gates, the harness or live data.

Anything the hook cannot parse is refused (fail closed). Policy data: desk_policy.json (patterns only,
no organization names). The protected paths come from builder.py so the two lists cannot drift.

  python assistant/agent/desk_policy.py --hook                 # stdin: the hook JSON; stdout: the decision
  python assistant/agent/desk_policy.py classify "<command>"   # prints ALLOW/DENY and why
  python assistant/agent/desk_policy.py path <file>            # the write decision for a path
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # assistant/agent
ROOT = HERE.parent                              # assistant/
VAULT = ROOT.parent
POLICY_FILE = Path(os.environ.get("AGENT_DESK_POLICY") or (HERE / "desk_policy.json"))
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "desk-wv.log"

WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
# a redirect or a write cmdlet in a shell line: the command writes a file
_WRITES = re.compile(r"(?<![<>])>{1,2}(?!&)|\b(?:Set-Content|Add-Content|Out-File|New-Item|Copy-Item|Move-Item|Remove-Item|"
                     r"tee|cp|mv|rm|del|erase|copy|move|ren|rename)\b|\bopen\([^)]*['\"][wa]", re.I)


def _policy() -> dict:
    return json.loads(POLICY_FILE.read_text(encoding="utf-8"))


def _rx(items) -> list:
    return [re.compile(p, re.I) for p in items or []]


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def norm(s: str) -> str:
    return " ".join(str(s or "").replace("\\", "/").split())


# ------------------------------------------------------------------ the owner's paths (builder.py is the source)
def owner_paths() -> list:
    """Paths only an owner's tap may change: builder.TAP_PATHS + HARD_PATHS, plus the desk's own belt."""
    out = []
    try:
        sys.path.insert(0, str(HERE))
        import builder
        out = list(builder.TAP_PATHS) + list(builder.HARD_PATHS)
    except Exception:  # noqa: BLE001  fail closed: the policy file's own list
        out = list(_policy().get("owner_paths_fallback") or [])
    out += list(_policy().get("owner_paths_extra") or [])
    return out


def _rel(path: str) -> str:
    """A path as the organization's folder sees it ('assistant/agent/x.py'); '' when outside it."""
    s = norm(path).strip("\"'")
    try:
        p = Path(s)
        if not p.is_absolute():
            p = VAULT / p
        return p.resolve().relative_to(VAULT.resolve()).as_posix()
    except (ValueError, OSError):
        return ""


def path_decision(path: str) -> tuple[bool, str]:
    """(allowed, why) for writing `path` with Write/Edit (or a shell write)."""
    rel = _rel(path)
    s = norm(path)
    if not rel:
        # outside the organization's folder: only the temp folders and the log folder
        if re.search(r"/(?:Temp|tmp)/|/\.claude/(?:[\w-]+-wv\.log|wv-)|^/?tmp/", s, re.I):
            return True, "scratch"
        return False, "outside the organization's folder"
    low = rel.lower()
    for p in owner_paths():
        q = p.replace("\\", "/").lower()
        if low == q or low.startswith(q if q.endswith("/") else q + "/") or (not q.endswith("/") and low == q):
            return False, f"an owner's path ({p}): say what would change and post a decide card"
    for rx in _rx(_policy().get("never_write")):
        if rx.search(rel):
            return False, f"never written by the desk ({rx.pattern})"
    return True, "ok"


# ------------------------------------------------------------------ shell lines
def command_decision(cmd: str) -> tuple[bool, str]:
    c = norm(cmd)
    if not c:
        return False, "empty command"
    pol = _policy()
    for rx in _rx(pol.get("never_run")):
        if rx.search(c):
            return False, f"never run by the desk ({rx.pattern})"
    # a sensitive file named in a line that writes: refused; named in a read (cat, grep, python x.py pending) it passes
    for rx in _rx(pol.get("sensitive_paths")):
        if rx.search(c) and _WRITES.search(c):
            return False, f"writes a sensitive file ({rx.pattern})"
    # a file written by redirect / cmdlet inside the owner's paths
    for m in re.finditer(r"(?:>{1,2}|Set-Content|Out-File|Add-Content)\s*\"?([^\s\"';|&]+)", c, re.I):
        ok, why = path_decision(m.group(1))
        if not ok:
            return False, why
    return True, "ok"


def egress_decision(text: str) -> tuple[bool, str]:
    """Every URL in a fetch or a shell line against the desk's egress list (assistant/doer/egress.py, upgrade 14):
    in allow_list mode a host not named is refused; learn mode notes it. Fail open on an egress error."""
    try:
        sys.path.insert(0, str(ROOT / "doer"))
        import egress
        for u in egress.urls_in(text):
            ok, why = egress.allowed(u, "desk")
            if not ok:
                return False, why
    except Exception:  # noqa: BLE001
        return True, "egress check unavailable"
    return True, "egress ok"


def decide(evt: dict) -> tuple[bool, str]:
    tool = str(evt.get("tool_name") or "")
    inp = evt.get("tool_input") or {}
    if tool in ("WebFetch", "WebSearch"):
        return egress_decision(json.dumps(inp))
    if tool in ("Bash", "PowerShell"):
        ok, why = command_decision(str(inp.get("command") or ""))
        if not ok:
            return ok, why
        return egress_decision(str(inp.get("command") or ""))
    if tool in WRITE_TOOLS:
        p = str(inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or "")
        if not p:
            return False, "no path"
        return path_decision(p)
    return True, "not a shell or write tool"


def hook() -> int:
    try:
        raw = sys.stdin.read()
        evt = json.loads(raw or "{}")
        ok, why = decide(evt)
    except Exception as e:  # noqa: BLE001  fail closed
        ok, why, evt = False, f"policy error ({type(e).__name__}); refusing", {}
    tool = str(evt.get("tool_name") or "?")
    what = json.dumps(evt.get("tool_input") or {}, ensure_ascii=False)[:300]
    log(f"{os.environ.get('AGENT_DESK_JOB', '-')} {tool} {'ALLOW' if ok else 'DENY'} {why[:120]} :: {what}")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "allow" if ok else "deny",
                                             "permissionDecisionReason": why}}))
    return 0


def main() -> int:
    a = sys.argv[1:]
    if a[:1] == ["--hook"]:
        return hook()
    if a[:1] == ["classify"] and len(a) > 1:
        ok, why = command_decision(" ".join(a[1:]))
        print(f"{'ALLOW' if ok else 'DENY'} {why}")
        return 0
    if a[:1] == ["path"] and len(a) > 1:
        ok, why = path_decision(a[1])
        print(f"{'ALLOW' if ok else 'DENY'} {why}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
