"""outcomes.py - the outcome ledger: "done" means read back (WP1 of the stand-alone proposal, 2026-10-24).

the owner, 2026-10-24: "take action with verifiable results and never let a failure go unnoticed without a
follow-up". Until this file, "done" meant one of three things, none of them a read of the record: a
ticket was consumed, a card was tapped, or a model said so. A sheet write whose read-back said
VERIFIED=no was `done`; a text handed to a phone that never acks got a green check; a follow-up step was
`done` the minute its ticket was minted. This module owns ONE row per thing the system claims to have
done and moves it to a terminal state that a read of the system of record justifies:

    claimed -> verifying -> verified | unverified | failed | abandoned

  claimed      a hand-off or an action is in flight; nothing has been read back yet
  verifying    a verifier is scheduled (due_verify) and has not reached a verdict
  verified     the record shows it happened (evidence says what was read and when)
  unverified   the window passed and the record cannot say (a phone that never acks, a timeout): the owner
               is asked ONCE ("Did this go?") and the answer is the evidence
  failed       the record shows it did NOT happen (a read-back that disagrees, a refusal, a dead route)
  abandoned    the owner dropped it, or the portal took it (nothing will re-queue it)

Writers: drain.py (every approved action, by kind), the doer (DOER_DONE + evidence.json), the agent's
commitments (hand-offs: task / do / build), the bridge (a completed-action sentence with no deed behind
it is a `said` row that fails at once). Readers: cards.close_answered (a ticket closes from the ledger,
not from the tap), chat_push.answered_icon (the mark on a card comes from here), /open and /failed (WP2),
the scorecard (WP6). settle() runs from the Day Spine (`outcomes-settle`, every 10 min): it re-runs the
verifier of every non-terminal row that is due, posts the one owner card an `unverified` row gets, and
edits the card's thread with the ledger's line when a state changes.

Append-only JSONL folded latest-per-id (exec_store's shape), zero-LLM, stdlib only. Nothing here sends.

  python assistant/scripts/outcomes.py open|failed|settle [--dry-run]|show <id>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from alib import DATA, load_json  # noqa: E402

STORE = DATA / "outcomes.jsonl"
FMT = "%Y-%m-%dT%H:%M:%S"
STATES = ("claimed", "verifying", "verified", "unverified", "failed", "abandoned")
TERMINAL = {"verified", "failed", "abandoned"}
KEEP_DAYS = 45

# how long a kind may stay unread before its verifier is asked (minutes); None = read at once
DUE_MIN = {"imessage_send": 5, "agent_run": 90, "task": 150, "do": 90, "build": 26 * 60, "followup": 60}
# an `unverified` row waits this long for a late verdict before the owner is asked once (minutes); a text's
# verifier already waited the phone's no-ack window (phone_bridge.HANDED_STALE_NOACK_MIN), so it is asked at once
ASK_AFTER_MIN = {"imessage_send": 0, "default": 60}
# nothing is chased past this (hours): the row is abandoned with a line
GIVEUP_H = 72
QUIET = (21, 7)

# route clocks (the property planner's max_wait, in minutes): a claim that has waited longer than its route
# allows is `unverified` and the owner is asked, instead of waiting again. Overridden by tracking.json -> routes.
ROUTES_DEFAULT = {"connector": 15, "phone": 240, "browser": 90, "operator": 150, "build": 26 * 60, "person": 2 * 24 * 60, "owner_card": 24 * 60}
ROUTE_OF = {"email_send": "connector", "hb_message": "connector", "hb_touches_send": "connector", "hb_add_participant": "connector",
            "hb_add_team_member": "connector", "hb_set_stage": "connector", "hb_note": "connector", "hb_project_update": "connector",
            "hb_proposal_draft": "connector", "hb_automation_approve": "browser", "sheet_set": "connector", "imessage_send": "phone",
            "agent_run": "browser", "do": "browser", "task": "operator", "build": "build", "followup": "person"}
RETRY_KINDS = {"email_send", "imessage_send", "hb_message", "hb_touches_send", "hb_add_participant", "hb_set_stage", "hb_note",
               "hb_project_update", "sheet_set", "hb_automation_approve"}

ICON = {"claimed": "⏳", "verifying": "⏳", "verified": "✅", "unverified": "⚠️", "failed": "❌", "abandoned": "⌛"}

_lock_guard = None


def _now() -> str:
    return time.strftime(FMT)


def _ts(s) -> float:
    try:
        return time.mktime(time.strptime(str(s)[:19], FMT))
    except (TypeError, ValueError):
        return 0.0


def _append(row: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    with open(STORE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def fold() -> dict:
    out = {}
    if not STORE.exists():
        return out
    for ln in STORE.read_text(encoding="utf-8-sig").splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("id"):
            prev = out.get(e["id"], {})
            checks = list(prev.get("checks") or []) + list(e.get("checks") or [])
            out[e["id"]] = {**prev, **e, "checks": checks[-20:]}
    return out


def _by_ref(kind: str, ref: str) -> dict | None:
    for r in fold().values():
        if r.get("kind") == kind and r.get("ref") == ref and r.get("state") not in TERMINAL:
            return r
    return None


def by_ticket(ticket_id: str) -> dict | None:
    """The row behind an exec ticket (drain writes `ref` = ticket id for exec kinds)."""
    rows = [r for r in fold().values() if r.get("ref") == ticket_id]
    rows.sort(key=lambda r: r.get("created", ""))
    return rows[-1] if rows else None


def state_for_ticket(ticket_id: str) -> str:
    r = by_ticket(ticket_id)
    return str(r.get("state") or "") if r else ""


# ------------------------------------------------------------------ writing
def claim(kind: str, ref: str, what: str, lane: str = "", by: str = "", convo: str = "", thread: str = "",
          route: str = "", verifier: str | None = None, due_min: int | None = None, meta: dict | None = None,
          card: str = "") -> dict:
    """One row for one claim; the same (kind, ref) while non-terminal is the same row. `card` is the approval
    card the claim came from (the thread the ledger speaks in); `ask_card` (set by ask_owner) is the one
    question card an unverified row gets."""
    ref = str(ref or "")
    if not kind or not ref:
        return {}
    cur = _by_ref(kind, ref)
    if cur:
        return cur
    now = datetime.now()
    due = due_min if due_min is not None else DUE_MIN.get(kind)
    row = {"id": "o-" + uuid.uuid4().hex[:10], "kind": kind, "ref": ref, "what": re.sub(r"\s+", " ", str(what or "")).strip()[:240],
           "lane": str(lane or ""), "by": str(by or ""), "convo": str(convo or ""), "thread": str(thread or ""),
           "route": str(route or kind), "verifier": verifier or kind, "state": "claimed", "created": now.strftime(FMT),
           "updated": now.strftime(FMT), "due_verify": (now + timedelta(minutes=due)).strftime(FMT) if due else now.strftime(FMT),
           "retries": 0, "escalation": "none", "evidence": {}, "checks": [], "card": str(card or ""), "meta": meta or {}}
    _append(row)
    _ledger("outcome_claim", id=row["id"], kind=kind, ref=ref[:80])
    return row


def set_state(oid: str, state: str, evidence: dict | None = None, detail: str = "", by: str = "code", **extra) -> dict:
    if state not in STATES:
        raise ValueError(f"bad state {state!r}")
    cur = fold().get(oid) or {}
    if cur.get("state") in TERMINAL and state != cur.get("state"):
        # a terminal verdict is never quietly rewritten (the property rule: a done ticket is never cancelled);
        # a correction is a new row
        return cur
    row = {"id": oid, "ts": _now(), "updated": _now(), "state": state,
           "checks": [{"ts": _now(), "result": state, "detail": str(detail or "")[:300], "by": by}], **extra}
    if evidence:
        row["evidence"] = {**(cur.get("evidence") or {}), **evidence}
    if state in TERMINAL:
        row["closed"] = _now()
    _append(row)
    _ledger("outcome_state", id=oid, state=state, detail=str(detail or "")[:120])
    return {**cur, **row}


def routes() -> dict:
    try:
        t = load_json(HERE.parent / "tracking.json", {}) or {}
        r = t.get("routes") if isinstance(t.get("routes"), dict) else {}
        return {**ROUTES_DEFAULT, **{k: int(v) for k, v in r.items() if isinstance(v, (int, float)) and not str(k).startswith("_")}}
    except Exception:  # noqa: BLE001
        return dict(ROUTES_DEFAULT)


def route_max_min(row: dict) -> int:
    rc = ROUTE_OF.get(str(row.get("kind") or ""), str(row.get("route") or "").split(":")[0])
    return int(routes().get(rc) or routes().get("connector"))


def _ledger(event: str, **fields) -> None:
    try:
        import exec_store as es
        es.ledger(event, **fields)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ drain: what an executor's line proves
_HB_ID = re.compile(r"HB_DONE=([0-9a-f]{24})")
_MSG_ID = re.compile(r"SENT=1\s+(\S+)")
_PHONE = re.compile(r"PHONE_QUEUED\s+(m-[0-9a-f]+)")


def verdict_from_exec(kind: str, ok: bool, result: str) -> tuple:
    """-> (state, evidence, detail). What the executor's own line proves, by kind. A hedged read-back
    (VERIFIED=no, MEMBER=no, ARCHIVED=no) is a FAILURE, not a done with a warning; an UNVERIFIED result is
    `unverified`; a kind with no read-back of its own stays `claimed` and its verifier settles it later."""
    out = str(result or "")
    if not ok:
        if "UNVERIFIED" in out.upper() or "unverified" in out.lower():
            return "unverified", {"result": out[:300]}, out[:200]
        return "failed", {"result": out[:300]}, out[:200]
    if kind == "email_send":
        m = _MSG_ID.search(out)
        ev = {"message_id": m.group(1) if m else "", "thread": "ok" if "THREAD=ok" in out else
              ("mismatch" if "THREAD_MISMATCH" in out else ("unverified" if "THREAD_UNVERIFIED" in out else "n/a"))}
        if not m:
            return "unverified", ev, "the send reported ok without a Message-ID"
        return "verified", ev, f"server accepted it (Message-ID {m.group(1)})"
    if kind == "imessage_send":
        m = _PHONE.search(out)
        return "claimed", {"outbox": m.group(1) if m else ""}, "handed to the phone outbox; a reply or an ack confirms it"
    if kind in ("hb_add_participant", "hb_add_team_member"):
        if "MEMBER=no" in out:
            return "failed", {"result": out[:300]}, "HoneyBook accepted the call but the member list does not show them"
        return ("verified", {"hb_id": (_HB_ID.search(out) or [None, ""])[1], "member": "yes"}, "member list read back") if _HB_ID.search(out) \
            else ("unverified", {"result": out[:300]}, "no HoneyBook id in the result")
    if kind == "hb_set_stage":
        if "VERIFIED=no" in out or "ARCHIVED=no" in out:
            return "failed", {"result": out[:300]}, "the read-back shows another stage"
        if "VERIFIED=yes" in out or "ARCHIVED=yes" in out:
            return "verified", {"hb_id": (_HB_ID.search(out) or [None, ""])[1]}, "stage read back"
        return "unverified", {"result": out[:300]}, "no read-back token"
    if kind == "sheet_set":
        m = re.search(r"SHEET_DONE=(\d+) VERIFIED=(\S+)", out)
        if m and m.group(2) == "yes":
            return "verified", {"cells": int(m.group(1))}, f"{m.group(1)} cells read back as written"
        if m:
            return "failed", {"cells": int(m.group(1)), "result": out[:300]}, "the read-back did not match"
        return "unverified", {"result": out[:300]}, "no read-back token"
    if kind == "hb_automation_approve":
        return ("verified", {"step": out[:120]}, "HoneyBook shows the step approved") if "APPROVED_STEP=" in out \
            else ("unverified", {"result": out[:300]}, out[:160])
    if kind.startswith("hb_"):
        m = _HB_ID.search(out)
        if m:
            ev = {"hb_id": m.group(1)}
            if "SKIPPED_EXISTING" in out:
                ev["skipped_existing"] = True
            return "verified", ev, "HoneyBook returned the record id"
        return "unverified", {"result": out[:300]}, "no HoneyBook id in the result"
    if kind == "agent_run":
        return "claimed", {}, "the doer's evidence settles it"
    if kind == "calendar_hold":
        return "verified", {"local": "holds.json"}, "recorded on the local calendar (nobody notified)"
    if kind == "ads_apply":
        return "verified", {"result": out[:200]}, "apply.py re-proved the change"
    return "verified", {"result": out[:200]}, "a local step"


def from_exec(t: dict, ok: bool, result: str) -> dict:
    """drain.py calls this for every executed ticket. -> the row (its `state` tells drain whether a
    read-back disagreed: `failed` on an ok result means the ticket must NOT be marked done)."""
    kind, tid = str(t.get("kind") or ""), str(t.get("id") or "")
    if not tid:
        return {}
    ref = str(t.get("approval_ref") or "")
    card = ref.split("card:", 1)[1] if ref.startswith("card:") else ""
    pay = t.get("payload") or {}
    what = str(pay.get("subject") or pay.get("body") or pay.get("text") or pay.get("stage") or kind)[:120]
    row = claim(kind, tid, what, lane=str(t.get("lane") or ""), by=str(t.get("approved_by") or ""), route=kind,
                verifier=kind, card=card, meta={"approval_ref": ref})
    state, ev, detail = verdict_from_exec(kind, ok, result)
    if state == "failed" and not card and kind in RETRY_KINDS:
        # a standing-rule send (a quiz draft, a standing text, a triage note) failed with no card to bring back:
        # ONE urgent card by code, Retry it / Drop it (WP2). Until this an exec_exception ticket waited for an LLM run.
        row = set_state(row["id"], "failed", ev, detail)
        ask_owner(fold().get(row["id"]) or row, failed=True)
        return fold().get(row["id"]) or row
    if state == "claimed":
        # the kind's own verifier reads the record later; record the hand-off evidence now
        if ev:
            row = set_state(row["id"], "verifying", ev, detail)
        return fold().get(row["id"]) or row
    return set_state(row["id"], state, ev, detail)


def readback_failed(row: dict) -> bool:
    return bool(row) and row.get("state") == "failed"


# ------------------------------------------------------------------ the doer
def from_doer(ticket_id: str, marker: str, ev_ok: bool, ev_n: int, what: str = "", lane: str = "", where: str = "browser") -> dict:
    """A doer run ended. DONE with checked evidence -> verified; DONE with no evidence file -> unverified (a
    claim the code could not check; the owner's card follows if nothing else settles it); evidence that
    does not match -> failed. A checkpoint or a blocked run leaves the row claimed/abandoned."""
    row = claim("agent_run", ticket_id, what or ticket_id, lane=lane, route=f"doer:{where}", verifier="doer")
    if marker in ("DOER_DONE", "DOER_ANSWER"):
        if ev_n and ev_ok:
            return set_state(row["id"], "verified", {"evidence_rows": ev_n}, f"{ev_n} evidence rows re-checked")
        if ev_n and not ev_ok:
            return set_state(row["id"], "failed", {"evidence_rows": ev_n}, "the evidence file does not match the machine")
        if marker == "DOER_ANSWER":
            return set_state(row["id"], "verified", {"answer": True}, "a read-only answer; nothing to read back")
        return set_state(row["id"], "unverified", {}, "DONE with no evidence file")
    if marker == "DOER_BLOCKED":
        return set_state(row["id"], "abandoned", {}, "blocked; the portal line gave the owner the steps", escalation="portal")
    return fold().get(row["id"]) or row


# ------------------------------------------------------------------ the agent's hand-offs and words
def from_commitment(kind: str, ref: str, what: str, by: str = "", convo: str = "", thread: str = "") -> dict:
    return claim(kind, ref, what, by=by, convo=convo, thread=thread, route=kind, verifier="commitment")


def close_commitment(kind: str, ref: str, verdict: str, detail: str = "") -> dict | None:
    """commitments.check closed a row: kept -> verified, failed/unkept -> failed, with_owner -> stays claimed
    (the card is the owner's), reported -> failed (the doer already said so)."""
    row = _by_ref(kind, ref)
    if not row:
        return None
    m = {"kept": "verified", "failed": "failed", "unkept": "failed", "reported": "failed"}
    if verdict in m:
        return set_state(row["id"], m[verdict], {}, detail, by="commitments")
    return row


def said_without_deed(person: str, convo: str, thread: str, sentence: str, needs: list) -> dict:
    """The property rule (exec_verify): a completed-action verb with no evidence token behind it is its own
    alarm. Here: a sentence in the agent's reply that claims work in motion while no deed backs it."""
    ref = f"said:{convo}:{int(time.time())}:{abs(hash(sentence)) % 10**6}"
    row = claim("said", ref, sentence[:200], by=person, convo=convo, thread=thread, route="words", verifier="none")
    return set_state(row["id"], "failed", {"needs": list(needs or [])[:4]}, "a claim with no deed behind it", by="claims")


# ------------------------------------------------------------------ verifiers (read the record, never write)
def _verify_phone(row: dict) -> tuple:
    """imessage_send: the phone outbox. sent (ack or reply) -> verified; expired / recalled -> failed;
    handed past the no-ack window -> unverified; else keep waiting."""
    try:
        import phone_bridge as pb
    except Exception:  # noqa: BLE001
        return "verifying", {}, "phone bridge unavailable"
    oid = str((row.get("evidence") or {}).get("outbox") or "")
    box = load_json(pb.OUTBOX, {"items": []})
    it = next((i for i in box.get("items", []) if i.get("id") == oid), None) if oid else None
    if not it:
        it = next((i for i in box.get("items", []) if i.get("ticket") == row.get("ref")), None)
    if not it:
        age_h = (time.time() - _ts(row.get("created"))) / 3600
        return ("unverified", {}, "no outbox record for this text") if age_h > 1 else ("verifying", {}, "outbox not written yet")
    st = str(it.get("status") or "")
    if st == "sent":
        how = str(it.get("confirmed_by") or "ack")
        return "verified", {"outbox": it.get("id"), "sent": it.get("ts_sent"), "confirmed_by": how}, \
            ("they replied, so it went" if how == "reply" else "the phone confirmed the send")
    if st in ("expired", "recalled"):
        return "failed", {"outbox": it.get("id"), "status": st}, f"the text {st} before the phone sent it"
    if st == "handed":
        # the owner 2026-10-24 20:52: "Stop confirming texts send. Leave that alone." The Shortcut never acks, so a
        # text the phone has taken is sent; no chase card, no "did this text go?" (and no "Confirmed" line, _line).
        return "verified", {"outbox": it.get("id"), "handed": it.get("ts_handed"), "confirmed_by": "handed"}, "handed to the phone"
    return "verifying", {"outbox": it.get("id"), "status": st}, "waiting for the phone to fetch it"


def _verify_commitment(row: dict) -> tuple:
    """The agent's hand-offs settle through commitments.check (which calls close_commitment); here only the
    give-up: a row still open long past its due is unverified, so the owner hears about it once."""
    if time.time() - _ts(row.get("due_verify")) > 6 * 3600:
        return "unverified", {}, "no outcome recorded for this hand-off"
    return "verifying", {}, "with the operator / doer / builder"


def _verify_doer(row: dict) -> tuple:
    try:
        import tickets
        t = tickets._load().get(row.get("ref"))
    except Exception:  # noqa: BLE001
        t = None
    if not t:
        return "verifying", {}, "no ticket"
    if t.get("status") == "dismissed":
        return "abandoned", {}, "the job was dismissed"
    if t.get("status") == "done" and time.time() - _ts(row.get("created")) > 3600:
        return "unverified", {}, "the doer said done and wrote no evidence"
    return "verifying", {}, t.get("status", "")


def _verify_followup(row: dict) -> tuple:
    """A fired step's ticket: done -> verified (the operator's word, the best this record gives); dismissed -> abandoned;
    the follow-up item itself marked done by a later record read (triage closing it) -> verified."""
    try:
        import followups
        st = followups.load_state()
        it = (st.get("items") or {}).get(str(row.get("ref") or ""))
    except Exception:  # noqa: BLE001
        it = None
    if isinstance(it, dict):
        if str(it.get("evidence") or "").startswith("by_record"):
            return "verified", {"by_record": str(it.get("evidence"))[:160]}, "the record shows it"
        if it.get("status") == "dismissed":
            return "abandoned", {}, "the step was dismissed"
    try:
        import tickets
        for t in tickets._load().values():
            if (t.get("payload") or {}).get("followup") == row.get("ref"):
                if t.get("status") == "done":
                    return "verified", {"ticket": t["id"]}, "its ticket was completed"
                if t.get("status") == "dismissed":
                    return "abandoned", {"ticket": t["id"]}, "its ticket was dismissed"
                return "verifying", {"ticket": t["id"]}, t.get("status", "")
    except Exception:  # noqa: BLE001
        pass
    return "verifying", {}, "no ticket yet"


VERIFIERS = {"imessage_send": _verify_phone, "commitment": _verify_commitment, "doer": _verify_doer, "followup": _verify_followup}


def verify(row: dict) -> tuple:
    fn = VERIFIERS.get(str(row.get("verifier") or ""))
    if not fn:
        return str(row.get("state") or "claimed"), {}, "no verifier for this kind"
    try:
        return fn(row)
    except Exception as e:  # noqa: BLE001
        return str(row.get("state") or "claimed"), {}, f"verifier error {type(e).__name__}: {str(e)[:100]}"


# ------------------------------------------------------------------ the owner's one card
def _quiet(now: datetime | None = None) -> bool:
    h = (now or datetime.now()).hour
    if h >= QUIET[0] or h < QUIET[1]:
        return True
    try:                      # /quiet until <time> (bridge.quiet_until): no asks from the ledger either
        agent_data = Path(os.environ["AGENT_DATA"]) if os.environ.get("AGENT_DATA") else DATA / "agent"
        d = load_json(agent_data / "quiet_until.json", {}) or {}
        return bool(d.get("until")) and (now or datetime.now()) < datetime.strptime(d["until"], FMT)
    except (ValueError, TypeError):
        return False


def _who(row: dict) -> str:
    try:
        import exec_store as es
        t = es.fold().get(row.get("ref")) or {}
        p = t.get("payload") or {}
        return str(p.get("to") or p.get("name") or p.get("email") or "")
    except Exception:  # noqa: BLE001
        return ""


def _allowed(cls: str) -> bool:
    """A code-posted card of this class may go out (assistant/agent/reflex.py: an owner's rule switches a class off)."""
    if not cls:
        return True
    try:
        sys.path.insert(0, str(HERE.parent / "agent"))
        import reflex
        return reflex.allowed(cls)
    except Exception:  # noqa: BLE001
        return True


def ask_owner(row: dict, dry: bool = False, failed: bool = False) -> dict:
    """ONE card per unverified row: the system cannot read the record, so the owner's answer is the evidence.
    Every button is something the code does on the tap (It went -> verified; Send it again -> the original
    card comes back marked RESEND; Drop it -> abandoned). A card the guards refuse rides one plain line."""
    kind, what = str(row.get("kind") or ""), str(row.get("what") or "")
    who = _who(row)
    if row.get("ask_card"):
        return {"posted": False, "reason": "already asked"}
    if kind in ("task", "do", "imessage_send") and not failed:
        # a hand-off has its own voice (commitments.py, one receipt where it was asked) and a handed text is sent:
        # a second "Not confirmed: task" / "Did this text go?" card on top was noise (2026-10-24 19:10, 20:50, 07:04)
        set_state(row["id"], "abandoned", {}, "closed quietly: the hand-off's own receipt is the record", escalation="none")
        return {"posted": False, "reason": "quiet close"}
    if not failed and not _allowed("build_unconfirmed" if kind == "build" else "text_unconfirmed" if kind == "imessage_send" else ""):
        # switched off at an owner's word (reflex.py; "I don't want these messages", 2026-10-26 19:37): the row closes
        # quietly and the record says why; nothing is asked
        set_state(row["id"], "abandoned", {}, "closed quietly: the owner switched these notices off", escalation="none")
        return {"posted": False, "reason": "muted"}
    if failed:
        last = str((row.get("checks") or [{}])[-1].get("detail") or "")[:140]
        header = f"Did not go: {kind.replace('_', ' ')}"[:60]
        q = f"This did not go through{(' to ' + who) if who else ''}: \"{what[:140]}\". {last}"
        card = {"header": header, "lane": str(row.get("lane") or "personal"), "audience": "personal", "urgent": True,
                "question": q, "outcome_id": row["id"],
                "options": [{"label": "Retry it (Recommended)", "description": "run the same step again, once, on your tap"},
                            {"label": "Drop it", "description": "stop tracking it"}],
                "details": f"Outcome {row['id']} · {kind} · route {row.get('route')} · ref {row.get('ref')}"}
        return _post_ask(row, card, dry, what, who, state="failed")
    if kind == "imessage_send":
        header = f"Did this text go? {who}".strip()
        q = (f"Your text to {who or 'them'} was handed to the phone at {str((row.get('evidence') or {}).get('handed') or row.get('created'))[11:16]} "
             f"and nothing confirms it went: the phone never acks and they have not replied. \"{what[:140]}\"")
    else:
        header = f"Not confirmed: {kind.replace('_', ' ')}"
        q = f"I could not confirm this landed: {what[:160]}. Last word: {str(row.get('checks', [{}])[-1].get('detail') or '')[:120]}"
    card = {"header": header[:60], "lane": str(row.get("lane") or "personal"), "audience": "personal", "urgent": True,
            "question": q, "outcome_id": row["id"],
            "options": [{"label": "It went (Recommended)", "description": "mark it confirmed; nothing is sent"},
                        {"label": "Send it again", "description": "the original card comes back marked RESEND for your tap"},
                        {"label": "Drop it", "description": "stop tracking it"}],
            "details": f"Outcome {row['id']} · {kind} · route {row.get('route')} · claimed {row.get('created')}"}
    return _post_ask(row, card, dry, what, who, state="unverified")


def _post_ask(row: dict, card: dict, dry: bool, what: str, who: str, state: str) -> dict:
    if dry:
        return {"posted": False, "dry": True, "card": card}
    try:
        import cards
        r = cards.post([card], "outcomes", lane=card["lane"])
    except Exception as e:  # noqa: BLE001
        r = {"posted": False, "reason": f"{type(e).__name__}: {str(e)[:120]}"}
    if r.get("posted"):
        set_state(row["id"], state, {}, "asked the owner", escalation="card", ask_card=str(r.get("qid") or ""), asked=_now())
        return r
    # refused (a duplicate about the same person, a quiet guard): one plain line instead, never nothing
    try:
        import cards
        verb = "did not go through" if state == "failed" else "went"
        cards.say("outcomes", card["lane"], f"I could not confirm that {what[:100]} {verb}{(' to ' + who) if who else ''}; check once and tell me.")
    except Exception:  # noqa: BLE001
        pass
    set_state(row["id"], state, {}, f"card refused ({str(r.get('reason') or '')[:80]}); said in chat", escalation="line", asked=_now())
    return r


def answer(oid: str, option: str, by: str = "owner1") -> dict:
    """The owner's tap on an outcome card (cards.sweep -> _act_on_outcome)."""
    row = fold().get(oid)
    if not row:
        return {"ok": False, "reason": "no such outcome"}
    opt = re.sub(r"\s*\(recommended[^)]*\)", "", str(option or ""), flags=re.I).strip().lower()
    if opt.startswith("it went") or opt.startswith("it landed") or opt.startswith("yes"):
        set_state(oid, "verified", {"owner_said": option}, f"{by} confirmed it went", by=by)
        _mark_outbox_sent(row, by)
        return {"ok": True, "state": "verified"}
    if opt.startswith("retry"):
        try:
            import exec_store as es
            t = es.fold().get(str(row.get("ref") or "")) or {}
            if not t.get("kind") or not isinstance(t.get("payload"), dict):
                return {"ok": False, "reason": "no exec ticket to retry"}
            new = es.enqueue(str(t["kind"]), dict(t["payload"]), es.APPROVER, f"card:{row.get('ask_card') or 'outcome'}", resend_ok=True)
            set_state(oid, "abandoned", {"owner_said": option, "retry_ticket": new["id"]}, f"{by} asked for a retry: ticket {new['id']}", by=by)
            return {"ok": True, "state": "abandoned", "retry_ticket": new["id"]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": f"retry refused: {type(e).__name__}: {str(e)[:140]}"}
    if opt.startswith("send it again") or opt.startswith("resend"):
        set_state(oid, "failed", {"owner_said": option}, f"{by} says it never arrived; resend offered", by=by)
        qid = str(row.get("card") or "")
        if qid:
            try:
                import cards
                r = cards.reopen(qid, "you said it never arrived", by="outcomes", resend=True)
                return {"ok": True, "state": "failed", "reopened": bool(r.get("ok")), "reason": r.get("reason", "")}
            except Exception as e:  # noqa: BLE001
                return {"ok": True, "state": "failed", "reopened": False, "reason": f"{type(e).__name__}: {str(e)[:100]}"}
        return {"ok": True, "state": "failed", "reopened": False, "reason": "no card to bring back"}
    set_state(oid, "abandoned", {"owner_said": option}, f"{by} dropped it", by=by)
    return {"ok": True, "state": "abandoned"}


def _mark_outbox_sent(row: dict, by: str) -> None:
    """The owner says the text went: the outbox agrees (confirmed_by owner), so nothing chases it again."""
    if row.get("kind") != "imessage_send":
        return
    try:
        import phone_bridge as pb
        from alib import QueueLock, save_json
        with QueueLock("outcomes") as ok:
            if not ok:
                return
            box = load_json(pb.OUTBOX, {"items": []})
            oid = str((row.get("evidence") or {}).get("outbox") or "")
            for it in box.get("items", []):
                if (oid and it.get("id") == oid) or it.get("ticket") == row.get("ref"):
                    if it.get("status") == "handed":
                        it["status"], it["ts_sent"], it["confirmed_by"] = "sent", _now(), f"owner:{by}"
            save_json(pb.OUTBOX, box)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ the tick
def _line(row: dict, state: str, detail: str) -> str:
    kind = str(row.get("kind") or "").replace("_", " ")
    who = _who(row)
    when = time.strftime("%I:%M %p").lstrip("0")
    if state == "verified":
        if row.get("kind") == "imessage_send":
            return ""              # the owner 2026-10-24 17:54: no "it went (they replied)" posts; the hand-over line already said so
        return f"Confirmed at {when}: {kind} landed ({detail})."
    if state == "failed":
        return f"Not done: {kind} to {who}: {detail}." if who else f"Not done: {kind}: {detail}."
    if state == "unverified":
        return f"I could not confirm the {kind}{' to ' + who if who else ''} landed: {detail}."
    if state == "abandoned":
        return f"Dropped: {kind}{' to ' + who if who else ''} ({detail})."
    return ""


def _tell(row: dict, state: str, detail: str, dry: bool) -> None:
    """The ledger's voice: the card's own thread (push_result) when the row came from a card, else the
    receipt in the conversation it was asked in (the agent's receipts), else nothing."""
    text = _line(row, state, detail)
    if not text or dry:
        return
    qid = str(row.get("card") or "")
    try:
        if qid:
            import cards
            import chat_push
            card = cards._read(cards.DONE / f"{qid}.json") or cards._read(cards.LIVE / f"{qid}.json") or cards._read(cards.ARCHIVE / f"{qid}.json")
            if card:
                chat_push.push_result(card, text)
                cards.note("outcomes", text + f" (card {qid})")
                return
        if row.get("convo"):
            sys.path.insert(0, str(HERE.parent / "agent"))
            import receipts
            receipts.say(row["id"], str(row.get("convo")), str(row.get("thread") or ""), text)
    except Exception:  # noqa: BLE001
        pass


def settle(now: datetime | None = None, dry: bool = False) -> dict:
    """Every non-terminal row that is due: run its verifier; an `unverified` row past its ask window gets the
    owner's one card (daytime only); anything past GIVEUP_H is abandoned with a line. -> counts."""
    now = now or datetime.now()
    out = {"checked": 0, "verified": 0, "failed": 0, "unverified": 0, "asked": 0, "abandoned": 0, "waiting": 0}
    for row in sorted(fold().values(), key=lambda r: r.get("created", "")):
        st = str(row.get("state") or "")
        if st in TERMINAL:
            continue
        if row.get("kind") == "said":
            continue
        age_h = (now.timestamp() - _ts(row.get("created"))) / 3600
        if st == "unverified":
            if row.get("asked"):
                if age_h > GIVEUP_H:
                    out["abandoned"] += 1
                    if not dry:
                        set_state(row["id"], "abandoned", {}, "no answer; nothing will chase it again", escalation=str(row.get("escalation") or "card"))
                        _tell(row, "abandoned", "nobody could confirm it", dry)
                continue
            # a late verdict (a reply that came in) beats asking
            state, ev, detail = verify(row)
            out["checked"] += 1
            if state in ("verified", "failed"):
                out[state] += 1
                if not dry:
                    set_state(row["id"], state, ev, detail)
                    _tell(row, state, detail, dry)
                continue
            since = (now.timestamp() - _ts(row.get("updated") or row.get("created"))) / 60
            if since >= ASK_AFTER_MIN.get(str(row.get("kind")), ASK_AFTER_MIN["default"]) or age_h > 24:
                if _quiet(now):
                    out["waiting"] += 1
                    continue
                out["asked"] += 1
                if not dry:
                    ask_owner(row)
            else:
                out["waiting"] += 1
            continue
        if _ts(row.get("due_verify")) > now.timestamp():
            out["waiting"] += 1
            continue
        state, ev, detail = verify(row)
        out["checked"] += 1
        if state in ("claimed", "verifying") and age_h * 60 > route_max_min(row) and row.get("kind") != "followup":
            # the route clock (WP2): waiting again is not a plan. The owner is asked instead of the row sitting there.
            state, detail = "unverified", f"waited {age_h * 60:.0f} min on route {row.get('route')}, past its {route_max_min(row)} min clock"
            out["route_clock"] = out.get("route_clock", 0) + 1
        if state in ("verified", "failed", "abandoned"):
            out[state] += 1
            if not dry:
                set_state(row["id"], state, ev, detail)
                _tell(row, state, detail, dry)
        elif state == "unverified":
            out["unverified"] += 1
            if not dry:
                set_state(row["id"], "unverified", ev, detail)
        else:
            out["waiting"] += 1
            if not dry and (ev or st == "claimed"):
                set_state(row["id"], "verifying", ev, detail)
    return out


# ------------------------------------------------------------------ reading
def open_rows() -> list:
    return sorted([r for r in fold().values() if r.get("state") not in TERMINAL and r.get("kind") != "said"], key=lambda r: r.get("created", ""))


def recent(hours: float = 24 * 7, states=("failed", "abandoned", "unverified")) -> list:
    cut = (datetime.now() - timedelta(hours=hours)).strftime(FMT)
    return sorted([r for r in fold().values() if r.get("state") in states and str(r.get("updated") or "") >= cut], key=lambda r: r.get("updated", ""))


def icon(state: str) -> str:
    return ICON.get(str(state or ""), "")


def render_open() -> str:
    rows = open_rows()
    if not rows:
        return "Nothing waiting to be confirmed."
    lines = [f"{len(rows)} thing{'s' if len(rows) != 1 else ''} not yet confirmed:"]
    for r in rows[:30]:
        age = (time.time() - _ts(r.get("created"))) / 3600
        lines.append(f"- {icon(r.get('state'))} {r.get('kind')}: {r.get('what', '')[:70]} ({age:.0f}h, {r.get('state')}; {str((r.get('checks') or [{}])[-1].get('detail') or '')[:60]})")
    return "\n".join(lines)


def render_failed(hours: float = 24 * 7) -> str:
    rows = recent(hours)
    if not rows:
        return "Nothing failed or went unconfirmed in that window."
    lines = [f"{len(rows)} not confirmed in the last {int(hours // 24)} days:"]
    for r in rows[-30:]:
        lines.append(f"- {icon(r.get('state'))} {r.get('kind')} via {r.get('route')}: {r.get('what', '')[:70]} -> {r.get('state')}: {str((r.get('checks') or [{}])[-1].get('detail') or '')[:80]}")
    return "\n".join(lines)


def prune(days: int = KEEP_DAYS) -> int:
    """Rewrite the store without terminal rows older than `days` (their ledger links stay)."""
    rows = fold()
    cut = (datetime.now() - timedelta(days=days)).strftime(FMT)
    keep = [r for r in rows.values() if r.get("state") not in TERMINAL or str(r.get("closed") or r.get("updated") or "") >= cut]
    if len(keep) == len(rows):
        return 0
    tmp = STORE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in sorted(keep, key=lambda r: r.get("created", "")):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, STORE)
    return len(rows) - len(keep)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("open")
    p = sub.add_parser("failed"); p.add_argument("--days", type=float, default=7)
    p = sub.add_parser("settle"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("show"); p.add_argument("id")
    p = sub.add_parser("prune"); p.add_argument("--days", type=int, default=KEEP_DAYS)
    a = ap.parse_args()
    if a.cmd == "open":
        print(render_open())
    elif a.cmd == "failed":
        print(render_failed(a.days * 24))
    elif a.cmd == "settle":
        r = settle(dry=a.dry_run)
        if not a.dry_run:
            prune()
        print("RESULT OUTCOMES " + json.dumps(r))
    elif a.cmd == "show":
        print(json.dumps(fold().get(a.id) or {}, indent=1, ensure_ascii=False))
    elif a.cmd == "prune":
        print(f"RESULT pruned={prune(a.days)}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
