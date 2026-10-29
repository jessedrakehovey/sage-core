#!/usr/bin/env python3
"""graphify_stale.py - deterministic staleness detector for the wedding-videography knowledge graph.

WHY: the graphify graph (graphify-out/graph.json) is built over Wiki/Operations by an
interactive session that dispatches LLM subagents. On this machine those subagents fail
in the headless terminal and there is no Gemini key, so the graph CANNOT be re-extracted
unattended -- it silently rots as Wiki/ docs change. This script is the deterministic,
no-LLM half of the automation: it answers "is the graph stale, and by how much?" so a
git hook, a SessionStart surface, and a scheduled cron can all flag when a refresh is due.

It compares the doc set the graph was last built from (recorded in
graphify-out/.graphify_detect.json) against the current files under the graph root, using
file mtime vs the graph.json build time to catch in-place edits.

OUTPUTS:
  - graphify-out/.needs_update  : written (JSON details) when stale, removed when fresh.
  - stdout                      : one-line human summary (ASCII only -- safe for the
                                  Windows cp1252 console, which graphify's own output is not).

EXIT CODES:
  0  fresh (or --no-exit-code given)
  2  stale, when --exit-stale is passed (lets a cron branch on $LASTEXITCODE)
  1  could not determine state (no graph built yet, missing detect.json)

USAGE:
  python scripts/graphify_stale.py            # check + write/clear flag, exit 0/1
  python scripts/graphify_stale.py --exit-stale   # same, but exit 2 when stale (for cron)
  python scripts/graphify_stale.py --quiet        # no stdout (for the git hook)
  python scripts/graphify_stale.py --json         # machine-readable summary to stdout
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# scripts/ -> wedding folder root
VAULT = Path(__file__).resolve().parent.parent
OUT = VAULT / "graphify-out"
DETECT = OUT / ".graphify_detect.json"
GRAPH = OUT / "graph.json"
FLAG = OUT / ".needs_update"

# Doc extensions graphify treats as text documents (the corpus is md/txt).
DOC_EXTS = {".md", ".txt", ".markdown", ".rst"}


def _common_root(files: set[Path]) -> Path:
    """Deepest directory containing every baseline file (the graph's scan root)."""
    if not files:
        return VAULT
    parents = [f.parent for f in files]
    common = parents[0]
    for p in parents[1:]:
        # walk `common` up until it is an ancestor of (or equal to) p
        while common not in (p, *p.parents):
            common = common.parent
    return common


def _load_baseline() -> tuple[Path, set[Path], float] | None:
    """Return (scan_root, baseline_files, build_time) or None if no graph exists yet.

    Baseline is read from graph.json's durable `source_file` paths so the detector
    keeps working after the graphify pipeline's Step 9 cleanup deletes the ephemeral
    .graphify_detect.json. detect.json is used only as a fallback for the scan root.
    """
    if not GRAPH.exists():
        return None
    build_time = GRAPH.stat().st_mtime
    try:
        g = json.loads(GRAPH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    baseline: set[Path] = set()
    for nd in g.get("nodes", []):
        sf = nd.get("source_file")
        # Skip synthetic node sources like "(external link target)" -- only real
        # files (those with an extension) belong in the staleness baseline.
        if sf and Path(sf).suffix:
            p = Path(sf)
            baseline.add(p if p.is_absolute() else (VAULT / p).resolve())

    if not baseline:
        return None

    # Prefer the recorded scan_root (covers subdirs with no nodes); else derive it.
    scan_root: Path | None = None
    if DETECT.exists():
        try:
            sr = json.loads(DETECT.read_text(encoding="utf-8")).get("scan_root")
            if sr:
                scan_root = Path(sr)
        except (json.JSONDecodeError, OSError):
            pass
    if scan_root is None:
        scan_root = _common_root(baseline)

    return scan_root, baseline, build_time


def _current_docs(scan_root: Path, baseline: set[Path]) -> set[Path]:
    """All doc files currently under the graph root.

    Extension set = the standard doc exts plus whatever the baseline actually used,
    so we never miss a file type the original build included.
    """
    exts = set(DOC_EXTS) | {p.suffix.lower() for p in baseline if p.suffix}
    found: set[Path] = set()
    if not scan_root.exists():
        return found
    for p in scan_root.rglob("*"):
        if p.is_file() and p.suffix.lower() in exts:
            # skip graphify's own sidecar output dir
            if OUT in p.parents:
                continue
            found.add(p.resolve())
    return found


def check() -> dict:
    base = _load_baseline()
    if base is None:
        return {"state": "no-graph", "stale": False, "reason": "no graph.json / detect.json found"}
    scan_root, baseline, build_time = base
    current = _current_docs(scan_root, baseline)

    new = sorted(current - baseline)
    deleted = sorted(p for p in baseline if not p.exists())
    common = current & baseline
    modified = sorted(p for p in common if p.stat().st_mtime > build_time)

    stale = bool(new or deleted or modified)

    def rel(p: Path) -> str:
        try:
            return str(p.relative_to(VAULT)).replace("\\", "/")
        except ValueError:
            return str(p)

    return {
        "state": "stale" if stale else "fresh",
        "stale": stale,
        "scan_root": rel(scan_root),
        "build_time": build_time,
        "baseline_count": len(baseline),
        "current_count": len(current),
        "new": [rel(p) for p in new],
        "deleted": [rel(p) for p in deleted],
        "modified": [rel(p) for p in modified],
    }


def _summary(r: dict) -> str:
    if r["state"] == "no-graph":
        return "[graphify] no graph built yet (run /graphify to create one)."
    if not r["stale"]:
        return f"[graphify] graph is fresh ({r['current_count']} docs, none changed)."
    n, d, m = len(r["new"]), len(r["deleted"]), len(r["modified"])
    parts = []
    if n: parts.append(f"{n} new")
    if d: parts.append(f"{d} deleted")
    if m: parts.append(f"{m} modified")
    return (f"[graphify] graph is STALE: {', '.join(parts)} doc(s) since last build. "
            f"Run /graphify --update (in the desktop app) to refresh.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Detect whether the graphify graph is stale.")
    ap.add_argument("--exit-stale", action="store_true", help="exit 2 when stale (for cron branching)")
    ap.add_argument("--quiet", action="store_true", help="suppress stdout summary")
    ap.add_argument("--json", action="store_true", help="emit JSON summary to stdout")
    ap.add_argument("--hook", action="store_true",
                    help="SessionStart mode: print the reminder ONLY when stale, stay silent otherwise")
    args = ap.parse_args()

    r = check()

    # Maintain the flag file: written when stale, removed when fresh.
    if r["stale"]:
        FLAG.write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
    elif FLAG.exists():
        try:
            FLAG.unlink()
        except OSError:
            pass

    if args.json:
        sys.stdout.write(json.dumps(r, ensure_ascii=False) + "\n")
    elif args.hook:
        # SessionStart surface: only speak up when there is something to act on,
        # and never fail the hook chain (always exit 0 below).
        if r["stale"]:
            sys.stdout.write(_summary(r) + "\n")
        return 0
    elif not args.quiet:
        sys.stdout.write(_summary(r) + "\n")

    if r["state"] == "no-graph":
        return 1
    if args.exit_stale and r["stale"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
