#!/usr/bin/env python3
"""semantic.py — local semantic search over the Wedding Videography vault.

Layer-3 retrieval for the LLM wiki. Embeds Wiki/ + Memory/ notes with a local
ONNX model (no network, no PII leaves the machine) and answers meaning-based
queries that keyword search (`wiki_tool.py search`) misses.

Commands:
  build             (Re)build the embedding index from Wiki/ + Memory/
  search "<query>"  Print the top-K semantically closest chunks
  hook              UserPromptSubmit hook: inject top-K chunks as context (JSON)
  status            Print index stats (model, chunk count, build date)

Runs from the interpreter org.json -> semantic_python names (a venv with fastembed + numpy, see
assistant/requirements-semantic.txt). Without those two packages every command prints one line and exits 2,
the keyword search (`wiki_tool.py search`) is the fallback, and the `hook` stays silent.
Model: BAAI/bge-small-en-v1.5 (fastembed/ONNX). Index is git-ignored, local-only.
"""

import io
import re
import sys
import json
import datetime
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent
WIKI = VAULT / "Wiki"
MEMORY = VAULT / "Memory"
STORE = VAULT / "scripts" / ".semantic_store"
EMB_FILE = STORE / "embeddings.npy"
META_FILE = STORE / "meta.json"
MANIFEST = STORE / "manifest.json"

MODEL_NAME = "BAAI/bge-small-en-v1.5"
CHUNK_CHARS = 1200          # target chunk size (split on paragraph boundaries)
DEFAULT_K = 8

# Query-time re-rank: nudge result *types* without touching the embeddings.
# Couples/ notes hold long free-text histories that out-match procedure notes on
# "how do we..." questions; a small penalty lets Operations/ procedures win those
# while a strong couple match (cosine ~0.75) still tops a couple-specific query
# (procedure notes only score ~0.5 there, so the boost can't flip them). Applied
# additively to cosine, so swings stay small and tunable.
AREA_BOOST = (
    ("Wiki/Operations/", 0.05),
    ("Wiki/Post-Production/", 0.04),
    ("Memory/", 0.03),
    ("Wiki/Couples/", -0.03),
    ("Wiki/Vendors/", -0.02),
    ("Wiki/Venues/", -0.02),
)

FM_RE = re.compile(r"^---\n.*?\n---\n", re.S)
HEADING_RE = re.compile(r"^#\s+(.+)$", re.M)


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


# ---- corpus -----------------------------------------------------------------

def source_files():
    """Notes to index: Wiki/** (minus _meta, about.md) + Memory/ logs & memory."""
    files = []
    for p in sorted(WIKI.rglob("*.md")):
        if "_meta" in p.parts or p.name.lower() == "about.md":
            continue
        files.append(p)
    if (MEMORY / "memory.md").exists():
        files.append(MEMORY / "memory.md")
    for p in sorted((MEMORY / "Daily_Logs").rglob("*.md")) if (MEMORY / "Daily_Logs").exists() else []:
        files.append(p)
    return files


def strip_fm(text: str) -> str:
    return FM_RE.sub("", text, count=1)


def title_of(p: Path, body: str) -> str:
    m = HEADING_RE.search(body)
    return m.group(1).strip() if m else p.stem


def chunk_body(body: str):
    """Greedily pack paragraphs into ~CHUNK_CHARS windows on blank-line splits."""
    kingsmere = [pp.strip() for pp in re.split(r"\n\s*\n", body) if pp.strip()]
    chunks, buf = [], ""
    for para in kingsmere:
        if buf and len(buf) + len(para) + 2 > CHUNK_CHARS:
            chunks.append(buf)
            buf = para
        else:
            buf = (buf + "\n\n" + para) if buf else para
    if buf:
        chunks.append(buf)
    return chunks or [""]


def iter_chunks():
    """Yield (relpath, title, embed_text, snippet) for every chunk in the corpus."""
    for p in source_files():
        body = strip_fm(read(p))
        title = title_of(p, body)
        rel = p.relative_to(VAULT).as_posix()
        for chunk in chunk_body(body):
            clean = re.sub(r"\s+", " ", re.sub(r"[*_`>#\[\]]", "", chunk)).strip()
            if not clean:
                continue
            # Title prefix gives every chunk topical anchoring for the embedder.
            embed_text = f"{title}\n\n{chunk}"
            snippet = clean[:200] + ("..." if len(clean) > 200 else "")
            yield rel, title, embed_text, snippet


# ---- model ------------------------------------------------------------------

def get_model():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=MODEL_NAME)


def _normalize(arr):
    import numpy as np
    arr = np.asarray(arr, dtype="float32")
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / (norms + 1e-9)


# ---- commands ---------------------------------------------------------------

def cmd_build():
    import numpy as np
    rows = list(iter_chunks())
    if not rows:
        sys.exit("build: no source notes found")
    print(f"build: embedding {len(rows)} chunks from {len(source_files())} notes "
          f"with {MODEL_NAME} ...")
    model = get_model()
    texts = [r[2] for r in rows]
    embs = _normalize(list(model.embed(texts, batch_size=64)))
    STORE.mkdir(parents=True, exist_ok=True)
    np.save(EMB_FILE, embs)
    meta = [{"path": r[0], "title": r[1], "snippet": r[3]} for r in rows]
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    MANIFEST.write_text(json.dumps({
        "model": MODEL_NAME,
        "chunks": len(rows),
        "notes": len(source_files()),
        "dim": int(embs.shape[1]),
        "built": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }, indent=2), encoding="utf-8")
    print(f"build: OK — {len(rows)} chunks, dim {embs.shape[1]} -> {STORE.relative_to(VAULT).as_posix()}")


def _load():
    import numpy as np
    if not EMB_FILE.exists():
        return None, None
    return np.load(EMB_FILE), json.loads(read(META_FILE))


def _boost_vector(meta):
    import numpy as np
    b = np.zeros(len(meta), dtype="float32")
    for i, m in enumerate(meta):
        path = m["path"]
        for prefix, val in AREA_BOOST:
            if path.startswith(prefix):
                b[i] += val
                break
    return b


def _rank(query, k):
    import numpy as np
    embs, meta = _load()
    if embs is None:
        return None
    model = get_model()
    q = _normalize(list(model.query_embed([query])))[0]
    scores = (embs @ q) + _boost_vector(meta)   # cosine + area re-rank
    k = min(k, len(meta))
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]
    return [(float(scores[i]), meta[i]) for i in top]


def cmd_search(query, k=DEFAULT_K):
    if not query:
        sys.exit('usage: semantic.py search "<query>"')
    hits = _rank(query, k)
    if hits is None:
        sys.exit("search: no index. Run: semantic.py build")
    for score, m in hits:
        print(f"{score:.3f}  {m['path']}  -- {m['snippet']}")


def cmd_hook():
    """UserPromptSubmit hook: inject top semantic matches for substantive prompts."""
    out = {"suppressOutput": True}
    try:
        # lstrip a UTF-8 BOM — some shells prepend one when piping to stdin.
        payload = json.loads((sys.stdin.read() or "{}").lstrip("﻿").strip() or "{}")
    except Exception:
        payload = {}
    prompt = (payload.get("prompt") or "").strip()
    # Skip trivial prompts (acks, one-word replies) — not worth the model load.
    if len(prompt) < 12:
        sys.stdout.write(json.dumps(out)); return
    try:
        hits = _rank(prompt, 5)
    except Exception:
        hits = None
    if hits:
        lines = [f"- {m['path']} — {m['snippet']}" for score, m in hits if score > 0.55]
        if lines:
            out["hookSpecificOutput"] = {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    "[vault semantic recall — possibly relevant notes; open the ones that fit]\n"
                    + "\n".join(lines)
                ),
            }
    sys.stdout.write(json.dumps(out))


def cmd_status():
    if not MANIFEST.exists():
        print("status: no index built yet. Run: semantic.py build")
        return
    print(read(MANIFEST))


MISSING = ("semantic: fastembed / numpy are not installed in this interpreter ({exe}); semantic search is off. "
           "Install them with: \"{exe}\" -m pip install -r assistant/requirements-semantic.txt  "
           "(keyword fallback: python scripts/wiki_tool.py search <terms>)")


def main():
    _utf8()
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    try:
        if cmd == "build":
            cmd_build()
        elif cmd == "search":
            cmd_search(" ".join(sys.argv[2:]))
        elif cmd == "hook":
            cmd_hook()
        elif cmd == "status":
            cmd_status()
        else:
            sys.exit(f"unknown command '{cmd}'\n{__doc__}")
    except ImportError as e:            # no fastembed / numpy here: one line, exit 2, never a traceback (2026-10-29)
        if cmd == "hook":
            sys.stdout.write(json.dumps({"suppressOutput": True}))
            return
        print(MISSING.format(exe=sys.executable) + f"  [{e.__class__.__name__}: {e}]", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
