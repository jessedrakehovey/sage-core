"""followups.py - the Follow-Through engine (lifecycle autopilot) for the personal OS.

Ported from the property OS (scripts/followups/followups.py). Turns open loops from
PASSIVE (a list) into ACTIVE: "after step A of an SOP happens, watch for trigger T, then
put step B in front of the operator as a ticket". Deterministic, zero-LLM, no mail, no
sends: a fired follow-up is an assistant TICKET (source=followups); the operator drafts
the step as a card and the owner approves it like anything else.

  Ledger:  assistant/data/followups/state.json (+ log.jsonl)      git-ignored runtime state
  SOP map: assistant/data/sop_map.json                           tracked config, {var} templates

CLI
  arm --sop S --step X [--var k=v ...] [--by who]     stage from the map (dedupes on sop+step+unit)
  list [--status waiting] [--sop S] [--json]
  mark <id> --status waiting|fired|done|dismissed|expired [--note ...]
  due [--now ISO]                                     date-trigger ids ready to fire
  tick                                                gc + fire due items + match recent mail tickets
                                                      -> assistant tickets. Runs every operator tick.
  stats

Triggers: date (fire_at, optional recur {every_days,max}), mail (from_contains /
subject_contains / body_contains, matched against mail tickets the mail sensor added since
the last tick), manual. `expires_at` hard-stops anything.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alib import DATA, config, load_json, save_json  # noqa: E402

STATE_DIR = DATA / "followups"
STATE_PATH, LOG_PATH = STATE_DIR / "state.json", STATE_DIR / "log.jsonl"
SOP_MAP_PATH = Path(os.environ.get("SOP_MAP") or (Path(__file__).resolve().parent.parent / "data" / "sop_map.json"))
if not SOP_MAP_PATH.is_file() and not os.environ.get("SOP_MAP"):   # the bare engine: the scrubbed template
    SOP_MAP_PATH = SOP_MAP_PATH.parents[1] / "templates" / "data" / SOP_MAP_PATH.name
VALID_STATUS = {"waiting", "fired", "done", "dismissed", "expired"}
VALID_TRIGGER = {"mail", "date", "manual"}
_VAR_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    if not s:
        return None
    s = str(s).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.rstrip("Z"), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class StateLock:
    """Cross-process mutex for followups/state.json. The spine's followups-tick, the operator
    tick's sensors (arm) and the operator's own `mark` all read-modify-write the ledger; the
    last save used to win, so a "stop reminding" could be silently reverted. Separate from
    the ticket QueueLock on purpose: _fire() takes that one inside this one. Not re-entrant.
    A lock that cannot be had in wait_s is skipped (the caller proceeds unlocked, as before)
    rather than blocking a cron; a crashed owner's lock is broken after max_age_s."""
    def __init__(self, wait_s=15, max_age_s=300):
        self.path, self.wait_s, self.max_age_s, self.held = STATE_DIR / ".state.lock", wait_s, max_age_s, False

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.wait_s
        while True:
            try:
                with open(self.path, "x", encoding="utf-8") as fd:
                    fd.write(str(os.getpid()))
                self.held = True
                return True
            except (FileExistsError, PermissionError):      # PermissionError = Windows delete-pending: contention
                try:
                    if time.time() - self.path.stat().st_mtime > self.max_age_s:
                        self.path.unlink()
                except OSError:
                    pass                      # vanished, or still held open: fall through to the deadline + sleep
                if time.time() >= deadline:
                    return False
                time.sleep(0.2)

    def __exit__(self, *exc):
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass


def load_state():
    st = load_json(STATE_PATH, {})
    st.setdefault("items", {})
    st.setdefault("last_mail_scan", None)
    return st


def save_state(st):
    save_json(STATE_PATH, st)


def log(event, it, extra=None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": now_iso(), "event": event, "id": it.get("id"), "sop": it.get("sop"), "step": it.get("step"),
           "unit": it.get("unit"), "status": it.get("status")}
    if extra:
        rec.update(extra)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ model
def _subst(obj, vars):
    if isinstance(obj, str):
        return _VAR_RE.sub(lambda m: str(vars.get(m.group(1), m.group(0))), obj)
    if isinstance(obj, list):
        return [_subst(x, vars) for x in obj]
    if isinstance(obj, dict):
        return {k: _subst(v, vars) for k, v in obj.items()}
    return obj


def _norm_list(v):
    if v is None:
        return []
    return [v] if isinstance(v, str) and v else [str(x) for x in v if str(x)] if isinstance(v, list) else []


def _offset_date(base, spec):
    """'{T-14d}' / '{T+2h}' style offsets resolved against a base date/datetime."""
    m = re.fullmatch(r"T([+-])(\d+)([dh])", spec or "")
    if not m or not base:
        return None
    n = int(m.group(2)) * (1 if m.group(1) == "+" else -1)
    return base + (timedelta(days=n) if m.group(3) == "d" else timedelta(hours=n))


def make_item(*, sop, step, trigger, action, subject="", unit="", lane="personal", created_by="manual",
              fire_at=None, expires_at=None, recur=None, vars=None):
    ttype = (trigger or {}).get("type")
    anchored = False
    if ttype not in VALID_TRIGGER:
        raise ValueError(f"trigger.type must be one of {VALID_TRIGGER}, got {ttype!r}")
    if ttype == "date":
        fa = fire_at or trigger.get("fire_at")
        if isinstance(fa, str) and fa.startswith("T") and vars:
            base = _anchor(vars)
            resolved = _offset_date(base, fa)
            fa = resolved.strftime("%Y-%m-%dT%H:%M:%S") if resolved else None
            anchored = bool(resolved)
        if not fa and recur:
            fa = (now_utc() + timedelta(days=max(1, int(recur.get("every_days") or 1)))).strftime("%Y-%m-%dT%H:%M:%S")
        if not fa:
            raise ValueError("date trigger requires fire_at (or an anchor var for a T+/-N offset)")
        fire_at = fa
    if ttype == "mail":
        for f in ("from_contains", "subject_contains", "body_contains"):
            trigger[f] = _norm_list(trigger.get(f))
        if not any(trigger.get(f) for f in ("from_contains", "subject_contains", "body_contains")):
            raise ValueError("mail trigger needs from_contains / subject_contains / body_contains")
    if isinstance(expires_at, str) and expires_at.startswith("T") and vars:
        base = _anchor(vars)
        r = _offset_date(base, expires_at)
        expires_at = r.strftime("%Y-%m-%dT%H:%M:%S") if r else None
    return {"id": "fu_" + uuid.uuid4().hex[:8], "sop": sop, "step": step, "subject": subject, "unit": unit,
            "lane": lane, "trigger": trigger, "action": action, "status": "waiting", "created": now_iso(),
            "created_by": created_by, "fire_at": fire_at, "expires_at": expires_at, "recur": recur or None,
            "fired_at": None, "fire_count": 0, "evidence": None, "anchored": anchored}


def _anchor(vars):
    """The anchor a T+/-N offset is measured from. A bare date (a wedding day, a bill's due
    date) means 09:00 LOCAL that day, not midnight UTC: midnight UTC is 5 pm Pacific the day
    BEFORE, so the day-after ingest gate used to fire during the reception and 'on the day'
    reminders the evening before. Full timestamps are taken as they are."""
    s = str(vars.get("anchor") or vars.get("wedding_date") or vars.get("date") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            return datetime.fromisoformat(s).replace(hour=9).astimezone().astimezone(timezone.utc)
        except ValueError:
            return None
    return parse_iso(s)


def load_sop_map():
    m = load_json(SOP_MAP_PATH, None)
    if m is None:
        sys.exit(f"FATAL: missing or unreadable {SOP_MAP_PATH}")
    return m


def item_from_map(sop, step, vars, created_by):
    sops = load_sop_map().get("sops", {})
    if sop not in sops:
        raise ValueError(f"unknown sop {sop!r}; known: {sorted(sops)}")
    steps = sops[sop].get("steps", {})
    if step not in steps:
        raise ValueError(f"unknown step {step!r} for {sop!r}; known: {sorted(steps)}")
    tmpl = _subst(steps[step], vars)
    trigger = dict(tmpl.get("trigger") or {"type": "manual"})
    return make_item(sop=sop, step=step, trigger=trigger, action=tmpl.get("action") or {},
                     subject=tmpl.get("subject", ""), unit=str(vars.get("unit", tmpl.get("unit", ""))),
                     lane=sops[sop].get("lane", "personal"), created_by=created_by,
                     fire_at=tmpl.get("fire_at"), expires_at=tmpl.get("expires_at"), recur=tmpl.get("recur"), vars=vars)


def arm(sop, step, vars, created_by="manual"):
    """Stage one follow-up from the map. Dedupes on sop+step+unit against every LIVE item
    (waiting or fired-not-consumed) so a repeating sensor cannot stack twins."""
    item = item_from_map(sop, step, vars, created_by)
    with StateLock():
        return _arm_locked(sop, step, item, created_by)


def _arm_locked(sop, step, item, created_by):
    st = load_state()
    for it in st["items"].values():
        # ANY prior item for this duty blocks a re-arm: waiting/fired (live), done (already
        # fired once, or cancelled), dismissed (the owner said no). Only expired ones may be
        # re-armed, and arm_chain never re-arms a step whose time has passed anyway.
        # (2026-09-30: with waiting/fired only, a sensor re-armed four just-fired one-shots
        # on its very next pass and fired them twice.)
        if it["sop"] == sop and it["step"] == step and it.get("unit") == item["unit"] and it["status"] != "expired":
            # the anchor moved (wedding date changed, the owner set a promised delivery date): a
            # duty that has not fired yet follows it. Never touches an item that already fired
            # or a recurring one that has advanced itself.
            if (it["status"] == "waiting" and not int(it.get("fire_count") or 0) and item.get("anchored")
                    and (it.get("trigger") or {}).get("type") == "date"
                    and (it.get("fire_at"), it.get("expires_at")) != (item["fire_at"], item["expires_at"])):
                old = it.get("fire_at")
                it["fire_at"], it["expires_at"] = item["fire_at"], item["expires_at"]
                it["subject"], it["action"] = item["subject"], item["action"]     # they quote the date
                it["anchored"] = True
                save_state(st)
                log("rearm_anchor", it, {"was": old})
                return {"armed": False, "dup_of": it["id"], "status": it["status"], "moved": True}
            # a live recurring duty follows the map's cap (the cap is copied at arm time, so
            # raising it in sop_map.json did nothing for weddings that were already armed)
            if (it["status"] in ("waiting", "fired") and isinstance(it.get("recur"), dict) and isinstance(item.get("recur"), dict)
                    and it["recur"].get("max") != item["recur"].get("max")):
                it["recur"]["max"] = item["recur"].get("max")
                save_state(st)
                log("recur_cap", it, {"max": it["recur"]["max"]})
            return {"armed": False, "dup_of": it["id"], "status": it["status"]}
    if item["expires_at"] and parse_iso(item["expires_at"]) and parse_iso(item["expires_at"]) <= now_utc():
        return {"armed": False, "reason": "already expired"}
    st["items"][item["id"]] = item
    save_state(st)
    log("arm", item, {"created_by": created_by})
    return {"armed": True, "id": item["id"], "fire_at": item["fire_at"]}


def cancel_unit(sop, unit, why="cancelled", keep_steps=(), hold=False):
    """Stop every live duty of one unit (a wedding that was cancelled or delivered): waiting /
    fired items are dismissed and their still-open follow-up tickets are closed, so nothing
    keeps reminding the owner about a couple who is no longer a client. Returns how many stopped.
    hold=True parks them as 'expired' instead, which arm_chain may re-arm: a wedding that is
    still a lead (not signed) gets its chain back the day the CRM calls it a client."""
    n, ids = 0, set()
    with StateLock():
        st = load_state()
        for it in st["items"].values():
            if it["sop"] == sop and it.get("unit") == unit and it["step"] not in keep_steps:
                ids.add(it["id"])             # a one-shot that already fired is 'done' but its ticket may still be open
                if it["status"] in ("waiting", "fired"):
                    it["status"] = "expired" if hold else "dismissed"
                    log("hold_unit" if hold else "cancel_unit", it, {"why": why})
                    n += 1
        if n:
            save_state(st)
    if ids:            # every pass (cheap at this scale): a one-shot that fired earlier may still have an open ticket
        try:
            import tickets
            for t in list(tickets._load().values()):
                if t.get("source") == "followups" and t.get("status") in ("new", "staged") and (t.get("payload") or {}).get("followup") in ids:
                    tickets._transition(t["id"], "dismissed", "followups", f"{unit}: {why}"[:200], {"new", "staged"})   # not 'claimed': the operator has it
        except Exception:
            pass
    return n


def arm_chain(sop, vars, created_by="sensor", steps=None, grace_days=1):
    """Arm every date step of a SOP whose fire time is still ahead (or inside grace_days
    behind, so a sensor that missed a day still fires it). Idempotent through arm()'s
    dedupe. Returns {step: result}."""
    sops = load_sop_map().get("sops", {})
    out = {}
    cutoff = now_utc() - timedelta(days=grace_days)
    for step in (steps or list(sops.get(sop, {}).get("steps", {}))):
        try:
            probe = item_from_map(sop, step, vars, created_by)
        except ValueError as e:
            out[step] = {"armed": False, "reason": str(e)}
            continue
        fa = parse_iso(probe.get("fire_at"))
        if (probe.get("trigger") or {}).get("type") == "date" and fa and fa < cutoff:
            out[step] = {"armed": False, "reason": "in the past"}
            if probe.get("anchored"):
                _retire_stale(sop, step, probe["unit"], probe["fire_at"])
            continue
        out[step] = arm(sop, step, vars, created_by)
    return out


def _retire_stale(sop, step, unit, new_fire_at):
    """The anchor moved so far back that this step's time has already passed (a wedding date
    pulled earlier). A not-yet-fired item still holding the OLD, later time would fire after
    the event it was meant to precede, so it is dismissed."""
    with StateLock():
        st = load_state()
        for it in st["items"].values():
            if (it["sop"] == sop and it["step"] == step and it.get("unit") == unit and it["status"] == "waiting"
                    and not int(it.get("fire_count") or 0) and it.get("anchored") and it.get("fire_at") != new_fire_at):
                it["status"] = "dismissed"
                save_state(st)
                log("anchor_moved_past", it, {"new_fire_at": new_fire_at})
                return True
    return False


# ------------------------------------------------------------------ evaluation
def mail_matches(trigger, mail):
    if trigger.get("type") != "mail":
        return False
    fr = str(mail.get("from", ""))
    subj = str(mail.get("subject", ""))
    body = str(mail.get("snippet", "") or mail.get("body", ""))
    checks = []
    for field, hay in (("from_contains", fr), ("subject_contains", subj), ("body_contains", body)):
        if trigger.get(field):
            checks.append(any(n.lower() in hay.lower() for n in trigger[field]))
    return bool(checks) and all(checks)


def _rearm(it, now):
    rec = it.get("recur")
    if not isinstance(rec, dict) or int(rec.get("every_days") or 0) <= 0:
        return False
    rec["count"] = int(rec.get("count") or 0) + 1
    if rec.get("max") is not None and rec["count"] >= int(rec["max"]):
        return False
    base = parse_iso(it.get("fire_at")) or now
    nxt = max(base, now) + timedelta(days=int(rec["every_days"]))
    ea = parse_iso(it.get("expires_at"))
    if ea and nxt > ea:
        return False
    it.update(fire_at=nxt.strftime("%Y-%m-%dT%H:%M:%S"), status="waiting", fired_at=None, evidence=None)
    return True


def _meeting_hold(st, it, now):
    """A lead who booked a meeting or call gets no nudge, no reply chase, nothing (the owner 2026-10-08,
    Arden & Reese: "they booked a meeting, no email needed"). True = skip this tick, silently.
    The item waits (and does not expire) until a day after the meeting; then, with no new meeting,
    the first held nudge fires (its instruction still checks the CRM for a signature) and the
    other held steps of that lead are closed, so a passed meeting costs the couple one nudge at most."""
    try:
        import lead_meetings
        m = lead_meetings.for_unit(it.get("unit"), str(it.get("subject") or "").split(" - ")[0])   # subject = "{couple} - ..."
    except Exception:  # noqa: BLE001   a meeting lookup must never cost the follow-up
        m = None
    if m:
        if (it.get("meeting_hold") or {}).get("start") != m.get("start"):
            it["meeting_hold"] = {"id": m.get("id"), "start": m.get("start"), "when": m.get("when")}
            log("meeting_hold", it, {"meeting": m.get("when")})
        return True
    hold = it.get("meeting_hold")
    if not hold:
        return False
    start = parse_iso(hold.get("start"))
    if start and now < start + timedelta(days=1):
        return True                       # the meeting just happened: give them a day
    others = [o for o in st["items"].values() if o is not it and o.get("sop") == "inquiry" and o.get("unit") == it.get("unit")]
    if it.get("step") == "reply-sla" or any(o.get("meeting_released") == hold.get("start") for o in others):
        it["status"] = "done"             # they met (a first reply is moot) / this lead already got its one nudge
        log("meeting_passed_closed", it, {"meeting": hold.get("when")})
        return True
    it.pop("meeting_hold", None)
    it["meeting_released"] = hold.get("start")
    log("meeting_passed_resume", it, {"meeting": hold.get("when")})
    return False


def _fire(st, it, now, evidence=None):
    """Mark fired and hand the step to the operator as an assistant ticket."""
    import tickets
    if it.get("sop") == "inquiry" and _meeting_hold(st, it, now):
        return 0
    act0 = it.get("action") or {}
    if isinstance(act0, dict) and act0.get("satisfied_when"):
        # WP3 (2026-10-24): a step reads the record before it fires. Satisfied = done by record, no ticket, no text;
        # moot (cancelled / lost) = dismissed; open / unknown = fire with what was read as the evidence.
        try:
            import preconditions
            verdict, detail = preconditions.check(act0.get("satisfied_when"), str(it.get("unit") or ""))
        except Exception as e:  # noqa: BLE001
            verdict, detail = "unknown", f"precondition failed: {type(e).__name__}"
        if verdict == "satisfied":
            it["status"], it["fired_at"], it["evidence"] = "done", now_iso(), "by_record: " + str(detail)[:200]
            log("satisfied", it, {"detail": detail})
            _outcome_followup(it, "verified", detail)
            return 0
        if verdict == "moot":
            it["status"], it["evidence"] = "dismissed", "moot: " + str(detail)[:200]
            log("moot", it, {"detail": detail})
            _outcome_followup(it, "abandoned", detail)
            return 0
        evidence = (("record checked: " if verdict == "open" else "record unreadable: ") + str(detail)[:160]
                    + (f"; {evidence}" if evidence else ""))
    it["status"], it["fired_at"], it["evidence"] = "fired", now_iso(), evidence
    it["fire_count"] = int(it.get("fire_count") or 0) + 1
    act = dict(it.get("action") or {})
    if isinstance(act.get("instruction"), str):
        act["instruction"] = act["instruction"].replace("<id>", it["id"])   # the cancel command names this item
    auto = str(act.get("auto") or "")
    if auto.startswith("client_texts:"):
        # the owner's standing rule (2026-10-17): a standard client text goes out by itself, no operator,
        # no card. Only when the text cannot go (no number on file, switched off) does the duty
        # become the usual operator ticket, whose instruction says what to do instead.
        try:
            import client_texts
            r = client_texts.auto_fire(it["step"], str(it.get("unit") or ""), {"kind": auto.split(":", 1)[1]})
        except Exception as e:  # noqa: BLE001   a broken texter must never lose the duty
            r = {"ok": False, "reason": f"{type(e).__name__}: {str(e)[:160]}"}
        if r.get("ok") or r.get("dup"):
            log("fire_auto", it, {"ticket": r.get("ticket"), "dup": bool(r.get("dup"))})
            if not _rearm(it, now):
                it["status"] = "done" if not it.get("recur") else "fired"
            return 0
        act["instruction"] = (f"[The standard text could NOT go by itself: {r.get('reason')}.] " + str(act.get("instruction") or ""))
        evidence = evidence or f"auto text held: {str(r.get('reason'))[:160]}"
    item = {"source": "followups", "source_id": f"{it['id']}|{it['fire_count']}", "lane": it.get("lane") or "personal",
            "kind": "followup", "title": f"{it.get('subject') or it['sop'] + '/' + it['step']}",
            "summary": str(act.get("instruction") or "")[:600],
            "payload": {"followup": it["id"], "sop": it["sop"], "step": it["step"], "unit": it.get("unit"),
                        "skill": act.get("skill"), "instruction": act.get("instruction"), "evidence": evidence,
                        "recurring": bool(it.get("recur")),
                        "cancel": f"python assistant/scripts/followups.py mark {it['id']} --status done"}}
    added = tickets.add_many([item], "followups")
    if added < 0:
        # QUEUE_BUSY: no ticket was written. Put the item back so the next tick fires it;
        # marking it done here would lose the duty (a timeline request, a thank-you) for good.
        it["status"], it["fired_at"], it["evidence"] = "waiting", None, None
        it["fire_count"] -= 1
        log("fire_deferred", it, {"reason": "QUEUE_BUSY"})
        return added
    log("fire", it, {"evidence": evidence, "ticket_added": added})
    _outcome_followup(it, "claimed", str(evidence or "")[:160])
    if not _rearm(it, now):
        # one-shot: the ticket now carries the duty; the item is consumed by that ticket
        it["status"] = "done" if not it.get("recur") else "fired"
    return added


def _outcome_followup(it: dict, state: str, detail: str) -> None:
    """One outcome row per fired step (outcomes.py): by_record = verified, moot = abandoned, fired = claimed with the
    `followup` verifier (its ticket's status settles it). Never raises."""
    try:
        import outcomes
        row = outcomes.claim("followup", str(it.get("id")), f"{it.get('sop')}/{it.get('step')} {it.get('subject') or ''}"[:160],
                             lane=str(it.get("lane") or ""), route="person", verifier="followup", meta={"unit": it.get("unit")})
        if row and state != "claimed":
            outcomes.set_state(row["id"], state, {"by_record": detail[:200]}, detail[:200], by="followups")
    except Exception:  # noqa: BLE001
        pass


def tick(now=None):
    """gc expired, fire due date items, match mail tickets added since the last scan."""
    with StateLock():
        return _tick_locked(now or now_utc())


def _tick_locked(now):
    st = load_state()
    fired = expired = 0
    busy = False
    for it in st["items"].values():
        if it["status"] != "waiting":
            continue
        ea = parse_iso(it.get("expires_at"))
        if ea and ea <= now and not it.get("meeting_hold"):     # a nudge held for a meeting resumes after it
            it["status"] = "expired"
            log("expire", it)
            expired += 1
            continue
        if (it.get("trigger") or {}).get("type") == "date" and not busy:
            fa = parse_iso(it.get("fire_at"))
            if fa and fa <= now:
                if _fire(st, it, now) >= 0:
                    fired += 1
                else:
                    busy = True               # each further try would wait 20 s on the same lock: next tick
    # mail triggers: look at mail tickets created since the last scan
    since = st.get("last_mail_scan") or "1970-01-01T00:00:00Z"
    matched = 0
    try:
        import tickets
        recent = [t for t in tickets._load().values() if str(t.get("source", "")).startswith("mail:") and t.get("created", "") >= since]   # >= : same-second ticks (second resolution)
    except Exception:
        recent = []
    for t in recent:
        p = t.get("payload") or {}
        mail = {"from": p.get("from") or p.get("sender") or "", "subject": p.get("subject") or t.get("title") or "",
                "snippet": p.get("snippet") or t.get("summary") or ""}
        for it in st["items"].values():
            if busy or it["status"] != "waiting" or not mail_matches(it.get("trigger") or {}, mail):
                continue
            if _fire(st, it, now, evidence=t.get("id")) >= 0:
                matched += 1
            else:
                busy = True
    if not busy:                              # a deferred fire must see the same mail tickets again next tick
        st["last_mail_scan"] = now_iso()
    save_state(st)
    waiting = sum(1 for it in st["items"].values() if it["status"] == "waiting")
    return {"fired": fired, "mail_matched": matched, "expired": expired, "waiting": waiting}


# ------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("arm"); p.add_argument("--sop", required=True); p.add_argument("--step", required=True)
    p.add_argument("--var", action="append", default=[]); p.add_argument("--by", default="manual")
    p = sub.add_parser("list"); p.add_argument("--status", default=""); p.add_argument("--sop", default=""); p.add_argument("--json", action="store_true")
    p = sub.add_parser("mark"); p.add_argument("id"); p.add_argument("--status", required=True); p.add_argument("--note", default="")
    p = sub.add_parser("due"); p.add_argument("--now", default="")
    p = sub.add_parser("tick"); p.add_argument("--now", default="")
    sub.add_parser("stats")
    a = ap.parse_args()

    if a.cmd == "arm":
        vars = dict(kv.split("=", 1) for kv in a.var if "=" in kv)
        try:
            print(json.dumps(arm(a.sop, a.step, vars, a.by)))
        except ValueError as e:
            print(json.dumps({"armed": False, "reason": str(e)})); return 2
        return 0
    st = load_state()
    if a.cmd == "list":
        rows = [it for it in st["items"].values() if (not a.status or it["status"] == a.status) and (not a.sop or it["sop"] == a.sop)]
        rows.sort(key=lambda x: (x.get("fire_at") or "9", x["created"]))
        if a.json:
            print(json.dumps(rows, indent=1, ensure_ascii=False))
        else:
            for it in rows:
                print(f"{it['id']} {it['status']:8} {it['sop']}/{it['step']} unit={it.get('unit') or '-'} fire_at={it.get('fire_at') or '-'} {it.get('subject','')[:60]}")
            if not rows:
                print("NO_FOLLOWUPS")
        return 0
    if a.cmd == "mark":
        if a.status not in VALID_STATUS:
            print("BAD_STATUS"); return 2
        with StateLock():
            st = load_state()
            it = st["items"].get(a.id)
            if not it:
                print("NOT_FOUND"); return 2
            it["status"] = a.status
            save_state(st)
        log("mark", it, {"note": a.note})
        print(f"MARKED {a.id} {a.status}"); return 0
    if a.cmd == "due":
        now = parse_iso(a.now) or now_utc()
        print(json.dumps([it["id"] for it in st["items"].values() if it["status"] == "waiting"
                          and (it.get("trigger") or {}).get("type") == "date" and parse_iso(it.get("fire_at")) and parse_iso(it["fire_at"]) <= now]))
        return 0
    if a.cmd == "tick":
        r = tick(parse_iso(a.now) if a.now else None)
        print("FOLLOWUPS " + " ".join(f"{k}={v}" for k, v in r.items())); return 0
    if a.cmd == "stats":
        c = {}
        for it in st["items"].values():
            c[it["status"]] = c.get(it["status"], 0) + 1
        print(json.dumps(c)); return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
