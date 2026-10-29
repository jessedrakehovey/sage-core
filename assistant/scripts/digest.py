"""digest.py - the weekly approvals digest: every held card, ONE drop-down message, once a week.

the owner 2026-10-20: "New design for approval cards since I'm getting overwhelmed with them. Help reduce
unnecessary decisions for me. I only want a drop down long list of cards once a week and making a
decision on those should be the final close to each unless they are not confirmed done. I need to have
an approval to text new prospects ASAP when a new one comes in with a phone number. Normal approvals
will be singular cards from now on besides one drop down card with them all together once a week."

With `config.json -> cards.delivery = "weekly"` cards.post HOLDS every non-urgent card: it is open on
Mission Control and in Sage's transcript the moment it is posted, but nothing reaches Chat. On digest
day (cards.digest_day + digest_hour, local) this module posts ONE collapsible Chat message per owner
space with every held card inside - each under its own head line with its question, the text that
would go out, advice, details and its own buttons (chat_push.stack_body, example's one-fold shape). A tap
on a member answers that card through the same guarded path as any card (gchat_events -> cards.answer)
and re-renders the message with the decided card gone from the fold (chat_push.body_for). More than
`max_per_message` cards = numbered parts, posted together.

A decision there is final: nothing re-asks it (cards.py ALREADY_ANSWERED + decision_keys), nothing
follows up with another card, and an approved action that was NOT confirmed done comes back as the
same card by itself (cards.reopen, called by drain on a provable failure). A member nobody decided
rides along `carry_over_weeks` more digests (marked "2nd week, drops after this"), then expires and is
dropped for good - no card_expired ticket, no re-post.

Zero-LLM. Runs from the Day Spine every 10 min (`cards-digest`); `due` is computed from the slot, so a
spine that was down at 08:00 posts the digest at its next tick, never twice for one slot.

  python assistant/scripts/digest.py status              what waits, when the next digest is
  python assistant/scripts/digest.py preview             the message(s) as JSON, nothing posted
  python assistant/scripts/digest.py tick [--dry-run]    post if the slot is due (the spine job)
  python assistant/scripts/digest.py post [--dry-run]    post now, whatever the clock says (the owner's call)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cards  # noqa: E402
import exec_store as es  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

STATE = cards.ROOT / "digest_state.json"
LANE_ORDER = ("wedding", "personal", "home", "money", "owner6")
LEAD = ("Decide each one once: a tap is final. Anything you leave comes back next week one more time, then drops. "
        "Something that did not go through comes back by itself.")


# ------------------------------------------------------------------ state + timing
def _state() -> dict:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STATE)
    except OSError:
        pass


def slot(now: datetime | None = None, pol: dict | None = None) -> datetime:
    """The most recent digest slot at or before `now` (local time)."""
    now = now or datetime.now()
    pol = pol or cards.delivery_policy()
    d = now.replace(hour=pol["hour"], minute=0, second=0, microsecond=0)
    d -= timedelta(days=(now.weekday() - pol["day"]) % 7)
    if d > now:
        d -= timedelta(days=7)
    return d


def next_slot(now: datetime | None = None, pol: dict | None = None) -> datetime:
    return slot(now, pol) + timedelta(days=7)


def due(now: datetime | None = None, pol: dict | None = None) -> bool:
    """True once per slot: the slot has passed and nothing was posted for it yet."""
    pol = pol or cards.delivery_policy()
    if pol["delivery"] != "weekly":
        return False
    s = slot(now, pol).isoformat(timespec="minutes")
    return str(_state().get("last_slot") or "") != s


# ------------------------------------------------------------------ what waits
def held(now: float | None = None) -> list:
    """Every open held card, oldest first, after the expiry rules have run."""
    cards.expire()
    out = [c for c in cards._live() if c.get("status") == "open" and c.get("delivery") == "weekly"]
    out.sort(key=lambda c: (LANE_ORDER.index(_lane(c)) if _lane(c) in LANE_ORDER else 99, float(c.get("held_ts") or c.get("ts") or 0)))
    return out


def _lane(c: dict) -> str:
    return str((c.get("questions") or [{}])[0].get("lane") or "personal")


def _by_space(members: list) -> dict:
    """{space: {"key": person_key, "cards": [card, ...]}} - which owner space each held card goes to
    (chat_push.targets, the same routing as a card posted on its own)."""
    import chat_push
    out = {}
    for c in members:
        for key, space in chat_push.targets(_lane(c), "card"):
            d = out.setdefault(space, {"key": key, "cards": []})
            if c["id"] not in {x["id"] for x in d["cards"]}:
                d["cards"].append(c)
    return out


def _chunks(items: list, n: int) -> list:
    n = max(2, int(n or 10))
    if len(items) <= n:
        return [items]
    parts = (len(items) + n - 1) // n
    size = (len(items) + parts - 1) // parts            # even parts: 12 cards at 10 = 6 + 6, not 10 + 2
    return [items[i:i + size] for i in range(0, len(items), size)]


def messages(now: datetime | None = None, pol: dict | None = None) -> list:
    """[{space, key, qids, body}] - the digest message(s), built, not posted."""
    import chat_push
    pol = pol or cards.delivery_policy()
    out = []
    for space, d in _by_space(held()).items():
        groups = _chunks(d["cards"], pol["max_per_message"])
        for i, g in enumerate(groups, 1):
            title = chat_push.DIGEST_TITLE
            if len(groups) > 1:
                title = f"This week's approvals · part {i} of {len(groups)} · {{n}} here"
            # rendered with the count it will have once posted, so a carried-over member reads "2nd week"
            members = [(dict(c, digest_count=int(c.get("digest_count") or 0) + 1), "open", None) for c in g]
            subtitle = f"next digest {next_slot(now, pol):%a %b %d, %H:%M}"
            lead = LEAD if i == 1 else ""
            body = chat_push.stack_body(members, title=title, lead=lead, subtitle=subtitle)
            out.append({"space": space, "key": d["key"], "qids": [c["id"] for c in g], "body": body,
                        "title": title, "subtitle": subtitle, "lead": lead})
    return out


# ------------------------------------------------------------------ posting
def _take_down_previous(st: dict, g) -> int:
    """Last week's digest messages: every open held card is in the new digest, so they hold nothing
    that still waits. Delete them (a stack message no live card points at is forgotten by
    chat_push._stack_remember on the next post). -> deleted."""
    import chat_push
    n = 0
    for m in st.get("messages") or []:
        name = str(m.get("name") or "")
        if not name:
            continue
        try:
            if g.delete(name):
                n += 1
        except Exception as e:  # noqa: BLE001
            chat_push._log(f"digest: could not take down {name}: {type(e).__name__}")
        chat_push._stack_set(name, [])
    return n


def post(now: datetime | None = None, dry: bool = False, force: bool = False) -> dict:
    """Post the digest for the current slot. -> {"posted", "cards", "messages", "slot", ...}"""
    import chat_push
    now = now or datetime.now()
    pol = cards.delivery_policy()
    s = slot(now, pol).isoformat(timespec="minutes")
    st = _state()
    if not force and not due(now, pol):
        return {"posted": False, "reason": "not due", "slot": s, "next": next_slot(now, pol).isoformat(timespec="minutes")}
    msgs = messages(now, pol)
    n_cards = len({q for m in msgs for q in m["qids"]})
    if dry:
        return {"posted": False, "dry": True, "slot": s, "cards": n_cards, "messages": len(msgs)}
    posted, names = 0, []
    if not chat_push.enabled():
        # no Chat: the cards stay open on Mission Control; the slot is still consumed so this does
        # not retry every 10 minutes, and the note says where they are
        cards.note("digest", f"weekly digest: {n_cards} card(s) waiting on Mission Control (Chat is off, nothing posted)")
    elif not msgs:
        if pol["say_empty"]:
            cards.say("digest", "", "Weekly approvals: nothing waiting for you this week.")
    else:
        g = chat_push._g()
        _take_down_previous(st, g)
        stamp = time.time()
        for m in msgs:
            try:
                name = g.post_body(m["space"], m["body"], "")
            except Exception as e:  # noqa: BLE001
                chat_push._log(f"digest post to {m['space']} failed: {type(e).__name__}: {str(e)[:160]}")
                name = ""
            if not name:
                continue
            posted += 1
            names.append({"name": name, "space": m["space"], "qids": m["qids"]})
            for qid in m["qids"]:
                c = cards._read(cards.LIVE / f"{qid}.json")
                if not isinstance(c, dict):
                    continue
                c["chat"] = {m["key"]: name}
                c["digest_ts"] = stamp
                c["digest_count"] = int(c.get("digest_count") or 0) + 1
                cards._write(cards.LIVE / f"{qid}.json", c)
            chat_push._stack_remember(m["space"], name, list(m["qids"]), title=m["title"], subtitle=m["subtitle"], lead=m["lead"])
        cards.note("digest", f"weekly digest: {n_cards} card(s) in {posted} message(s)")
    # the week's scorecard, one plain paragraph under the digest (WP6): the Monday drop-down is also the review
    try:
        import scorecard
        sc = scorecard.compute(7)
        scorecard.record(sc)
        if chat_push.enabled():
            cards.say("digest", "", scorecard.render(sc))
    except Exception as e:  # noqa: BLE001
        chat_push._log(f"scorecard failed: {type(e).__name__}: {str(e)[:120]}")
    st.update(last_slot=s, last_posted=now.isoformat(timespec="seconds"), messages=names, cards=n_cards)
    _save(st)
    es.ledger("card_digest", slot=s, cards=n_cards, messages=posted)
    return {"posted": True, "slot": s, "cards": n_cards, "messages": posted, "next": next_slot(now, pol).isoformat(timespec="minutes")}


def tick(dry: bool = False) -> dict:
    return post(dry=dry)


def status() -> dict:
    pol = cards.delivery_policy()
    hs = held()
    st = _state()
    return {"delivery": pol["delivery"], "slot": pol["when"], "held": len(hs),
            "by_lane": {ln: sum(1 for c in hs if _lane(c) == ln) for ln in sorted({_lane(c) for c in hs})},
            "last_posted": st.get("last_posted", ""), "last_cards": st.get("cards", 0),
            "next": next_slot(None, pol).isoformat(timespec="minutes"), "due": due(None, pol)}


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("preview")
    p = sub.add_parser("tick"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("post"); p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=1)); return 0
    if a.cmd == "preview":
        print(json.dumps(messages(), ensure_ascii=False, indent=1)); return 0
    if a.cmd == "tick":
        r = tick(dry=a.dry_run)
        print("DIGEST " + " ".join(f"{k}={v}" for k, v in r.items())); return 0
    if a.cmd == "post":
        r = post(dry=a.dry_run, force=True)
        print("DIGEST " + " ".join(f"{k}={v}" for k, v in r.items())); return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
