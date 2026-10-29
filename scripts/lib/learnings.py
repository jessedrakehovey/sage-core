"""learnings.py -- capture durable edit-corrections to a per-desk learnings.md (self-learning).

When the owner taps Edit on a staged card and the redraft succeeds, the redraft step distills a
one-line, GENERALIZABLE lesson from his change and calls `record_lesson()` here. The desk's
drafting step reads the same learnings.md so the NEXT similar draft starts closer to what the owner
would send -- the desk gets smarter from his taps without him repeating himself.

This is a pure local note: it never sends, never writes to any external service, touches no
send-gate / client-facing / legal text. Append-only, deduped, capped. Stdlib only. Reversible.

Paths self-resolve to the wedding folder (C:\\Users\\the owner\\Documents\\the organization).

Public API:
    record_lesson(md_path, lesson, context="") -> bool
        Append one distilled lesson. Returns True if written, False if skipped (empty / "NONE" /
        duplicate). Safe to call on every successful redraft.
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MAX_LESSONS = 50  # bound the file so the drafting prompt stays cheap to read

_HEADER = (
    "# {title}\n\n"
    "Auto-appended when owner1 taps Edit on a staged card and the redraft succeeds.\n"
    "Each line is a durable, generalizable preference distilled from ONE correction. The desk's\n"
    "drafting step reads this file so future drafts start closer to what owner1 would send.\n"
    "Git is the rollback; do not hand-tune individual lines casually.\n\n"
    "## Lessons\n"
)


def _norm(s: str) -> str:
    """Lowercase, collapse whitespace, drop trailing punctuation -- for dedup comparison."""
    s = re.sub(r"\s+", " ", (s or "").strip().lower())
    return s.rstrip(".!,;: ")


def _bullet_text(line: str) -> str:
    """Strip '- YYYY-MM-DD -- ' prefix and any '(context)' suffix to get the lesson core."""
    m = re.match(r"-\s*\d{4}-\d{2}-\d{2}\s*[—-]\s*(.*)$", line.strip())
    core = m.group(1) if m else line.strip().lstrip("- ")
    core = re.sub(r"\s*\([^)]*\)\s*$", "", core)  # drop trailing (context)
    return core


def _is_dup(new_norm: str, existing_lines: list[str]) -> bool:
    for ln in existing_lines:
        ex = _norm(_bullet_text(ln))
        if not ex:
            continue
        if ex == new_norm:
            return True
        # substring containment in either direction for non-trivial lessons
        if len(new_norm) >= 15 and (new_norm in ex or ex in new_norm):
            return True
    return False


def record_lesson(md_path, lesson: str, context: str = "", title: str = "Desk learnings") -> bool:
    """Append a distilled lesson to md_path. Returns True if written, False if skipped."""
    lesson = (lesson or "").strip()
    if not lesson or lesson.upper() == "NONE":
        return False
    # one line only; guard against a model emitting a paragraph
    lesson = re.sub(r"\s+", " ", lesson).strip()
    if len(lesson) > 300:
        lesson = lesson[:297].rstrip() + "..."

    path = Path(md_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    bullet_lines = [ln for ln in existing.splitlines() if ln.strip().startswith("- ")]

    if _is_dup(_norm(lesson), bullet_lines):
        return False

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ctx = f" ({context.strip()})" if context and context.strip() else ""
    new_bullet = f"- {date} — {lesson}{ctx}"

    if not existing.strip():
        body = _HEADER.format(title=title) + new_bullet + "\n"
    else:
        # cap: keep header + most-recent (MAX_LESSONS-1) bullets + the new one
        kept = bullet_lines[-(MAX_LESSONS - 1):] if len(bullet_lines) >= MAX_LESSONS else bullet_lines
        head = existing.split("## Lessons")[0].rstrip() if "## Lessons" in existing else _HEADER.format(title=title).rstrip()
        body = head + "\n\n## Lessons\n" + "\n".join(kept + [new_bullet]) + "\n"

    # atomic write
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return True
