"""computer_policy.py - the code-enforced tiers for the doer's computer lane (assistant/doer/computer.py).

The browser doer decides its tier by reading prompt.md; here the tier is decided by THIS file,
which the headless turn cannot edit, running as a PreToolUse hook on every Bash, Write and Edit
call. the owner, 2026-10-11 ("full control of computer ... safe"): the model may do anything
read-only or reversible on this PC on its own, must stop at a checkpoint card before anything
irreversible, outward or system-changing, and can never reach a short list of things at all
(formatting a drive, deleting footage, credentials, the approval chain, the property business,
the registry, scheduled tasks, obfuscated code, a nested agent). Policy data: computer_policy.json.

  python assistant/doer/computer_policy.py --hook                 # stdin: the hook JSON; stdout: the decision
  python assistant/doer/computer_policy.py classify "<command>"   # prints TIER reason
  python assistant/doer/computer_policy.py scan <script.py>       # the static scan a tool script must pass

Approved step: when an owner taps Approve on a checkpoint card, drain -> doer.py --approved ->
computer.run_approved sets DOER_APPROVED_SHA (sha256 of the exact command, whitespace collapsed)
and DOER_APPROVED_USED (a marker path). That ONE command, once, is allowed at tier C. A NEVER
command is never unlocked. Anything the hook cannot parse is refused (fail closed).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                  # assistant/
VAULT = ROOT.parent
POLICY_FILE = Path(os.environ.get("DOER_COMPUTER_POLICY") or (HERE / "computer_policy.json"))
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "doer-computer-wv.log"

A, B, C, NEVER = "A", "B", "C", "NEVER"
_PATHISH = re.compile(r"""(?:"([^"]+)"|'([^']+)'|([A-Za-z]:[\\/][^\s"'<>|;&]*|~[\\/][^\s"'<>|;&]*|\.{1,2}[\\/][^\s"'<>|;&]*|[\w.-]+(?:[\\/][\w. -]+)+))""")
# a full interpreter path counts too ("C:/.../Python312/python.exe" editor/x.py): the script is scanned either way (2026-10-15)
_SCRIPT = re.compile(r"(?:^|[\s;&|(])\"?(?:[A-Za-z]:[\/][^\s\"']*[\/]|~[\/][^\s\"']*[\/])?(?:python[0-9.]*|py|pythonw)(?:\.exe)?\"?\s+(?:-[uBEsqI]+\s+)*(?:(-c)\s+(.+)|(\"[^\"]+\.pyw?\"|'[^']+\.pyw?'|\S+\.pyw?))", re.I)
_PS = re.compile(r"(?:^|[\s;&|(])(?:powershell|pwsh)(?:\.exe)?\s", re.I)


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


def norm(cmd: str) -> str:
    return " ".join(str(cmd or "").split())


def sha(cmd: str) -> str:
    return hashlib.sha256(norm(cmd).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ paths
def _resolve(tok: str) -> Path:
    s = tok.strip().strip("\"'")
    s = os.path.expandvars(s)
    if s.startswith("~"):
        s = str(Path.home()) + s[1:]
    p = Path(s)
    if not p.is_absolute():
        p = VAULT / p
    try:
        return Path(os.path.normpath(str(p)))
    except (OSError, ValueError):
        return p


def _under(p: Path, root: Path) -> bool:
    try:
        a, b = str(p).lower().rstrip("\\/"), str(root).lower().rstrip("\\/")
        return a == b or a.startswith(b + os.sep) or a.startswith(b + "/")
    except Exception:  # noqa: BLE001
        return False


def paths_in(text: str) -> list:
    out = []
    text = str(text or "").replace("\\ ", "\x00")          # a shell-escaped space stays inside its path
    for m in _PATHISH.finditer(text):
        tok = next(g for g in m.groups() if g).replace("\x00", " ")
        if re.fullmatch(r"[\d.:/-]+", tok) or "://" in tok or tok.startswith("-"):
            continue
        out.append(tok)
    return out


class Paths:
    def __init__(self, pol: dict):
        self.write_roots = [_resolve(r) for r in pol.get("write_roots", [])]
        self.footage_roots = [_resolve(r) for r in pol.get("footage_roots", [])]
        self.footage_exts = {e.lower().lstrip(".") for e in pol.get("footage_exts", [])}
        self.footage_markers = _rx(pol.get("footage_markers"))
        self.protected = [_resolve(r) for r in pol.get("vault_protected", [])]
        self.secret = _rx(pol.get("secret_paths"))

    def is_writable(self, tok: str) -> bool:
        p = _resolve(tok)
        return any(_under(p, r) for r in self.write_roots)

    def is_footage(self, tok: str) -> bool:
        p = _resolve(tok)
        if p.suffix.lower().lstrip(".") in self.footage_exts:
            return True
        if any(_under(p, r) for r in self.footage_roots):
            return True
        return any(rx.search(str(p)) or rx.search(tok) for rx in self.footage_markers)

    def is_protected(self, tok: str) -> bool:
        p = _resolve(tok)
        return any(_under(p, r) for r in self.protected)

    def is_secret(self, tok: str) -> bool:
        s = str(_resolve(tok)) + " " + tok
        return any(rx.search(s) for rx in self.secret)


# ------------------------------------------------------------------ classification
def _hit(rxs: list, text: str):
    for rx in rxs:
        m = rx.search(text)
        if m:
            return m.group(0)[:60]
    return ""


def scan_script(src: str, pol: dict | None = None) -> tuple:
    """Static scan of Python (or inline code / PowerShell text) the turn wants to run or save.
    -> (tier, reason). NEVER for forbidden imports / patterns, C for deletes, moves, installs,
    network sends, process kills, B otherwise (a script may write; its paths are checked when
    it runs by the command policy, and the prompt confines them to the write roots)."""
    pol = pol or _policy()
    text = str(src or "")
    for mod in pol.get("script_never_imports", []):
        if re.search(rf"^\s*(import\s+{re.escape(mod)}\b|from\s+{re.escape(mod)}\b)", text, re.M):
            return NEVER, f"the script imports {mod}"
    h = _hit(_rx(pol.get("never")), text)
    if h:
        return NEVER, f"never: {h}"
    h = _hit(_rx(list(pol.get("checkpoint_destructive") or []) + list(pol.get("checkpoint") or [])), text)
    if h:
        return C, f"needs a checkpoint: {h}"
    return B, "script passed the scan"


def classify(cmd: str, pol: dict | None = None) -> tuple:
    """-> (tier, reason) for one shell command. Fails closed on anything odd."""
    pol = pol or _policy()
    text = norm(cmd)
    if not text:
        return NEVER, "empty command"
    P = Paths(pol)
    low = text.lower()

    h = _hit(_rx(pol.get("never")), text)
    if h:
        return NEVER, f"never: {h}"
    # the milestone note (report.py note, 2026-10-28): its words are the turn's own line ("deleted 412 cache files")
    # and must not be judged as a command; the script itself only writes the job's notes file and posts a line
    m = re.match(r"^(?:\S*python(?:\.exe)?\s+)(\S+report\.py)\s+note\s", text, re.I)
    if m and _under(_resolve(m.group(1)), HERE):
        return A, "the milestone note"

    toks = paths_in(text)
    # bare names (".git", "Raw", "Memory") are paths too when a command deletes or moves
    bare = [w.strip("\"'") for w in text.split() if not w.startswith("-") and re.fullmatch(r"\.?[\w.-]+", w.strip("\"'") or "-")]
    for t in toks:
        if P.is_secret(t):
            return NEVER, f"touches a credential or another business's files: {t[:50]}"

    delete_rx = _rx(pol.get("checkpoint_destructive"))                      # delete + move families
    destructive = bool(_hit(delete_rx, text)) or bool(re.search(r"(^|[^>])>(?!>|&)\s*\S", text))
    for t in toks + bare:
        if destructive and P.is_footage(t):
            return NEVER, f"would delete, move or overwrite footage: {t[:60]} (the footage is sacred)"
        if _hit(delete_rx, text) and P.is_protected(t):
            return NEVER, f"would delete or move a protected vault path: {t[:60]}"

    # the script a command runs is scanned before the command is judged
    for m in _SCRIPT.finditer(text):
        if m.group(1):                                   # python -c "<code>"
            tier, why = scan_script(m.group(2), pol)
        else:
            sp = _resolve(m.group(3))
            # the doer's own scripts are not scanned: report.py and login_fill.py (always), and browser.py
            # (2026-10-30: it speaks CDP over a socket to bring the browser on screen; the scan read "import
            # socket" and refused every `--front`, so a hand-off window stayed hidden and the owner asked
            # three days running for a window he could see). Its arguments are still judged below.
            if sp.name in ("report.py", "login_fill.py", "browser.py") and _under(sp, HERE):
                continue
            if not sp.is_file():
                return NEVER, f"script not found, refusing to guess: {m.group(3)[:60]}"
            try:
                tier, why = scan_script(sp.read_text(encoding="utf-8", errors="replace"), pol)
            except OSError as e:
                return NEVER, f"cannot read the script ({type(e).__name__})"
            if not P.is_writable(str(sp)) and not any(_under(sp, VAULT / d) for d in ("scripts", "assistant", "editor")):
                tier = C if tier == B else tier
                why = f"a script outside the tool folders ({sp.name}); {why}"
        if tier == NEVER:
            return NEVER, why
        if tier == C:
            return C, why
    if _PS.search(text) or re.search(r"(^|[\s;&|(])cmd(\.exe)?\s+/c\b", low):
        tier, why = scan_script(text, pol)
        if tier != B:
            return tier, why

    h = _hit(_rx(list(pol.get("checkpoint_destructive") or []) + list(pol.get("checkpoint") or [])), text)
    if h:
        return C, f"needs a checkpoint: {h}"

    # writes land only inside the write roots
    writes = bool(_hit(_rx(pol.get("write_hints")), text))
    if re.search(r"(^|[^>])>{1,2}(?!&)", text):
        for t in re.findall(r">{1,2}\s*(\S+)", text):
            if t.startswith("/dev/") or t.lower() == "nul":
                continue
            if not P.is_writable(t):
                return C, f"writes outside the write roots: {t[:60]}"
    if re.search(r"(^|[\s;&|(])(cp|copy|xcopy|robocopy|mkdir|md|touch)\s", low) or re.search(r"\b(copy|new)-item\b|\b(set|add)-content\b|\bout-file\b", low):
        dests = [t for t in toks]
        dest = dests[-1] if dests else ""
        if dest and not P.is_writable(dest):
            if P.is_footage(dest):
                return C, f"writes into a footage folder: {dest[:60]}"
            return C, f"writes outside the write roots: {dest[:60]}"
    return (B if writes else A), ("writes inside the write roots" if writes else "read-only")


def check_path_write(path: str, content: str = "", pol: dict | None = None) -> tuple:
    """Write/Edit tool: the file must sit in a write root, never footage or a secret; the
    content must pass the scan (a tool script with a NEVER pattern is not even saved)."""
    pol = pol or _policy()
    P = Paths(pol)
    if not path:
        return NEVER, "no path"
    if P.is_secret(path):
        return NEVER, "that is a credential file"
    if P.is_footage(path):
        return NEVER, "footage is never written by the assistant"
    if not P.is_writable(path):
        return C, f"files are written only under {', '.join(pol.get('write_roots', []))}"
    if content:
        if len(content.splitlines()) > int(pol.get("max_tool_lines", 400)) and _under(_resolve(path), HERE / "tools"):
            return C, f"a tool is at most {pol.get('max_tool_lines', 400)} lines; split it"
        tier, why = scan_script(content, pol)
        if tier == NEVER:
            return NEVER, why
    return B, "ok"


# ------------------------------------------------------------------ the hook
def _approved(cmd: str) -> bool:
    want = os.environ.get("DOER_APPROVED_SHA", "")
    used = os.environ.get("DOER_APPROVED_USED", "")
    if not want or sha(cmd) != want:
        return False
    if used:
        p = Path(used)
        if p.exists():
            return False
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + " " + norm(cmd)[:300], encoding="utf-8")
        except OSError:
            return False
    return True


def decide(evt: dict) -> tuple:
    """-> (tier, allowed: bool, reason) for one PreToolUse event."""
    tool = str(evt.get("tool_name") or "")
    inp = evt.get("tool_input") or {}
    pol = _policy()
    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        tier, why = classify(cmd, pol)
        if tier == NEVER:
            return tier, False, f"Refused, no card can unlock this: {why}. Say DOER_BLOCKED and name the rule."
        if tier == C:
            if _approved(cmd):
                return tier, True, "approved by the owner's tap on the checkpoint card (one run)"
            return tier, False, (f"Tier C ({why}). Do not retry. Write the checkpoint JSON with this exact command in "
                                 "\"command\", run report.py checkpoint, and end with DOER_CHECKPOINT.")
        return tier, True, why
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = str(inp.get("file_path") or inp.get("notebook_path") or "")
        content = str(inp.get("content") or inp.get("new_string") or "")
        tier, why = check_path_write(path, content, pol)
        if tier in (NEVER, C):
            return tier, False, f"Refused: {why}."
        return tier, True, why
    return A, True, "not a policed tool"


def hook() -> int:
    try:
        raw = sys.stdin.read()
        evt = json.loads(raw or "{}")
        tier, ok, why = decide(evt)
    except Exception as e:  # noqa: BLE001  fail closed
        tier, ok, why = NEVER, False, f"policy error ({type(e).__name__}); refusing"
        evt = {}
    tool = str(evt.get("tool_name") or "?")
    what = json.dumps(evt.get("tool_input") or {}, ensure_ascii=False)[:300]
    log(f"{os.environ.get('DOER_COMPUTER_TICKET', '-')} {tool} {tier} {'ALLOW' if ok else 'DENY'} {why[:120]} :: {what}")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "allow" if ok else "deny",
                                             "permissionDecisionReason": why}}))
    return 0


def main() -> int:
    a = sys.argv[1:]
    if a[:1] == ["--hook"]:
        return hook()
    if a[:1] == ["classify"] and len(a) > 1:
        tier, why = classify(" ".join(a[1:]))
        print(f"{tier} {why}")
        return 0
    if a[:1] == ["scan"] and len(a) > 1:
        tier, why = scan_script(Path(a[1]).read_text(encoding="utf-8", errors="replace"))
        print(f"{tier} {why}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
