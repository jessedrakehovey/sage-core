"""weekly.py - the Friday readback and the weekly hygiene card (rulings 5 and 7, 2026-10-15).

example posts a 17:30 readback every weekday and a daily hygiene card; the owner wants both ONCE A
WEEK, on the day in tracking.json -> weekly.readback_day, after readback_after. Deterministic,
no model.

  Readback  what moved this week (cards answered, things sent, tickets done, follow-ups fired
            and done), what is stalled (chases awaiting / parked), what is done but not closed
            (drift findings). Plain sentences to the owners' shared space (ruling 16: owner
            reads it too).
  Hygiene   ONE card: every drift finding, numbered; the approve button mints one `tracking_fix`
            ticket that applies every row with a record fix (internal writes only); rows that
            need the owner's own hand (a CRM stage move, "what happened at X?") are listed for him.
            "Skip this week" latches nothing: the same rows come back next week unless fixed.

  python assistant/scripts/tracking/weekly.py            # tick (the spine runs this hourly)
  python assistant/scripts/tracking/weekly.py --dry-run  # print what would go out, post nothing
  python assistant/scripts/tracking/weekly.py --force    # ignore the day/time gate (still once per ISO week)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import crm  # noqa: E402
import drift  # noqa: E402

AGENT = C.ROOT / "agent"


def _week(now) -> str:
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def _lane_counts(rows: list, key="lane") -> str:
    from collections import Counter
    cnt = Counter(str(r.get(key) or "personal") for r in rows)
    return ", ".join(f"{n} {k}" for k, n in cnt.most_common()) if cnt else "none"


def readback_text(now=None) -> str:
    now = now or C.now_local()
    days = 7
    answers = C.card_answers(days)
    answered = [a for a in answers if str(a.get("by") or "").endswith("owner1") or str(a.get("by") or "").endswith("owner6")]
    sent = [t for t in C.exec_tickets(days) if t.get("status") == "done"]
    tix = [t for t in C.tickets_all().values() if t.get("status") == "done" and C.age_days(t.get("updated"), now) <= days]
    fus = C.followups_state().get("items") or {}
    fired = [f for f in fus.values() if C.age_days(f.get("fired_at"), now) <= days and f.get("fired_at")]
    done = [f for f in fired if f.get("status") == "done"]
    chase = C.state("chase", {}).get("items") or {}
    awaiting = [i for i in chase.values() if i.get("status") == "awaiting"]
    parked = [i for i in chase.values() if i.get("status") == "parked"]
    fs = drift.cached()
    lines = [f"Week in review ({now.strftime('%b %d')}):"]
    lines.append(f"Moved: you answered {len(answered)} card{'s' if len(answered) != 1 else ''}, {len(sent)} approved action{'s' if len(sent) != 1 else ''} went out, "
                 f"{len(tix)} queue item{'s' if len(tix) != 1 else ''} closed, {len(fired)} reminder{'s' if len(fired) != 1 else ''} fired ({len(done)} done).")
    if awaiting or parked:
        names = [str(i.get("title") or "")[:40] for i in awaiting][:4]
        s = f"Stalled: {len(awaiting)} nudge{'s' if len(awaiting) != 1 else ''} still open" + (f" ({'; '.join(names)})" if names else "")
        if parked:
            s += f"; {len(parked)} parked after three asks"
        lines.append(s + ".")
    else:
        lines.append("Stalled: nothing is waiting on you from me.")
    if fs:
        fix = sum(1 for f in fs if f.get("fix"))
        lines.append(f"Done but not closed: {len(fs)} record{'s' if len(fs) != 1 else ''} lag what already happened ({fix} I can fix on your tap; the card is below).")
    else:
        lines.append("Records: everything I can see matches what happened.")
    try:                                   # ruling 10: Q2 (important, not urgent) gets one weekly nudge line, never a card
        import priority
        q2 = priority.q2_lines(now=now)
    except Exception:  # noqa: BLE001
        q2 = []
    if q2:
        lines.append("Worth scheduling this week (important, not urgent): " + " ".join(q2))
    return "\n".join(lines)


def _post_text(text: str, dry: bool) -> bool:
    if dry:
        print(text); return True
    try:
        sys.path.insert(0, str(AGENT))
        import bridge  # noqa: E402  the agent's own poster: home space once, outbox, thread root
        bridge.post("owner1", text, channel="readback")
        return True
    except Exception as e:  # noqa: BLE001
        C.log("readback_post_error", error=f"{type(e).__name__}: {str(e)[:160]}")
    try:
        subprocess.run([sys.executable, str(C.SCRIPTS / "notify.py")], input=text, text=True, encoding="utf-8",
                       cwd=str(C.SCRIPTS), timeout=30, capture_output=True, env={**os.environ, "CHAT_PUSH": "on"})
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _groups(fs: list, rows: list) -> list:
    """[{title, rows}] in the order the rows already have: a finding's subject is the part of its
    title before ':' (Sloane: editing has started), else the lane; rows about one subject fold
    together, and the first row of each group stays visible when the section is collapsed."""
    out, by = [], {}
    for f, row in zip(fs, rows):
        title = str(f.get("title") or "")
        subject = (title.split(":", 1)[0].strip() if ":" in title else str(f.get("lane") or "").capitalize()) or "Other"
        if subject not in by:
            by[subject] = {"title": subject, "rows": []}
            out.append(by[subject])
        by[subject]["rows"].append(row)
    return out


def _advice(fs: list, fixes: list) -> str:
    """The one-line advice under the weekly card. 2026-10-18: CRM stage moves are fixes the tap applies
    (through the connector), so the line says how many touch the CRM and that the target stage's
    own automation may run; manual CRM stages are counted separately."""
    crm_fixes = sum(1 for f in fixes if str(f.get("op") or "") in drift.stage_ops())
    manual = sum(1 for f in fs if f.get("manual") and f.get("manual") not in ("ask",))
    crm_nm = crm.name()
    if not fixes:
        return "Nothing here writes anything" + (f"; {manual} {crm_nm} stage{'s' if manual != 1 else ''} wait{'s' if manual == 1 else ''} for you." if manual else ".")
    n = len(fixes)
    parts = [f"Approve: {n} fix{'es' if n != 1 else ''}"]
    if crm_fixes:
        parts.append(f"{crm_fixes} move{'s' if crm_fixes == 1 else ''} a {crm_nm} stage (read back afterwards; that stage's automation may run)")
    if n - crm_fixes:
        parts.append(f"{n - crm_fixes} only move internal stages or close stale reminders")
    line = parts[0] + ": " + "; ".join(parts[1:]) + "."
    if manual:
        line += f" {manual} {crm_nm} stage{'s' if manual != 1 else ''} still wait{'s' if manual == 1 else ''} for you."
    return line


def hygiene_card(fs: list, max_rows: int) -> dict | None:
    if not fs:
        return None
    fs = sorted(fs, key=lambda f: (not f.get("fix"), -float(f.get("age_days") or 0)))[:max_rows]
    fixes = [f["fix"] for f in fs if f.get("fix")]
    rows = []
    for i, f in enumerate(fs, 1):
        tag = "fix" if f.get("fix") else (f"{crm.name()}, your hand" if f.get("manual") and f.get("manual") not in ("ask",) else "tell me")
        rows.append(f"{i}. {f['text']} [{tag}]" + (f" Fix: {f['fix']['desc']}." if f.get("fix") else ""))
    lanes = sorted({f["lane"] for f in fs})
    q = {"header": "Weekly record check", "lane": lanes[0] if len(lanes) == 1 else "personal",
         "question": f"{len(fs)} record{'s' if len(fs) != 1 else ''} lag what already happened. " +
                     (f"Approve and I update the {len(fixes)} marked fix: nothing goes to anyone. " if fixes else "") +
                     "The rest need your hand or a word in this thread.\n" + "\n".join(rows),
         "advice": "💡 " + _advice(fs, fixes),
         "details": "\n".join(f"{i}. {f['title']}: {f.get('why') or ''}" for i, f in enumerate(fs, 1)),
         # the Chat card folds these rows by who they are about, one collapsible section each (example's
         # approvals card by unit, the owner 2026-10-16); the numbered text above stays for the phone preview,
         # Mission Control and the exec check (a fix's words must be in the question)
         "groups": _groups(fs, rows)}
    if fixes:
        q["options"] = [{"label": f"Fix {len(fixes)} record{'s' if len(fixes) != 1 else ''} (Recommended)"}, {"label": "Skip this week"}]
        q["exec"] = {"kind": "tracking_fix", "payload": {"fixes": fixes}, "approve_label": q["options"][0]["label"]}
    else:
        # nothing the system can do on a tap: the list is a chat line, not a "Noted" card
        # (the owner 2026-10-17 evening, cards.NEEDS_ACTION)
        q["say"] = True
        q["options"] = []
    return q


def tick(now=None, dry: bool = False, force: bool = False) -> dict:
    c = C.cfg()
    w = c["weekly"]
    now = now or C.now_local()
    st = C.state("weekly", {})
    wk = _week(now)
    if not w.get("enabled", True):
        return {"skipped": "disabled"}
    if not force:
        if C.DOW[now.weekday()] != str(w.get("readback_day") or "fri").lower()[:3]:
            return {"skipped": "not the day"}
        h, m = C.hm(w.get("readback_after") or "15:00")
        if (now.hour, now.minute) < (h, m):
            return {"skipped": "too early"}
    if st.get("last_week") == wk and not dry:
        return {"skipped": "done this week"}
    drift.run()
    fs = drift.cached()
    text = readback_text(now)
    ok = _post_text(text, dry)
    card = hygiene_card(fs, int(w.get("hygiene_max_rows") or 12))
    qid = ""
    if card:
        if dry:
            print(("WOULD SAY " if card.get("say") else "WOULD POST CARD ") + json.dumps(card, ensure_ascii=False)[:1200])
        elif card.get("say"):
            import cards
            r = cards.say("tracking.weekly", card["lane"], "Weekly record check. " + card["question"])
            if not r.get("said"):
                C.log("hygiene_say_failed", reason=str(r.get("reason"))[:200])
        else:
            import cards
            r = cards.post([card], owner="tracking.weekly", lane=card["lane"])
            qid = str(r.get("qid") or "")
            if not r.get("posted"):
                C.log("hygiene_post_refused", reason=str(r.get("reason"))[:200])
    if not dry:
        C.save_state("weekly", {"last_week": wk, "ts": now.strftime("%Y-%m-%d %H:%M"), "readback_ok": ok, "hygiene_qid": qid, "rows": len(fs)})
        C.log("weekly_posted", week=wk, rows=len(fs), qid=qid)
    out = {"week": wk, "readback": ok, "hygiene_rows": len(fs), "qid": qid}
    print("WEEKLY " + json.dumps(out))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    tick(dry=a.dry_run, force=a.force)
