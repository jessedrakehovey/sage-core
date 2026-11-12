"""chase.py - Sage chases the owner about what waits on them, one ask per lane per day
(ruling 1, 2026-10-15; the shape is example's chaser.py).

What counts as stalled (all read-only): an open card nobody answered for stale_card_hours, a
staged ticket older than stale_ticket_days with no live card, a follow-up that fired
stale_followup_days ago and was never consumed. Ranked by advice.score inside each lane.

The state machine per item (data/tracking/chase.json):
  awaiting  -> an ask is out; re-asked after reask_hours of silence, at most max_follow_ups times
  answered  -> the owner tapped Handled, Bring it back up or Hand it to Sage (or it cleared on its own)
  snoozed   -> "Remind me tomorrow": next ask tomorrow, does not count toward max_follow_ups; a typed
               "postpone / hold off ... until November (or 11/5)" snoozes it to that day instead
  folded    -> max_follow_ups unanswered asks: no more cards; the item is ONE quiet line a day in
               the owner's conversation with the others that folded (example's FOLD_AFTER, 2026-10-16:
               chases had reached 7-9 sends). A tap on its old card still counts; "Remind me
               tomorrow" resets the count. After park_days it may be asked again as a card.
  dropped   -> "Drop it": latched off; a ticket is dismissed, a follow-up dismissed

Posting: in the owner's measured active hours (rhythm.py), never in quiet hours, at most
chases_per_lane_per_day new or repeated asks per lane per calendar day. Replies to a chase are
read on every tick (never throttled). owner's lane goes to owner (ruling 16). A kind the
owner never answers (rhythm.ignored_kinds) is left to the weekly card.

Nothing here sends to anyone outside the household: a chase is ONE daily chat line (chase.nudge
"line", the default) or, with nudge "card", a card (cards.post). The card's first button is something
the system does (the button grammar, cards.NEEDS_ACTION, 2026-10-17 evening): "Bring it back up"
re-posts an open card to chat, "Hand it to Sage" queues a ticket so the operator carries the item out
(anything outbound still comes back as a card). Handled / Remind me / Drop it change only this ledger
and the item's own queue row. A card the guards refuse rides the daily line instead of vanishing.

  python assistant/scripts/tracking/chase.py            # tick (what the spine runs)
  python assistant/scripts/tracking/chase.py --dry-run  # show what it would ask
  python assistant/scripts/tracking/chase.py status
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import advice  # noqa: E402
import common as C  # noqa: E402
import rhythm  # noqa: E402
sys.path.insert(0, str(C.ROOT / "agent"))
import causes  # noqa: E402       Seed E: why a stalled chase closed, from the owner's tap only
import outreach  # noqa: E402     Seed A: every ask carries its channel and whether it was answered
import preconditions as PC  # noqa: E402   Seed D: the loop reads the record before a step fires

OPT_HANDLED, OPT_TOMORROW, OPT_DROP = "Handled", "Remind me tomorrow", "Drop it"
OPT_REPOST, OPT_HAND = "Bring it back up (Recommended)", "Hand it to Sage (Recommended)"
CHAIN_FILE = C.ROOT / "agent" / "chase_chain.json"         # Seed D: the loop as operators the record is read against
VOCAB_FILE = C.DATA / "cause_vocab.json"                   # Seed E: this organization's labels (the engine default otherwise)
FOLD_REPEAT_DAYS = 7                                       # the unchanged "still open" list is said again after this long
CAUSE_MIN_SENDS = 2                                        # a close is "stalled" (worth a why) after two unanswered asks
CAUSE_HEADER = "Why did it stall"
_CHAIN = None


def chain() -> dict:
    global _CHAIN
    if _CHAIN is None:
        try:
            _CHAIN = PC.load_chain(str(CHAIN_FILE))
        except Exception:  # noqa: BLE001
            _CHAIN = {"chain": "owner-chase", "initial": "waiting", "operators": []}
    return _CHAIN


def vocab() -> dict:
    return causes.load_vocab(str(VOCAB_FILE) if VOCAB_FILE.exists() else None)


def record_of(it: dict, now=None) -> dict:
    """The chase item as the record Seed D reads: as_of = now (the loop just read the live cards and tickets), the
    legacy counters as outreach history, a terminal status when the item is closed."""
    r = dict(it)
    r["as_of"] = (now or C.now_local()).strftime("%Y-%m-%dT%H:%M")
    if not r.get("last_sent") and r.get("last_sent_epoch"):          # an older row: only the epoch; the stamps are derived from it
        try:
            r["last_sent"] = datetime.fromtimestamp(float(r["last_sent_epoch"])).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            pass
    if not r.get("first_sent") and r.get("last_sent"):
        r["first_sent"] = r["last_sent"]
    if it.get("status") in ("answered", "dropped", "parked"):
        r["status"] = it["status"]
    else:
        r["status"] = "awaiting"
    return r


def reading(it: dict, now=None) -> dict:
    """Seed D: {verdict, line, operator} for the item now. UNKNOWN never advances (the caller skips and logs it);
    MOOT means the record says it is over; otherwise the line rides the nudge as its advice."""
    try:
        ex = PC.explain(chain(), record_of(it, now), now, terminal_statuses=("answered", "dropped", "parked"))
        op = ex.get("operator") or {}
        return {"verdict": str(ex.get("verdict") or "unknown"), "line": str(ex.get("line") or ""), "operator": str(op.get("id") or "")}
    except Exception as e:  # noqa: BLE001
        return {"verdict": "unknown", "line": f"the record could not be read ({type(e).__name__})", "operator": ""}


def _load() -> dict:
    st = C.state("chase", {})
    st.setdefault("items", {})
    st.setdefault("lane_day", {})         # lane -> {"day": "YYYY-MM-DD", "n": asks posted that day}
    return st


def _save(st: dict) -> None:
    C.save_state("chase", st)


# ------------------------------------------------------------------ what is stalled
def candidates(now=None, c: dict | None = None, cards=None, tickets=None, fus=None) -> list:
    now = now or C.now_local()
    ch = (c or C.cfg())["chase"]
    out = []
    live = cards if cards is not None else C.cards_live()
    live_sources = {str((q.get("source_id") or "")) for cd in live for q in (cd.get("questions") or [])}
    excl_cards = [e for e in (ch.get("exclude_cards") or []) if isinstance(e, dict) and e]
    for cd in live:
        q = (cd.get("questions") or [{}])[0]
        age_h = (time.time() - float(cd.get("ts") or time.time())) / 3600.0
        if age_h < float(ch["stale_card_hours"]) or str(q.get("header") or "").startswith("Nudge"):
            continue
        if any(all(str(q.get(k) or "") == str(v) for k, v in e.items()) for e in excl_cards):
            continue
        out.append({"key": f"card:{cd['id']}", "kind": "card", "lane": str(q.get("lane") or "personal"),
                    "title": str(q.get("header") or "a decision"), "text": str(q.get("question") or "")[:300],
                    "age_days": age_h / 24.0, "waiting_on": "a client" if str(q.get("audience") or "") == "client" else "",
                    "why": "the card is still open above; it only needs your tap"})
    tix = tickets if tickets is not None else C.tickets_all()
    excl = [e for e in (ch.get("exclude_tickets") or []) if isinstance(e, dict) and e]
    for t in tix.values():
        if t.get("status") != "staged":
            continue
        if any(all(str(t.get(k) or "") == str(v) for k, v in e.items()) for e in excl):
            continue
        age = C.age_days(t.get("updated") or t.get("created"), now)
        if age < float(ch["stale_ticket_days"]) or t["id"] in live_sources:
            continue
        out.append({"key": f"ticket:{t['id']}", "kind": "ticket", "lane": str(t.get("lane") or "personal"),
                    "title": str(t.get("title") or t.get("kind") or "a staged item")[:90], "text": str(t.get("summary") or "")[:300],
                    "age_days": age, "why": "staged for you and not moved since"})
    st = fus if fus is not None else C.followups_state()
    for it in (st.get("items") or {}).values():
        if it.get("status") != "fired":
            continue
        age = C.age_days(it.get("fired_at"), now)
        if age < float(ch["stale_followup_days"]):
            continue
        out.append({"key": f"fu:{it['id']}", "kind": "followup", "lane": str(it.get("lane") or "personal"),
                    "title": str(it.get("subject") or f"{it.get('sop')}/{it.get('step')}")[:90],
                    "text": str((it.get("action") or {}).get("instruction") or "")[:300], "age_days": age,
                    "why": "its moment came and nothing followed"})
    for it in out:
        it["score"] = advice.score(it, c)
    return sorted(out, key=lambda x: -x["score"])


# ------------------------------------------------------------------ the owner's answers
# 2026-10-31 (ported from example's chaser, br-6a443d97): a typed "postpone ... until November" was read as a
# plain reply: the item closed for good, so postponing it was dropping it. A typed postpone / hold-off answer that names a month or
# m/d snoozes the item until that day (08:00) instead of closing it or bringing it back tomorrow.
_POSTPONE_RX = re.compile(r"\b(?:postpone|hold off|pause|stop (?:asking|checking|chasing)|"
                          r"don'?t (?:ask|check|chase))\b.*?\buntil\s+([a-z]+|\d{1,2}/\d{1,2})", re.I | re.S)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def postpone_resume(text: str, reply_ts: str = "") -> str:
    """The day chasing resumes (YYYY-MM-DD) named by a typed postpone answer, or ''.
    "until November" resumes 11/1; "until 11/5" resumes 11/5; a day already past means next year."""
    m = _POSTPONE_RX.search(str(text or ""))
    if not m:
        return ""
    sent = C.parse_any(reply_ts) or C.now_local()
    sent = sent.date()
    w = m.group(1).lower()
    if "/" in w:
        mo, d = (int(x) for x in w.split("/"))
    elif w[:3] in _MONTHS:
        mo, d = _MONTHS.index(w[:3]) + 1, 1
    else:
        return ""
    try:
        r = date(sent.year, mo, d)
        if r <= sent:
            r = date(sent.year + 1, mo, d)
    except ValueError:
        return ""
    return r.isoformat()


def harvest(st: dict, answers: list | None = None) -> int:
    """Read the owner's taps on our own cards; replies are never throttled."""
    answers = answers if answers is not None else C.card_answers(30)
    by_qid = {}
    for a in answers:
        by_qid.setdefault(str(a.get("qid")), a)
    n = 0
    for key, it in st["items"].items():
        qid = str(it.get("qid") or "")
        if not qid or it.get("status") not in ("awaiting", "folded") or qid not in by_qid:
            continue
        a = by_qid[qid]
        opt = str(a.get("option") or "").strip().lower()
        resume = postpone_resume(str(a.get("text") or "") or opt, str(a.get("ts") or ""))
        if resume:
            it["status"] = "snoozed"
            it["sends"] = 0
            it["snooze_until"] = f"{resume} 08:00"
        elif opt.startswith("bring") or opt.startswith("hand it"):
            it["status"] = "answered"
            _act(key, it)
        elif opt.startswith("handled"):
            it["status"] = "answered"
        elif opt.startswith("remind"):
            it["status"] = "snoozed"
            it["sends"] = 0                                   # a reply resets the count (a folded item starts over)
            it["snooze_until"] = (C.now_local() + timedelta(days=1)).replace(hour=8, minute=0).strftime("%Y-%m-%d %H:%M")
        elif opt.startswith("drop"):
            it["status"] = "dropped"
            _drop_item(key, it)
        else:
            it["status"] = "answered"
        it["answered"] = str(a.get("ts") or "")
        if a.get("text"):
            it["answer_text"] = str(a.get("text"))[:300]
        outreach.record_reply(it, ts=str(a.get("ts") or "")[:16].replace(" ", "T") or None)     # Seed A: the ask was answered
        if it["status"] in ("answered", "dropped") and int(it.get("sends") or 0) >= CAUSE_MIN_SENDS and not it.get("cause") and not it.get("cause_qid"):
            _ask_cause(key, it)                               # Seed E: a stalled chase closed; the owner may say why
        n += 1
        C.log("chase_answer", key=key, option=opt)
    n += harvest_causes(st, by_qid)
    return n


# ------------------------------------------------------------------ Seed E: why it stalled (the owner's tap, never inferred)
def _ask_cause(key: str, it: dict) -> None:
    """ONE non-urgent card (the weekly digest) with the vocabulary's labels and Skip; a typed note is a free text."""
    v = vocab()
    opts = [{"label": v["words"].get(lb, lb)} for lb in v["labels"]] + [{"label": v["words"].get(v["skip"], "Skip"), "description": "no label"}]
    q = {"question": f"{str(it.get('title') or key)[:120]}: it took {int(it.get('sends') or 0)} asks. What stalled it?",
         "header": f"{CAUSE_HEADER} · {it.get('lane') or 'personal'}", "lane": str(it.get("lane") or "personal"),
         "cause_of": key, "options": opts,
         "details": "A label here teaches me what tends to stall things; it is never about the people we serve. A typed note works too."}
    try:
        import cards
        r = cards.post([q], str(it.get("owner") or C.owner_for(str(it.get("lane") or "personal"))), lane=str(it.get("lane") or "personal"))
        if r.get("posted"):
            it["cause_qid"] = r.get("qid") or ""
            C.log("chase_cause_asked", key=key, qid=it["cause_qid"])
    except Exception as e:  # noqa: BLE001
        C.log("chase_cause_error", key=key, error=f"{type(e).__name__}: {str(e)[:120]}")


def _label_for(option: str, v: dict) -> str:
    o = str(option or "").strip().lower().replace(" (recommended)", "")
    for lb, words in (v.get("words") or {}).items():
        if o == str(words).strip().lower():
            return lb
    return o if o in v.get("labels", []) or o == v.get("skip") else ""


def harvest_causes(st: dict, by_qid: dict) -> int:
    """The owner's taps (or typed notes) on the cause cards -> causes.label_close on the chase item."""
    n = 0
    v = vocab()
    for key, it in st["items"].items():
        qid = str(it.get("cause_qid") or "")
        if not qid or qid not in by_qid or it.get("cause") or it.get("cause_text"):
            continue
        a = by_qid[qid]
        label = _label_for(a.get("option"), v)
        typed = causes.parse_typed(str(a.get("text") or "")) or str(a.get("text") or "").strip()
        try:
            if label:
                causes.label_close(it, label, "tap", vocab=v)
            elif typed:
                causes.label_close(it, typed, "typed", vocab=v)
            else:
                continue
            it["cause_answered"] = str(a.get("ts") or "")
            n += 1
            C.log("chase_cause", key=key, cause=it.get("cause") or "", text=bool(it.get("cause_text")))
        except causes.CauseError as e:
            it["cause_refused"] = str(e)[:160]
            C.log("chase_cause_refused", key=key, why=str(e)[:120])
    return n


def cause_coverage() -> dict:
    """Seed E's read-back over the ledger: how many stalled closes carry a cause (weekly readback)."""
    st = _load()
    items = [it for it in st["items"].values() if int(it.get("sends") or 0) >= CAUSE_MIN_SENDS]
    return causes.coverage(items, vocab(), closed_statuses=("answered", "dropped"))


def _drop_item(key: str, it: dict) -> None:
    try:
        if key.startswith("ticket:"):
            import tickets
            tickets._transition(key[7:], "dismissed", "tracking.chase", "owner1: Drop it (chase)", allowed_from={"staged", "new", "claimed"})
        elif key.startswith("fu:"):
            import followups
            with followups.StateLock():
                s = followups.load_state()
                f = s["items"].get(key[3:])
                if f:
                    f["status"] = "dismissed"
                    followups.save_state(s)
    except Exception as e:  # noqa: BLE001
        C.log("chase_drop_error", key=key, error=f"{type(e).__name__}: {str(e)[:120]}")


def _act(key: str, it: dict) -> None:
    """The chase card's action button. A card: re-post it to chat so it is on top again. A ticket or a
    follow-up: one operator ticket to carry it out; the operator drafts, and anything that reaches a
    person comes back as its own card (the send gate is unchanged)."""
    try:
        if key.startswith("card:"):
            import chat_push
            cd = C.card(key[5:])
            if cd and cd.get("status") == "open":
                chat_push.push_card(cd)
                C.log("chase_reposted", key=key)
            return
        import tickets
        title = str(it.get("title") or key)[:150]
        n = tickets.add_many([{"source": "tracking.chase", "source_id": f"{key}|{C.now_local().strftime('%Y-%m-%d')}",
                               "lane": str(it.get("lane") or "personal"), "kind": "item", "title": f"Carry out: {title}",
                               "summary": (f"The owner tapped Hand it to Sage on the chase for '{title}' ({key}). Do the next step "
                                           "yourself; draft anything outbound and card it."),
                               "payload": {"chase_of": key}}], "tracking.chase")
        C.log("chase_handed", key=key, added=n)
    except Exception as e:  # noqa: BLE001
        C.log("chase_act_error", key=key, error=f"{type(e).__name__}: {str(e)[:120]}")


def _clear_gone(st: dict, cands: list) -> int:
    """An item that left the stalled set on its own (card answered, ticket done) is answered."""
    keys = {x["key"] for x in cands}
    n = 0
    for key, it in st["items"].items():
        if it.get("status") in ("awaiting", "snoozed") and key not in keys:
            it["status"] = "answered"
            it["answered"] = C.now_local().strftime("%Y-%m-%d %H:%M") + " (cleared)"
            n += 1
    return n


# ------------------------------------------------------------------ what to ask now
def pick(st: dict, cands: list, lane: str, now, ch: dict) -> dict | None:
    items = st["items"]
    reask_s = float(ch["reask_hours"]) * 3600
    overdue, fresh = [], []
    for x in cands:
        if x["lane"] != lane:
            continue
        it = items.get(x["key"])
        if not it:
            fresh.append(x); continue
        s = it.get("status")
        if s in ("dropped", "answered"):
            if s == "answered" and it.get("reopen_ok"):
                fresh.append(x)
            continue
        if s in ("parked", "folded"):
            until = C.parse_any(it.get("parked_until"))
            if until and now >= until:
                it["status"] = "awaiting"; it["sends"] = 0
                fresh.append(x)
            continue
        if s == "snoozed":
            until = C.parse_any(it.get("snooze_until"))
            if until and now >= until:
                overdue.append(x)
            continue
        if time.time() - float(it.get("last_sent_epoch") or 0) >= reask_s:
            rd = reading(it, now)                 # Seed D: the record before the step
            if rd["verdict"] == "unknown":
                C.log("chase_unknown", key=x["key"], why=rd["line"][:120])
                continue                          # never advance on a record that cannot say
            if rd["verdict"] == "moot":
                it["status"] = "answered"
                it["answered"] = now.strftime("%Y-%m-%d %H:%M") + " (record says it is over)"
                C.log("chase_moot", key=x["key"], why=rd["line"][:120])
                continue
            x["reading"] = rd["line"]
            if int(it.get("sends") or 0) >= int(ch["max_follow_ups"]):
                # fold, don't park: no more cards, but the item stays in view as one quiet daily
                # line (folded_line) until park_days pass or the owner answers its old card
                it["status"] = "folded" if ch.get("fold_daily_line", True) else "parked"
                it["parked_until"] = (now + timedelta(days=int(ch["park_days"]))).strftime("%Y-%m-%d %H:%M")
                it["folded_at"] = now.strftime("%Y-%m-%d %H:%M")
                C.log("chase_folded" if it["status"] == "folded" else "chase_parked", key=x["key"])
                continue
            overdue.append(x)
    if overdue:
        return overdue[0]
    return fresh[0] if fresh else None


def _question(x: dict, it: dict) -> dict:
    nth = int(it.get("sends") or 0) + 1
    lead = "Still waiting: " if nth > 1 else ""
    days = int(round(float(x.get("age_days") or 0)))
    q = f"{lead}{x['title']} has waited {days} day{'s' if days != 1 else ''}."
    if x.get("text") and x["kind"] != "card":
        q += f" {x['text'][:200]}"
    adv = advice.line(x)
    if x.get("reading"):                          # Seed D: the chain's plain-words reading of the record
        adv = (adv + " " if adv else "") + x["reading"]
    review = outreach.review_line(it, who="you", asker="", min_tries=outreach.MIN_TRIES) if it else ""
    if review:                                    # Seed A: asks on one channel went unanswered; said, never acted on
        adv = (adv + " " if adv else "") + review
    return {"question": q, "header": f"Nudge · {x['lane']}", "lane": x["lane"], "chase_of": x["key"],
            "details": x.get("text", "") if x["kind"] == "card" else "",
            "advice": adv,
            "options": [{"label": OPT_REPOST, "description": "I post the card again so it's on top"} if x["kind"] == "card" else
                        {"label": OPT_HAND, "description": "I take it from here; anything that goes out comes back as a card"},
                        {"label": OPT_HANDLED, "description": "it's done or you've dealt with it"},
                        {"label": OPT_TOMORROW}, {"label": OPT_DROP, "description": "stop asking about this one"}]}


def _nudge_line(rows: list) -> str:
    """The daily line: what waits on this owner, oldest first, where to answer it."""
    rows = sorted(rows, key=lambda r: -float(r[1].get("age_days") or 0))[:6]
    parts = []
    for lane, x, it in rows:
        days = int(round(float(x.get("age_days") or 0)))
        parts.append(f"{x['title']} ({lane}, {days} day{'s' if days != 1 else ''})")
    lead = "Still waiting on you: " if any(int(it.get("sends") or 0) for _, _, it in rows) else "Waiting on you: "
    return lead + "; ".join(parts) + ". The card for each is above; tap it, or tell me here to drop one."


def _post(q: dict, lane: str, dry: bool) -> str:
    if dry:
        print("WOULD POST " + json.dumps(q, ensure_ascii=False)[:400])
        return "dry"
    import cards
    r = cards.post([q], owner="tracking.chase", lane=lane)
    if not r.get("posted"):
        C.log("chase_post_refused", lane=lane, reason=str(r.get("reason"))[:200])
        return ""
    return str(r.get("qid") or "")


def folded_line(st: dict, now, ch: dict) -> dict:
    """ONE quiet line a day per owner listing what folded (asked max_follow_ups times, never
    answered): no buttons, no new card. Returns {owner_key: text}. Nothing when nothing is folded
    or today's line already went."""
    day = now.strftime("%Y-%m-%d")
    by_owner = {}
    for key, it in st["items"].items():
        if it.get("status") != "folded":
            continue
        by_owner.setdefault(str(it.get("owner") or "owner1"), []).append(it)
    out = {}
    for owner, items in by_owner.items():
        if (st.setdefault("fold_day", {}).get(owner)) == day:
            continue
        items = sorted(items, key=lambda i: i.get("folded_at") or "")[:8]
        names = "; ".join(str(i.get("title") or i.get("kind") or "")[:60] for i in items)
        # the same list is said ONCE, then again only when it changes or a week has gone by (2026-11-07..10: the
        # identical eight items every morning for four days)
        told = (st.setdefault("fold_told", {}).get(owner)) or {}
        try:
            fresh = (now - datetime.strptime(str(told.get("day")), "%Y-%m-%d")).days < FOLD_REPEAT_DAYS
        except (TypeError, ValueError):
            fresh = False
        if told.get("names") == names and fresh:
            continue
        st["fold_told"][owner] = {"names": names, "day": day}
        n = int(ch["max_follow_ups"])
        out[owner] = (f"Still open, asked {n} times each so I've stopped carding them: {names}. "
                      "Tap the old card when one is handled, or tell me here to drop it.")
    return out


def _post_line(owner: str, text: str, dry: bool) -> bool:
    if dry:
        print(f"WOULD POST to {owner}: {text}"); return True
    try:
        # the owner 2026-10-21: no restating of open items in the family chat; the digest already carries the
        # cards. The folded list goes to Mission Control as a note (and the agent's transcript, so Sage can
        # answer "what's stuck?" from it).
        sys.path.insert(0, str(C.ROOT / "scripts"))
        import cards  # noqa: E402
        cards.note("chase", f"{owner}: {text}")
        sys.path.insert(0, str(C.ROOT / "agent"))
        import core as _core  # noqa: E402
        _core.say(f"dm:{owner}", _core.profile().get("name", "Agent"), text, channel="chase-note")
        return True
    except Exception as e:  # noqa: BLE001
        C.log("chase_fold_post_error", owner=owner, error=f"{type(e).__name__}: {str(e)[:160]}")
        return False


def tick(now=None, dry: bool = False) -> dict:
    c = C.cfg()
    ch = c["chase"]
    now = now or C.now_local()
    st = _load()
    harvested = harvest(st)
    cands = candidates(now, c)
    cleared = _clear_gone(st, cands)
    posted = []
    if ch.get("enabled", True) and (dry or rhythm.good_time(now)):
        ignored = set((rhythm.build() or {}).get("ignored_kinds") or [])
        cands = [x for x in cands if x["kind"] not in ignored]
        day = now.strftime("%Y-%m-%d")
        as_line = str(ch.get("nudge") or "line") == "line"
        picks = []
        for lane in C.lanes():
            ld = st["lane_day"].get(lane) or {}
            n_today = int(ld.get("n") or 0) if ld.get("day") == day else 0
            if n_today >= int(ch["chases_per_lane_per_day"]):
                continue
            x = pick(st, cands, lane, now, ch)
            if not x:
                continue
            it = st["items"].setdefault(x["key"], {"kind": x["kind"], "lane": lane, "title": x["title"], "sends": 0, "status": "awaiting"})
            if as_line:
                picks.append((lane, x, it))
                continue
            qid = _post(_question(x, it), lane, dry)
            if not qid:
                picks.append((lane, x, it))       # refused (a guard): it rides the daily line, never lost
                continue
            it.update({"status": "awaiting", "sends": int(it.get("sends") or 0) + 1, "qid": qid, "last_sent_epoch": time.time(),
                       "last_sent": now.strftime("%Y-%m-%d %H:%M"), "first_sent": it.get("first_sent") or now.strftime("%Y-%m-%d %H:%M"),
                       "owner": C.owner_for(lane, c)})
            outreach.record_send(it, "card", ts=now.strftime("%Y-%m-%dT%H:%M"))       # Seed A
            it.pop("snooze_until", None)
            st["lane_day"][lane] = {"day": day, "n": n_today + 1}
            posted.append({"lane": lane, "key": x["key"], "qid": qid})
            C.log("chase_posted", lane=lane, key=x["key"], qid=qid, n=it["sends"])
        if picks:
            # ONE plain line a day, all lanes, no buttons (the owner 2026-10-17 evening: little decisions
            # must not come as cards; three "Nudge" cards landed at 07:03). The item's own card is
            # the thing to tap; "drop it" said here reaches Sage. owner's lane goes to owner.
            by_owner = {}
            for lane, x, it in picks:
                by_owner.setdefault(C.owner_for(lane, c), []).append((lane, x, it))
            for owner, rows in by_owner.items():
                if not _post_line(owner, _nudge_line(rows), dry):
                    continue
                for lane, x, it in rows:
                    it.update({"status": "awaiting", "sends": int(it.get("sends") or 0) + 1, "qid": "", "last_sent_epoch": time.time(),
                               "last_sent": now.strftime("%Y-%m-%d %H:%M"), "first_sent": it.get("first_sent") or now.strftime("%Y-%m-%d %H:%M"),
                               "owner": owner})
                    outreach.record_send(it, "chat", ts=now.strftime("%Y-%m-%dT%H:%M"))   # Seed A
                    it.pop("snooze_until", None)
                    ld = st["lane_day"].get(lane) or {}
                    st["lane_day"][lane] = {"day": day, "n": (int(ld.get("n") or 0) if ld.get("day") == day else 0) + 1}
                    posted.append({"lane": lane, "key": x["key"], "line": True})
                    C.log("chase_line", lane=lane, key=x["key"], n=it["sends"])
        for owner, text in folded_line(st, now, ch).items():
            if _post_line(owner, text, dry):
                st.setdefault("fold_day", {})[owner] = day
                posted.append({"lane": "folded", "owner": owner})
                C.log("chase_fold_line", owner=owner)
    if not dry:
        _save(st)
    out = {"harvested": harvested, "cleared": cleared, "candidates": len(cands), "posted": posted}
    print("CHASE " + json.dumps(out))
    return out


def status() -> None:
    st = _load()
    for key, it in sorted(st["items"].items(), key=lambda kv: kv[1].get("last_sent") or ""):
        print(f"{it.get('status', '?'):9} x{it.get('sends', 0)} {it.get('lane', ''):9} {key:40} {str(it.get('title', ''))[:50]}  last={it.get('last_sent', '-')}")
    print(rhythm.advice())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="tick", choices=["tick", "status", "candidates"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "status":
        status()
    elif a.cmd == "candidates":
        for x in candidates():
            print(f"{x['score']:6.2f} {x['lane']:9} {x['kind']:9} {x['title'][:60]}  ({x['age_days']:.1f} d)")
    else:
        tick(dry=a.dry_run)
