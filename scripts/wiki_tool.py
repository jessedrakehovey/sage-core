#!/usr/bin/env python3
"""wiki_tool.py — deterministic tooling for the Wedding Videography LLM wiki.

Commands:
  index             Rebuild Wiki/_meta/index.md (the catalog agents search first)
  sources           Rebuild Wiki/_meta/sources.md (Raw/ ingestion coverage manifest)
  lint [paths...]   Validate every Wiki/ note against Schema/schema.md (or only the given notes)
  search <terms>    Rank wiki notes against query terms, print top hits
  audit             sources + index + lint in one pass, print summary
  review            Rebuild Wiki/_meta/review-queue.md (all status: draft notes)
  log "<message>"   Append a timestamped entry to Wiki/_meta/log.md
  hook-pending      SessionStart hook: surface pending Raw/ sources (JSON to stdout)
  hook-context      SessionStart hook: inject mistakes.md + memory.md (JSON to stdout)

Run from anywhere; the vault root is located relative to this file.
Exit code 1 if lint finds errors, else 0.
"""

import io
import os
import re
import sys
import json
import datetime
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent
WIKI = VAULT / "Wiki"
RAW = VAULT / "Raw"
META = WIKI / "_meta"
SCHEMA_FILE = VAULT / "Schema" / "schema.md"

REQUIRED_FIELDS = ["type", "area", "tags", "created", "updated", "sources", "status"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FILENAME_RE = re.compile(r"^[A-Za-z0-9 \-&+',.()#]+$")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")

SKIP_DIRS = {".git", ".obsidian", ".claude", "scripts", "__pycache__"}
# compiled-knowledge types are expected to be linked from somewhere (orphan = a lint warning); every other type is a
# profile, a reference leaf (2026-10-29: the set used to be named per profile type, which tied the linter to one schema)
KNOWLEDGE_TYPES = {"workflow", "concept", "reference", "integration", "builder", "package"}

# Files in Raw/ that are never treated as ingestable sources (OS cruft only).
RAW_SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def raw_sources():
    """Every ingestable Raw/ file, ANY type (md, pptx, pdf, docx, txt, media...).

    The wiki coverage tracker is deliberately file-type-agnostic: a contract PDF,
    vendor brochure, or shoot transcript is a source just like a markdown file.
    Only dot-files and OS cruft are skipped. Ingestion itself extracts text per
    type (the wiki-ingest skill handles pptx/pdf/docx extraction).
    """
    out = []
    for p in RAW.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.name in RAW_SKIP_NAMES or p.name.startswith("."):
            continue
        out.append(p)
    return sorted(out)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def load_schema_lists():
    """Parse allowed types/tags from the fenced blocks in Schema/schema.md."""
    text = read(SCHEMA_FILE)
    def block(lang):
        m = re.search(r"```" + lang + r"\n(.*?)```", text, re.S)
        if not m:
            sys.exit(f"ERROR: no ```{lang} block found in Schema/schema.md")
        return {ln.strip() for ln in m.group(1).splitlines() if ln.strip()}
    return block("types"), block("tags")


def parse_front_matter(text):
    """Minimal YAML-subset parser: scalars, inline lists, block lists."""
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end == -1:
        return None, text
    fm_text, body = text[3:end], text[end + 4:]
    fm, current_key = {}, None
    for line in fm_text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if re.match(r"^\s+-\s", line) or (line.startswith("-") and current_key):
            if current_key is not None:
                fm.setdefault(current_key, [])
                if not isinstance(fm[current_key], list):
                    fm[current_key] = [fm[current_key]] if fm[current_key] else []
                fm[current_key].append(line.split("-", 1)[1].strip().strip("\"'"))
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        current_key = key
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            fm[key] = [v.strip().strip("\"'") for v in inner.split(",") if v.strip()] if inner else []
        elif val == "":
            fm[key] = []  # expect block list to follow; stays [] if none
        else:
            fm[key] = val.strip("\"'")
    return fm, body


def wiki_notes():
    """All lintable wiki notes: Wiki/**/*.md minus about.md and _meta/."""
    notes = []
    for p in sorted(WIKI.rglob("*.md")):
        if "_meta" in p.parts or p.name.lower() == "about.md":
            continue
        notes.append(p)
    return notes


def all_note_basenames():
    """Every md basename in the vault — the wikilink resolution set — plus each note's vault-relative path without the
    extension, so an Obsidian path link such as [[Wiki/Clients/about|Clients]] resolves too."""
    names = set()
    for p in VAULT.rglob("*.md"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        names.add(p.stem.lower())
        names.add(p.relative_to(VAULT).with_suffix("").as_posix().lower())
    return names


def first_paragraph(body: str, limit=160):
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if not para or para.startswith("#") or para.startswith("<!--"):
            continue
        text = re.sub(r"\s+", " ", re.sub(r"[*_`>\[\]]", "", para))
        return text[:limit] + ("..." if len(text) > limit else "")
    return ""


def note_title(p: Path, body: str):
    m = re.search(r"^#\s+(.+)$", body, re.M)
    return m.group(1).strip() if m else p.stem


def normalize_source_ref(ref: str):
    """Turn a sources: entry into a comparable basename (lowercase, no ext)."""
    ref = ref.strip().strip("\"'")
    inner = WIKILINK_RE.match(ref)
    if inner:
        ref = inner.group(1)
    ref = ref.replace("\\", "/").split("/")[-1]
    # Strip the final extension of ANY type so a pptx/pdf/docx citation matches
    # its Raw file by stem (mirrors Path.stem used in the coverage scan).
    ref = Path(ref).stem
    return ref.lower()


def collect_ingested_sources():
    """Set of raw-source basenames cited by any wiki note."""
    cited = set()
    for p in wiki_notes():
        fm, _ = parse_front_matter(read(p))
        if not fm:
            continue
        srcs = fm.get("sources", [])
        if isinstance(srcs, str):
            srcs = [srcs]
        for s in srcs:
            cited.add(normalize_source_ref(s))
    return cited


def cmd_index():
    groups = {}
    for p in wiki_notes():
        fm, body = parse_front_matter(read(p))
        area = (fm or {}).get("area", "(no front matter)")
        if isinstance(area, list):
            area = area[0] if area else "(no area)"
        groups.setdefault(str(area), []).append((p, fm or {}, body))
    today = datetime.date.today().isoformat()
    lines = [
        "# Wiki Index (generated — do not hand-edit)",
        "",
        f"Rebuilt: {today} by `scripts/wiki_tool.py index`",
        "",
        "Search this file first. Open only the notes that match. Go to Raw/ only for source-level verification.",
        "",
    ]
    for area in sorted(groups):
        lines.append(f"## {area}")
        lines.append("")
        for p, fm, body in sorted(groups[area], key=lambda t: t[0].name.lower()):
            tags = fm.get("tags", [])
            tags = ", ".join(tags) if isinstance(tags, list) else str(tags)
            desc = first_paragraph(body)
            lines.append(f"- [[{p.stem}]] ({fm.get('type','?')}; {tags or 'no tags'}; {fm.get('status','?')}) — {desc}")
        lines.append("")
    write(META / "index.md", "\n".join(lines) + "\n")
    n = sum(len(v) for v in groups.values())
    print(f"index: OK — {n} notes across {len(groups)} areas -> Wiki/_meta/index.md")


def cmd_sources():
    cited = collect_ingested_sources()
    rows, pending, done = [], 0, 0
    for p in raw_sources():
        rel = p.relative_to(VAULT).as_posix()
        status = "ingested" if p.stem.lower() in cited else "pending"
        if status == "ingested":
            done += 1
        else:
            pending += 1
        rows.append((rel, status))
    today = datetime.date.today().isoformat()
    lines = [
        "# Raw Source Manifest (generated — do not hand-edit)",
        "",
        f"Rebuilt: {today}. {done} ingested / {pending} pending / {len(rows)} total.",
        "",
        "A source is 'ingested' when at least one Wiki/ note cites it in front matter `sources:`.",
        "",
        "| Raw source | Status |",
        "|---|---|",
    ]
    for rel, status in rows:
        mark = "OK" if status == "ingested" else "**pending**"
        lines.append(f"| {rel} | {mark} |")
    write(META / "sources.md", "\n".join(lines) + "\n")
    print(f"sources: OK — {done} ingested, {pending} pending -> Wiki/_meta/sources.md")
    return pending


def cmd_lint(only=None):
    """only: note paths (absolute, or relative to the vault or the cwd) to check instead of the whole wiki. The
    agent's note writer lints just the note it wrote (2026-10-29), so one older note that fails the contract no longer
    refuses every new one; the orphan pass needs the whole wiki and is skipped for a subset."""
    allowed_types, allowed_tags = load_schema_lists()
    link_targets = all_note_basenames()
    errors, warnings = [], []
    notes = wiki_notes()
    if only:
        want = set()
        for o in only:
            p = Path(o)
            for cand in ([p] if p.is_absolute() else [VAULT / p, Path.cwd() / p]):
                try:
                    want.add(cand.resolve())
                except OSError:
                    pass
        notes = [p for p in notes if p.resolve() in want]
    linked_to = set()

    for p in notes:
        rel = p.relative_to(VAULT).as_posix()
        text = read(p)
        fm, body = parse_front_matter(text)

        if not FILENAME_RE.match(p.stem):
            errors.append(f"{rel}: filename has disallowed characters")
        if fm is None:
            errors.append(f"{rel}: missing front matter")
            continue
        for field in REQUIRED_FIELDS:
            if field not in fm:
                errors.append(f"{rel}: missing required field '{field}'")
        t = fm.get("type")
        if t and t not in allowed_types:
            errors.append(f"{rel}: type '{t}' not in Schema/schema.md allowed types")
        tags = fm.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        for tag in tags:
            if tag not in allowed_tags:
                errors.append(f"{rel}: tag '{tag}' not in Schema/schema.md allowed tags")
        for field in ("created", "updated"):
            v = fm.get(field)
            if isinstance(v, str) and not DATE_RE.match(v):
                errors.append(f"{rel}: {field} '{v}' is not YYYY-MM-DD")
        status = fm.get("status")
        if status not in ("draft", "verified"):
            errors.append(f"{rel}: status '{status}' must be draft or verified")
        srcs = fm.get("sources", [])
        if isinstance(srcs, str):
            srcs = [srcs]
        if not srcs:
            (errors if status == "verified" else warnings).append(
                f"{rel}: empty sources ({'verified note must cite sources' if status == 'verified' else 'draft, ok for now'})")
        for link in WIKILINK_RE.findall(text):
            target = link.strip().lower()
            linked_to.add(target)
            if target not in link_targets:
                warnings.append(f"{rel}: unresolved link [[{link.strip()}]] (placeholder — write it or fix the name)")

    # Hub notes (about.md) are exempt from front-matter linting, but their wikilinks are
    # real navigation — count them for orphan detection. (Linking an orphan from its
    # folder's about.md hub clears the orphan warning.)
    for p in sorted(WIKI.rglob("about.md")):
        if "_meta" in p.parts:
            continue
        for link in WIKILINK_RE.findall(read(p)):
            linked_to.add(link.strip().lower())

    for p in ([] if only else notes):
        fm, _ = parse_front_matter(read(p))
        if fm and p.stem.lower() not in linked_to and fm.get("type") in KNOWLEDGE_TYPES:
            warnings.append(f"{p.relative_to(VAULT).as_posix()}: orphan — no other wiki note links to it")

    for e in errors:
        print(f"ERROR  {e}")
    for w in warnings:
        print(f"WARN   {w}")
    print(f"lint: {len(notes)} notes checked — {len(errors)} errors, {len(warnings)} warnings")
    return len(errors)


def cmd_search(terms):
    if not terms:
        sys.exit("usage: wiki_tool.py search <terms>")
    # Split each arg on whitespace so a quoted phrase ("delivery timeline email")
    # is matched as individual words, not as one literal phrase that never occurs.
    terms = [w.lower() for arg in terms for w in arg.split()]
    scored = []
    for p in wiki_notes():
        text = read(p)
        fm, body = parse_front_matter(text)
        fm = fm or {}
        title = note_title(p, body if fm else text).lower()
        tags = fm.get("tags", [])
        tags = [t.lower() for t in (tags if isinstance(tags, list) else [tags])]
        lower = text.lower()
        score = 0
        for t in terms:
            score += 6 * title.count(t) + 4 * sum(tag.count(t) for tag in tags) + lower.count(t)
        if score > 0:
            scored.append((score, p, first_paragraph(body)))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        print("search: no hits in Wiki/. Try Wiki/_meta/sources.md for a pending raw source.")
        return
    for score, p, desc in scored[:10]:
        print(f"{score:>5}  {p.relative_to(VAULT).as_posix()}  -- {desc}")


def cmd_review():
    """Rebuild Wiki/_meta/review-queue.md: every draft note awaiting the owner's review."""
    drafts = []
    for p in wiki_notes():
        fm, body = parse_front_matter(read(p))
        if not fm or fm.get("status") != "draft":
            continue
        area = fm.get("area", "")
        if isinstance(area, list):
            area = area[0] if area else ""
        drafts.append((str(area), p, note_title(p, body), first_paragraph(body, 120)))
    drafts.sort(key=lambda t: (t[0], t[1].name.lower()))
    today = datetime.date.today().isoformat()
    lines = [
        "# Review Queue (generated — do not hand-edit)",
        "",
        f"Rebuilt: {today} by `scripts/wiki_tool.py review`",
        "",
        f"{len(drafts)} note(s) in `status: draft` awaiting review. To clear an item: "
        "open the note, verify it against its cited Raw/ sources, fix anything off, "
        "then change `status: draft` to `status: verified` and re-run this command.",
        "",
    ]
    if not drafts:
        lines.append("*Nothing pending — every wiki note is verified.*")
    else:
        current = None
        for area, p, title, desc in drafts:
            if area != current:
                lines += ["", f"## {area or '(no area)'}", ""]
                current = area
            lines.append(f"- [ ] [[{p.stem}]] — {desc}")
    write(META / "review-queue.md", "\n".join(lines) + "\n")
    print(f"review: OK — {len(drafts)} draft note(s) -> Wiki/_meta/review-queue.md")
    return len(drafts)


def dreaming_wired(max_age_h=72):
    """Is a nightly dreaming job actually running on this machine? Token-free evidence only (2026-10-29): the
    supervisor's heartbeat (assistant/data/serve_alive.json, fresh) with a dreaming job in assistant/serve.json, a fresh
    dreaming log (~/.claude/dreaming-wv.log or WV_DREAM_LOG), or a dreaming task among org.json -> watched_tasks (this
    PC's scheduled tasks). A new organization has none of these until it starts `sage serve`."""
    now = datetime.datetime.now().timestamp()

    def fresh(p):
        try:
            return p.exists() and now - p.stat().st_mtime < max_age_h * 3600
        except OSError:
            return False
    try:
        if fresh(VAULT / "assistant" / "data" / "serve_alive.json"):
            jobs = json.loads(read(VAULT / "assistant" / "serve.json")).get("jobs", [])
            if any(j.get("name") == "dreaming" and j.get("enabled", True) for j in jobs):
                return True
    except (OSError, ValueError, AttributeError):
        pass
    log = Path(os.environ.get("WV_DREAM_LOG") or (Path.home() / ".claude" / "dreaming-wv.log"))
    if fresh(log):
        return True
    try:
        org = json.loads(read(VAULT / "assistant" / "org.json"))
        if any("dreaming" in str(t).lower() for t in (org.get("watched_tasks") or [])):
            return True
    except (OSError, ValueError, AttributeError):
        pass
    return False


def cmd_hook_pending():
    """SessionStart hook: emit a reminder if Raw/ sources are pending ingestion.

    Prints hook JSON to stdout. Reminder only when count > 0, so a fully
    ingested vault produces a silent, empty-context hook. Never auto-runs
    ingestion — it only surfaces the count and how to act. The nightly ingest is
    promised only when a dreaming job is actually wired (dreaming_wired).
    """
    cited = collect_ingested_sources()
    pending = [p for p in raw_sources() if p.stem.lower() not in cited]
    n = len(pending)
    out = {"suppressOutput": True}
    if n:
        if dreaming_wired():
            tail = ("The nightly dreaming cron auto-ingests up to 3/night into draft notes, so "
                    "this backlog drains on its own — no action needed. To compile some now, run "
                    "the wiki-ingest skill (or say \"ingest\").")
        else:
            tail = ("No nightly dreaming job is running on this machine yet (no supervisor heartbeat, dreaming log or "
                    "scheduled task), so this backlog does NOT drain on its own: run the wiki-ingest skill (say "
                    "\"ingest\"), or start `python assistant/sage.py serve` so the 06:30 dreaming job takes up to 3 a night.")
        out["hookSpecificOutput"] = {
            "hookEventName": "SessionStart",
            "additionalContext": (
                f"[wiki] {n} Raw/ source file(s) of any type (md, pptx, pdf, docx, "
                f"transcripts...) are pending ingestion (not yet cited by any Wiki/ note). "
                + tail + " Pending list: Wiki/_meta/sources.md."
            ),
        }
    sys.stdout.write(json.dumps(out))


def cmd_hook_context():
    """SessionStart hook: inject the two must-read files directly into context.

    Workflow Rule #1 says read CLAUDE.md -> mistakes.md -> memory.md before any
    work. CLAUDE.md auto-loads; this hook guarantees the other two are present
    instead of depending on the agent remembering to read them.

    Borrowed from everything-claude-code's session-start.js (2026-06-29):
      - VISIBLE truncation markers. These files can outgrow a silent per-file
        cut (mistakes.md grows over time), so any truncation announces itself
        and points at the full file on disk.
      - A TOTAL budget ceiling (not just per-file), since this is paid on every
        turn. mistakes.md is emitted first, so memory.md is trimmed first.
      - A stale-replay guard on memory.md: status/situation lines are
        stale-by-default and must be verified against live state before action.
    Env overrides: WV_HOOK_CONTEXT_OFF=1 disables injection entirely;
    WV_HOOK_CONTEXT_MAX caps the total chars (0 = no total cap);
    WV_HOOK_CONTEXT_PER_FILE caps each file (0 = no per-file cap).
    Fails silent (empty context) if either file is missing.
    """
    if os.environ.get("WV_HOOK_CONTEXT_OFF", "").lower() in ("1", "true", "yes", "on"):
        sys.stdout.write(json.dumps({"suppressOutput": True}))
        return

    def _int_env(name, default):
        try:
            return int(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default

    # Per-file and total budget. mistakes.md is emitted first so if the total
    # budget is tight, memory.md is trimmed before mistakes.md is touched.
    PER_FILE = _int_env("WV_HOOK_CONTEXT_PER_FILE", 12000)
    TOTAL = _int_env("WV_HOOK_CONTEXT_MAX", 22000)

    def _clip(text, cap, label):
        if cap and len(text) > cap:
            marker = ("\n[... %s truncated to %d chars — full file on disk; read it "
                      "if you need the rest ...]" % (label, cap))
            return text[:max(0, cap - len(marker))].rstrip() + marker
        return text

    def _clip_keep_newest(text, cap, label):
        """mistakes.md is append-only, newest at the bottom. Clipping the tail (as _clip does)
        would drop exactly the newest lessons the moment the file passes the cap, while the
        oldest stay injected forever. Keep the header (up to '## Entries') plus the newest
        entries that fit."""
        if not cap or len(text) <= cap:
            return text
        i = text.find("## Entries")
        head = text[:i + len("## Entries")] if 0 <= i < cap // 3 else ""
        marker = ("\n[... older %s entries omitted to fit %d chars; full file on disk ...]\n" % (label, cap))
        room = max(0, cap - len(head) - len(marker))
        tail = text[len(text) - room:] if room else ""
        j = tail.find("\n---")                 # start at an entry boundary
        if 0 <= j < len(tail) // 2:
            tail = tail[j:]
        return head + marker + tail.lstrip("\n")

    MEMORY = VAULT / "Memory" / "memory.md"
    MISTAKES = VAULT / "Schema" / "mistakes.md"
    parts = []
    if MISTAKES.exists():
        parts.append("=== Schema/mistakes.md (never repeat a logged mistake) ===\n"
                     + _clip_keep_newest(read(MISTAKES), PER_FILE, "mistakes.md"))
    if MEMORY.exists():
        parts.append("=== Memory/memory.md (couple/vendor/venue & project context not in a booking system) ===\n"
                     "[stale-replay guard] Status/situation lines below (\"X is editing / "
                     "awaiting / half-done / active\") are STALE-BY-DEFAULT: they reflect when "
                     "written, not now. Verify against live project state / git / working-tree "
                     "before acting on any of them.\n"
                     + _clip(read(MEMORY), PER_FILE, "memory.md"))
    out = {"suppressOutput": True}
    if parts:
        body = "\n\n".join(parts)
        if TOTAL and len(body) > TOTAL:
            marker = ("\n\n[SessionStart context capped at %d chars — full files at "
                      "Schema/mistakes.md + Memory/memory.md. Raise WV_HOOK_CONTEXT_MAX "
                      "or set WV_HOOK_CONTEXT_OFF=1.]" % TOTAL)
            body = body[:max(0, TOTAL - len(marker))].rstrip() + marker
        out["hookSpecificOutput"] = {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "[vault memory — auto-injected; you do NOT need to re-read these files]\n\n"
                + body
                + "\n\n[retrieval] For operational lookups, semantic-search the wiki: "
                  "`python scripts/semantic.py search \"<query>\"` "
                  "(keyword fallback: `python scripts/wiki_tool.py search <terms>`)."
            ),
        }
    sys.stdout.write(json.dumps(out))


def cmd_log(message):
    today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    log = META / "log.md"
    existing = read(log) if log.exists() else "# Wiki Log (newest first)\n\n"
    head, _, rest = existing.partition("\n\n")
    entry = f"- **{today}** — {message}\n"
    write(log, head + "\n\n" + entry + rest)
    print(f"log: OK — entry added to Wiki/_meta/log.md")


def main():
    # Windows consoles default to cp1252; wiki notes contain em-dashes and curly
    # quotes, so printing search/audit output would raise UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "index":
        cmd_index()
    elif cmd == "sources":
        cmd_sources()
    elif cmd == "lint":
        sys.exit(1 if cmd_lint(sys.argv[2:] or None) else 0)
    elif cmd == "search":
        cmd_search(sys.argv[2:])
    elif cmd == "review":
        cmd_review()
    elif cmd == "hook-pending":
        cmd_hook_pending()
    elif cmd == "hook-context":
        cmd_hook_context()
    elif cmd == "log":
        cmd_log(" ".join(sys.argv[2:]) or "(no message)")
    elif cmd == "audit":
        pending = cmd_sources()
        cmd_index()
        errs = cmd_lint()
        drafts = cmd_review()
        print(f"audit: {'FAIL' if errs else 'PASS'} — {pending} raw sources pending ingestion, {drafts} draft(s) to review")
        sys.exit(1 if errs else 0)
    else:
        sys.exit(f"unknown command '{cmd}'\n{__doc__}")


if __name__ == "__main__":
    main()
