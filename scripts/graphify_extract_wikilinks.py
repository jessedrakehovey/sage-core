#!/usr/bin/env python3
"""graphify_extract_wikilinks.py - deterministic, LLM-free extractor for the wedding-videography doc graph.

WHY: the wedding folder's graphify graph is a *document reference graph* -- one node per note,
edges are `references` derived from Obsidian `[[wikilinks]]`. graphify normally fills that
in with an LLM (Claude subagents / Gemini), but on this machine subagents fail
(OpenRouter can't serve the subagent model) and there is no Gemini key. For a wikilink
graph the LLM is unnecessary: the structure is fully recoverable by parsing the links.
This makes `/graphify --update` -- and the headless staleness cron -- able to rebuild the
graph with zero LLM calls, which is the real unlock for unattended refresh.

It emits the same JSON schema graphify's subagents do (nodes/edges/hyperedges), so the
downstream build/cluster/report/HTML steps are unchanged. Run, then feed the output into
graphify.build.build_from_json + cluster + report.

USAGE:
  python scripts/graphify_extract_wikilinks.py <scan_root> <out_json>
  # e.g. python scripts/graphify_extract_wikilinks.py "Wiki/Operations" graphify-out/.graphify_extract.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent
DOC_EXTS = {".md"}
WIKILINK = re.compile(r"\[\[([^\[\]]+?)\]\]")
H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def norm(s: str) -> str:
    """lowercase, non-alphanumeric -> _, collapse repeats, strip underscores."""
    s = re.sub(r"[^a-z0-9]+", "_", s.lower())
    return s.strip("_")


def node_id(rel_parts: list[str], stem: str, scan_root_name: str) -> str:
    """Match graphify's IDs: files directly in the scan root get an `ops_`-style prefix
    from the root dir name; files in a subdir use the immediate parent dir name."""
    if len(rel_parts) == 1:  # directly in scan root
        prefix = "ops" if scan_root_name.lower() == "operations" else norm(scan_root_name)
    else:
        prefix = norm(rel_parts[-2])  # immediate parent dir
    return f"{prefix}_{norm(stem)}"


def label_for(text: str, stem: str) -> str:
    m = H1.search(strip_frontmatter(text))
    if m:
        return m.group(1).strip()
    return stem


def strip_frontmatter(text: str) -> str:
    return FRONTMATTER.sub("", text, count=1)


def link_target(raw: str) -> str:
    """Resolve a wikilink body to its target note basename.
    Handles [[Target|alias]], [[Target#heading]], [[folder/Target]]."""
    t = raw.split("|", 1)[0]          # drop alias
    t = t.split("#", 1)[0]            # drop heading anchor
    t = t.strip().replace("\\", "/")
    if "/" in t:
        t = t.rsplit("/", 1)[-1]     # basename only
    return t.strip()


def main() -> int:
    scan_root = Path(sys.argv[1]) if len(sys.argv) > 1 else (VAULT / "Wiki" / "Operations")
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else (VAULT / "graphify-out" / ".graphify_extract.json")
    if not scan_root.is_absolute():
        scan_root = (VAULT / scan_root).resolve()
    scan_root_name = scan_root.name

    files = sorted(p for p in scan_root.rglob("*") if p.is_file() and p.suffix.lower() in DOC_EXTS)

    # Pass 1: build node table + resolution map (norm(stem) -> id).
    nodes: dict[str, dict] = {}
    by_stem: dict[str, str] = {}
    file_text: dict[str, str] = {}
    ambiguous_stems: set[str] = set()
    for f in files:
        rel = f.relative_to(scan_root)
        rel_parts = list(rel.parts)
        stem = f.stem
        nid = node_id(rel_parts, stem, scan_root_name)
        text = f.read_text(encoding="utf-8", errors="replace")
        file_text[nid] = text
        src = str(f.relative_to(VAULT)).replace("\\", "/")
        nodes[nid] = {
            "id": nid, "label": label_for(text, stem), "file_type": "document",
            "source_file": src, "source_location": None, "source_url": None,
            "captured_at": None, "author": None, "contributor": None,
        }
        key = norm(stem)
        if key in by_stem:
            ambiguous_stems.add(key)  # e.g. multiple "about" notes -> don't auto-resolve
        else:
            by_stem[key] = nid

    # Pass 2: parse wikilinks -> reference edges; unresolved targets become ext_ nodes.
    edges: list[dict] = []
    seen_edges: set[tuple[str, str]] = set()
    ext_nodes: dict[str, dict] = {}
    for nid, text in file_text.items():
        src = nodes[nid]["source_file"]
        for raw in WIKILINK.findall(strip_frontmatter(text)):
            tgt_name = link_target(raw)
            if not tgt_name:
                continue
            key = norm(tgt_name)
            if not key:
                continue
            if key in by_stem and key not in ambiguous_stems:
                tid = by_stem[key]
            else:
                tid = f"ext_{key}"
                ext_nodes.setdefault(tid, {
                    "id": tid, "label": tgt_name, "file_type": "document",
                    "source_file": "(external link target)", "source_location": None,
                    "source_url": None, "captured_at": None, "author": None, "contributor": None,
                })
            if tid == nid:
                continue  # skip self-reference
            if (nid, tid) in seen_edges:
                continue
            seen_edges.add((nid, tid))
            edges.append({
                "source": nid, "target": tid, "relation": "references",
                "confidence": "EXTRACTED", "confidence_score": 1.0,
                "source_file": src, "source_location": None, "weight": 1.0,
            })

    all_nodes = list(nodes.values()) + list(ext_nodes.values())
    out = {
        "nodes": all_nodes, "edges": edges, "hyperedges": [],
        "input_tokens": 0, "output_tokens": 0,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wikilink extract: {len(nodes)} file nodes + {len(ext_nodes)} ext nodes, "
          f"{len(edges)} reference edges -> {out_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
