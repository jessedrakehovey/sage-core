"""comms_state.py - the comms substrate: every message thread, its state and its SLA. Zero-LLM.

Ported in shape from the property OS's scripts/comms/coordinator.py (2026-10-06) after the owner
asked to "borrow how comms works": the thing he likes is that it never misses an incoming
message and replies to texts fast. This file is the deterministic half: it folds the phone
threads (phone_bridge), the mail / inquiry / text tickets, the open cards and the phone
outbox into ONE record per thread and derives the state by the evidence rule:

  staff/closed     automated / marketing / code texts, or the owner closed it
  awaiting_party   our last outbound is newer than their last inbound AND verified (phone ack,
                   or an exec ticket done), or an owner told Sage after it that they replied
                   themselves (an owner-said memory fact, 2026-10-25)
  awaiting_the owner   a card for this thread is open (a draft waits for his tap)
  needs_reply      otherwise, with the SLA from assistant/data/comms_law.json by class

Writes assistant/data/comms_state.json (atomic) and, for a thread whose SLA just breached,
ONE `sla_breach` ticket (latched per thread per inbound) plus a Chat note. Never sends.

  python assistant/scripts/comms_state.py            # rebuild + breach tickets, prints COMMS_STATE ...
  python assistant/scripts/comms_state.py --dry-run  # rebuild only, print the lanes
  python assistant/scripts/comms_state.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from alib import DATA, VAULT, load_json, now_iso, save_json  # noqa: E402

LAW = Path(__file__).resolve().parents[1] / "data" / "comms_law.json"
if not LAW.is_file():                                      # the bare engine: the scrubbed template
    LAW = LAW.parents[1] / "templates" / "data" / LAW.name
STATE = DATA / "comms_state.json"
LATCH = DATA / "comms_breach_latch.json"
NOISE = re.compile(r"\b(verification code|your code is|one-time|otp|unsubscribe|reply stop|flash sale|% off|promo|"
                   r"liked|loved|laughed at|emphasized|questioned)\b|^\d{4,8}$", re.I)
SNIPPET = 90
AGENT_STATE = Path(os.environ["AGENT_STATE"]) if os.environ.get("AGENT_STATE") else VAULT / "assistant" / "agent" / "state"
REPLIED = re.compile(r"\b(replied|responded|answered|wrote back|texted (?:her|him|them) back|got back to)\b", re.I)
NOT_REPLIED = re.compile(r"\b(not|never|no|hasn't|haven't|didn't|yet to|still needs?)\b\W+(?:\w+\W+){0,3}?(replied|responded|answered|wrote back|got back)\b", re.I)
NAME_STOP = {"and", "the", "mrs", "mr", "jr", "sr"}


def law() -> dict:
    return load_json(LAW, {"classes": {}, "wake": {}})


def _ts(iso: str) -> float:
    s = str(iso or "")[:19]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            if str(iso).endswith("Z"):
                return dt.replace(tzinfo=timezone.utc).timestamp()
            return dt.timestamp()                    # local stamps (phone threads, cards)
        except ValueError:
            continue
    return 0.0


def _digits(h: str) -> str:
    return re.sub(r"\D", "", str(h or ""))[-10:]


def _leads() -> dict:
    """digits / email -> lead name, from the quiz lead files and the CRM snapshot."""
    out = {}
    try:
        for f in (DATA / "leads").glob("*.json"):
            lead = (load_json(f, {}) or {}).get("lead") or {}
            nm = " & ".join(x for x in (lead.get("name"), lead.get("partner")) if x)
            if lead.get("phone"):
                out[_digits(lead["phone"])] = nm
            if lead.get("email"):
                out[str(lead["email"]).lower()] = nm
    except OSError:
        pass
    return out


def _couples() -> dict:
    out = {}
    ps = load_json(VAULT / "scripts" / "projects" / "project_state.json", {}) or {}
    for key, p in (ps.items() if isinstance(ps, dict) else []):
        if not isinstance(p, dict):
            continue
        for ph in (p.get("phones") or []) + ([p.get("phone")] if p.get("phone") else []):
            out[_digits(ph)] = p.get("couple") or key
        for em in (p.get("emails") or []) + ([p.get("email")] if p.get("email") else []):
            out[str(em).lower()] = p.get("couple") or key
    return out


AUTOMATED_SENDER = re.compile(r"^(?:no-?reply|do-?not-?reply|donotreply|notifications?|notify|concierge|mailer-daemon|postmaster|alerts?|newsletter|marketing|bounce)[@.+-]", re.I)


def is_automated(handle: str) -> bool:
    """A sender nobody answers (a CRM's concierge mail, no-reply notices): never a vendor on an SLA (2026-10-30:
    'Unanswered vendor email: the CRM Concierge (1440 min)' chased the owner about a Zoom notice he had already
    said to drop). Addresses only; a phone number is always a person."""
    h = str(handle or "").lower()
    return "@" in h and bool(AUTOMATED_SENDER.match(h))


def classify(handle: str, lane: str, leads: dict, couples: dict) -> tuple:
    h = handle.lower()
    key = h if "@" in h else _digits(h)
    if key in leads:
        return "lead", leads[key]
    if key in couples:
        return "couple", couples[key]
    if is_automated(h):
        return "notification", ""
    if lane == "owner6":
        return "owner6", ""
    if lane == "wedding":
        return "vendor", ""
    return "personal", ""


def _open_cards_by_handle() -> dict:
    """handle digits/email -> open card qid (a draft waiting on the owner)."""
    out = {}
    try:
        import cards
        for c in cards._live():
            if c.get("status") != "open":
                continue
            q = (c.get("questions") or [{}])[0]
            txt = f"{q.get('question', '')}\n{q.get('details', '')}"
            ex = (q.get("exec") or {}).get("payload") or {}
            for cand in [ex.get("to")] + re.findall(r"\+?1?[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{4}", txt) + re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", txt):
                for one in (cand if isinstance(cand, (list, tuple)) else [cand]):
                    if not one:
                        continue
                    k = str(one).lower() if "@" in str(one) else _digits(str(one))
                    if k and len(k) >= 5:
                        out.setdefault(k, c["id"])
    except Exception:  # noqa: BLE001
        pass
    return out


def _owner_replies() -> list:
    """Active memory facts in which an OWNER told Sage they replied themselves (from the phone, in
    the CRM: out of band, so no log here ever sees it). Sloane 2026-10-22: the owner said on 10/21 he had
    replied, yet her 10/9 text kept raising reply cards. Dream-written facts do not count."""
    try:
        users = (load_json(VAULT / "assistant" / "agent" / "people.json", {}) or {}).get("users") or {}
        owners = {k for k, u in users.items() if isinstance(u, dict) and u.get("role") == "owner"}
        facts = (load_json(AGENT_STATE / "memory.json", {}) or {}).get("facts") or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for f in facts:
        txt = f"{f.get('subject', '')} {f.get('fact', '')}"
        if f.get("active", True) and f.get("by") in owners and REPLIED.search(txt) and not NOT_REPLIED.search(txt):
            out.append({"id": f.get("id", ""), "ts": _ts(f.get("added", "")), "text": txt.lower(),
                        "subject": str(f.get("subject", "")).lower()})
    return out


def _replied_by_owner(th: dict, replies: list) -> str:
    """The memory id of an owner's 'I replied' about this contact made AFTER their last inbound, else ''.
    Matched by the contact's number / address anywhere in it, or a word of their name in its SUBJECT
    (a fact about Corwin that mentions 'the Sloane handoff' is not about Sloane)."""
    li = th.get("last_inbound")
    if not li or not replies:
        return ""
    h = str(th.get("handle") or "")
    keys = {h.lower()} if "@" in h else ({_digits(h)} if len(_digits(h)) >= 7 else set())
    keys |= {w for w in re.findall(r"[a-z]+", str(th.get("name") or "").lower()) if len(w) >= 3 and w not in NAME_STOP}
    t_in = _ts(li["ts"])
    for r in replies:
        if r["ts"] <= t_in:
            continue
        digits = re.sub(r"\D", "", r["text"])
        if any((k.isdigit() and k in digits) or ("@" in k and k in r["text"])
               or (not k.isdigit() and "@" not in k and re.search(rf"\b{re.escape(k)}\b", r["subject"])) for k in keys):
            return r["id"]
    return ""


def build(now: float | None = None) -> dict:
    now = now or time.time()
    L = law()
    classes = L.get("classes") or {}
    leads, couples = _leads(), _couples()
    open_cards = _open_cards_by_handle()
    replies = _owner_replies()
    threads = {}

    # 1. phone threads (both iPhones): the thread log holds every in/out line
    for handle, th in (load_json(DATA / "phone_threads.json", {}) or {}).items():
        msgs = th.get("messages") or []
        if not msgs:
            continue
        last_in = next((m for m in reversed(msgs) if m.get("dir") == "in"), None)
        last_out = next((m for m in reversed(msgs) if m.get("dir") == "out"), None)
        threads[f"text:{handle}"] = {
            "key": f"text:{handle}", "channel": "text", "handle": handle, "name": th.get("name") or "",
            "lane": "", "last_inbound": {"ts": last_in["ts"], "text": last_in["text"][:SNIPPET]} if last_in else None,
            "last_outbound": {"ts": last_out["ts"], "text": last_out["text"][:SNIPPET], "verified": True, "proof": "phone ack"} if last_out else None,
            "n": len(msgs)}

    # 2. tickets: lanes, accounts, the mail threads, the open/closed judgement
    tickets = load_json(DATA / "tickets.json", {}) or {}
    for t in tickets.values():
        if not isinstance(t, dict):
            continue
        p = t.get("payload") or {}
        if t.get("kind") == "imessage_in":
            key = f"text:{p.get('from')}"
            th = threads.setdefault(key, {"key": key, "channel": "text", "handle": p.get("from", ""), "name": p.get("name", ""),
                                          "lane": "", "last_inbound": {"ts": p.get("received") or t.get("created"), "text": str(p.get("text", ""))[:SNIPPET]},
                                          "last_outbound": None, "n": 1})
            th["lane"] = th["lane"] or t.get("lane", "")
            th["account"] = p.get("account", "")
            th.setdefault("tickets", []).append({"id": t["id"], "status": t.get("status"), "created": t.get("created")})
            if not th.get("name") and p.get("name"):
                th["name"] = p["name"]
        elif t.get("kind") == "email" and not p.get("bulk"):
            frm = str(p.get("from") or "")
            addr = (re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", frm) or [frm])[0].lower()
            key = f"email:{addr}"
            th = threads.setdefault(key, {"key": key, "channel": "email", "handle": addr, "name": frm.split("<")[0].strip().strip('"'),
                                          "lane": t.get("lane", ""), "last_inbound": None, "last_outbound": None, "n": 0})
            ts = t.get("created")
            if not th["last_inbound"] or _ts(ts) > _ts(th["last_inbound"]["ts"]):
                th["last_inbound"] = {"ts": ts, "text": f"{p.get('subject', '')}: {t.get('summary', '')}"[:SNIPPET]}
            th["n"] += 1
            th["account"] = p.get("account", "")
            th.setdefault("tickets", []).append({"id": t["id"], "status": t.get("status"), "created": t.get("created")})

    # 3. verified email sends (executor tickets done) close the loop on email threads
    try:
        import exec_store as es
        for tk in es.fold().values():
            if tk.get("kind") == "email_send" and tk.get("status") == "done":
                to = tk.get("payload", {}).get("to")
                for one in (to if isinstance(to, (list, tuple)) else [to]):
                    th = threads.get(f"email:{str(one or '').lower()}")
                    if th:
                        ts = tk.get("updated") or tk.get("created")
                        if not th["last_outbound"] or _ts(ts) > _ts(th["last_outbound"]["ts"]):
                            th["last_outbound"] = {"ts": ts, "text": str(tk["payload"].get("subject", ""))[:SNIPPET], "verified": True, "proof": "executor done"}
    except Exception:  # noqa: BLE001
        pass

    # 3b. texts handed to a phone and not yet confirmed (the outcome ledger, WP3): the thread is awaiting the party,
    # flagged unconfirmed, never a false needs_reply and never a false 'verified' (the ledger's own card settles it)
    try:
        import exec_store as es
        import outcomes
        for tk in es.fold().values():
            if tk.get("kind") != "imessage_send" or tk.get("status") != "done":
                continue
            handle = _digits(str((tk.get("payload") or {}).get("to") or ""))
            th = next((t for k, t in threads.items() if t.get("channel") == "text" and _digits(t.get("handle", "")) == handle), None)
            if not th:
                continue
            ts = tk.get("ts_done") or tk.get("ts")
            if th.get("last_outbound") and _ts(th["last_outbound"]["ts"]) >= _ts(ts):
                continue
            state = outcomes.state_for_ticket(tk["id"])
            if state in ("failed", "abandoned"):
                continue
            th["last_outbound"] = {"ts": ts, "text": str((tk.get("payload") or {}).get("body") or "")[:SNIPPET],
                                   "verified": state == "verified", "proof": f"ledger {state or 'handed'}", "unconfirmed": state != "verified"}
    except Exception:  # noqa: BLE001
        pass

    # 4. state + SLA per thread
    lanes = {"needs_reply": [], "breached": [], "awaiting_owner1": [], "awaiting_party": [], "closed": []}
    import text_gate
    business_only = text_gate.business_only()
    for key, th in threads.items():
        cls, who = classify(th["handle"], th.get("lane", ""), leads, couples)
        th["class"], th["name"] = cls, th.get("name") or who
        li, lo = th.get("last_inbound"), th.get("last_outbound")
        tix = th.get("tickets") or []
        all_closed = tix and all(x["status"] in ("dismissed", "done") for x in tix)
        noise = bool(li and NOISE.search(li["text"]))
        hk = th["handle"].lower() if "@" in th["handle"] else _digits(th["handle"])
        # a friend / family / group text is not wedding work: recorded, never on an SLA or chased
        # (the owner 2026-10-08; text_gate.py, switch phone.triage_all_texts)
        offtopic = business_only and th["channel"] == "text" and cls == "personal"
        if noise or offtopic or (all_closed and not (lo and li and _ts(lo["ts"]) < _ts(li["ts"]))) or not li:
            st = "closed"
        elif lo and (lo.get("verified") or lo.get("unconfirmed")) and _ts(lo["ts"]) >= _ts(li["ts"]):
            st = "awaiting_party"
            if lo.get("unconfirmed"):
                th["unconfirmed_out"] = True
        elif mid := _replied_by_owner(th, replies):
            st, th["owner_replied"] = "awaiting_party", mid       # an owner answered it themselves: no card, no chase
        elif hk in open_cards:
            st, th["card"] = "awaiting_owner1", open_cards[hk]
        else:
            st = "needs_reply"
        th["state"] = st
        sla_h = (classes.get(cls) or {}).get("sla_h")
        th["sla"] = None
        if st in ("needs_reply", "awaiting_owner1") and sla_h and li:
            due = _ts(li["ts"]) + float(sla_h) * 3600
            left_h = (due - now) / 3600
            band = "breached" if left_h < 0 else ("red" if left_h < float(sla_h) * 0.25 else ("amber" if left_h < float(sla_h) * 0.6 else "green"))
            th["sla"] = {"due": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(due)), "hours_left": round(left_h, 2), "band": band, "breached": left_h < 0}
        th["age_min"] = round((now - _ts(li["ts"])) / 60) if li else None
        lanes[st].append(key)
        if th["sla"] and th["sla"]["breached"]:
            lanes["breached"].append(key)
    for k in lanes:
        lanes[k].sort(key=lambda key: -(_ts((threads[key].get("last_inbound") or {}).get("ts", "")) or 0))
    counts = {k: len(v) for k, v in lanes.items()}
    return {"_generated": now_iso(), "threads": threads, "lanes": lanes, "counts": counts}


def breach_tickets(state: dict, dry: bool = False) -> int:
    """One ticket + one Chat note per (thread, inbound) when its SLA passes with no reply out
    and no card open. The latch keeps it to once."""
    latch = load_json(LATCH, {}) or {}
    items, n = [], 0
    for key in state["lanes"]["breached"]:
        th = state["threads"][key]
        if th["state"] != "needs_reply":
            continue                              # a card is already waiting on the owner: the card is the alarm
        mark = f"{key}|{(th.get('last_inbound') or {}).get('ts', '')}"
        if latch.get(mark):
            continue
        latch[mark] = now_iso()
        who = th.get("name") or th["handle"]
        items.append({"source": "comms", "source_id": mark, "lane": th.get("lane") or "wedding", "kind": "sla_breach",
                      "title": f"Unanswered {th['class']} {th['channel']}: {who} ({th['age_min']} min)",
                      "summary": f"Last message {th['last_inbound']['ts'][11:16]}: {th['last_inbound']['text']}. No reply drafted and no card open. "
                                 f"Draft the reply now (exec card) or close the thread.",
                      "payload": {"thread": key, "handle": th["handle"], "class": th["class"], "channel": th["channel"]}})
    if items and not dry:
        import tickets
        n = tickets.add_many(items, "comms_state")
        save_json(LATCH, latch)
        try:
            import chat_push
            for it in items:
                chat_push.push_text(f"⏰ {it['title']}\n{it['summary']}", it["lane"])
        except Exception:  # noqa: BLE001
            pass
    return max(n, 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    st = build()
    if not a.dry_run:
        tmp = STATE.with_suffix(".tmp")
        save_json(tmp, st)
        tmp.replace(STATE)
    n = breach_tickets(st, dry=a.dry_run)
    if a.json:
        print(json.dumps(st, ensure_ascii=False)[:200000])
    print("COMMS_STATE " + " ".join(f"{k}={v}" for k, v in st["counts"].items()) + f" breach_tickets={n}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
