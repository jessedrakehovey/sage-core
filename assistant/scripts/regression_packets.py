"""regression_packets.py - fixed replays that a prompt or rule change must not make worse (WP6, 2026-10-24).

scripts/evals/packets/*.json each hold one owner line that once went wrong (Corwin's card, Wren's contact, Delia's
send), the deeds a good turn makes (`expect_deeds`: tool kinds Sage must call), phrases a good reply never contains
(`forbid_phrases`) and, optionally, phrases it must (`require_phrases`). Two modes:

  --check   zero-LLM: every packet is well-formed and names only deed kinds the brain has (intents.KEY_OF)
  --live    one chat turn per packet through assistant/agent/chat.py (needs the User-env token: run it from the
            dreaming cron or a terminal, never inside a Claude session); the reply is checked against the phrases,
            the turn's intents file against expect_deeds; each verdict lands in the eval ledger as
            pipeline regression-<packet>, so a packet that passed last run and fails now is a flagged regression.

Not yet wired to a cron: run `--live` after a prompt or rule change (the builder's verify step and the nightly
review are the natural callers; see HANDOFF 2026-10-24).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VAULT = ROOT.parent
PACKETS = Path(os.environ.get("WV_PACKETS") or (VAULT / "scripts" / "evals" / "packets"))


def load() -> list:
    out = []
    for f in sorted(glob.glob(str(PACKETS / "*.json"))):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
            d["_name"] = Path(f).stem
            out.append(d)
        except ValueError as e:
            out.append({"_name": Path(f).stem, "_error": str(e)})
    return out


def check() -> list:
    try:
        sys.path.insert(0, str(ROOT / "agent"))
        import intents
        kinds = set(getattr(intents, "KEY_OF", {}) or {}) | {"task", "do", "note", "decide", "call", "learn", "remember", "event", "schedule", "followup", "skill", "org", "switch", "identify", "approve"}
    except Exception:  # noqa: BLE001
        kinds = {"task", "do", "note", "decide", "call", "learn", "remember", "event", "schedule", "followup", "skill", "org", "switch", "identify", "approve"}
    problems = []
    for p in load():
        if p.get("_error"):
            problems.append(f"{p['_name']}: {p['_error']}")
            continue
        if not str(p.get("message") or "").strip():
            problems.append(f"{p['_name']}: no message")
        bad = [k for k in (p.get("expect_deeds") or []) if k not in kinds]
        if bad:
            problems.append(f"{p['_name']}: unknown deed kinds {bad}")
        if not isinstance(p.get("forbid_phrases", []), list):
            problems.append(f"{p['_name']}: forbid_phrases must be a list")
    return problems


def _record(name: str, verdict: str, note: str) -> None:
    try:
        subprocess.run([sys.executable, str(VAULT / "scripts" / "evals" / "eval_ledger.py"), "record", "--pipeline", f"regression-{name}",
                        "--artifact", str(PACKETS / f"{name}.json"), "--verdict", verdict, "--note", note[:300]],
                       capture_output=True, text=True, timeout=60, cwd=str(VAULT))
    except Exception:  # noqa: BLE001
        pass


def live(timeout: int = 300) -> list:
    results = []
    for p in load():
        if p.get("_error"):
            continue
        t0 = time.time()
        r = subprocess.run([sys.executable, str(ROOT / "agent" / "chat.py"), "--as", str(p.get("who") or "owner1"), "--timeout", str(timeout), str(p["message"])],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout + 60, cwd=str(VAULT))
        reply = (r.stdout or "").strip()
        bad = [ph for ph in (p.get("forbid_phrases") or []) if ph.lower() in reply.lower()]
        missing = [ph for ph in (p.get("require_phrases") or []) if ph.lower() not in reply.lower()]
        verdict = "FAIL" if (bad or missing or not reply or reply.startswith("TIMEOUT")) else "PASS"
        note = f"{time.time() - t0:.0f}s; forbidden said: {bad}; required missing: {missing}; reply: {reply[:120]}"
        _record(p["_name"], verdict, note)
        results.append({"packet": p["_name"], "verdict": verdict, "note": note})
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    if a.live:
        res = live()
        for r in res:
            print(f"{r['verdict']} {r['packet']}: {r['note']}")
        print(f"RESULT REGRESSION pass={sum(1 for r in res if r['verdict'] == 'PASS')} fail={sum(1 for r in res if r['verdict'] == 'FAIL')}")
        return 0
    probs = check()
    for x in probs:
        print("PROBLEM " + x)
    print(f"RESULT REGRESSION packets={len(load())} problems={len(probs)}")
    return 1 if probs else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
