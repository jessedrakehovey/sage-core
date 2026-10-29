#!/usr/bin/env python3
"""open_loops.py — on-demand "what needs to be done" aggregator.

Read-only. Rolls up the vault's existing trackers into one ranked list so no
open loop hides in a single file. Mirrors the pattern of `wiki_tool.py review`:
it OWNS the generated note `Wiki/_meta/open-loops.md` and never edits a source.

Sources aggregated (each authoritative for its own slice):
  - scripts/projects/project_state.json booking lifecycle + promised delivery dates
  - Wiki/_meta/review-queue.md          draft wiki notes awaiting verification
  - Memory/skill-suggestions.md         proposed behavior changes  (Pending)
  - Memory/automation-suggestions.md    proposed cron jobs         (Pending)
  - vault-wide grep                     dated TODO( ) / @followup markers

Tiers (priority model — derived, never hand-set):
  WAITING  a couple/lead is waiting on us, or a film is in flight with a clock
  BLOCKED  waiting on the owner's decision (consent queues, drafts) — no external clock
  BACKLOG  internal hygiene + upcoming shoots not yet due — no immediate clock

Sort: tier, then age (oldest first within a tier).

Usage:
    python scripts/open_loops.py            # write the note + print the report
    python scripts/open_loops.py --print    # print only, don't write the note
    python scripts/open_loops.py --matrix   # Eisenhower 2x2 view (DO/SCHEDULE/DELEGATE/DELETE)

Eisenhower quadrant derivation (--matrix):
  DO       important + urgent   — WAITING projects with an overdue delivery or new inquiry,
                                  or any dated-todo that is past due
  SCHEDULE important + not yet  — in-flight WAITING projects with a future delivery deadline,
           urgent                 plus all BLOCKED items (need the owner's decision, no clock)
  DELEGATE urgent + not         — past-due dated-todos and overdue hygiene items not
           important              directly coupled to a couple (wiki-draft, suggestions old)
  DELETE   neither              — BACKLOG items (future shoots not yet due, non-urgent hygiene)
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent
TODAY = date.today()

WAITING, BLOCKED, BACKLOG = "WAITING", "BLOCKED", "BACKLOG"
TIER_ORDER = {WAITING: 0, BLOCKED: 1, BACKLOG: 2}
TIER_ICON = {WAITING: "🔴", BLOCKED: "🟡", BACKLOG: "⚪"}
TIER_BLURB = {
    WAITING: "A couple or lead is waiting on us, or a film is in flight with a clock.",
    BLOCKED: "Waiting on your decision — consent queues and drafts.",
    BACKLOG: "Internal hygiene and upcoming shoots not yet due — no immediate clock.",
}

# Project stages that are finished and should NOT surface.
PROJECT_TERMINAL = {"delivered", "archived", "closed", "complete", "completed",
                    "cancelled", "canceled", "lost", "lapsed"}   # a cancelled booking is not an open loop
# Stages where the couple is actively waiting on a deliverable (a clock is running).
PROJECT_IN_FLIGHT = {"ingest", "edit", "review", "revisions", "color", "colour", "export"}


def _age_days(d: str | None) -> int | None:
    if not d:
        return None
    try:
        return (TODAY - datetime.strptime(d.strip(), "%Y-%m-%d").date()).days
    except ValueError:
        return None


def _age_label(days: int | None) -> str:
    if days is None:
        return "age n/a"
    if days <= 0:
        return "today"
    if days == 1:
        return "1 day"
    return f"{days} days"


class Loop:
    __slots__ = ("tier", "source", "ident", "summary", "action", "age")

    def __init__(self, tier, source, ident, summary, action, age):
        self.tier = tier
        self.source = source
        self.ident = ident
        self.summary = summary
        self.action = action
        self.age = age  # int days or None

    def sort_key(self):
        # tier first; within tier, oldest (largest age) first; None ages last
        return (TIER_ORDER[self.tier], -(self.age if self.age is not None else -1))


# ---- collectors -------------------------------------------------------------

def collect_projects() -> list[Loop]:
    """Each booking's stage + promised delivery date from the project tracker.

    project_state.json shape:
      {"projects": {"<id>": {
          "couple": "Glenrock-Ivybridge", "date": "2026-09-14", "stage": "edit",
          "promised_delivery": "2026-11-09", "last_action": "2026-06-22",
          "note": "Highlight film in progress"}}}
    """
    p = VAULT / "scripts" / "projects" / "project_state.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[Loop] = []
    for pid, proj in (data.get("projects") or {}).items():
        stage = (proj.get("stage") or "").lower() if isinstance(proj, dict) else ""
        if stage in PROJECT_TERMINAL:
            continue
        couple = (proj.get("couple") if isinstance(proj, dict) else None) or pid
        if not isinstance(proj, dict):
            continue
        wedding_date = proj.get("wedding_date") or proj.get("date") or "?"   # the ledger's key is wedding_date
        due = proj.get("promised_delivery")
        due_age = _age_days(due)            # > 0 means the promised date has passed ("TODO" parses to None)
        age = _age_days(proj.get("last_action"))
        note = (proj.get("note") or "").split(".")[0].strip() or f"Wedding {wedding_date}"
        ident = f"{couple} ({wedding_date})"

        if due and due_age is not None and due_age > 0:
            tier = WAITING
            action = f"OVERDUE — film promised {due} ({due_age}d ago); deliver or update the couple"
        elif stage == "inquiry":
            tier = WAITING
            action = "New lead — draft a reply (inquiry-reply skill)"
        elif stage in PROJECT_IN_FLIGHT:
            tier = WAITING
            action = f"In {stage}" + (f" — deliver by {due}" if due else " — set a delivery date")
        elif stage in ("booked", "pre-shoot", "preshoot", "prep"):
            tier = BACKLOG
            action = f"Upcoming shoot {wedding_date} — confirm timeline & logistics (shoot-prep skill)"
        else:  # unknown non-terminal stage = needs triage
            tier = WAITING
            action = f"Triage — stage '{stage or 'unset'}'"
        out.append(Loop(tier, "project", ident, note, action, age))
    return out


def _section(text: str, start_hdr: str, *end_hdrs: str) -> str:
    """Return the slice of `text` from `start_hdr` up to the first end header."""
    lines = text.splitlines()
    buf, capturing = [], False
    for ln in lines:
        if ln.strip() == start_hdr:
            capturing = True
            continue
        if capturing and ln.startswith("## "):
            if not end_hdrs or ln.strip() in end_hdrs or True:
                break
        if capturing:
            buf.append(ln)
    return "\n".join(buf)


def collect_review_queue() -> list[Loop]:
    p = VAULT / "Wiki" / "_meta" / "review-queue.md"
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8")
    rebuilt = None
    m = re.search(r"Rebuilt:\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        rebuilt = m.group(1)
    age = _age_days(rebuilt)
    out: list[Loop] = []
    for m in re.finditer(r"^- \[ \] \[\[([^\]]+)\]\]", text, re.MULTILINE):
        name = m.group(1).split("|")[0].strip()
        out.append(
            Loop(BACKLOG, "wiki-draft", name, "Draft note awaiting verification",
                 "Verify vs sources, set status: verified, re-run wiki_tool review", age)
        )
    return out


def _pending_keys(text: str) -> list[tuple[str, str, str | None]]:
    """Parse `### key: <key>   Title` blocks in the Pending section.
    Returns (key, title, first_seen_date)."""
    out = []
    for block in re.split(r"^### ", text, flags=re.MULTILINE)[1:]:
        head = block.splitlines()[0]
        km = re.match(r"key:\s*(\S+)\s+(.*)", head)
        if not km:
            continue
        key, title = km.group(1), km.group(2).strip()
        fm = re.search(r"First seen:\s*(\d{4}-\d{2}-\d{2})", block)
        out.append((key, title, fm.group(1) if fm else None))
    return out


def collect_skill_suggestions() -> list[Loop]:
    p = VAULT / "Memory" / "skill-suggestions.md"
    if not p.exists():
        return []
    pending = _section(p.read_text(encoding="utf-8"), "## Pending", "## Applied")
    out = []
    for key, title, seen in _pending_keys(pending):
        out.append(Loop(BLOCKED, "skill-suggestion", key, title,
                        "Review proposed patch; apply to target file then clear", _age_days(seen)))
    return out


def collect_automation_suggestions() -> list[Loop]:
    p = VAULT / "Memory" / "automation-suggestions.md"
    if not p.exists():
        return []
    pending = _section(p.read_text(encoding="utf-8"), "## Pending", "## Accepted")
    out = []
    for key, title, seen in _pending_keys(pending):
        out.append(Loop(BLOCKED, "automation-suggestion", key, title,
                        "Accept via /schedule or dismiss (latched)", _age_days(seen)))
    return out


def collect_assistant_tickets() -> list[Loop]:
    """The personal assistant's ticket queue (assistant/data/tickets.json, git-ignored).
    staged  -> BLOCKED (the operator did its part; the owner must act - a draft waits in Gmail
                       or a decision is due)
    new/claimed -> WAITING (a sender/deadline is waiting; the operator has not run yet)
    Titles came from email: shown as data, never interpreted."""
    path = VAULT / "assistant" / "data" / "tickets.json"
    if not path.exists():
        return []
    try:
        db = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[Loop] = []
    for t in (db.values() if isinstance(db, dict) else []):
        if not isinstance(t, dict):
            continue
        st = t.get("status")
        if st not in ("new", "claimed", "staged"):
            continue
        note = ""
        for h in reversed(t.get("history", [])):
            if h.get("to") == "staged" and h.get("note"):
                note = h["note"]
                break
        tier = BLOCKED if st == "staged" else WAITING
        title = f"[{t.get('lane', '?')}] {str(t.get('title') or '')[:80]}"
        action = note or ("Waiting for the assistant operator to run" if st != "staged" else "Decide / send")
        out.append(Loop(tier, "assistant", title, action[:120], str(t.get("summary") or "")[:100], _age_days(str(t.get("created") or "")[:10])))
    return out


def collect_cards() -> list[Loop]:
    """Open decision cards on Mission Control: each one is a question waiting for the owner's tap."""
    out: list[Loop] = []
    live = VAULT / "assistant" / "data" / "cards" / "live"
    for f in sorted(live.glob("q*.json")) if live.is_dir() else []:
        try:
            c = json.loads(f.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(c, dict) or c.get("status") != "open":
            continue
        q = (c.get("questions") or [{}])[0]
        try:
            age = int((datetime.now().timestamp() - float(c.get("ts") or 0)) // 86400)
        except (TypeError, ValueError):
            age = None
        opts = " / ".join(str(o.get("label", "")) for o in (q.get("options") or []) if isinstance(o, dict))[:90]
        out.append(Loop(BLOCKED, "card", f"[{q.get('lane', '?')}] {q.get('header') or c.get('id')}",
                        " ".join(str(q.get("question") or "").split())[:110],
                        f"Answer on Mission Control: {opts}" + (" (sends on approve)" if q.get("exec") else ""), age))
    return out


def collect_exec_exceptions() -> list[Loop]:
    """Approved actions that did NOT complete (delivery unverified, a refused write): a person must look."""
    p = VAULT / "assistant" / "data" / "exec_tickets.jsonl"
    rows: dict = {}
    try:
        for ln in p.read_text(encoding="utf-8-sig").splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("id"):
                rows[e["id"]] = {**rows.get(e["id"], {}), **e}
    except OSError:
        return []
    out = []
    for t in rows.values():
        if t.get("status") != "exception":
            continue
        age = _age_days(str(t.get("ts_done") or t.get("ts") or "")[:10])
        if age is not None and age > 14:
            continue
        out.append(Loop(WAITING, "executor", f"{t.get('kind')} {t.get('id')}", str(t.get("result") or "")[:110],
                        "An approved action did not complete - check before re-carding", age))
    return out


def collect_owner_decisions() -> list[Loop]:
    """Decisions the improvement loop parked for the owner (Memory/improvement-backlog.md) and the
    newest Optimization Scout proposals: gated on him, and on no other surface."""
    out: list[Loop] = []
    p = VAULT / "Memory" / "improvement-backlog.md"
    try:
        text = p.read_text(encoding="utf-8")
        sec = ""
        for hdr in re.findall(r"^## Owner decisions[^\n]*$", text, re.M):
            sec += _section(text, hdr, "## Done", "## Open", "## Skipped", "## Log")
        n = len(re.findall(r"^\d+\. ", sec, re.M))
        if n:
            out.append(Loop(BLOCKED, "owner-decision", "Pricing questions", f"{n} pricing questions only owner1 can settle",
                            "Read Memory/improvement-backlog.md -> Owner decisions", None))
        for m in re.finditer(r"^- \[ \] (B\d+) owner1's call: (.+)$", text, re.M):
            out.append(Loop(BLOCKED, "owner-decision", m.group(1), m.group(2)[:110], "Decide (improvement backlog)", None))
    except OSError:
        pass
    cache = VAULT / "scripts" / "optimizer" / ".cache"
    try:
        files = sorted(f for f in cache.glob("proposals_*.json") if "raw_debug" not in f.name)
        if files:
            d = json.loads(files[-1].read_text(encoding="utf-8-sig"))
            props = d.get("proposals") if isinstance(d, dict) else d
            titles = "; ".join(str(x.get("title", "")) for x in (props or []) if isinstance(x, dict))[:110]
            out.append(Loop(BACKLOG, "optimizer", files[-1].stem.replace("proposals_", "proposals "), titles,
                            "Say 'review the optimizer proposals' to go through them", _age_days(files[-1].stem[-10:])))
    except (OSError, ValueError):
        pass
    return out


def collect_selftest() -> list[Loop]:
    """A nightly self-test suite whose latest verdict is FAIL (includes the send-gate tripwire)."""
    p = VAULT / "scripts" / "evals" / "eval_ledger.jsonl"
    last: dict = {}
    try:
        for ln in p.read_text(encoding="utf-8-sig").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and str(r.get("pipeline", "")).startswith("selftest-"):
                last[r["pipeline"]] = r
    except OSError:
        return []
    return [Loop(WAITING, "self-test", p_[9:], str(r.get("note") or "")[:110], "A safety suite is failing - fix before trusting the automation",
                 _age_days(str(r.get("ts") or "")[:10])) for p_, r in sorted(last.items()) if str(r.get("verdict", "")).upper() == "FAIL"]


def collect_dated_todos() -> list[Loop]:
    """Grep vault for `TODO(YYYY-MM-DD)` / `@followup YYYY-MM-DD` markers.
    Pure-python walk to stay dependency-free; skips .git and generated dirs."""
    out: list[Loop] = []
    skip = {".git", ".obsidian", "graphify-out", ".cache", "_meta"}
    pat = re.compile(r"(?:TODO|@followup)\s*[:(]?\s*(\d{4}-\d{2}-\d{2})\)?\s*[:)-]?\s*(.{0,80})")
    for path in VAULT.rglob("*.md"):
        if any(part in skip for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in pat.finditer(text):
            due, rest = m.group(1), m.group(2).strip()
            age = _age_days(due)
            # a due date in the past is a WAITING item; future = BACKLOG
            tier = WAITING if (age is not None and age >= 0) else BACKLOG
            rel = path.relative_to(VAULT).as_posix()
            out.append(Loop(tier, "dated-todo", rel, rest or "(dated TODO)",
                            f"Due {due}", age))
    return out


# ---- Eisenhower matrix view -------------------------------------------------

# Quadrant constants (same taxonomy as the Eisenhower matrix pattern; inlined here
# so wedding automation stays self-contained with zero cross-business imports).
_Q1, _Q2, _Q3, _Q4 = "DO", "SCHEDULE", "DELEGATE", "DELETE"
_QUADRANTS = (_Q1, _Q2, _Q3, _Q4)
_Q_LABEL = {_Q1: "Do", _Q2: "Schedule", _Q3: "Delegate", _Q4: "Delete"}
_Q_ICON = {_Q1: "🔴", _Q2: "🗓", _Q3: "🤝", _Q4: "🗑"}
_Q_AXES = {
    _Q1: "important + urgent",
    _Q2: "important · not yet urgent",
    _Q3: "urgent · not important",
    _Q4: "neither",
}
_Q_BLURB = {
    _Q1: "Important and urgent — do now. A couple or lead is actively waiting.",
    _Q2: ("Important, not yet urgent — schedule it before it becomes a DO emergency. "
          "In-flight films with breathing room, plus owner1's decision queue."),
    _Q3: "Urgent timing but lower stakes — dated housekeeping items past their marker date.",
    _Q4: "Neither — backlog hygiene and upcoming shoots not yet due; batch or defer.",
}


def _loop_quadrant(loop: Loop) -> str:
    """Classify a Loop into an Eisenhower quadrant without any external dependency.

    Axes derived from the wedding data model:

    IMPORTANT  = directly coupled to a couple/lead (project, inquiry, delivery deadline,
                 skill/automation decision the owner must make).
    URGENT     = there is a real-world clock ticking right now (delivery overdue,
                 inquiry unanswered, dated TODO past due, suggestion sitting long).

    Mapping:
      project WAITING + overdue delivery  → DO   (important + urgent)
      project WAITING + inquiry           → DO   (important + urgent)
      project WAITING + in-flight future  → SCHEDULE (important, clock hasn't hit)
      BLOCKED (skill/automation/review)   → SCHEDULE (important, the owner's call, no ext. clock)
      dated-todo WAITING (past-due)       → DELEGATE (timed but lower-stakes hygiene)
      dated-todo BACKLOG (future)         → DELETE
      BACKLOG (shoots, wiki drafts, etc.) → DELETE
    """
    tier = loop.tier
    src = loop.source

    if tier == WAITING:
        # Overdue delivery or new inquiry → most urgent + important
        if src == "project":
            action_lower = loop.action.lower()
            if "overdue" in action_lower or "inquiry" in action_lower or "triage" in action_lower:
                return _Q1  # DO
            # In-flight with future deadline → important but not yet urgent
            return _Q2  # SCHEDULE

        if src == "dated-todo":
            # Past-due dated TODOs: timed but not directly couple-facing → DELEGATE
            return _Q3

        # Any other WAITING source → treat as important + urgent
        return _Q1

    if tier == BLOCKED:
        # the owner's decision queue: important (blocks progress) but no external clock
        return _Q2  # SCHEDULE

    # BACKLOG — future shoots, wiki hygiene, etc. → DELETE
    return _Q4


def build_matrix(loops: list[Loop]) -> str:
    """Render the collected loops as an Eisenhower 2x2 (DO / SCHEDULE / DELEGATE / DELETE).

    The 2x2 separates IMPORTANCE from URGENCY, rescuing the "Schedule" lane — important
    work that is not yet urgent, which a flat ranked list tends to bury beneath noise.
    Derived purely from the Loop objects; no external cache or data files required.
    """
    buckets: dict[str, list[Loop]] = {q: [] for q in _QUADRANTS}
    for loop in loops:
        buckets[_loop_quadrant(loop)].append(loop)
    # Within each bucket: oldest (most-aged) first
    for items in buckets.values():
        items.sort(key=lambda l: -(l.age if l.age is not None else -1))

    lines: list[str] = [
        "# Eisenhower Matrix", "",
        "> [!warning] Generated — do not hand-edit",
        f"> Rebuilt: {TODAY.isoformat()} by `scripts/open_loops.py --matrix`. "
        "Two-axis lens: **importance** (couple-facing impact) × **urgency** (clock running now). "
        "The **Schedule** quadrant is the rescue lane — important work not yet on fire that "
        "the flat ranked list buries.", "",
    ]
    counts = " · ".join(
        f"{len(buckets[q])} {_Q_LABEL[q].lower()}" for q in _QUADRANTS
    )
    lines += [f"**{counts}**", ""]

    for q in _QUADRANTS:
        items = buckets[q]
        lines.append(f"## {_Q_ICON[q]} {_Q_LABEL[q]} — {_Q_AXES[q]}  ({len(items)})")
        lines.append(f"*{_Q_BLURB[q]}*")
        lines.append("")
        if not items:
            lines += ["- *(clear)*", ""]
            continue
        for l in items:
            lines.append(
                f"- **{l.ident}** · {l.summary}  "
                f"\n  ↳ {l.action}  ·  _{_age_label(l.age)}_  ·  `{l.source}`"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---- render -----------------------------------------------------------------

def build_report(loops: list[Loop]) -> str:
    loops.sort(key=lambda l: l.sort_key())
    counts = {WAITING: 0, BLOCKED: 0, BACKLOG: 0}
    for l in loops:
        counts[l.tier] += 1

    lines: list[str] = []
    lines.append("# Open Loops")
    lines.append("")
    lines.append("> [!warning] Generated — do not hand-edit")
    lines.append(f"> Rebuilt: {TODAY.isoformat()} by `scripts/open_loops.py`. "
                 "Read-only roll-up of the vault's trackers. Edit the source, not this note.")
    lines.append("")
    lines.append(f"**{counts[WAITING]} waiting · {counts[BLOCKED]} blocked · "
                 f"{counts[BACKLOG]} backlog**")
    lines.append("")

    for tier in (WAITING, BLOCKED, BACKLOG):
        group = [l for l in loops if l.tier == tier]
        lines.append(f"## {TIER_ICON[tier]} {tier.title()}  ({len(group)})")
        lines.append(f"*{TIER_BLURB[tier]}*")
        lines.append("")
        if not group:
            lines.append("- *(clear)*")
            lines.append("")
            continue
        for l in group:
            lines.append(
                f"- **{l.ident}** · {l.summary}  "
                f"\n  ↳ {l.action}  ·  _{_age_label(l.age)}_  ·  `{l.source}`"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to cp1252
    except (AttributeError, ValueError):
        pass
    print_only = "--print" in sys.argv
    loops: list[Loop] = []
    for fn in (collect_cards, collect_exec_exceptions, collect_selftest, collect_projects, collect_review_queue,
               collect_skill_suggestions, collect_automation_suggestions, collect_owner_decisions,
               collect_dated_todos, collect_assistant_tickets):
        try:                         # one unreadable input must cost one section, never the whole list
            loops += fn()
        except Exception as e:
            loops.append(Loop(WAITING, "open-loops", fn.__name__, f"this section could not be read: {type(e).__name__}: {e}"[:110],
                              "Fix the input file it reads", None))

    if "--matrix" in sys.argv:
        # Eisenhower 2x2 view. Writes Wiki/_meta/eisenhower-matrix.md unless --print.
        report = build_matrix(loops)
        if not print_only:
            out = VAULT / "Wiki" / "_meta" / "eisenhower-matrix.md"
            out.write_text(report, encoding="utf-8")
        sys.stdout.write(report)
        return 0

    report = build_report(loops)
    if not print_only:
        out = VAULT / "Wiki" / "_meta" / "open-loops.md"
        out.write_text(report, encoding="utf-8")
    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
