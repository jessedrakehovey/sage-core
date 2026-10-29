#!/usr/bin/env python3
"""
check_gates.py - the send-gate tripwire for wedding videography.

Proves "instruction-following protects the core" is actually true by checking it
mechanically. The autonomous LLM cron jobs (dream_cron, optimizer) are constrained
by construction: dream_cron carries --dangerously-skip-permissions (write/commit is
the point) but no allowedTools; optimizer carries an explicit --allowedTools
read-only allowlist and no --dangerously-skip-permissions. This script freezes those
invariants and fails loudly if any of them is ever weakened.

Why it lives in scripts/guardrails/ : the 6:30 AM dreaming cron only patches
.claude/skills/, Schema/mistakes.md, and Memory/. It does NOT edit scripts/.
So the baseline here is outside its reach, and (once wired as a pre-commit hook)
a commit that weakens a gate cannot land - including a commit the dreaming run
makes itself.

Checks (per target, against scripts/guardrails/gate_baseline.json):
  A. CRITICAL never-allow  : no send/delete/UI-submit tool appears in any
                             allowlist. (absolute - not baseline-relative)
  B. Denylist floor        : every tool denied at freeze time is still denied.
                             (the denylist may only grow, never shrink)
  C. No skip-permissions   : dream_cron is DELIBERATELY excluded (it uses
                             --dangerously-skip-permissions to write/commit, which
                             is the whole point). optimizer_cron must NOT carry it.
  D. Send-gate hook intact : .claude/settings.json still gates mcp__gmail__send_email
                             behind WV_GATED_SEND, defaulting to "ask".

Usage:
  python scripts/guardrails/check_gates.py            # verify (exit 1 on any FAIL)
  python scripts/guardrails/check_gates.py --freeze   # (re)write the baseline from
                                                       # the CURRENT files, after a
                                                       # legitimate, reviewed change
"""
import json
import re
import sys
from pathlib import Path

VAULT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).resolve().parent / "gate_baseline.json"

# The autonomous LLM cron jobs for wedding videography.
#
# dream_cron.ps1: uses --dangerously-skip-permissions (writes + commits are the
#   point); no allowedTools/disallowedTools. Excluded from skip-permissions check
#   (C) because that flag is intentional. Included only for the send-gate hook check.
#
# optimizer_cron.ps1: STRICTLY read-only. Uses --allowedTools with Read,Grep,Glob,
#   WebSearch,WebFetch,firecrawl tools ONLY. Must never gain a send/UI tool and
#   must never carry --dangerously-skip-permissions.
#
# graphify_cron.ps1, yt_ingest_cron.ps1, omi_ingest/run.ps1:
#   confirmed zero-LLM (no claude -p calls); excluded from TARGETS.
# (telegram_bridge.ps1 removed 2026-10-04)
TARGETS = {
    "optimizer": ("scripts/optimizer/optimizer_cron.ps1", "ps1"),
    # assistant operator: reads untrusted email, so it runs on an explicit
    # --allowedTools list (Read/Glob/Grep/Skill, Write to assistant/out only,
    # Bash for tickets.py + mail.py fetch/draft) and must never gain a send tool
    # or --dangerously-skip-permissions.
    "assistant-operator": ("assistant/scripts/operator_cron.ps1", "ps1"),
    # travel / lots scouts: write files + commit, so like dream_cron they carry
    # --dangerously-skip-permissions (exempt from check C). They are still held to
    # check A (no send/UI tool may ever be allow-listed) and D.
    # hb_exec.py: the HoneyBook write transport. Exactly one tool (take_action) and a
    # fixed deny belt; it must never gain a send/UI tool.
    "hb-exec": ("assistant/scripts/hb_exec.py", "py"),
    # hb_snapshot.py: the HoneyBook READ transport (snapshot refresh from the Day Spine).
    # Exactly one tool, the read-only action; take_action itself is on its denylist.
    "hb-snapshot": ("assistant/scripts/hb_snapshot.py", "py"),
    "travel-scout": ("travel/scripts/travel_cron.ps1", "ps1"),
    "lot-scout": ("lots/scripts/lots_cron.ps1", "ps1"),
    # ads_cron.ps1: daily Ad Scout learning pass. Read-only tools + the read-only
    # HoneyBook action only; must never gain take_action, Bash, or a UI/send tool.
    "ad-scout": ("ads/ads_cron.ps1", "ps1"),
    # agent brain (assistant/agent, 2026-10-05): the conversational agent's model turn.
    # Read tools + the read-only HoneyBook action only. Everything it changes goes through
    # the SUGGEST line and zero-LLM writers; it must never gain a write, send or UI tool.
    "agent-brain": ("assistant/agent/brain.py", "py"),
    # the doer (assistant/doer, 2026-10-05): Sage's hands. The ONE target that may act in a
    # browser, and only through its own private MCP server (mcp__playwright-doer__*, a Chromium
    # on the wedding profile with a CDP port); the machine-wide playwright/playwright-cli tools
    # stay on CRITICAL_NEVER_ALLOW for every target including this one. Its belt: no mail, no
    # HoneyBook write, no web fetch, no evaluate/run_code/file_upload/network_request, Bash only
    # for login_fill.py (vault logins typed out of the model's sight), report.py checkpoint and
    # vault.py has; file writes only under assistant/out/doer. Irreversible steps end at a
    # checkpoint card (tier C in policy.json); drain runs the approved step through --approved.
    "doer": ("assistant/doer/doer.py", "py"),
    # the doer's computer lane (assistant/doer/computer.py, 2026-10-11): hands on the PC. Bash is
    # allow-listed on purpose and the belt is the computer_policy.py PreToolUse hook passed with
    # --settings (fail-closed tiers: A/B run, C refused until a sha-bound card tap, NEVER refused for
    # good). No MCP servers (--strict-mcp-config, empty), no PowerShell tool, no web, no nested
    # agents; Write/Edit only under assistant/out/doer and assistant/doer/tools. The deny floor
    # below keeps mail, HoneyBook write, every playwright click and the approval chain out.
    "doer-computer": ("assistant/doer/computer.py", "py"),
    # Sage's builder (assistant/agent/builder.py, 2026-10-07): turns build notes into code in a git
    # worktree with Edit/Write and Bash limited to py_compile, git diff/status and the test files.
    # No skip-permissions, no send/UI/mail/HoneyBook-write tool; its PROTECTED_PATHS keep this file,
    # the baseline, .claude/ and the send path out of its diffs.
    "agent-builder": ("assistant/agent/builder.py", "py"),
}

# dream_cron is checked for allow/deny but SKIPPED for the skip-permissions test
# because it deliberately carries --dangerously-skip-permissions.
SKIP_PERMS_EXEMPT = {"dream-cron", "travel-scout", "lot-scout"}

# Tools that must NEVER appear in any allowlist above. Drafting (draft_email) is
# intentionally allowed and is NOT on this list - drafts don't send.
CRITICAL_NEVER_ALLOW = [
    "mcp__gmail__send_email",
    "mcp__gmail__delete_email",
    "mcp__gmail__batch_delete_emails",
    # personal-side Gmail connectors (2026-10-01): same never-allow as the property one
    "mcp__example-mail-personal__send_email",
    "mcp__example-mail-personal__delete_email",
    "mcp__example-mail-personal__batch_delete_emails",
    "mcp__example-mail-films__send_email",
    "mcp__example-mail-films__delete_email",
    "mcp__example-mail-films__batch_delete_emails",
    "mcp__playwright__browser_click",
    "mcp__playwright__browser_type",
    "mcp__playwright__browser_press_key",
    "mcp__playwright__browser_file_upload",
    "mcp__playwright__browser_fill_form",
    "mcp__playwright-cli__browser_click",
    "mcp__playwright-cli__browser_type",
    "mcp__playwright-cli__browser_press_key",
    "mcp__playwright-cli__browser_file_upload",
    "mcp__playwright-cli__browser_fill_form",
]

SETTINGS = ".claude/settings.json"
TOKEN_RE = re.compile(r"[A-Za-z_][\w]*(?:__[\w]+)*")


def _tokens(fragment: str) -> set:
    """Pull tool-name tokens out of a captured code fragment. Handles both
    individually-quoted tokens ('a','b') and comma-joined strings ('a,b,c')."""
    out = set()
    for quoted in re.findall(r"""['"]([^'"]*)['"]""", fragment):
        for piece in quoted.split(","):
            piece = piece.strip()
            if TOKEN_RE.fullmatch(piece):
                out.add(piece)
    return out


def _canonical_deny(text: str, kind: str) -> set:
    """The MAIN belt-and-suspenders denylist (not inline pre-gate lists)."""
    if kind == "ps1":
        m = re.search(r"\$denied\s*=\s*@\((.*?)\)\s*-join", text, re.DOTALL)
    else:
        m = re.search(r"DENIED(?:_TOOLS)?\s*=\s*\",\"\.join\(\[(.*?)\]\)", text, re.DOTALL)
    return _tokens(m.group(1)) if m else set()


def _all_allow(text: str, kind: str) -> set:
    """Every allowlist token anywhere in the file (main + inline)."""
    out = set()
    if kind == "ps1":
        for m in re.finditer(r"\$allowed\s*=\s*@\((.*?)\)\s*-join", text, re.DOTALL):
            out |= _tokens(m.group(1))
        for m in re.finditer(r"--allowedTools\s+'([^']*)'", text):
            out |= _tokens("'" + m.group(1) + "'")
        # also capture bare variable assignment used in -ClaudeArgs
        for m in re.finditer(r"\$allowed\s*=\s*'([^']*)'", text):
            out |= _tokens("'" + m.group(1) + "'")
    else:
        for m in re.finditer(r"ALLOWED(?:_TOOLS)?\s*=\s*\",\"\.join\(\[(.*?)\]\)", text, re.DOTALL):
            out |= _tokens(m.group(1))
    return out


def _read(rel: str) -> str:
    return (VAULT / rel).read_text(encoding="utf-8")


def freeze() -> None:
    data = {
        "_comment": "Frozen send-gate baseline for wedding videography. Do not hand-edit "
                    "to weaken a gate. After a LEGITIMATE, reviewed change to an allow/deny "
                    "list, re-run with --freeze and commit the new baseline alongside it.",
        "critical_never_allow": CRITICAL_NEVER_ALLOW,
        "settings_gate": SETTINGS,
        "targets": {},
    }
    for name, (rel, kind) in TARGETS.items():
        text = _read(rel)
        data["targets"][name] = {
            "path": rel,
            "type": kind,
            "denied": sorted(_canonical_deny(text, kind)),
            "allowed_count": len(_all_allow(text, kind)),
        }
    BASELINE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Froze baseline for {len(TARGETS)} targets -> {BASELINE.relative_to(VAULT)}")


def verify() -> int:
    if not BASELINE.exists():
        print("FAIL: no baseline. Run with --freeze first.")
        return 1
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    fails, warns = [], []

    for name, (rel, kind) in TARGETS.items():
        text = _read(rel)
        allow = _all_allow(text, kind)
        deny = _canonical_deny(text, kind)

        # A. critical never allowed
        leaked = sorted(set(CRITICAL_NEVER_ALLOW) & allow)
        if leaked:
            fails.append(f"[{name}] {rel}: CRITICAL tool granted in allowlist: {', '.join(leaked)}")

        # B. denylist floor
        bdeny = set(base["targets"].get(name, {}).get("denied", []))
        dropped = sorted(bdeny - deny)
        if dropped:
            fails.append(f"[{name}] {rel}: denylist WEAKENED - no longer denies: {', '.join(dropped)}")

        # C. no skip-permissions (dream-cron is exempt - it deliberately uses it to write/commit).
        # Match the real ARGUMENT form (quoted, as passed in a ClaudeArgs array / arg list),
        # not the bare mention in a comment.
        if name not in SKIP_PERMS_EXEMPT:
            if re.search(r"""['"]--dangerously-skip-permissions['"]""", text):
                fails.append(f"[{name}] {rel}: passes --dangerously-skip-permissions "
                             "(read-only scouts must not)")

        # drift visibility (not a failure on its own)
        added = sorted(deny - bdeny)
        if added:
            warns.append(f"[{name}] denylist grew (OK, stronger): +{', '.join(added)}")

    # D. settings.json send gate (WV_GATED_SEND is the wedding-workspace gate name)
    try:
        s = _read(SETTINGS)
        if not ("mcp__gmail__send_email" in s and "WV_GATED_SEND" in s
                and '\\"ask\\"' in s and '\\"allow\\"' in s):
            fails.append(f"[send-gate] {SETTINGS}: PreToolUse gmail send gate missing or "
                         f"no longer defaults to ask (expected WV_GATED_SEND)")
    except FileNotFoundError:
        fails.append(f"[send-gate] {SETTINGS}: file not found")

    for w in warns:
        print("WARN " + w)
    if fails:
        print("\n".join("FAIL " + f for f in fails))
        print(f"\nGATE CHECK FAILED: {len(fails)} problem(s). A send/UI gate was weakened.")
        return 1
    print(f"GATE CHECK PASSED: {len(TARGETS)} poll gate(s) + send-gate hook intact.")
    return 0


if __name__ == "__main__":
    if "--freeze" in sys.argv[1:]:
        freeze()
        sys.exit(0)
    sys.exit(verify())
