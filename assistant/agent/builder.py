"""builder.py - Sage's build requests become real code, without waiting for an owner. Built 2026-10-07.

Sage cannot change code (brain.py is read-only by design), so when feedback needs a code change it
writes a wiki build note (rule r7) or its nightly review says "needs a code change". Until today
nothing read those: they sat as drafts. the owner, 2026-10-07: "I would like Sage to self improve
without my approvals." This is the hands for that, with the protected core still out of reach.

  run = collect open requests -> fresh git worktree off HEAD (branch sage-build)
        -> baseline the test suite -> ONE headless model turn per request (Opus, edit tools +
        tests only, scratch data dirs) -> verify -> commit -> fast-forward master -> note to owners

Verify (all must hold, or the attempt is thrown away and retried once more on a later run):
  * the live checkout did not change while the model ran (tamper canary)
  * no changed path is in PROTECTED_PATHS, nothing is deleted, the diff is under MAX_DIFF_LINES
  * every changed .py compiles; every test file that passed before still passes
  * scripts/guardrails/check_gates.py passes in the worktree (and again in the pre-commit hook)
A request that needs a protected file or an owner's decision is put in front of the owner as a
CARD in chat (ask_owner: "Build it, I approve" / "Build it another way" / "Drop it"), never built
on its own and never left to stall (the owner 2026-10-18: "everything needs to be accessible through
Sage google chat so these decisions or approvals get through"). The tap comes back through
cards.sweep -> owner_answer: an approved request may touch exactly the files it was refused for
(HARD_PATHS never), "another way" re-runs it with those files forbidden, and the builder is kicked
at once. Files the test run itself regenerates in the worktree (the doer's tool scores) are never
counted as the build's changes.
Master is only fast-forwarded: a merge that would touch a file with uncommitted edits waits.
Every build is one commit; the owners' note carries `git revert <hash>` for each.

  python assistant/agent/builder.py status           # open requests + recent results
  python assistant/agent/builder.py kick             # Day Spine: start a detached run if there is work
  python assistant/agent/builder.py run [--max 2] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import account  # noqa: E402
import core  # noqa: E402

CLAUDE = os.path.expandvars(r"%APPDATA%\npm\claude.cmd")
# check_gates.py parses these two shapes (target "agent-builder"). Edit/Write land only in the
# worktree (the tamper canary throws the run away otherwise); Bash is limited to compile/diff and
# the test files, added per run from TEST_GLOB below.
ALLOWED = ",".join(["Read", "Glob", "Grep", "Edit", "Write",
                    "Bash(python -m py_compile:*)", "Bash(git diff:*)", "Bash(git status:*)"])
DENIED = ",".join(["PowerShell", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent",
                   "mcp__honeybook__take_action", "mcp__honeybook__send_feedback",
                   "mcp__gmail__send_email", "mcp__gmail__delete_email",
                   "mcp__example-mail-personal__send_email", "mcp__example-mail-films__send_email",
                   "mcp__playwright__browser_click", "mcp__playwright-cli__browser_click"])

# The protected core in file form: sending, approvals, money, credentials, footage, gates, the
# agent's own guardrails. A request that needs one of these is an owner's job (needs_owner).
PROTECTED_PATHS = [
    "scripts/guardrails/", ".claude/", ".git", "CLAUDE.md", "scripts/card_ingest/",
    "assistant/config.json", "assistant/org.json", "assistant/agent/core.py", "assistant/agent/brain.py",
    "assistant/agent/account.py", "assistant/agent/builder.py", "assistant/agent/people.json",
    "assistant/agent/profile.json", "assistant/scripts/email_send.py", "assistant/scripts/exec_store.py",
    "assistant/scripts/drain.py", "assistant/scripts/cards.py", "assistant/scripts/hb_exec.py",
    "assistant/scripts/collection_autosend.py", "assistant/scripts/draft_lint.py",
    "assistant/scripts/rulings.py", "assistant/scripts/phone_bridge.py", "assistant/scripts/mail.py",
    "assistant/scripts/setup_creds.py", "assistant/data/", "assistant/doer/policy.json",
    "assistant/doer/vault.py", "assistant/doer/login_fill.py", "assistant/prompts/operator.md",
    "assistant/doer/computer.py", "assistant/doer/computer_policy.py", "assistant/doer/computer_policy.json",
    "assistant/doer/computer_prompt.md", "assistant/doer/tools/", "assistant/doer/learn.py",
    "assistant/scripts/operator_cron.ps1", "Memory/", "Schema/", "Raw/",
]
# Never buildable, even with an owner's tap: the gates and their tripwire, the harness, the builder
# itself (it cannot rewrite itself mid-run), live data, raw sources.
HARD_PATHS = ["scripts/guardrails/", ".claude/", ".git", "assistant/agent/builder.py", "assistant/data/", "Raw/"]
BUILD_OK, BUILD_OTHER, BUILD_DROP = "Build it, I approve (Recommended)", "Build it another way", "Drop it"
MAX_DIFF_LINES = 2000      # was 600 until 2026-10-23: four owner-asked builds of 1400-1800 lines (tests included) died on it
FLAKY_RERUNS = 1           # a suite that fails after the change is run once more before the build is blamed for it
MAX_ATTEMPTS = 2
RUNS_PER_DAY = 4
MIN_GAP_MIN = 60
TEST_GLOB = "assistant/tests/test_*.py"
TURN_TIMEOUT_S = 45 * 60
TEST_TIMEOUT_S = 300

STATE = core.DATA / "builds.json"
LOCK = core.DATA / "builder.lock"
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "builder-wv.log"
WT = Path(os.environ.get("BUILDER_WT") or (Path.home() / ".claude" / "wv-builder" / "wt"))
BRANCH = "sage-build"
MARK = "<!-- builder:"
_BUILD_NOTE = re.compile(r"(?im)^\s*(?:#+\s*)?build request\b")
_HAND_BUILT = re.compile(r"(?im)^\s*(?:>\s*)?(?:\*\*)?(?:built|done|fixed) by hand\b")
MERGE_RETRIES = 3          # master moves every few minutes (notes, nightly state): rebase again before giving up (2026-10-27)
_CODE_ASK = re.compile(r"(?i)\b(?:needs a code change|code change|system change|build request)\b")
_NOT_FILED = re.compile(r"(?i)^\s*build request\s*\((?:protected core, )?not filed\)\s*:\s*")


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg
    print(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def git(*args, cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=str(cwd or core.VAULT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=core.NO_WINDOW)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:300]}")
    return r


# ------------------------------------------------------------------ requests
def collect(state: dict | None = None) -> list:
    """Open requests: wiki build notes without a builder mark, and Pending dream-asks that say a
    code change is needed. Each -> {"key", "title", "source", "text"}. Skips what is finished,
    waiting on an owner, or out of attempts."""
    state = state if state is not None else core.load(STATE, {})
    out = []
    wiki = core.VAULT / "Wiki"
    for p in sorted(wiki.rglob("*.md")) if wiki.exists() else []:
        rel = p.relative_to(core.VAULT).as_posix()
        if "/_meta/" in rel:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if MARK in text or not _BUILD_NOTE.search(text):
            continue
        if _HAND_BUILT.search(text):
            continue          # done by hand in a session (2026-10-26: one such request was rebuilt twice and never landed)
        out.append({"key": rel, "title": p.stem, "source": "wiki", "text": text[:6000]})
    for key, body in _pending_blocks():
        if key.startswith("sage-") and "-ask-" in key and _CODE_ASK.search(body):
            prop = re.sub(r"\s+", " ", re.sub(r"(?s).*?\*Proposal:\*\s*", "", body, count=1))
            # "Build request (protected core, not filed): <title>: <what>" was the dream parking a request
            # it would not file; those are requests like any other now (the card is the gate)
            title = _NOT_FILED.sub("", prop)[:80]
            out.append({"key": key, "title": title, "source": "pending", "text": body[:4000]})
    open_ = []
    for r in out:
        s = state.get(r["key"]) or {}
        if s.get("status") in ("built", "needs_owner", "not_code", "asked", "dropped"):
            continue
        if s.get("status") == "waiting" and s.get("commit") and time.time() - _stamp_epoch(s.get("waiting_since") or s.get("last")) < LAND_WINDOW_H * 3600:
            continue          # built and waiting to land: the reflex cherry-picks it (land); no rebuild inside the window
        if int(s.get("attempts", 0)) >= MAX_ATTEMPTS:
            continue
        open_.append(r)
    return open_


LAND_WINDOW_H = 24


def _stamp_epoch(s) -> float:
    try:
        return time.mktime(time.strptime(str(s or "")[:16], "%Y-%m-%d %H:%M"))
    except ValueError:
        return 0.0


def mark(key: str, line: str = "") -> bool:
    """A request done by hand in a session: the note gets the builder's mark so it is never built again
    (python assistant/agent/builder.py mark <wiki path> ["what was done"])."""
    p = core.VAULT / key
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return False
    if MARK in text:
        return True
    stamp = f"\n\n{MARK} built by hand {time.strftime('%Y-%m-%d')} -->\n> Built by hand {time.strftime('%Y-%m-%d')}: {line or 'done in a Claude session'}\n"
    p.write_text(text.rstrip() + stamp, encoding="utf-8")
    def _st(st):
        st.setdefault(key, {"title": p.stem, "attempts": 0})
        st[key].update(status="built", summary=line or "built by hand", commit=st[key].get("commit") or "hand")
        return ""
    edit_state(_st)
    return True


def land(key: str) -> dict:
    """Land a built-but-unmerged build (status waiting, a commit sha) on today's master WITHOUT a model turn:
    cherry-pick the commit in a fresh worktree, run the test files it touched, fast-forward master. A conflict
    marks the row 'conflict' (the attempt stays counted; a later run rebuilds on the new code, MAX_ATTEMPTS in all).
    -> {"ok", "status", "why"}."""
    st = core.load(STATE, {})
    s = st.get(key) or {}
    sha = str(s.get("commit") or "").strip()
    if not sha or s.get("status") not in ("waiting", "retry", "conflict"):
        return {"ok": False, "status": str(s.get("status") or "none"), "why": "nothing to land"}
    if running():
        return {"ok": False, "status": "busy", "why": "the builder is running"}
    if git("branch", "--show-current").stdout.strip() != "master":
        return {"ok": False, "status": "waiting", "why": "the live checkout is not on master"}
    if git("cat-file", "-e", sha + "^{commit}").returncode != 0:
        _set(key, status="failed", summary=f"commit {sha} is gone; rebuild")
        return {"ok": False, "status": "failed", "why": f"commit {sha} is gone"}
    wt = fresh_worktree()
    cp = git("cherry-pick", "-x", sha, cwd=wt)
    if cp.returncode != 0:
        git("cherry-pick", "--abort", cwd=wt)
        files = [ln.split("\t")[-1] for ln in git("diff", "--name-only", "--diff-filter=U", cwd=wt).stdout.splitlines()]
        _set(key, status="conflict", summary="conflicts with today's master: " + ", ".join(files)[:160] or "conflict")
        log(f"BUILDER land conflict on {key}: {', '.join(files)[:160]}")
        return {"ok": False, "status": "conflict", "why": "conflicts with today's master: " + ", ".join(files)[:160]}
    touched = [p for p in git("diff", "--name-only", "HEAD~1", "HEAD", cwd=wt).stdout.splitlines() if p.strip()]
    env = sandbox_env()
    for p in touched:
        if p.endswith(".py"):
            c = subprocess.run([sys.executable, "-m", "py_compile", str(wt / p)], capture_output=True, text=True, timeout=60, creationflags=core.NO_WINDOW)
            if c.returncode != 0:
                _set(key, status="conflict", summary=f"{p} does not compile on today's master")
                return {"ok": False, "status": "conflict", "why": f"{p} does not compile on today's master"}
    tests = [p for p in touched if p.startswith("assistant/tests/test_") and p.endswith(".py")]
    res = run_tests(wt, env, only=tuple(tests)) if tests else {}
    if res and not all(res.values()):
        bad = [t for t, okk in res.items() if not okk]
        _set(key, status="conflict", summary="its own tests fail on today's master: " + ", ".join(bad)[:160])
        return {"ok": False, "status": "conflict", "why": "its own tests fail on today's master: " + ", ".join(bad)[:160]}
    ff = git("merge", "--ff-only", "-q", BRANCH)
    if ff.returncode != 0:
        why = "uncommitted edits in the live checkout overlap: " + (ff.stderr or ff.stdout).strip()[-200:]
        _set(key, status="waiting", summary=why)
        return {"ok": False, "status": "waiting", "why": why}
    new_sha = git("rev-parse", "--short", "HEAD").stdout.strip()
    _set(key, status="built", summary=str(s.get("summary") or "landed by the reflex").replace("master moved and the build no longer applies cleanly", "landed by cherry-pick"), commit=new_sha)
    try:
        import capability
        capability.on_built(key)
    except Exception as e:  # noqa: BLE001
        log(f"BUILDER replay schedule failed for {key}: {type(e).__name__}")
    if any(p.startswith("assistant/agent/") for p in touched):
        core.DATA.mkdir(parents=True, exist_ok=True)
        (core.DATA / "bridge.stop").write_text("stop", encoding="utf-8")
    log(f"BUILDER landed {key} by cherry-pick -> {new_sha}")
    return {"ok": True, "status": "built", "why": f"landed as {new_sha}"}


def _set(key: str, **fields) -> None:
    def _st(st):
        st.setdefault(key, {"title": Path(key).stem, "attempts": 0}).update(fields)
        return ""
    edit_state(_st)


def _pending_blocks() -> list:
    try:
        text = core.SUGGESTIONS.read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r"(?ms)^## Pending[ \t]*$(.*?)(?=^## |\Z)", text)
    if not m:
        return []
    return [(b.group(1), b.group(0)) for b in re.finditer(r"(?m)^- key: (\S+)\n(?:  .*\n?)*", m.group(1))]


def _close_pending(key: str, line: str) -> None:
    """A built dream-ask leaves ## Pending and is recorded under ## Applied automatically."""
    try:
        text = core.SUGGESTIONS.read_text(encoding="utf-8")
        new = re.sub(r"(?m)^- key: " + re.escape(key) + r"\n(?:  .*\n?)*", "", text, count=1)
        if new != text:
            core.SUGGESTIONS.write_text(new, encoding="utf-8")
    except OSError:
        return
    core.log_suggestion("## Applied automatically", key + "-built", [line])


# ------------------------------------------------------------------ verification pieces
def protected_hits(paths: list) -> list:
    hits = []
    for p in paths:
        q = p.replace("\\", "/")
        while q.startswith("./"):                          # not lstrip("./"): that eats the dot of .claude/
            q = q[2:]
        if any(q == x.rstrip("/") or q.startswith(x if x.endswith("/") else x + "/") or q == x
               for x in PROTECTED_PATHS):
            hits.append(q)
    return hits


def hard_hits(paths: list) -> list:
    return [p for p in protected_hits(paths) if any(p == x.rstrip("/") or p.startswith(x if x.endswith("/") else x + "/")
                                                    for x in HARD_PATHS)]


def fence(paths: list, allow=(), ignore=()) -> tuple:
    """(protected hits the owner has NOT approved, hard hits). `allow` = paths the owner approved by tap
    for this request ("*" = any protected path but the hard ones); `ignore` = paths the test run itself
    regenerates in the worktree (never the build's doing)."""
    paths = [p for p in paths if p not in set(ignore)]
    hits = protected_hits(paths)
    hard = hard_hits(paths)
    if "*" in allow:
        hits = hard
    else:
        hits = [h for h in hits if h not in set(allow)] + [h for h in hard if h in set(allow)]
    return sorted(set(hits)), hard


def changes(wt: Path) -> list:
    """[(status, path)] for every change in the worktree, untracked files included."""
    out = []
    for ln in git("status", "--porcelain", "--untracked-files=all", cwd=wt).stdout.splitlines():
        if len(ln) > 3:
            path = ln[3:].strip().strip('"')
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            out.append((ln[:2].strip() or "?", path))
    return out


def diff_lines(wt: Path) -> int:
    n = 0
    git("add", "-A", "--intent-to-add", cwd=wt)
    for ln in git("diff", "--numstat", cwd=wt).stdout.splitlines():
        a, d, *_ = (ln.split("\t") + ["0", "0"])[:3]
        n += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    return n


def run_tests(wt: Path, env: dict, only=()) -> dict:
    """{test file: passed?} for every test file in the worktree (`only`: just these relative paths)."""
    res = {}
    # Tests start their own python/git children. Under pythonw (no console) each child would pop
    # its own window; python.exe + NO_WINDOW gives the test one hidden console they all share.
    exe = Path(sys.executable)
    con = exe.with_name("python.exe")
    py = str(con if exe.name.lower() == "pythonw.exe" and con.exists() else exe)
    for t in sorted(wt.glob(TEST_GLOB)):
        if only and t.relative_to(wt).as_posix() not in set(only):
            continue
        try:
            r = subprocess.run([py, str(t)], cwd=str(wt), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=TEST_TIMEOUT_S, env=env,
                               creationflags=core.NO_WINDOW)
            res[t.relative_to(wt).as_posix()] = r.returncode == 0
        except subprocess.TimeoutExpired:
            res[t.relative_to(wt).as_posix()] = False
    return res


def sandbox_env(base: dict | None = None) -> dict:
    """Anything the model or a test runs sees scratch state, never the live cards, data or logs."""
    env = dict(base or os.environ)
    tmp = Path(tempfile.mkdtemp(prefix="wv_builder_"))
    env.update({"AGENT_DATA": str(tmp / "agent_data"), "AGENT_STATE": str(tmp / "agent_state"),
                "AGENT_SUGGESTIONS": str(tmp / "skill-suggestions.md"),
                "ASSISTANT_DATA": str(tmp / "adata"), "ASSISTANT_CARDS": str(tmp / "cards"),
                "WV_LOG_DIR": str(tmp / "logs"), "CHAT_PUSH": "off", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


_ASK_LINE = re.compile(r"\*\*Ask \([^)]*\):\*\*\s*\"(.+?)\"", re.S)
_STOP = set("the a an and or of to in on for with my our me us is are be it this that at by from as into onto up down please "
            "can you could would their them they his her him she he we what when where who how into about just also then".split())


def ask_of(req: dict) -> str:
    """The owner's ask a capability-filed (or brain-filed) request carries, '' for an ordinary request."""
    m = _ASK_LINE.search(str((req or {}).get("text") or ""))
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def _stems(s: str) -> set:
    return {w[:6] for w in re.findall(r"[a-z0-9]+", str(s or "").lower()) if len(w) >= 4 and w not in _STOP}


def acceptance_ok(wt: Path, paths: list, ask: str) -> tuple[bool, str]:
    """A build for a real ask must come with a test that drives THAT ask (2026-10-25): a changed or added
    file under assistant/tests/ whose text shares at least two content stems with the ask. 'Compiles and
    passes' is not 'does the thing'; the capability loop replays the ask afterwards, and a build with no
    such test is the one that fails that replay."""
    if not ask:
        return True, ""
    want = _stems(ask)
    if len(want) < 2:
        return True, ""
    tests = [p for p in paths if re.match(r"assistant/tests/test_[^/]+\.py$", p.replace("\\", "/"))]
    for p in tests:
        try:
            have = _stems((wt / p).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if len(want & have) >= 2:
            return True, p
    return False, ("no test drives the ask: add or extend a file under assistant/tests/ that replays the owner's ask "
                   f"(\"{ask[:120]}\") through the deed path with a fake brain, then run it")


def verify(wt: Path, baseline: dict, env: dict, allow=(), ignore=(), ask: str = "") -> tuple[bool, str, list]:
    ch = [(s, p) for s, p in changes(wt) if p not in set(ignore)]
    paths = [p for _, p in ch]
    if not ch:
        return False, "no changes", []
    acc, why_acc = acceptance_ok(wt, paths, ask)
    if not acc:
        return False, why_acc, paths
    if any(s.startswith("D") for s, _ in ch):
        return False, "deletes a file: " + ", ".join(p for s, p in ch if s.startswith("D")), paths
    hits, hard = fence(paths, allow, ignore)
    if hits:
        return False, "touches the protected core: " + ", ".join(hits), paths
    n = diff_lines(wt)
    if n > MAX_DIFF_LINES:
        return False, f"diff too large ({n} lines > {MAX_DIFF_LINES})", paths
    for p in paths:
        if p.endswith(".py"):
            r = subprocess.run([sys.executable, "-m", "py_compile", str(wt / p)], capture_output=True, text=True,
                               creationflags=core.NO_WINDOW)
            if r.returncode != 0:
                return False, f"does not compile: {p}", paths
    after = run_tests(wt, env)
    broke = [t for t, ok in after.items() if baseline.get(t, False) and not ok]
    for _ in range(FLAKY_RERUNS if broke else 0):
        # 2026-10-23: test_phone_bridge failed on a second boundary under load and three builds were blamed
        # for it; a suite that passes on a quiet second run never was the build's fault
        again = run_tests(wt, env, only=broke)
        broke = [t for t in broke if not again.get(t, False)]
    if broke:
        return False, "breaks tests: " + ", ".join(broke), paths
    g = subprocess.run([sys.executable, str(wt / "scripts/guardrails/check_gates.py")], cwd=str(wt),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=core.NO_WINDOW)
    if g.returncode != 0:
        return False, "gate check failed: " + (g.stdout or "")[-300:], paths
    return True, "ok", paths


# ------------------------------------------------------------------ the model turn
PROMPT = """You are {agent}'s builder: you turn one build request into a small, working code change in this
repository (the assistant of {org}, a git worktree). Nobody will review it before it ships, so it must be
correct, minimal and tested. The owners asked for this; their feedback must become real change.{feedback}

Rules
- Read the code around the change first (and assistant/README.md, assistant/agent/README.md or
  assistant/doer/README.md for the part you touch). Match the surrounding style and comment density.
- Smallest change that fully does what the request asks. No refactors, no new dependencies.
- Hidden by default: anything you start runs in the background with no window unless a person must interact with
  it. A subprocess gets creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0); a long-lived process starts under
  pythonw; PowerShell gets -WindowStyle Hidden; a browser opens off-screen (show=False) and is brought on-screen
  only for the step that needs a person.
- Never edit these (the protected core; the change is thrown away if you do): {protected}
  If the request can only be done there, change nothing and answer needs_owner: the owner then gets a
  card in chat and may approve exactly those files; name them in your question.{owner}
- Otherwise DECIDE YOURSELF. The owners do not want to be asked (owner1 2026-10-07: "I don't want to have
  to approve anything, I want it automatic"). When the request leaves something open, pick the sensible
  default that matches how the system already works, build it, and state the assumption in one clause of
  your summary. needs_owner is only for the protected core above, never for a design choice.
- If the request is not a code change at all (a question, a calendar fix, a one-off), answer not_code.
- A request that carries an **Ask** and an **Acceptance** line came from a real ask the agent could not do
  (capability.py). The ask is the acceptance test: after your change, that exact ask must take a working
  route (a deed tool, an executor kind, a doer skill or tool, a connector action, a prompt rung), and code
  will replay it to the agent the moment this lands. Build the capability, not a message about it; if the
  ability exists and only the prompt or a rung was missing, fix the prompt (templates/prompt.generic.md and
  prompt.md together). The build is REJECTED unless a test file under assistant/tests/ (new or extended)
  drives that ask: the fake-brain pattern in assistant/tests/test_capability.py section 9 (bridge.handle with
  a fake `brain._run_claude` whose reply calls the deed tool, then assert the card / ticket / deed), with the
  ask's own words in it.
- When you change assistant code, add or extend a check in the matching assistant/tests/test_*.py and
  run it: python assistant/tests/<file>.py . Every test file that passes now must still pass.
- If behaviour a person would notice changes, update the README section that describes it, in a line or two.
- Do not delete files. Do not commit. Do not run anything except the test files, py_compile and git diff/status.

The request ({source}, key {key}):
---
{text}
---

When you are done, the LAST line of your reply must be exactly:
BUILD_RESULT {{"status": "built|needs_owner|not_code", "summary": "one plain sentence a non-programmer understands", "question": "for needs_owner: the one decision you need"}}"""


def model_turn(req: dict, wt: Path, env: dict) -> dict:
    """One build turn. A usage limit runs it once more on profile.json `claude_account_fallback`,
    from a clean worktree (whatever the limited turn half-wrote is thrown away first)."""
    res = None
    for acct in account.order():                         # the account with room first (reflex, 2026-10-27)
        if res is not None:
            log(f"BUILDER usage limit on {req.get('key')}; running it once more on account {acct}")
            reset(wt)
        res = _model_turn_on(req, wt, env, acct=acct)
        if not (res.get("status") == "error" and account.is_limit(res.get("summary"))):
            return res
        account.note_limit(acct, str(res.get("summary") or ""))
    return res or {"status": "error", "summary": "no account"}


def _model_turn_on(req: dict, wt: Path, env: dict, acct: str | None = None) -> dict:
    if not Path(CLAUDE).exists():
        return {"status": "error", "summary": "claude CLI not found"}
    tests = [f"Bash(python {t.relative_to(wt).as_posix()}:*)" for t in sorted(wt.glob(TEST_GLOB))]
    acct_env, note = account.env(acct)
    run_env = dict(env, WV_HEADLESS="1")
    for k in (account.VAR,):
        if acct_env.get(k):
            run_env[k] = acct_env[k]
    for k in account.METERED:
        run_env.pop(k, None)
    s = req.get("state") or {}
    owner = ""
    ok = [p for p in (s.get("owner_ok") or []) if p]
    if ok:
        owner = ("\n  THE OWNER APPROVED, by tap, changing these protected files for THIS request only: " + ", ".join(ok)
                 + ". You may edit them (never " + ", ".join(HARD_PATHS) + "). Build it; do not answer needs_owner for them.")
    avoid = [p for p in (s.get("avoid") or []) if p]
    if avoid:
        owner += ("\n  THE OWNER ASKED FOR ANOTHER WAY: do not touch " + ", ".join(avoid)
                  + ". Find a route that leaves those files alone; if there is none, answer needs_owner and say so.")
    feedback = ""
    if s.get("last_failure"):
        # 2026-10-25: a second attempt used to start blind and fail the same way; it now knows what stopped the first
        feedback = ("\n\nTHE PREVIOUS ATTEMPT AT THIS REQUEST FAILED: " + str(s["last_failure"])[:400]
                    + " Do not repeat it: take the smallest slice that works, keep the diff small, and run the suites you touch.")
    p = core.profile()
    prompt = PROMPT.format(protected=", ".join(PROTECTED_PATHS), source=req["source"], key=req["key"], text=req["text"], owner=owner,
                           agent=p.get("name", "the agent"), org=p.get("household") or p.get("name", "this organization"), feedback=feedback)
    cmd = [CLAUDE, "-p", "--model", core.profile().get("build_model", "opus"), "--output-format", "json",
           "--max-turns", "80", "--allowedTools", ",".join([ALLOWED] + tests), "--disallowedTools", DENIED]
    try:
        r = subprocess.run(cmd, input=prompt, cwd=str(wt), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=TURN_TIMEOUT_S, env=run_env, creationflags=core.NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"status": "error", "summary": "model turn timed out"}
    text = ""
    for ln in reversed((r.stdout or "").strip().splitlines() or [""]):
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict) and "result" in d:
            text = str(d.get("result") or "")
            if d.get("is_error"):
                return {"status": "error", "summary": text[:200]}
            break
    return parse_result(text) if text else {"status": "error", "summary": ((r.stderr or "") + (r.stdout or ""))[-200:] or f"exit {r.returncode}"}


def parse_result(text: str) -> dict:
    for ln in reversed(str(text).strip().splitlines()):
        m = re.match(r"\s*`?BUILD_RESULT\s*(\{.*\})", ln)
        if m:
            try:
                d = json.loads(m.group(1))
            except ValueError:
                break
            if d.get("status") in ("built", "needs_owner", "not_code"):
                return d
    return {"status": "error", "summary": "no BUILD_RESULT line"}


# ------------------------------------------------------------------ worktree
def fresh_worktree() -> Path:
    git("worktree", "prune")
    if WT.exists():
        git("worktree", "remove", "--force", str(WT))
        shutil.rmtree(WT, ignore_errors=True)
    WT.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "--force", "-B", BRANCH, str(WT), "HEAD", check=True)
    return WT


def reset(wt: Path) -> None:
    git("reset", "--hard", "-q", cwd=wt)
    git("clean", "-fdq", cwd=wt)


def live_fingerprint() -> str:
    return git("rev-parse", "HEAD").stdout.strip() + "\n" + git("status", "--porcelain").stdout


def mark_note(wt: Path, rel: str, line: str) -> None:
    p = wt / rel
    try:
        text = p.read_text(encoding="utf-8").rstrip("\n")
        p.write_text(text + f"\n\n{MARK} {line} -->\n> [!success] {line}\n", encoding="utf-8")
    except OSError:
        pass


# ------------------------------------------------------------------ the run
def save_state(state: dict, since: str = "", base: dict | None = None) -> None:
    """Write builds.json WITHOUT clobbering what an owner (or any other writer) decided meanwhile.

    2026-10-18 09:34: the owner tapped "Build it, I approve" on two cards; owner_answer wrote status=approved
    + owner_ok at 09:40, and the builder run that had started 09:23 (still building other requests) saved
    its whole in-memory state at the end and put both back to needs_owner with no owner_ok.
    2026-10-18 19:01: the same again, through the first fix: two taps one second apart, the first tap's
    kick started a run that loaded builds.json before the second tap saved, and core.stamp() is to the
    MINUTE, so the second tap's `answered` (19:01) was not "newer than the run's start" (19:01) and the run
    put that key back to asked. So, under core.locked, reload the file and per key:
      - a key this caller did not change (equal to `base`, its copy as loaded) keeps the disk's copy;
      - an entry on disk whose `answered` stamp is newer than this caller's copy of it wins (an owner tap
        this caller never saw; `since` no longer gates it, a same-minute tap counts too);
      - everything else is this caller's.
    `since` is kept for old callers and ignored."""
    with core.locked:
        disk = core.load(STATE, {})
        merged = dict(disk)
        for k, v in state.items():
            if base is not None and k in base and base[k] == v:
                continue                                    # untouched here: the disk's copy (or its removal) stands
            d = disk.get(k)
            if (isinstance(d, dict) and isinstance(v, dict)
                    and str(d.get("answered") or "") > str(v.get("answered") or "")):
                continue                                    # the owner's tap landed after we loaded: keep it
            merged[k] = v
        core.save(STATE, merged)
    state.clear()
    state.update(merged)
    if base is not None:                                    # a second save from the same run diffs against this one
        base.clear()
        base.update(json.loads(json.dumps(merged)))


def edit_state(fn):
    """Locked load-modify-save of builds.json for the short writers (owner taps, kick, the YouTube
    scout, pending): `fn(state)` changes the dict in place and returns anything; it is saved only when
    it changed. Never hold this across a model turn: the builder run uses save_state's merge."""
    with core.locked:
        st = core.load(STATE, {})
        before = json.dumps(st, sort_keys=True)
        out = fn(st)
        if json.dumps(st, sort_keys=True) != before:
            core.save(STATE, st)
    return out


def run(max_n: int = 2, dry: bool = False, first: str = "") -> dict:
    """`first`: the request key (wiki path) an owner just filed in chat; it is built in this run whatever
    its place in the sorted list (2026-10-23: the owner's example-handoff skill was third of three open
    requests, the run built the two older ones and told him "being built now" about one that waited)."""
    started = core.stamp()
    state = core.load(STATE, {})
    base = json.loads(json.dumps(state))                 # as loaded: save_state keeps every key this run leaves alone
    reqs = collect(state)
    if first:
        reqs.sort(key=lambda r: 0 if r["key"] == first else 1)
    reqs = reqs[:max_n]
    if not reqs:
        log("BUILDER nothing to build")
        return {"built": [], "other": []}
    if dry:
        for r in reqs:
            log(f"BUILDER would build: {r['key']}")
        return {"built": [], "other": [r["key"] for r in reqs]}
    wt = fresh_worktree()
    env = sandbox_env()
    baseline = run_tests(wt, env)
    # whatever the test run itself leaves in the worktree (the doer's tool scores, assistant/doer/tools/
    # meta.json, failed every build on 2026-10-17 as "touches the protected core") is not the build's
    generated = {p for _, p in changes(wt)}
    if generated:
        log("BUILDER ignoring files the tests regenerate: " + ", ".join(sorted(generated)))
    reset(wt)
    log(f"BUILDER baseline: {sum(baseline.values())}/{len(baseline)} test files pass")
    built, other = [], []
    for req in reqs:
        key = req["key"]
        s = state.setdefault(key, {"title": req["title"], "attempts": 0})
        s["attempts"] = int(s.get("attempts", 0)) + 1
        s["last"] = core.stamp()
        req["state"] = s
        before = live_fingerprint()
        res = model_turn(req, wt, sandbox_env())
        if live_fingerprint() != before and _live_touched_by(wt, before):
            reset(wt)
            # not the request's fault (2026-10-25: three sessions committing tripped it twice in an hour and a
            # request lost both attempts): the attempt is given back and it runs again on the next kick
            s.update(status="retry", summary="the live checkout changed during the build; thrown away, runs again",
                     attempts=max(0, int(s.get("attempts", 1)) - 1))
            log(f"BUILDER tamper canary tripped on {key}; stopping (attempt not counted)")
            save_state(state, started, base)
            break
        st = res.get("status")
        if st == "needs_owner":
            # the model wants a protected file or a decision: the owner decides on a card, in chat
            paths_now = [p for _, p in changes(wt) if p not in generated]
            hits = fence(paths_now)[0] or _named_paths(str(res.get("question") or "") + " " + str(res.get("summary") or ""))
            reset(wt)
            s.update(summary=res.get("summary", ""), question=res.get("question", ""))
            ask_owner(req, s, hits, str(res.get("question") or res.get("summary") or ""))
            other.append((req, res))
            log(f"BUILDER asked the owner: {key} :: {res.get('summary', '')}")
            continue
        if st == "not_code":
            reset(wt)
            s.update(status=st, summary=res.get("summary", ""), question=res.get("question", ""))
            other.append((req, res))
            log(f"BUILDER {st}: {key} :: {res.get('summary', '')}")
            continue
        if st != "built":
            reset(wt)
            why = str(res.get("summary", ""))
            if _TRANSIENT.search(why):
                # the account's usage limit or a timeout is not the request's fault: the attempt is given back
                # (2026-10-25; three requests had burned both attempts on "session limit" alone)
                s.update(status="retry", summary=why, attempts=max(0, int(s.get("attempts", 1)) - 1))
                log(f"BUILDER transient error on {key}, attempt not counted: {why[:120]}")
                continue
            s.update(status="error", summary=why, last_failure=why[:400])
            log(f"BUILDER error on {key}: {why}")
            _stuck(req, s)
            continue
        ok, why, paths = verify(wt, baseline, env, allow=s.get("owner_ok") or (), ignore=generated, ask=ask_of(req))
        if not ok:
            if why.startswith("touches the protected core"):
                hits = fence(paths, s.get("owner_ok") or (), generated)[0]
                reset(wt)
                s.update(summary=res.get("summary", ""))
                ask_owner(req, s, hits, str(res.get("summary") or ""))
                log(f"BUILDER asked the owner (protected files): {key} :: {', '.join(hits)}")
                continue
            reset(wt)
            s.update(status="failed", summary=why, last_failure=why[:400])
            log(f"BUILDER rejected {key}: {why}")
            _stuck(req, s)
            continue
        line = f"Built {time.strftime('%Y-%m-%d')} by Sage's builder: {res.get('summary', '')}"
        if req["source"] == "wiki":
            mark_note(wt, key, line)
            paths = sorted(set(paths + [key]))
        git("add", "--", *paths, cwd=wt)
        msg = f"builder: {req['title'][:60]}\n\n{res.get('summary', '')}\nRequest: {key}\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
        c = git("commit", "-q", "-m", msg, cwd=wt)
        if c.returncode != 0:
            reset(wt)
            s.update(status="failed", summary="commit refused: " + (c.stdout + c.stderr).strip()[-200:])
            log(f"BUILDER commit refused for {key}")
            continue
        sha = git("rev-parse", "--short", "HEAD", cwd=wt).stdout.strip()
        s.update(status="built", commit=sha, summary=res.get("summary", ""), files=paths)
        built.append((req, res, sha, paths))
        log(f"BUILDER built {key} -> {sha}")
    merged = merge(wt, built, state) if built else False
    save_state(state, started, base)
    note_owners(built if merged else [], other, state, merged or not built)
    return {"built": [b[0]["key"] for b in built] if merged else [], "other": [o[0]["key"] for o in other]}


_TRANSIENT = re.compile(r"session limit|usage limit|rate limit|timed out|timeout|claude CLI not found|exit 1$|overloaded", re.I)


def _stuck(req: dict, s: dict) -> None:
    """Out of attempts: the owner decides in chat (Try another way / Drop it) instead of the request dying in
    silence after a commitments line (the owner 2026-10-25: roadblocks must be solvable in chat)."""
    if int(s.get("attempts", 0)) < MAX_ATTEMPTS:
        return
    ask_owner(req, s, [], f"it failed {s.get('attempts')} times: {str(s.get('last_failure') or s.get('summary') or '')[:200]}", stuck=True)


def _live_touched_by(wt: Path, before: str) -> bool:
    """The live tree moves on its own (Sage, the spine, the dream). Only blame the build when a file
    the build also changed is now different in the live tree."""
    live = {ln[3:].strip() for ln in git("status", "--porcelain").stdout.splitlines()}
    was = {ln[3:].strip() for ln in before.split("\n", 1)[1].splitlines()} if "\n" in before else set()
    built = {p for _, p in changes(wt)}
    return bool((live - was) & built)


def merge(wt: Path, built: list, state: dict) -> bool:
    """Rebase the builds onto today's master and fast-forward it. Uncommitted edits in the live tree
    that overlap block the merge; the builds then wait (status built-waiting) for the next run."""
    on = git("branch", "--show-current").stdout.strip()
    rb = None
    if on == "master":
        # the live checkout commits every few minutes (wiki notes, nightly state): a rebase that fails because HEAD
        # moved during the model turn is tried again on the new HEAD, MERGE_RETRIES times, before the build waits
        for _ in range(MERGE_RETRIES):
            head = git("rev-parse", "HEAD").stdout.strip()
            rb = git("rebase", head, cwd=wt)
            if rb.returncode == 0:
                break
            git("rebase", "--abort", cwd=wt)
            if head == git("rev-parse", "HEAD").stdout.strip():
                break                                      # HEAD did not move: a real conflict, not a race
    if rb is None:
        why = f"the live checkout is on {on or 'a detached HEAD'}, not master"
    elif rb.returncode != 0:
        why = "master moved and the build no longer applies cleanly"
    else:
        ff = git("merge", "--ff-only", "-q", BRANCH)
        if ff.returncode != 0 and _wip_commit_overlap(ff.stderr + ff.stdout):
            # the live tree held stale uncommitted edits to a file the build also changed (two builds waited a
            # whole day on chat_push.py, 2026-10-25): those edits are now a commit of their own, so rebase once more
            head2 = git("rev-parse", "HEAD").stdout.strip()
            rb2 = git("rebase", head2, cwd=wt)
            if rb2.returncode != 0:
                git("rebase", "--abort", cwd=wt)
                for req, *_ in built:
                    state[req["key"]].update(status="retry", summary="master moved under it; rebuilt on the new code next run",
                                             attempts=0)
                log("BUILDER merge: local edits committed, the build needs a rebuild on the new code")
                return False
            ff = git("merge", "--ff-only", "-q", BRANCH)
        if ff.returncode == 0:
            for req, res, sha, paths in built:
                try:
                    import capability
                    capability.on_built(req["key"])             # the ask this build came from is replayed to the agent
                except Exception as e:  # noqa: BLE001
                    log(f"BUILDER replay schedule failed for {req['key']}: {type(e).__name__}")
                state[req["key"]]["commit"] = git("log", "-1", "--format=%h", "--grep", "Request: " + req["key"],
                                                  "--fixed-strings").stdout.strip() or sha
                if req["source"] == "pending":
                    _close_pending(req["key"], f"Built by Sage's builder ({state[req['key']]['commit']}): {res.get('summary', '')}")
            if any(p.startswith("assistant/agent/") for _, _, _, ps in built for p in ps):
                core.DATA.mkdir(parents=True, exist_ok=True)
                (core.DATA / "bridge.stop").write_text("stop", encoding="utf-8")   # the spine restarts it on new code
            log(f"BUILDER merged {len(built)} build(s) into master")
            return True
        why = "uncommitted edits in the live checkout overlap: " + (ff.stderr or ff.stdout).strip()[-200:]
    for req, *_ in built:
        # the attempt stays counted: the commit exists and the reflex lands it by cherry-pick (land); a rebuild is
        # only for a commit that cannot be landed (2026-10-26: two builds were rebuilt twice a day and never landed)
        state[req["key"]].update(status="waiting", summary=why, waiting_since=core.stamp())
    log(f"BUILDER merge waits: {why}")
    return False


STALE_EDIT_MIN = 120      # a live-tree edit untouched this long is nobody's work in progress


def _wip_commit_overlap(err: str) -> bool:
    """`git merge --ff-only` refused because uncommitted live edits overlap the build. When every such file is
    outside the protected core and has not been touched for STALE_EDIT_MIN, those edits become one commit
    ("wip (builder): ...") so the build can land; a fresh edit or a protected file keeps the build waiting.
    Nothing is discarded: the edits are in git, where they were going anyway."""
    files = [ln.strip() for ln in str(err or "").splitlines() if ln.startswith("\t") or ln.startswith("    ")]
    files = [f for f in files if f and (core.VAULT / f).exists()]
    if not files:
        return False
    if protected_hits(files):
        log("BUILDER merge waits: a protected file is being edited in the live tree: " + ", ".join(protected_hits(files)))
        return False
    now = time.time()
    fresh = [f for f in files if now - (core.VAULT / f).stat().st_mtime < STALE_EDIT_MIN * 60]
    if fresh:
        log("BUILDER merge waits: live edits are recent: " + ", ".join(fresh))
        return False
    git("add", "--", *files)
    c = git("commit", "-q", "-m", "wip (builder): uncommitted local edits to " + ", ".join(files)
            + "\n\nCommitted by the builder so a finished build could land; nothing in them was changed.", "--", *files)
    if c.returncode != 0:
        git("reset", "-q", "--", *files)
        log("BUILDER wip commit refused: " + (c.stdout + c.stderr).strip()[-160:])
        return False
    log("BUILDER committed stale live edits so the build can land: " + ", ".join(files))
    return True


def note_owners(built: list, other: list, state: dict, merged: bool) -> None:
    """One short note in chat, in Sage's voice (no commit ids: the owner 2026-10-07, nothing that reads
    like a status report); the full version with `git revert` lines goes to Mission Control."""
    done = [(res.get("summary") or req["title"]).rstrip(".") for req, res, sha, paths in built]
    asks = [f"{req['title']}: {res.get('question') or res.get('summary')}" for req, res in other
            if res.get("status") == "needs_owner" and not (state.get(req["key"]) or {}).get("card")]   # carded asks speak for themselves
    if not done and not asks:
        return
    chat = ""
    if done:
        chat = ("I built that thing you asked for: " if len(done) == 1 else "I built a couple of things you asked for: ") \
            + "; ".join(d[0].lower() + d[1:] for d in done) + ". It's live now; if anything feels off, tell me and I'll undo it."
    if asks:
        chat += ("\n\n" if chat else "") + "One I need you on before I can build it: " + " | ".join(asks)
    full = "\n".join([f"- {d} (undo: git revert {state[req['key']].get('commit') or sha})"
                      for (req, res, sha, paths), d in zip(built, done)] + [f"- needs an owner: {a}" for a in asks])
    # the owner 2026-10-08: no status messages in Chat. The line goes into Sage's transcript (so it can
    # say what was built when asked) and the full version to Mission Control; nothing is posted.
    core.say(core.home_space() or f"dm:{core.first_owner()}", core.profile().get("name", "Agent"), chat, channel="builder")
    try:
        subprocess.run([sys.executable, str(core.AGENT.parent / "scripts" / "notify.py")], input="Sage's builder:\n" + full, text=True,
                       encoding="utf-8", capture_output=True, timeout=60, creationflags=core.NO_WINDOW,
                       env={**os.environ, "CHAT_PUSH": "off"})
    except (OSError, subprocess.SubprocessError):
        pass


# ------------------------------------------------------------------ the owner's decision, as a card
_PATH_RE = re.compile(r"(?:assistant|scripts|\.claude)/[A-Za-z0-9_./-]+\.(?:py|json|md|ps1)|\.claude/[A-Za-z0-9_./-]+|CLAUDE\.md")


_BARE_RE = re.compile(r"\b([A-Za-z0-9_]+\.(?:py|json|ps1))\b")


def _named_paths(text: str) -> list:
    """Repo paths a builder question names: full ones, and bare file names ("phone_bridge.py") resolved
    against the protected list (the model often names files without their folders)."""
    t = str(text or "")
    out = {m.group(0) for m in _PATH_RE.finditer(t)}
    by_name = {x.rsplit("/", 1)[-1]: x for x in PROTECTED_PATHS if not x.endswith("/")}
    for m in _BARE_RE.finditer(t):
        if m.group(1) in by_name:
            out.add(by_name[m.group(1)])
    if re.search(r"(?i)\boperator prompt\b", t):
        out.add("assistant/prompts/operator.md")
    return sorted(out)


def _lane_for(req: dict) -> str:
    k = (req.get("key") or "") + " " + (req.get("title") or "")
    if "/Money/" in k or re.search(r"(?i)\b(bank|plaid|simplefin|relay|tax|money)\b", k):
        return "money"
    if "/Operations/" in k or re.search(r"(?i)\b(lead|couple|honeybook|wedding|client|shoot|film)\b", k):
        return "wedding"
    return "personal"


def ask_owner(req: dict, s: dict, hits: list, question: str, stuck: bool = False) -> str:
    """ONE card in chat for a build the builder may not finish on its own: the protected files it needs
    (or the model's one question). Buttons are things the system does on the tap (cards.NEEDS_ACTION):
    build it with those files approved, build it another way, or drop it. -> the card id ('' when it
    could not post; the state still says 'asked' so nothing re-runs blindly).
    `stuck` (2026-10-25): the request failed MAX_ATTEMPTS times; only another way / drop are offered."""
    hits = [h for h in hits if h]
    hard = [h for h in hits if h in hard_hits(hits)]
    s.update(status="asked", hits=hits, asked=core.stamp())
    title = re.sub(r"(?i)^build request\s*", "", str(req.get("title") or req.get("key") or "")).strip() or str(req.get("key"))
    short_q = re.sub(r"\s+", " ", question).strip()
    if len(short_q) > 170:
        short_q = short_q[:170].rsplit(" ", 1)[0] + "..."
    if stuck:
        q = f"I tried to build \"{title}\" and {short_q or 'it failed twice'}. "
    elif hits:
        q = (f"To build \"{title}\" I'd have to change {', '.join(hits)}, which only you may approve. "
             + ("I can't change " + ", ".join(hard) + " even with your OK (the gates and the harness are hand-only), so another way is the real option there. " if hard else ""))
    else:
        q = f"The builder stopped on \"{title}\" with a question for you: {short_q or 'it needs your go-ahead'} "
    q += "What do you want?"
    options = [{"label": BUILD_OK, "description": "the builder may change exactly those files for this request"},
               {"label": BUILD_OTHER, "description": "the builder tries again without touching them"},
               {"label": BUILD_DROP, "description": "forget this request"}]
    if (hard and set(hard) == set(hits)) or stuck:
        options = options[1:]
        options[0]["label"] += " (Recommended)"
    # urgent (2026-10-23): an owner-asked build that waits for Monday's digest is a build that did not happen
    # when it was needed ("build things right away when it solves an immediate problem")
    card = {"header": "Build stuck" if stuck else "Build needs your OK", "lane": _lane_for(req), "subject": req.get("key"), "build_key": req.get("key"),
            "question": q, "options": options, "urgent": True,
            "advice": ("💡 If you know what's missing, say it in this thread first; the next try will carry it." if stuck else
                       "💡 Approve and it builds within the hour, tested, with an undo. Nothing changes until you tap."),
            "details": (f"Request: {req.get('key')}\n\n" + re.sub(r"\s+", " ", str(req.get("text") or ""))[:1200]
                        + (f"\n\nBuilder: {str(s.get('summary') or '')[:400]}" if s.get("summary") else ""))}
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import cards
        r = cards.post([card], "builder", lane=card["lane"])
    except Exception as e:  # noqa: BLE001
        r = {"posted": False, "reason": f"{type(e).__name__}: {e}"}
    if r.get("posted"):
        s["card"] = r["qid"]
        return str(r["qid"])
    if r.get("qid"):                                   # DUPLICATE_SUBJECT: the earlier card is still up
        s["card"] = r["qid"]
        return str(r["qid"])
    log(f"BUILDER card for {req.get('key')} not posted: {r.get('reason')}")
    s["card_error"] = str(r.get("reason") or "")[:200]
    return ""


def owner_answer(key: str, option: str, by: str = "") -> dict:
    """The tap on a 'Build needs your OK' card (cards.sweep -> _act_on_build). Approve: the request may
    touch the files it was refused for (hard paths never); another way: those files are forbidden;
    drop: closed. An approved or redirected request runs on the next kick, which happens right away."""
    opt = re.sub(r"\s*\((?:recommended)[^)]*\)\s*$", "", str(option or ""), flags=re.I).strip().lower()

    def _answer(st: dict):
        # load-modify-save under core.locked: two taps a second apart (2026-10-18 19:01) never race
        s = st.get(key)
        if not isinstance(s, dict):
            return {"ok": False, "reason": "no such build request"}
        hits = [h for h in (s.get("hits") or []) if h]
        if opt.startswith("build it, i approve"):
            hard = hard_hits(hits)
            ok = [h for h in hits if h not in hard] or (["*"] if not hits else [])
            s.update(status="approved", owner_ok=sorted(set((s.get("owner_ok") or []) + ok)), attempts=0, answered=core.stamp(), by=by)
            core.log_change("build_approved", f"owner approved protected files for build {key}: {', '.join(ok) or 'any'}", {"op": "none"}, by or "owner")
            return {"ok": True, "status": "approved"}
        if opt.startswith("build it another way"):
            s.update(status="retry", avoid=sorted(set((s.get("avoid") or []) + hits)), attempts=0, answered=core.stamp(), by=by)
            if s.get("last_failure"):                        # a stuck build: the next try knows what stopped the last ones
                s["last_failure"] = "the owner asked for another way after: " + str(s["last_failure"])[:300]
            return {"ok": True, "status": "retry"}
        if opt.startswith("drop"):
            s.update(status="dropped", answered=core.stamp(), by=by)
            return {"ok": True, "status": "dropped"}
        return {"ok": False, "reason": f"unknown option {option!r}"}

    r = edit_state(_answer)
    if not r.get("ok"):
        return r
    what = r["status"]
    log(f"BUILDER owner {what} {key} ({option})")
    kicked = kick(force=True) if what != "dropped" else ""
    return {"ok": True, "status": what, "kick": kicked}


# ------------------------------------------------------------------ lock + kick
def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                             creationflags=core.NO_WINDOW).stdout
        return str(pid) in out
    except OSError:
        return False


def running() -> bool:
    d = core.load(LOCK, {})
    if d.get("starting") and not int(d.get("pid", 0) or 0):
        return time.time() - float(d.get("ts", 0)) < 120          # claimed by kick(); the process writes its pid in a moment
    return bool(d) and _pid_running(int(d.get("pid", 0))) and time.time() - float(d.get("ts", 0)) < 4 * 3600


def kick(force: bool = False, first: str = "") -> str:
    """`force` (an owner's tap on a build card, an owner's ask in chat): the hourly gap and the daily cap
    do not apply. `first`: that request's key, built first in the run it starts."""
    if running():
        return "BUILDER running"
    if not collect():
        return "BUILDER idle (nothing open)"
    day = time.strftime("%Y-%m-%d")

    def _count(st: dict) -> str:
        runs = st.get("_runs") or {}
        if not force and int(runs.get(day, 0)) >= RUNS_PER_DAY:
            return "BUILDER daily cap reached"
        if not force and time.time() - float(st.get("_last_start", 0)) < MIN_GAP_MIN * 60:
            return "BUILDER waiting (ran recently)"
        st["_runs"] = {day: int(runs.get(day, 0)) + 1}
        st["_last_start"] = time.time()
        return ""

    why = edit_state(_count)
    if why:
        return why
    # claim the lock BEFORE the process exists: two kicks in the same second (the reflex on three stalled
    # requests, 2026-10-27) must not both say "started" and spawn two runs on the same worktree
    core.DATA.mkdir(parents=True, exist_ok=True)
    core.save(LOCK, {"pid": 0, "ts": time.time(), "starting": True})
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(pyw if pyw.exists() else exe), str(Path(__file__).resolve()), "run"] + (["--first", first] if first else []),
                     cwd=str(core.VAULT), creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "BUILDER started"


def status() -> str:
    st = core.load(STATE, {})
    out = [f"open: {r['key']}" for r in collect(st)]
    out += [f"{v.get('status')}: {k} {v.get('commit', '')} {v.get('summary', '')}"[:200]
            for k, v in st.items() if not k.startswith("_")]
    return "\n".join(out) or "nothing"


def render(days: int = 7, max_rows: int = 12) -> str:
    """The builder as the brain sees it (2026-10-23, the owner: "I can't see how many runs it has already used
    today" came back as Sage's answer). Runs used today and the cap, whether a build is going now, and the
    recent requests with their state in plain words. Reads only; never raises."""
    try:
        st = core.load(STATE, {})
        day = time.strftime("%Y-%m-%d")
        used = int((st.get("_runs") or {}).get(day, 0))
        head = (f"BUILDS (code changes you file as a `note` with '## Build request'; an owner's ask in chat starts at once, "
                f"the nightly ones are capped): {used} of {RUNS_PER_DAY} scheduled runs used today; "
                + ("a build is running right now" if running() else "nothing building right now") + ".")
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        words = {"built": "built", "retry": "waiting for the next run", "approved": "approved, waiting for the next run",
                 "asked": "waiting for an owner's tap on its card", "needs_owner": "waiting for an owner's tap on its card",
                 "failed": "failed", "error": "failed", "not_code": "not a code change", "dropped": "dropped", "waiting": "built, waiting to land"}
        rows = []
        for k, v in st.items():
            if k.startswith("_") or not isinstance(v, dict):
                continue
            if str(v.get("last") or v.get("updated") or "")[:10] < cutoff and v.get("status") not in ("retry", "approved", "asked", "needs_owner"):
                continue
            title = k.rsplit("/", 1)[-1].replace("Build request ", "").replace(".md", "")[:70]
            line = f"- {title}: {words.get(str(v.get('status')), str(v.get('status')))}"
            if v.get("status") in ("failed", "error") and v.get("summary"):
                line += f" ({str(v['summary'])[:90]})"
            rows.append((str(v.get("last") or ""), line))
        rows.sort(reverse=True)
        return "\n".join([head] + [r[1] for r in rows[:max_rows]])
    except Exception:  # noqa: BLE001
        return ""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "kick", "status", "land", "mark"])
    ap.add_argument("key", nargs="?", default="", help="land/mark: the request key (wiki path)")
    ap.add_argument("note", nargs="?", default="", help="mark: what was done by hand")
    ap.add_argument("--max", type=int, default=2)
    ap.add_argument("--first", default="", help="request key (wiki path) to build first in this run")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "kick":
        print(kick())
    elif a.cmd == "status":
        print(status())
    elif a.cmd == "land":
        keys = [a.key] if a.key else [k for k, v in core.load(STATE, {}).items() if not k.startswith("_") and isinstance(v, dict) and v.get("status") == "waiting" and v.get("commit")]
        for k in keys:
            print(k, json.dumps(land(k)))
    elif a.cmd == "mark":
        print("marked" if mark(a.key, a.note) else "no such note")
    else:
        claim = core.load(LOCK, {})
        if running() and not a.dry_run and not (claim.get("starting") and not int(claim.get("pid", 0) or 0)):
            print("BUILDER already running")
            sys.exit(0)
        core.DATA.mkdir(parents=True, exist_ok=True)
        if not a.dry_run:
            core.save(LOCK, {"pid": os.getpid(), "ts": time.time()})
        try:
            r = run(a.max, a.dry_run, first=a.first)
            print(f"BUILDER built={len(r['built'])} other={len(r['other'])}")
        finally:
            if not a.dry_run:
                LOCK.unlink(missing_ok=True)
