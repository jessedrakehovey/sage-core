#!/usr/bin/env python3
"""
loop_audit.py — closed-loop audit instrument for the wedding-videography automation.

GOAL: prove the wedding-automation system is CLOSED — every couple project that
has a pending/open item surfaced it to the owner via the dreaming loop + Mission Control
diff, and every self-improvement suggestion or automation proposal reached him
in the consent queue before any auto-apply ran. The failure signal is the
inverse: a project moving forward (or a suggestion being applied) with no
corresponding prior surface event.

NOTE: Telegram was removed 2026-10-04. The bridge log reference below is kept for
graceful-absent handling but will never have content; the bridge_events list will
always be empty. The reached_operator field (formerly reached_telegram) is True
for all proposals.

DESIGN — two halves, cleanly separated:

  1. RECONCILER (this script, pure Python, no network): builds the canonical
     index of everything the system PROPOSED / the owner saw, from local artifacts:
       - scripts/projects/project_state.json   (couple project tracker)
       - Memory/skill-suggestions.md           (auto-apply audit + pending queue)
       - Memory/automation-suggestions.md      (consent-first automation queue)
       - ~/.claude/telegram-bridge-wv.log      (legacy; always absent now)

  2. GAP CHECK: compares the proposal index against an optional actions feed:
         Memory/audit/the owner_actions.jsonl
     one JSON object per line, e.g.:
         {"ts":"2026-07-06","channel":"email","key":"couple-Glenrock-Ivybridge",
          "subject":"delivery ready","scope":"couple"}
         {"ts":"2026-07-06","channel":"skill-apply","key":"wiki-ingest step fix",
          "scope":"skill"}
     If the feed is absent the script still emits the proposal index and reports
     the feed as PENDING — a future loop run can populate it and re-run.

OUTPUTS (under Memory/audit/):
  - proposals_index.json  — canonical index (machine-readable)
  - gap_report.md         — human-readable reconciliation + gap list
Exit code 0 always (reporting instrument, not a gate).

Run:  python scripts/loop_audit.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- paths -------------------------------------------------------------------
VAULT = Path(__file__).resolve().parent.parent
SCRIPTS = VAULT / "scripts"
AUDIT_DIR = VAULT / "Memory" / "audit"
HOME = Path(os.path.expanduser("~"))

PROJECT_STATE = SCRIPTS / "projects" / "project_state.json"
SKILL_SUGGESTIONS = VAULT / "Memory" / "skill-suggestions.md"
AUTO_SUGGESTIONS = VAULT / "Memory" / "automation-suggestions.md"
BRIDGE_LOG = HOME / ".claude" / "telegram-bridge-wv.log"
ACTIONS_FEED = AUDIT_DIR / "owner1_actions.jsonl"

PROPOSALS_OUT = AUDIT_DIR / "proposals_index.json"
GAP_REPORT_OUT = AUDIT_DIR / "gap_report.md"

# --- go-live cutoff ----------------------------------------------------------
# Automation was established 2026-06-22 (first dreaming cron run). Any the owner
# action BEFORE this is the pre-automation baseline and cannot be a triage gap.
# Override with env LOOP_AUDIT_GOLIVE (ISO 8601 UTC).
GO_LIVE_ISO = os.environ.get("LOOP_AUDIT_GOLIVE", "2026-06-22T00:00:00Z")

# Scopes that are NOT bot-proposable and therefore can never be a gap.
NON_PROPOSABLE_SCOPES = {"internal", "self", "staff"}


# --- small helpers -----------------------------------------------------------
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_jsonl(path: Path):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def norm_subject(s: str) -> str:
    """Normalize for fuzzy matching: lowercase, collapse whitespace."""
    s = (s or "").lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_ts(s: str):
    """Parse a feed `ts` into an aware UTC datetime, or None if unparseable."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        if "T" in s or (" " in s and ":" in s):
            iso = s.replace("Z", "+00:00").replace(" ", "T", 1)
            dt = datetime.fromisoformat(iso)
        else:
            dt = datetime.fromisoformat(s)  # bare date
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# --- 1. build the proposal / approval index ----------------------------------

def _extract_md_sections(text: str) -> dict[str, str]:
    """Split a markdown file into sections keyed by heading text (## level)."""
    sections: dict[str, str] = {}
    cur_heading = "__top__"
    cur_lines: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.*)", line)
        if m:
            sections[cur_heading] = "\n".join(cur_lines)
            cur_heading = m.group(1).strip()
            cur_lines = []
        else:
            cur_lines.append(line)
    sections[cur_heading] = "\n".join(cur_lines)
    return sections


def _parse_skill_entries(section_text: str) -> list[dict]:
    """Extract ### key: ... entries from a skill-suggestions section."""
    entries = []
    for block in re.split(r"(?=^###\s)", section_text, flags=re.MULTILINE):
        m = re.match(r"^###\s+key:\s*(\S+)\s*(.*)", block, re.MULTILINE)
        if not m:
            continue
        key, title = m.group(1).strip(), m.group(2).strip()
        target_m = re.search(r"- Target:\s*(.*)", block)
        signal_m = re.search(r"- Signal:\s*(.*)", block)
        date_m = re.search(r"- First seen:\s*(\S+)", block)
        entries.append({
            "key": key,
            "title": title,
            "target": (target_m.group(1).strip() if target_m else ""),
            "signal": (signal_m.group(1).strip() if signal_m else ""),
            "first_seen": (date_m.group(1).strip() if date_m else ""),
        })
    return entries


def _parse_auto_entries(section_text: str) -> list[dict]:
    """Extract ### key: ... entries from an automation-suggestions section."""
    entries = []
    for block in re.split(r"(?=^###\s)", section_text, flags=re.MULTILINE):
        m = re.match(r"^###\s+key:\s*(\S+)\s*(.*)", block, re.MULTILINE)
        if not m:
            continue
        key, title = m.group(1).strip(), m.group(2).strip()
        cadence_m = re.search(r"- Cadence:\s*(.*)", block)
        date_m = re.search(r"- First seen:\s*(\S+)", block)
        entries.append({
            "key": key,
            "title": title,
            "cadence": (cadence_m.group(1).strip() if cadence_m else ""),
            "first_seen": (date_m.group(1).strip() if date_m else ""),
        })
    return entries


def build_proposals() -> tuple[dict, list]:
    """Return (proposals dict keyed by stable id, bridge_events list)."""
    proposals: dict[str, dict] = {}

    # --- project_state.json: one entry per couple project ---
    state = load_json(PROJECT_STATE, {})
    projects = state.get("projects") or {}
    for pid, proj in projects.items():
        if not isinstance(proj, dict):
            continue
        stage = proj.get("stage") or proj.get("status") or "unknown"
        couple = proj.get("couple") or proj.get("name") or pid
        wedding_date = proj.get("wedding_date") or proj.get("date") or ""
        delivery_due = proj.get("delivery_due") or proj.get("deliver_by") or ""
        proposals[f"project:{pid}"] = {
            "id": pid,
            "channel": "project",
            "desk": "project-tracker",
            "subject": couple,
            "subject_norm": norm_subject(couple),
            "stage": stage,
            "wedding_date": wedding_date,
            "delivery_due": delivery_due,
            "reached_operator": True,  # project state is surfaced via dreaming + open-loops
        }

    # --- skill-suggestions.md: pending + applied entries ---
    if SKILL_SUGGESTIONS.exists():
        skill_text = SKILL_SUGGESTIONS.read_text(encoding="utf-8", errors="replace")
        sections = _extract_md_sections(skill_text)
        pending_entries = _parse_skill_entries(sections.get("Pending", ""))
        applied_entries = _parse_skill_entries(sections.get("Applied automatically", ""))
        for e in pending_entries:
            proposals[f"skill-pending:{e['key']}"] = {
                "id": e["key"],
                "channel": "skill-suggestion",
                "desk": "dreaming-skill",
                "subject": e["title"],
                "subject_norm": norm_subject(e["title"]),
                "status": "pending",
                "first_seen": e["first_seen"],
                "reached_operator": True,  # pending = not yet applied, the owner must approve
            }
        for e in applied_entries:
            proposals[f"skill-applied:{e['key']}"] = {
                "id": e["key"],
                "channel": "skill-suggestion",
                "desk": "dreaming-skill",
                "subject": e["title"],
                "subject_norm": norm_subject(e["title"]),
                "status": "applied",
                "first_seen": e["first_seen"],
                "reached_operator": True,  # applied entries were Mission Control-diffed
            }

    # --- automation-suggestions.md: pending + accepted entries ---
    if AUTO_SUGGESTIONS.exists():
        auto_text = AUTO_SUGGESTIONS.read_text(encoding="utf-8", errors="replace")
        sections = _extract_md_sections(auto_text)
        pending_entries = _parse_auto_entries(sections.get("Pending", ""))
        accepted_entries = _parse_auto_entries(sections.get("Accepted", ""))
        for e in pending_entries:
            proposals[f"auto-pending:{e['key']}"] = {
                "id": e["key"],
                "channel": "automation-suggestion",
                "desk": "dreaming-auto",
                "subject": e["title"],
                "subject_norm": norm_subject(e["title"]),
                "status": "pending",
                "cadence": e["cadence"],
                "first_seen": e["first_seen"],
                "reached_operator": True,
            }
        for e in accepted_entries:
            proposals[f"auto-accepted:{e['key']}"] = {
                "id": e["key"],
                "channel": "automation-suggestion",
                "desk": "dreaming-auto",
                "subject": e["title"],
                "subject_norm": norm_subject(e["title"]),
                "status": "accepted",
                "cadence": e["cadence"],
                "first_seen": e["first_seen"],
                "reached_operator": True,
            }

    bridge_events = parse_bridge_log()
    return proposals, bridge_events


BRIDGE_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\s+"
    r"(?P<dir>[><.])\s+(?P<rest>.*)$"
)


def parse_bridge_log() -> list[dict]:
    """Extract dispositions/sends from the (legacy) wedding bridge log.

    Telegram was removed 2026-10-04; this function always returns [] now.
    Kept for graceful handling of the absent log file. Recognized:
      > APPROVE / SKIP / DISMISS  (the owner taps, if any two-way flow is added)
      . diff_sent(<key>): OK       (dreaming diff delivered)
    Returns empty list gracefully if the log doesn't exist.
    """
    events: list[dict] = []
    if not BRIDGE_LOG.exists():
        return events
    for line in BRIDGE_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        m = BRIDGE_LINE_RE.match(line)
        if not m:
            continue
        ts, direction, rest = m.group("ts"), m.group("dir"), m.group("rest")
        # the owner taps: "> APPROVE from ...: <key>"
        tap = re.match(r"(APPROVE|SKIP|DISMISS)\b.*?:\s*(\S+)", rest)
        if direction == ">" and tap:
            events.append({"ts": ts, "kind": tap.group(1), "id": tap.group(2), "raw": rest})
            continue
        # bot actions: ". diff_sent(key): OK" or ". notify(key): sent"
        act = re.match(r"(diff_sent|notify|dreaming_diff)\((\S+?)\)", rest)
        if direction == "." and act:
            sent = "OK" in rest or "sent" in rest.lower()
            events.append({"ts": ts, "kind": act.group(1), "id": act.group(2),
                           "sent": sent, "raw": rest})
    return events


# --- 2. gap check against the owner's actual actions -----------------------------

def classify_gap(action: dict, proposals: dict) -> tuple[str, str]:
    """Heuristic classification of an unmatched the owner action.

    Returns (category, rationale). Categories:
      - dead-channel       : a matching item WAS in the proposal index but the
                             legacy bridge log has no delivery event for it.
      - missed-surface     : the action is in a channel the system already watches
                             (project/skill/automation) but no proposal was staged
                             — the dreaming loop missed it.
      - missing-poll       : the action is in a scope no desk covers — needs a new
                             poll or skill.
    """
    scope = (action.get("scope") or "").lower()
    channel = (action.get("channel") or "").lower()
    subj_norm = norm_subject(action.get("subject") or "")

    # dead-channel: staged but not confirmed delivered via bridge
    for p in proposals.values():
        staged_match = (
            (subj_norm and p.get("subject_norm") and subj_norm == p["subject_norm"])
            or action.get("key") == p["id"]
        )
        if staged_match and not p.get("reached_operator", True):
            return ("dead-channel",
                    f"Item {p['id']} ({p['desk']}) was staged but no delivery "
                    f"event found in the legacy bridge log.")

    # missed-surface: scope/channel already watched
    watched_scopes = {
        "couple": "project-tracker",
        "project": "project-tracker",
        "skill": "dreaming-skill",
        "skill-suggestion": "dreaming-skill",
        "automation": "dreaming-auto",
        "automation-suggestion": "dreaming-auto",
    }
    if scope in watched_scopes:
        return ("missed-surface",
                f"Scope '{scope}' is already watched by {watched_scopes[scope]}, but no "
                f"proposal was staged for this action. Extend the dreaming loop or "
                f"project-tracker to catch it.")
    if channel in watched_scopes:
        return ("missed-surface",
                f"Channel '{channel}' is watched by {watched_scopes[channel]}, but no "
                f"proposal was found.")

    # otherwise: no desk covers this
    return ("missing-poll",
            f"No existing desk watches scope '{scope or channel or 'unknown'}'. "
            f"This action class needs a NEW skill/poll to propose it first.")


def match_action(action: dict, proposals: dict) -> dict | None:
    """Try to match a the owner action to a prior proposal.

    Precedence: exact key match > normalized subject fuzzy fallback.
    """
    key = action.get("key") or ""
    subj_norm = norm_subject(action.get("subject") or "")
    for pid, p in proposals.items():
        if key and (key == p["id"] or key in pid):
            return p
        if subj_norm and p.get("subject_norm") and subj_norm == p["subject_norm"]:
            return p
    return None


# --- 3. report ---------------------------------------------------------------

def main() -> int:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")

    proposals, bridge_events = build_proposals()

    # mark which proposals were acknowledged via the bridge
    sent_ids = {e["id"] for e in bridge_events
                if e.get("kind") in ("APPROVE", "diff_sent", "notify", "dreaming_diff")
                and e.get("sent", True)}
    for p in proposals.values():
        p["approved_or_sent_via_bridge"] = p["id"] in sent_ids

    # persist the canonical proposal index
    PROPOSALS_OUT.write_text(
        json.dumps({"generated": now, "proposals": proposals,
                    "bridge_events": bridge_events}, indent=2),
        encoding="utf-8")

    # dead-channel proposals: staged but never confirmed delivered
    dead_channel = [p for p in proposals.values() if not p.get("reached_operator", True)]

    # gap check against the owner's actual actions feed
    go_live = parse_ts(GO_LIVE_ISO)
    actions = load_jsonl(ACTIONS_FEED)
    matched, gaps = [], []
    pre_golive, non_proposable = [], []
    for a in actions:
        if (a.get("scope") or "").lower() in NON_PROPOSABLE_SCOPES:
            non_proposable.append(a)
            continue
        a_ts = parse_ts(a.get("ts", ""))
        if go_live and a_ts and a_ts < go_live:
            pre_golive.append(a)
            continue
        m = match_action(a, proposals)
        if m:
            matched.append({"action": a, "proposal_id": m["id"], "desk": m["desk"]})
        else:
            cat, why = classify_gap(a, proposals)
            gaps.append({"action": a, "category": cat, "rationale": why})

    in_scope = len(matched) + len(gaps)

    # --- channel breakdown for the proposal index ---
    def _count(ch): return sum(1 for p in proposals.values() if p["channel"] == ch)

    # --- write the markdown gap report ---
    lines: list[str] = []
    lines.append("# Closed-loop audit — gap report")
    lines.append("")
    lines.append(f"_Generated {now} by `scripts/loop_audit.py`. Do not hand-edit._")
    lines.append("")
    lines.append("## Proposal index (what the system proposed / owner1 saw)")
    lines.append("")
    lines.append(f"- Total tracked proposals: **{len(proposals)}** "
                 f"(projects {_count('project')}, "
                 f"skill-suggestions {_count('skill-suggestion')}, "
                 f"automation-suggestions {_count('automation-suggestion')})")
    lines.append(f"- Acknowledged via operator channel: "
                 f"**{sum(1 for p in proposals.values() if p['approved_or_sent_via_bridge'])}**")
    lines.append(f"- Bridge log events parsed: **{len(bridge_events)}** "
                 f"({'log missing — graceful' if not BRIDGE_LOG.exists() else BRIDGE_LOG.name})")
    lines.append("")

    # source availability note
    for label, path in [
        ("project_state.json", PROJECT_STATE),
        ("skill-suggestions.md", SKILL_SUGGESTIONS),
        ("automation-suggestions.md", AUTO_SUGGESTIONS),
        ("telegram-bridge-wv.log (legacy, always absent)", BRIDGE_LOG),
    ]:
        status = "present" if path.exists() else "absent (graceful)"
        lines.append(f"- `{label}`: {status}")
    lines.append("")

    lines.append("## owner1's actions feed")
    lines.append("")
    if not ACTIONS_FEED.exists():
        lines.append(f"> **PENDING** — no actions feed at `{ACTIONS_FEED.relative_to(VAULT)}`.")
        lines.append("> The deterministic proposal index is built, but the loop cannot prove")
        lines.append("> *closedness* until owner1's real actions are fed in. A future loop run")
        lines.append("> (with MCP access) can populate it then re-run this script.")
        lines.append("> Schema: one JSON line per action — `{\"ts\", \"channel\", \"key\", \"subject\", \"scope\"}`.")
    else:
        lines.append(f"- Actions in feed: **{len(actions)}**")
        lines.append(f"- Set aside — non-proposable (internal/self): **{len(non_proposable)}**")
        lines.append(f"- Set aside — pre-go-live (before {GO_LIVE_ISO}): **{len(pre_golive)}**")
        lines.append(f"- **In-scope (post-go-live, proposable): {in_scope}**")
        lines.append(f"  - Matched to a prior proposal: **{len(matched)}**")
        lines.append(f"  - **GAPS (unproposed actions): {len(gaps)}**")
        if in_scope == 0 and (pre_golive or non_proposable):
            lines.append("")
            lines.append("> No in-scope actions yet — every fed action is either pre-go-live "
                         "or non-proposable. Re-run after a representative window.")
    lines.append("")

    if gaps:
        lines.append("## Gaps — owner1 actions with NO prior proposal")
        lines.append("")
        for cat in ("dead-channel", "missed-surface", "missing-poll"):
            cat_gaps = [g for g in gaps if g["category"] == cat]
            if not cat_gaps:
                continue
            lines.append(f"### {cat} ({len(cat_gaps)})")
            for g in cat_gaps:
                a = g["action"]
                desc = a.get("subject") or a.get("key") or "(no description)"
                lines.append(f"- **{a.get('ts','?')}** [{a.get('channel','?')}] {desc} — {g['rationale']}")
            lines.append("")

    if dead_channel:
        lines.append("## Dead-channel watch — proposals that never reached owner1")
        lines.append("")
        for p in dead_channel:
            lines.append(f"- `{p['id']}` ({p['desk']}, status={p.get('status', '?')}) — "
                         f"no delivery confirmed in legacy bridge log.")
        lines.append("")

    # closedness verdict
    lines.append("## Verdict")
    lines.append("")
    if not ACTIONS_FEED.exists():
        verdict = "INCOMPLETE — proposal index built; awaiting owner1's actions feed to test closedness."
    elif in_scope == 0:
        verdict = (f"INCONCLUSIVE — feed has {len(actions)} action(s) but 0 are in-scope "
                   f"(post-go-live + proposable): {len(pre_golive)} pre-go-live, "
                   f"{len(non_proposable)} non-proposable. Re-run after a live window.")
    elif gaps:
        verdict = (f"NOT CLOSED — {len(gaps)}/{in_scope} in-scope action(s) had no prior "
                   f"proposal. Each maps to a fix (see categories above).")
    else:
        verdict = (f"CLOSED for this window — all {in_scope} in-scope owner1 action(s) had a "
                   f"matching prior proposal. Re-run over a representative window to confirm.")
    lines.append(f"**{verdict}**")
    lines.append("")

    GAP_REPORT_OUT.write_text("\n".join(lines), encoding="utf-8")

    # console summary (kept short — this runs headless)
    print(f"[loop_audit] proposals={len(proposals)} bridge_events={len(bridge_events)} "
          f"actions={len(actions)} in_scope={in_scope} matched={len(matched)} gaps={len(gaps)} "
          f"pre_golive={len(pre_golive)} non_proposable={len(non_proposable)} "
          f"dead_channel={len(dead_channel)}")
    print(f"[loop_audit] wrote {PROPOSALS_OUT.relative_to(VAULT)} and "
          f"{GAP_REPORT_OUT.relative_to(VAULT)}")
    print(f"[loop_audit] verdict: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
