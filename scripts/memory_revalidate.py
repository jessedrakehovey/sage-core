#!/usr/bin/env python3
"""
memory_revalidate.py  --  SHADOW TEST (read-only, writes nothing)

Proves the reshaped "status-claim revalidation" design from the 2026-06-29 roast
BEFORE anything is wired into the dreaming cron. It does NOT edit memory, does NOT
send, does NOT touch any external service. It only prints what a real pass WOULD decide.

The roast (RESHAPE verdict) imposed three load-bearing rules this script encodes:
  1. POSITIVE evidence only -- a claim is "resolved" only if a check finds a signal
     the thing WORKS (success line, LastResult=0, import present, file non-empty).
     Mere ABSENCE of an error never clears a claim (Contrarian's fatal flaw).
  2. PROPOSE-ONLY by default -- auto-resolve is allowed only for a claim that both
     (a) has unambiguous positive evidence AND (b) passes the safety keyword gate.
  3. HARD KEYWORD GATE -- any claim whose text mentions a protected-core word is
     never auto-resolved regardless of tagging (Safety persona's veto condition).

Run:  python scripts/memory_revalidate.py
"""
from __future__ import annotations
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent
CLAUDE = Path(os.path.expanduser("~")) / ".claude"

# Safety keyword gate -- tag is advisory, THIS is the enforced invariant.
# Wedding protected core: send-gating, footage handling, delivery commitments,
# contract/pricing language, and dreaming guardrails.
SAFETY_KEYWORDS = [
    "send",        # send-gating: never send client/vendor-facing without the owner's approval
    "footage",     # footage safety: irreplaceable single-take moments
    "delivery",    # delivery commitments: promised dates to couples
    "contract",    # contract/pricing language
    "pricing",     # pricing commitments
    "gate",        # approval gating machinery
    "guardrail",   # dreaming guardrails
    "legal",       # legal language (business-agnostic)
    "template",    # template integrity (business-agnostic)
]


def safety_gated(text: str) -> str | None:
    low = text.lower()
    for kw in SAFETY_KEYWORDS:
        if kw in low:
            return kw
    return None


# ---- positive-evidence check primitives (each returns (found_positive, detail)) ----

def file_contains(relpath: str, needle: str) -> tuple[bool, str]:
    p = VAULT / relpath
    try:
        if not p.exists():
            return False, f"missing file {relpath}"
        txt = p.read_text(encoding="utf-8", errors="replace")
        return (needle in txt), f"{'found' if needle in txt else 'absent'} `{needle}` in {relpath}"
    except Exception as e:  # never let a bad check crash the pass
        return False, f"check error: {e!r}"


def file_nonempty(abspath: str) -> tuple[bool, str]:
    try:
        p = Path(os.path.expanduser(abspath))
        ok = p.exists() and p.stat().st_size > 0
        return ok, f"{'exists, non-empty' if ok else 'missing/empty'}: {abspath}"
    except Exception as e:
        return False, f"check error: {e!r}"


def log_has_recent_success(logname: str, patterns: list[str], days: int = 7) -> tuple[bool, str]:
    """POSITIVE: a success line matching any pattern within `days`. Not 'no errors'."""
    try:
        p = CLAUDE / logname
        if not p.exists():
            return False, f"missing log {logname}"
        cutoff = datetime.now() - timedelta(days=days)
        hit = None
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if any(pat in line for pat in patterns):
                m = re.search(r"(\d{4}-\d{2}-\d{2})", line)
                if m:
                    try:
                        if datetime.strptime(m.group(1), "%Y-%m-%d") >= cutoff:
                            hit = line.strip()
                    except ValueError:
                        pass
                else:
                    hit = line.strip()  # undated success line still counts as positive
        return (hit is not None), (f"recent success: {hit[:90]}" if hit else f"no success line in last {days}d")
    except Exception as e:
        return False, f"check error: {e!r}"


def task_last_result_zero(taskname: str) -> tuple[bool, str]:
    try:
        out = subprocess.run(
            ["schtasks", "/query", "/tn", taskname, "/fo", "LIST", "/v"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        m = re.search(r"Last Result:\s*([0-9]+)", out)
        if not m:
            return False, "LastResult not found"
        code = int(m.group(1))
        return (code == 0), f"LastResult={code}"
    except Exception as e:
        return False, f"check error: {e!r}"


# ---- the registry: wedding-specific claims as the shadow corpus ----
# kind: a callable returning (positive_evidence_found, detail)
CLAIMS = [
    {
        "id": "dreaming-cron-live",
        "claim": "example-org scheduled task runs successfully each morning",
        "check": lambda: task_last_result_zero("example-org"),
        "positive_means": "LastResult=0 => cron ran cleanly => resolved",
    },
    {
        "id": "dreaming-log-recent",
        "claim": "Dreaming log shows a recent successful consolidation run",
        "check": lambda: log_has_recent_success(
            "dreaming-wv.log",
            ["memory consolidated", "dreaming complete", "phase 1 done", "DONE", "success"],
        ),
        "positive_means": "recent success line in dreaming-wv.log => dreaming is running => resolved",
    },
    {
        "id": "mc-send-gated",
        "claim": "Mission Control send-gating intact: WV_GATED_SEND hook in .claude/settings.json gates gmail send",
        "check": lambda: file_contains(".claude/settings.json", "WV_GATED_SEND"),
        "positive_means": "WV_GATED_SEND hook in settings.json => send is gated on owner1 approval; "
                          "Telegram removed 2026-10-04; Mission Control is the only channel",
    },
]


def decide(claim: dict) -> dict:
    gate = safety_gated(claim["claim"])
    try:
        found, detail = claim["check"]()
    except Exception as e:
        found, detail = False, f"check raised: {e!r}"
    if gate:
        decision = "PROPOSE-ONLY (safety-gated)"
    elif found:
        decision = "AUTO-RESOLVE"
    else:
        decision = "HOLD (no positive evidence)"
    return {"id": claim["id"], "gate": gate or "-", "positive": found, "detail": detail, "decision": decision}


def main() -> None:
    print("=" * 78)
    print("MEMORY REVALIDATION -- SHADOW TEST (read-only; writes/sends nothing)")
    print("Rule: POSITIVE evidence required; safety-keyword claims are propose-only.")
    print("=" * 78)
    for c in CLAIMS:
        r = decide(c)
        print(f"\n[{r['id']}]")
        print(f"  claim     : {c['claim']}")
        print(f"  evidence  : {r['detail']}")
        print(f"  safety kw : {r['gate']}")
        print(f"  -> DECISION: {r['decision']}")
    print("\n" + "-" * 78)
    print("Expected, if the design holds:")
    print("  dreaming-cron-live     -> AUTO-RESOLVE or HOLD (no safety kw; positive only if cron ran)")
    print("  dreaming-log-recent    -> AUTO-RESOLVE or HOLD (no safety kw; positive only if log has success)")
    print("  mc-send-gated          -> PROPOSE-ONLY (safety kw 'send') -- gate refuses auto-resolve")
    print("Nothing was written or sent.")


if __name__ == "__main__":
    main()
