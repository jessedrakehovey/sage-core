#!/usr/bin/env python3
"""graphify_rebuild.py - full, LLM-free rebuild of the wedding-videography knowledge graph.

WHY: the wedding folder graph is a wikilink reference graph (see graphify_extract_wikilinks.py),
so it can be rebuilt entirely deterministically -- no Claude subagents (which fail on
this machine's OpenRouter routing) and no Gemini key. That makes genuine *unattended*
auto-rebuild possible: the staleness cron can call this and the graph just stays current.

Pipeline: detect -> wikilink extract -> build -> cluster -> auto-label communities by
their dominant source folder -> report + graph.json + graph.html -> save manifest.

Community labels are derived deterministically from the most common immediate parent
folder of each community's member notes (e.g. a community of Vendors/* notes -> "Vendors").
This is stable across rebuilds and needs no human input; richer hand labels can still be
applied by an interactive /graphify run afterward.

USAGE:
  python scripts/graphify_rebuild.py [scan_root]
  # default scan_root = Wiki/Operations

  python scripts/graphify_rebuild.py --self-test
  # Regression tripwire: inflates graph.json with dummy nodes so its node count is
  # higher than the real wikilink build produces, then verifies the rebuild STILL
  # writes graph.json (i.e. force=True bypasses the shrink-guard).  Also verifies
  # that the written graph has real edges (not 0 -- the 0-edge symptom was the
  # observable sign of the shrink-guard blocking the write).
  # Exit 0 = pass, exit 1 = fail (regression detected).
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent
OUT = VAULT / "graphify-out"
EXTRACTOR = VAULT / "scripts" / "graphify_extract_wikilinks.py"


def _folder_label(member_ids, nodes_by_id) -> str:
    """Name a community after the dominant immediate parent folder of its notes."""
    folders = []
    for nid in member_ids:
        n = nodes_by_id.get(nid)
        if not n:
            continue
        sf = n.get("source_file") or ""
        if sf == "(external link target)":
            continue
        parts = Path(sf).parts
        # .../Wiki/Operations/<folder>/<file>  -> <folder>; file directly in root -> root name
        if len(parts) >= 2:
            folders.append(parts[-2])
    if not folders:
        return "Misc"
    return Counter(folders).most_common(1)[0][0]


def main() -> int:
    scan_root = sys.argv[1] if len(sys.argv) > 1 else "Wiki/Operations"
    OUT.mkdir(exist_ok=True)
    py = sys.executable

    from graphify.detect import detect, save_manifest
    from graphify.build import build_from_json
    from graphify.cluster import cluster, score_all
    from graphify.analyze import god_nodes, surprising_connections, suggest_questions
    from graphify.report import generate
    from graphify.export import to_json

    root_abs = (VAULT / scan_root).resolve()

    # 1. detect (for report stats + manifest)
    det = detect(root_abs)
    (OUT / ".graphify_detect.json").write_text(json.dumps(det, ensure_ascii=False), encoding="utf-8")

    # 2. deterministic wikilink extraction
    subprocess.run([py, str(EXTRACTOR), scan_root, str(OUT / ".graphify_extract.json")],
                   check=True, cwd=str(VAULT))
    ext = json.loads((OUT / ".graphify_extract.json").read_text(encoding="utf-8"))
    nodes_by_id = {n["id"]: n for n in ext["nodes"]}

    # 3. build + cluster + analyze
    G = build_from_json(ext)
    if G.number_of_nodes() == 0:
        print("ERROR: graph empty after extraction"); return 1
    communities = cluster(G)
    cohesion = score_all(G, communities)
    gods = god_nodes(G)
    surprises = surprising_connections(G, communities)

    # 4. deterministic folder-based labels (dedupe collisions with a suffix)
    raw = {cid: _folder_label(members, nodes_by_id) for cid, members in communities.items()}
    seen: Counter = Counter()
    labels = {}
    for cid in sorted(communities, key=lambda c: -len(communities[c])):
        name = raw[cid]
        seen[name] += 1
        labels[cid] = name if seen[name] == 1 else f"{name} ({seen[name]})"

    # 5. report + outputs
    q = suggest_questions(G, communities, labels)
    rep = generate(G, communities, cohesion, labels, gods, surprises, det,
                   {"input": 0, "output": 0}, ".", suggested_questions=q)
    (OUT / "GRAPH_REPORT.md").write_text(rep, encoding="utf-8")
    (OUT / ".graphify_labels.json").write_text(
        json.dumps({str(k): v for k, v in labels.items()}, ensure_ascii=False), encoding="utf-8")
    # force=True: the wikilink extractor is authoritative for this graph; the
    # existing graph.json may have been built by an old LLM/interactive run with
    # more nodes (ext_ nodes from LLM-inferred links).  A deterministic rebuild
    # is always a legitimate full replacement, so we bypass the shrink-guard.
    ok = to_json(G, communities, str(OUT / "graph.json"), force=True)
    if not ok:
        print("ERROR: to_json returned False -- graph.json was not written.", file=sys.stderr)
        return 1

    # 6. manifest (so any future graphify --update is genuinely incremental)
    # Pass the absolute path explicitly so save_manifest never falls back to the
    # relative "graphify-out/manifest.json" default, which throws PermissionError
    # when the scheduled task runs from a system CWD (no <WorkingDirectory> set).
    save_manifest(det.get("all_files") or det["files"], str(OUT / "manifest.json"))

    # 7. HTML
    try:
        subprocess.run(["graphify", "export", "html"], check=True, cwd=str(VAULT))
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"warning: html export failed ({e}); graph.json + report still written")

    # 8. cleanup ephemeral sidecars (graph.json/report/html/labels/manifest persist)
    for f in (".graphify_detect.json", ".graphify_extract.json", ".graphify_analysis.json"):
        (OUT / f).unlink(missing_ok=True)

    print(f"rebuilt: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, "
          f"{len(communities)} communities, {det['total_files']} docs (0 tokens)")
    return 0


def self_test() -> int:
    """Regression tripwire for the shrink-guard bypass (force=True in to_json call).

    What it checks
    --------------
    The bug that caused 9+ days of silent graph staleness was:
      graphify's to_json() refused to overwrite graph.json when the new build had
      FEWER nodes than the existing file (an old LLM-built graph had 90 nodes;
      the deterministic wikilink rebuild produces ~86).  Without force=True, every
      cron run exited 0 but wrote nothing, and the graph accumulated 0 edges.

    This test reproduces exactly that condition and verifies the fix holds:
      1. Back up real graph.json, inject a fake one with node_count + 50 dummy nodes.
      2. Run a real rebuild (same code path as the cron).
      3. Assert graph.json was WRITTEN (mtime changed) and has real edges (> 0).
      4. Restore the original so the folder is unchanged.

    Exit 0 = pass.  Exit 1 = regression (force=True was removed or broken).
    """
    import shutil
    import time

    graph = OUT / "graph.json"
    backup = OUT / ".graph_selftest_backup.json"

    print("[self-test] starting shrink-guard regression check")

    # Step 1: inflate the existing graph.json with dummy nodes so node_count is higher.
    if not graph.exists():
        print("[self-test] SKIP: no graph.json yet -- run a normal rebuild first")
        return 0

    real_data = json.loads(graph.read_text(encoding="utf-8"))
    real_node_count = len(real_data.get("nodes", []))
    shutil.copy2(graph, backup)

    # Build a fake graph.json that looks like an old LLM run: same structure but
    # +50 dummy nodes and 0 edges (exactly the symptom we saw).
    dummy_nodes = [
        {"id": f"selftest_dummy_{i}", "label": f"dummy {i}", "file_type": "document",
         "source_file": f"(selftest dummy {i})", "source_location": None,
         "source_url": None, "captured_at": None, "author": None, "contributor": None,
         "community": 0, "norm_label": f"dummy {i}"}
        for i in range(50)
    ]
    fake_data = dict(real_data)
    fake_data["nodes"] = real_data.get("nodes", []) + dummy_nodes
    fake_data["links"] = []   # 0 edges -- the observable symptom
    graph.write_text(json.dumps(fake_data, indent=2), encoding="utf-8")
    inflated_count = len(fake_data["nodes"])
    print(f"[self-test] injected fake graph.json: {inflated_count} nodes (real={real_node_count}), 0 edges")

    # Give the fake file a mtime clearly in the past so we can detect a write.
    past = time.time() - 3600
    import os
    os.utime(graph, (past, past))
    mtime_before = graph.stat().st_mtime

    # Step 2: rebuild (uses force=True via main()).
    print("[self-test] running rebuild ...")
    rc = main()
    if rc != 0:
        print(f"[self-test] FAIL: rebuild exited {rc}")
        shutil.copy2(backup, graph)
        backup.unlink(missing_ok=True)
        return 1

    # Step 3: assert graph.json was WRITTEN (mtime changed) and has real edges.
    mtime_after = graph.stat().st_mtime
    if mtime_after <= mtime_before:
        print("[self-test] FAIL: graph.json mtime unchanged -- shrink-guard blocked the write (force=True missing?)")
        shutil.copy2(backup, graph)
        backup.unlink(missing_ok=True)
        return 1

    written = json.loads(graph.read_text(encoding="utf-8"))
    written_nodes = len(written.get("nodes", []))
    written_edges = len(written.get("links", []))   # networkx node_link format uses 'links'
    if written_edges == 0:
        print(f"[self-test] FAIL: graph.json was written but has 0 edges ({written_nodes} nodes) -- "
              f"rebuild produced empty graph, or to_json still skipped the write")
        shutil.copy2(backup, graph)
        backup.unlink(missing_ok=True)
        return 1

    if written_nodes >= inflated_count:
        print(f"[self-test] FAIL: graph.json still has {written_nodes} nodes (>= inflated {inflated_count}); "
              f"the fake file was not replaced")
        shutil.copy2(backup, graph)
        backup.unlink(missing_ok=True)
        return 1

    print(f"[self-test] PASS: shrink-guard correctly bypassed")
    print(f"  inflated fake had {inflated_count} nodes, 0 edges")
    print(f"  rebuilt graph has {written_nodes} nodes, {written_edges} edges")
    backup.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        # Pop the flag so main() doesn't see it as a scan_root argument.
        sys.argv.pop(1)
        raise SystemExit(self_test())
    raise SystemExit(main())
