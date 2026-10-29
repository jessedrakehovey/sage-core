"""notes.py - the doer's milestone notes: its checkpoints, and the few "still on it" lines an owner sees.

Borrowed from the property OS's desk (2026-10-28): its owner was asking "where is it?" eleven times a day, and
a run that died lost everything it had done, so every job there writes a one-line note at each milestone and
a resumed turn reads the notes back. Here a note is one plain line the doer's model turn writes at a milestone
(signed in, dates set, the second item in the cart), when blocked, and right before a checkpoint:

    python assistant/doer/report.py note --ticket <id> [--kind progress|blocked|done] "<line>"

Code does three things with it, no model:
  1. appends it to out/doer/<ticket>/notes.jsonl, the job's checkpoint file. A turn that ran out of steps or
     time is CONTINUED (doer.carry_on): the same session, or a fresh one, gets the notes and carries on from the
     last milestone instead of starting the job over (2026-10-27: a six-item rental cart died at step 80 with
     nothing to show for it).
  2. rewrites the Now: line of the live status file, so the agent answers "where is it?" with it.
  3. records it on the job's transcript through receipts.note, held: Mission Control shows it, Chat does not.
     Chat hears the job once, when it finishes or stops at its checkpoint (2026-10-28: three step-by-step lines
     with prices went to the team space, where staff read, in four minutes). config doer.notes_per_job_max is
     no longer read.

Org-free: the agent's name, the owner and the chat come from the ticket and the profile, never from here.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (ROOT / "scripts", ROOT / "agent"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

OUT = ROOT / "out" / "doer"
STATUS = Path(os.environ["DOER_STATUS"]) if os.environ.get("DOER_STATUS") else OUT / "status.md"
KINDS = ("progress", "blocked", "done")
TEXT_MAX = 300
_TID = re.compile(r"[0-9a-f]{16}")
# a password, or a verification code next to the word "code", never lands in a note (an order number is fine)
_SECRET = re.compile(r"\b(?:password|passcode)\b\s*[:=]|\bcode\b[^.]{0,20}\b\d{4,8}\b|\b\d{4,8}\b[^.]{0,20}\bcode\b", re.I)


def path(tid: str) -> Path:
    return OUT / tid / "notes.jsonl"


def read(tid: str) -> list:
    p = path(tid)
    rows = []
    if not p.is_file():
        return rows
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("text"):
            rows.append(d)
    return rows


def last(tid: str, by: str = "") -> dict:
    """The newest note; `by` = 'turn' for the newest the model turn itself wrote (not the code's 'still on it')."""
    rows = [r for r in read(tid) if not by or r.get("by", "turn") == by]
    return rows[-1] if rows else {}


def add(tid: str, text: str, kind: str = "progress", by: str = "turn") -> dict:
    """Append one note. Raises ValueError on a bad ticket id, an empty line, an unknown kind or a secret."""
    if not _TID.fullmatch(str(tid or "")):
        raise ValueError("ticket id must be the 16-hex assistant ticket id")
    t = " ".join(str(text or "").split())
    if not t:
        raise ValueError("a note needs a line of text")
    if kind not in KINDS:
        raise ValueError("kind must be one of " + ", ".join(KINDS))
    if _SECRET.search(t):
        raise ValueError("a note never carries a password or a code")
    row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "text": t[:TEXT_MAX], "by": by}
    p = path(tid)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def set_now(text: str) -> None:
    """Only the Now: line of the live status file (the turn's own edits touch the same line)."""
    try:
        body = STATUS.read_text(encoding="utf-8") if STATUS.is_file() else ""
    except OSError:
        body = ""
    line = f"Now: {time.strftime('%H:%M')} {' '.join(str(text or '').split())[:280]}"
    lines = body.splitlines()
    if any(ln.startswith("Now:") for ln in lines):
        lines = [line if ln.startswith("Now:") else ln for ln in lines]
    else:
        lines.append(line)
    try:
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        STATUS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def block(tid: str) -> str:
    """The notes as a resumed turn reads them: a numbered list, oldest first, '' when there are none."""
    rows = read(tid)
    if not rows:
        return ""
    return "\n".join(f"{i}. [{r.get('ts', '')[-8:-3]} {r.get('kind', 'progress')}] {r.get('text', '')}" for i, r in enumerate(rows, 1))


def _ticket(tid: str) -> dict:
    try:
        import tickets
        return tickets._load().get(tid) or {}
    except Exception:  # noqa: BLE001
        return {}


def post_line(t: dict, text: str) -> str:
    """The note on the job's transcript, held off Chat (receipts.note with cap 0: an outbox row and a held
    transcript line, which Mission Control shows). Tests replace this. -> '' (nothing reaches Chat)."""
    p = t.get("payload") or {}
    key = str(p.get("from") or "owner")
    try:
        import receipts
        return receipts.note(str(t.get("id") or ""), str(p.get("convo") or f"dm:{key}"), str(p.get("thread") or ""), text,
                             space=str(p.get("space") or ""), channel="doer", cap=0)
    except Exception as e:  # noqa: BLE001
        _log(f"note post failed: {type(e).__name__}: {str(e)[:120]}")
        return ""


def record(tid: str, text: str, kind: str = "progress", by: str = "turn") -> str:
    """add + the status line + the held transcript line. -> 'NOTE=<n>' or 'REFUSED <why>'."""
    t = _ticket(tid)
    if not t:
        return "REFUSED no such ticket"
    try:
        row = add(tid, text, kind, by)
    except ValueError as e:
        return f"REFUSED {e}"
    set_now(("blocked: " if kind == "blocked" else "") + row["text"])
    post_line(t, row["text"])
    return f"NOTE={len(read(tid))}"


def _log(msg: str) -> None:
    try:
        import doer
        doer.log(msg)
    except Exception:  # noqa: BLE001
        pass
