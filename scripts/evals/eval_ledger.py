#!/usr/bin/env python3
"""
eval_ledger.py  -  regression baselines for artifact-producing pipelines.

ECC borrow #4 (everything-claude-code eval-harness, 2026-06-29). The artifact-checker
already verifies each artifact per-run (a "human/model grader" in ECC terms), but
nothing tracks whether a pipeline REGRESSES across runs - e.g. the same deliverable
failing the same way twice. This is a thin ledger the artifact-check verdict feeds:

  record  -> append one PASS/FAIL outcome (+ which field-checks failed) as JSONL.
  report  -> per-pipeline pass@1 over recent runs + a regression flag (a field
             that passed last run and fails this run), and recent failures.

It writes ONLY its own ledger file; it never touches artifacts or sends.

Wire-in (after an artifact verdict):
  python scripts/evals/eval_ledger.py record --pipeline <doctype> \
      --artifact <file> --verdict PASS|FAIL [--fail-fields "a,b"] [--note "..."]

Usage:
  python scripts/evals/eval_ledger.py report
  python scripts/evals/eval_ledger.py report --pipeline highlight-film
  python scripts/evals/eval_ledger.py record --pipeline highlight-film --verdict FAIL \
      --artifact export.mp4 --fail-fields "color grade" --note "wrong LUT applied"
Env: WV_EVAL_LEDGER overrides the ledger path (used by tests).
"""
from __future__ import annotations
import argparse
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DEFAULT_LEDGER = Path(__file__).resolve().parent / "eval_ledger.jsonl"


def ledger_path() -> Path:
    return Path(os.environ.get("WV_EVAL_LEDGER", str(DEFAULT_LEDGER)))


def load() -> list[dict]:
    p = ledger_path()
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # tolerate a bad line rather than crash the report
    return rows


def cmd_record(args) -> None:
    verdict = args.verdict.upper()
    if verdict not in ("PASS", "FAIL"):
        raise SystemExit("verdict must be PASS or FAIL")
    fields = [f.strip() for f in (args.fail_fields or "").split(",") if f.strip()]
    rec = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pipeline": args.pipeline,
        "artifact": args.artifact or "",
        "verdict": verdict,
        "fail_fields": fields,
        "note": args.note or "",
    }
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(f"recorded: [{rec['pipeline']}] {verdict} {rec['artifact']}"
          + (f" (fail: {', '.join(fields)})" if fields else ""))


def cmd_report(args) -> None:
    rows = load()
    if args.pipeline:
        rows = [r for r in rows if r.get("pipeline") == args.pipeline]
    if not rows:
        print("no eval records yet." + (f" (pipeline={args.pipeline})" if args.pipeline else ""))
        return
    by_pipe: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_pipe[r.get("pipeline", "?")].append(r)

    N = args.window
    print("=" * 74)
    print(f"PIPELINE REGRESSION REPORT  -  {len(rows)} records, last {N}/pipeline")
    print("=" * 74)
    for pipe in sorted(by_pipe):
        runs = by_pipe[pipe]               # chronological (append order)
        recent = runs[-N:]
        passes = sum(1 for r in recent if r.get("verdict") == "PASS")
        rate = passes / len(recent)
        print(f"\n[{pipe}]  pass@1 = {passes}/{len(recent)} = {rate:.0%}  (total runs: {len(runs)})")

        # regression: field failing now that passed in the immediately prior run
        if len(runs) >= 2:
            prev_fails = set(runs[-2].get("fail_fields", []))
            cur_fails = set(runs[-1].get("fail_fields", []))
            new_reg = cur_fails - prev_fails
            persistent = cur_fails & prev_fails
            if new_reg:
                print(f"  !! NEW regression(s): {', '.join(sorted(new_reg))}")
            if persistent:
                print(f"  !! persistent failure(s): {', '.join(sorted(persistent))}")
            if runs[-1].get("verdict") == "PASS" and runs[-2].get("verdict") == "FAIL":
                print(f"  ++ recovered since prior run")

        fails = [r for r in recent if r.get("verdict") == "FAIL"]
        if fails:
            print(f"  recent FAILs:")
            for r in fails[-5:]:
                ff = f" [{', '.join(r['fail_fields'])}]" if r.get("fail_fields") else ""
                note = f" - {r['note']}" if r.get("note") else ""
                print(f"    {r['ts']}  {r.get('artifact','')}{ff}{note}")


def main() -> None:
    ap = argparse.ArgumentParser(description="pipeline regression ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="append a PASS/FAIL outcome")
    rec.add_argument("--pipeline", required=True, help="artifact type, e.g. highlight-film / ceremony-edit")
    rec.add_argument("--verdict", required=True, help="PASS or FAIL")
    rec.add_argument("--artifact", default="", help="file name / id")
    rec.add_argument("--fail-fields", default="", help="comma-separated failing field names")
    rec.add_argument("--note", default="", help="short context")
    rec.set_defaults(func=cmd_record)

    rep = sub.add_parser("report", help="per-pipeline pass@1 + regressions")
    rep.add_argument("--pipeline", default="", help="filter to one pipeline")
    rep.add_argument("--window", type=int, default=20, help="recent runs per pipeline")
    rep.set_defaults(func=cmd_report)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
