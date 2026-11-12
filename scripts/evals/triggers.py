#!/usr/bin/env python3
"""triggers.py - the change-triggered cadence of the regression packets (Seed F). Never nightly on a timer, never
from inside an interactive agent session.

  python scripts/evals/triggers.py staged                pre-commit: exit 1 when a STAGED file is a trigger and the
                                                         zero-LLM packet check (--check --changed) fails
  python scripts/evals/triggers.py range A..B [--live]   the dreaming cron's post-patch step: when a commit in the
                                                         range touched a trigger, run --check --changed, then (with
                                                         --live) the regression suite live, a few turns at most.
                                                         Non-fatal: exit 0 always, findings on stdout for the log
  python scripts/evals/triggers.py files <f...>          just say which of these files are triggers

A trigger is the agent's prompt (assistant/agent/prompt.md, prompt.local.md, desk_prompt.md), a rules file the drafts
and cards obey (assistant/data/*_rules.json, comms_law.json, sop_map.json, client_texts.json, the card policy in
assistant/config.json), the chase / follow-up code (agent/followups.py, scripts/followups.py, scripts/tracking/chase.py,
scripts/cards.py, scripts/draft_lint.py), the agent's deed registry (agent/intents.py) or a packet itself. The list is
data at the top of this file; a pack may extend it with scripts/evals/triggers.json ({"extra": [regex, ...]}).
Org-free: no organization, person or service is named here.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT = HERE.parents[1]
TRIGGER_RX = re.compile(
    r"^assistant/agent/(?:prompt|prompt\.local|desk_prompt)\.md$|"
    r"^assistant/data/(?:[a-z_]+_rules|comms_law|sop_map|client_texts)\.json$|"
    r"^assistant/config\.json$|"
    r"^assistant/agent/(?:followups|intents)\.py$|^assistant/scripts/(?:followups|cards|draft_lint)\.py$|"
    r"^assistant/scripts/tracking/chase\.py$|"
    r"^scripts/evals/packets/.*\.jsonl?$", re.I)
EXTRA = HERE / "triggers.json"
RUNNER = VAULT / "assistant" / "scripts" / "regression_packets.py"      # the evals engine (its packets live here, under packets/)
LIVE_LIMIT = 12            # live turns per post-patch run, at most (each is a model turn in a throwaway lane)


def _extra() -> list:
    try:
        d = json.loads(EXTRA.read_text(encoding="utf-8-sig"))
        return [re.compile(str(x), re.I) for x in (d.get("extra") or [])]
    except (OSError, ValueError):
        return []


def is_trigger(path: str) -> bool:
    p = str(path).replace("\\", "/").lstrip("./")
    if TRIGGER_RX.search(p):
        return True
    return any(rx.search(p) for rx in _extra())


def triggers_in(files: list) -> list:
    return [f for f in files if is_trigger(f)]


def _git(args: list) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(VAULT))
    return r.stdout or ""


def staged_files() -> list:
    return [ln.strip() for ln in _git(["diff", "--cached", "--name-only"]).splitlines() if ln.strip()]


def range_files(rng: str) -> list:
    return [ln.strip() for ln in _git(["diff", "--name-only", rng]).splitlines() if ln.strip()]


def run_check(changed: list) -> int:
    r = subprocess.run([sys.executable, str(RUNNER), "--check", "--changed", *changed],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(VAULT), timeout=300)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if out:
        print(out)
    return r.returncode


def run_live(limit: int = LIVE_LIMIT) -> int:
    r = subprocess.run([sys.executable, str(RUNNER), "--live", "--suite", "regression", "--limit", str(limit)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(VAULT), timeout=3600)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if out:
        print(out[-4000:])
    return r.returncode


def main(argv=None) -> int:
    a = list(argv if argv is not None else sys.argv[1:])
    sys.stdout.reconfigure(encoding="utf-8")
    if not a:
        print(__doc__)
        return 2
    if a[0] == "files":
        hits = triggers_in(a[1:])
        print("\n".join(hits) if hits else "no triggers")
        return 0
    if a[0] == "staged":
        hits = triggers_in(staged_files())
        if not hits:
            return 0
        print("evals: staged trigger(s): " + ", ".join(hits))
        return 1 if run_check(hits) != 0 else 0
    if a[0] == "range":
        if len(a) < 2:
            print("range needs A..B")
            return 2
        hits = triggers_in(range_files(a[1]))
        if not hits:
            print("evals: no trigger in " + a[1])
            return 0
        print("evals: trigger(s) in " + a[1] + ": " + ", ".join(hits))
        rc = run_check(hits)
        if rc != 0:
            print("evals: --check failed; the live run is skipped")
            return 0
        if "--live" in a:
            lim = int(a[a.index("--limit") + 1]) if "--limit" in a else LIVE_LIMIT
            run_live(lim)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
