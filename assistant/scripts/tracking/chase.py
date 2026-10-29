"""chase.py - Sage chases the owner about what waits on them, one ask per lane per day
(ruling 1, 2026-10-15; the shape is example's chaser.py).

What counts as stalled (all read-only): an open card nobody answered for stale_card_hours, a
staged ticket older than stale_ticket_days with no live card, a follow-up that fired
stale_followup_days ago and was never consumed. Ranked by advice.score inside each lane.

The state machine per item (data/tracking/chase.json):
  awaiting  -> an ask is out; re-asked after reask_hours of silence, at most max_follow_ups times
  answered  -> the owner tapped Handled, Bring it back up or Hand it to Sage (or it cleared on its own)
  snoozed   -> "Remind me tomorrow": next ask tomorrow, does not count toward max_follow_ups
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
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import advice  # noqa: E402
import common as C  # noqa: E402
import rhythm  # noqa: E402

OPT_HANDLED, OPT_TOMORROW, OPT_DROP = "Handled", "Remind me tomorrow", "Drop it"
OPT_REPOST, OPT_HAND = "Bring it back up (Recommended)", "Hand it to Sage (Recommended)"


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
        if opt.startswith("bring") or opt.startswith("hand it"):
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
        n += 1
        C.log("chase_answer", key=key, option=opt)
    return n


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
    return {"question": q, "header": f"Nudge · {x['lane']}", "lane": x["lane"], "chase_of": x["key"],
            "details": x.get("text", "") if x["kind"] == "card" else "",
            "advice": advice.line(x),
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
