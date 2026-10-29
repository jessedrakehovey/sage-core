"""preconditions.py - read the record before a lifecycle step fires (WP3 of the stand-alone proposal, 2026-10-24).

Until this, every SOP step fired on time alone and its ticket told an LLM operator to "check HoneyBook first".
The property OS has the same weakness (its world model only ranks). Here a step may carry
`action.satisfied_when = {"check": <name>, ...}`; followups._fire asks this module first:

  satisfied   the record already shows the step happened -> the item is done `by_record`, no ticket, no text
  open        the record shows it has NOT happened        -> fire, with what was read as evidence
  unknown     the record cannot say                        -> fire, saying so (the operator checks)
  moot        the wedding is cancelled / lost              -> dismissed

Checks (zero-LLM, read-only; `unit` is the ledger key in scripts/projects/project_state.json):
  questionnaire_answered   a questionnaire smart file on the couple's HoneyBook project is completed
  timeline_on_file         the couple's ledger entry or wiki page holds a timeline, or their questionnaire is completed
  outbound_since           an email / HoneyBook message / text to the couple went out in the last `days` (default 3)
                           or since `anchor` (the wedding date) when `since: "anchor"`
  touches_sent             the Finishing Touches file went to them (an hb_touches_send ticket done)

  python assistant/scripts/preconditions.py <check> <unit> [--days N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from alib import DATA, VAULT, load_json  # noqa: E402

PROJECTS = Path(os.environ.get("WV_PROJECT_STATE") or (VAULT / "scripts" / "projects" / "project_state.json"))
SNAP = Path(os.environ.get("WV_HB_SNAPSHOT") or os.environ.get("HB_SNAPSHOT_FILE") or (VAULT / "dashboard" / "data" / "honeybook.json"))
COUPLES = VAULT / "Wiki" / "Couples"
TERMINAL = {"cancelled", "lost", "archived", "delivered"}


def _project(unit: str) -> dict:
    led = load_json(PROJECTS, {}) or {}
    p = (led.get("projects") or {}).get(str(unit or ""))
    return p if isinstance(p, dict) else {}


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))[-10:]


def _contacts(p: dict) -> tuple:
    """(emails, phone digit tails) the ledger knows for the couple, plus the quiz lead file when linked."""
    emails = {str(x).lower() for x in ([p.get("email")] + list(p.get("emails") or []) + list(p.get("also_notify") or [])) if x}
    phones = {_digits(x) for x in ([p.get("phone")] + list(p.get("phones") or [])) if _digits(x)}
    tok = str(p.get("lead_token") or p.get("quiz_token") or "")
    if tok:
        lead = load_json(DATA / "leads" / f"{tok}.json", {}) or {}
        for k in ("email",):
            if lead.get(k):
                emails.add(str(lead[k]).lower())
        if _digits(lead.get("phone")):
            phones.add(_digits(lead.get("phone")))
    return emails, phones


def _hb_ids(p: dict, snap: dict) -> set:
    ids = {str(p.get("honeybook_id") or "")} - {""}
    for e in snap.get("projects") or []:
        if isinstance(e, dict) and (str(e.get("id")) in ids or str(e.get("project_id")) in ids):
            ids |= {str(e.get("id") or ""), str(e.get("project_id") or "")} - {""}
    return ids


def questionnaire_answered(unit: str, p: dict, **kw) -> tuple:
    snap = load_json(SNAP, {}) or {}
    files = snap.get("questionnaires")
    if not isinstance(files, list):
        return "unknown", "the HoneyBook snapshot lists no questionnaire files"
    ids = _hb_ids(p, snap)
    if not ids:
        return "unknown", "no HoneyBook project id on the ledger entry"
    mine = [f for f in files if isinstance(f, dict) and (str(f.get("workspace_id")) in ids or str(f.get("project_id")) in ids)]
    if any(str(f.get("status") or "").lower() in ("completed", "complete", "answered", "submitted") for f in mine):
        return "satisfied", "the questionnaire on their HoneyBook project is completed"
    if mine:
        return "open", "their questionnaire file is sent and not completed"
    return "open", "no questionnaire file on their project"


def timeline_on_file(unit: str, p: dict, **kw) -> tuple:
    if p.get("timeline") or p.get("day_of_timeline"):
        return "satisfied", "the ledger entry holds a timeline"
    try:
        for f in COUPLES.glob("*.md"):
            if unit and str(unit) in f.name or (p.get("couple") and str(p["couple"]).split()[0].lower() in f.name.lower()
                                                and str(p.get("wedding_date") or "")[:10] in f.name):
                txt = f.read_text(encoding="utf-8", errors="replace")
                if re.search(r"^#+\s*.*timeline", txt, re.I | re.M) and re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", txt, re.I):
                    return "satisfied", f"a timeline is on {f.name}"
    except OSError:
        pass
    v, d = questionnaire_answered(unit, p)
    if v == "satisfied":
        return "satisfied", "their completed questionnaire covers the timeline"
    return ("open", "no timeline on the ledger, the wedding page or a completed questionnaire") if v != "unknown" else ("unknown", d)


def outbound_since(unit: str, p: dict, days: float = 3, since: str = "", **kw) -> tuple:
    emails, phones = _contacts(p)
    if not emails and not phones:
        return "unknown", "no email or phone on file for them"
    if since == "anchor" and p.get("wedding_date"):
        cut = str(p["wedding_date"])[:10] + "T00:00:00"
    else:
        cut = (datetime.now() - timedelta(days=float(days))).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        import exec_store as es
        import outcomes
        for t in es.fold().values():
            if t.get("kind") not in ("email_send", "hb_message", "imessage_send", "hb_touches_send") or t.get("status") != "done":
                continue
            if str(t.get("ts_done") or t.get("ts") or "") < cut:
                continue
            pay = t.get("payload") or {}
            to = pay.get("to")
            tos = [str(x).lower() for x in (to if isinstance(to, (list, tuple)) else [to]) if x]
            hit = any(x in emails for x in tos) or any(_digits(x) in phones for x in tos) or str(pay.get("workspace_id") or "") in {str(p.get("honeybook_id") or "")} - {""}
            if hit and outcomes.state_for_ticket(t["id"]) not in ("failed", "abandoned"):
                return "satisfied", f"{t['kind'].replace('_', ' ')} to them on {str(t.get('ts_done') or t.get('ts'))[:10]}"
    except Exception as e:  # noqa: BLE001
        return "unknown", f"could not read the send ledger: {type(e).__name__}"
    return "open", f"nothing went to them since {cut[:10]}"


def touches_sent(unit: str, p: dict, **kw) -> tuple:
    try:
        import exec_store as es
        wid = str(p.get("honeybook_id") or "")
        for t in es.fold().values():
            if t.get("kind") == "hb_touches_send" and t.get("status") == "done":
                pay = t.get("payload") or {}
                if wid and wid in (str(pay.get("workspace_id") or ""), str(pay.get("project_id") or "")):
                    return "satisfied", f"Finishing Touches file sent {str(t.get('ts_done') or '')[:10]}"
                lead = pay.get("lead") or {}
                emails, _ = _contacts(p)
                if str(lead.get("email") or "").lower() in emails:
                    return "satisfied", f"Finishing Touches file sent {str(t.get('ts_done') or '')[:10]}"
    except Exception as e:  # noqa: BLE001
        return "unknown", f"could not read the send ledger: {type(e).__name__}"
    return "open", "no Finishing Touches send on record"


CHECKS = {"questionnaire_answered": questionnaire_answered, "timeline_on_file": timeline_on_file,
          "outbound_since": outbound_since, "touches_sent": touches_sent}


def check(spec: dict | str, unit: str) -> tuple:
    """-> (verdict, detail). verdict: satisfied | open | unknown | moot."""
    spec = {"check": spec} if isinstance(spec, str) else dict(spec or {})
    name = str(spec.pop("check", "") or "")
    p = _project(unit)
    if p and str(p.get("stage") or "").lower() in TERMINAL - {"delivered"}:
        return "moot", f"the ledger says {p.get('stage')}"
    fn = CHECKS.get(name)
    if not fn:
        return "unknown", f"no such check {name!r}"
    if not p:
        return "unknown", f"no ledger entry for {unit!r}"
    try:
        return fn(unit, p, **spec)
    except Exception as e:  # noqa: BLE001
        return "unknown", f"{name} failed: {type(e).__name__}: {str(e)[:100]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("check")
    ap.add_argument("unit")
    ap.add_argument("--days", type=float, default=3)
    a = ap.parse_args()
    v, d = check({"check": a.check, "days": a.days}, a.unit)
    print(f"{v}: {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
