#!/usr/bin/env python3
"""session_capture.py — SessionEnd hook: deposit a raw session record into the
day's Memory/Daily_Logs file, so the Dreaming phase has something to consolidate.

This is the *capture* half of memory creation. It is deliberately dumb and
token-free: it records what the human asked and the assistant's closing note,
verbatim-ish (truncated). The *intelligence* — deciding what's durable — is the
Dreaming skill's job, run later over these logs.

Reads the SessionEnd hook JSON from stdin (transcript_path, session_id, cwd),
parses the transcript, and appends one dated block to
Memory/Daily_Logs/YYYY-MM-DD.md. Append-only; never rewrites past entries.
Fails silent on any error — capture must never break a session's exit.
"""

import io
import os
import sys
import json
import datetime
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent
LOGS = VAULT / "Memory" / "Daily_Logs"

PROMPT_CAP = 400     # truncate each human prompt
OUTCOME_CAP = 600    # truncate the closing assistant note
MAX_PROMPTS = 25     # guard against pathological sessions


def _text_from_content(content):
    """Extract human/assistant text from a message.content (str or list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts)
    return ""


def _is_noise(text):
    """Skip injected/non-human turns: tool results, hooks, system reminders."""
    t = text.strip()
    if not t:
        return True
    # Hook output, command wrappers, and system reminders are not human prompts.
    low = t.lstrip()
    if low.startswith("<system-reminder>") or low.startswith("<command-")           or low.startswith("Caveat:") or low.startswith("<local-command"):
        return True
    return False


def parse_transcript(path):
    """Return (human_prompts, final_assistant_text) from a transcript JSONL."""
    prompts, final_assistant = [], ""
    try:
        with io.open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                typ = obj.get("type")
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                # Skip tool_result turns (they're type user but machine-generated).
                if isinstance(content, list) and any(
                    isinstance(i, dict) and i.get("type") == "tool_result" for i in content
                ):
                    continue
                text = _text_from_content(content).strip()
                if typ == "user":
                    if not _is_noise(text):
                        prompts.append(text)
                elif typ == "assistant":
                    if text:
                        final_assistant = text  # keep the last non-empty one
    except Exception:
        pass
    return prompts, final_assistant


def main():
    try:
        payload = json.loads((sys.stdin.read() or "{}").lstrip("﻿").strip() or "{}")
    except Exception:
        payload = {}

    # Headless cron sessions (operator ticks, the CRM transports, dreaming itself, the
    # scouts) are not the owner working: 11 of 13 logged sessions on 2026-10-01 were crons, which
    # dreaming then re-read every night. The headless launchers set WV_HEADLESS=1.
    if os.environ.get("WV_HEADLESS") == "1":
        return

    tpath = payload.get("transcript_path")
    sid = (payload.get("session_id") or "unknown")
    cwd = payload.get("cwd") or ""
    if not tpath or not os.path.exists(tpath):
        return  # nothing to capture

    prompts, outcome = parse_transcript(tpath)
    if not prompts:
        return  # empty / non-substantive session — nothing worth logging

    LOGS.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    day_file = LOGS / f"{now:%Y-%m-%d}.md"
    sid8 = sid[:8]
    marker = f"<!--sid:{sid}-->"

    # Dedup: SessionEnd should fire once, but guard against a double-append.
    if day_file.exists() and marker in day_file.read_text(encoding="utf-8", errors="ignore"):
        return

    lines = []
    if not day_file.exists():
        lines += [
            f"# Daily Log — {now:%Y-%m-%d}",
            "",
            "<!-- Auto-captured by scripts/session_capture.py (SessionEnd hook). "
            "Raw source material for the Dreaming phase; do not hand-edit past entries. -->",
            "",
        ]
    cwd_tag = Path(cwd).name if cwd else ""
    lines.append(f"## {now:%H:%M} — session {sid8}" + (f" ({cwd_tag})" if cwd_tag else ""))
    lines.append(marker)
    lines.append("")
    # Flatten, then collapse consecutive duplicates (repeated pastes/retries).
    flat_prompts = []
    for p in prompts:
        flat = " ".join(p.split())
        if len(flat) > PROMPT_CAP:
            flat = flat[:PROMPT_CAP] + "…"
        if flat_prompts and flat_prompts[-1][0] == flat:
            flat_prompts[-1][1] += 1
        else:
            flat_prompts.append([flat, 1])
    lines.append("**Asked:**")
    # Long sessions: keep the first few (what the session was for) and the LAST ones, where
    # corrections and "remember this" usually come. Keeping only the first 25 dropped them.
    HEAD = 5
    if len(flat_prompts) > MAX_PROMPTS:
        shown = flat_prompts[:HEAD] + [None] + flat_prompts[-(MAX_PROMPTS - HEAD):]
    else:
        shown = flat_prompts
    for row in shown:
        if row is None:
            lines.append(f"- …(+{len(flat_prompts) - MAX_PROMPTS} earlier prompts omitted)")
            continue
        text, count = row
        lines.append(f"- {text}" + (f" (×{count})" if count > 1 else ""))
    if outcome:
        flat = " ".join(outcome.split())
        if len(flat) > OUTCOME_CAP:
            flat = flat[:OUTCOME_CAP] + "…"
        lines += ["", "**Closing note:**", f"> {flat}"]
    lines += ["", ""]

    with io.open(day_file, "a", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
