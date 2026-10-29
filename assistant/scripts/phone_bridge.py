"""phone_bridge.py - the iMessage bridge to owner's iPhone (lane `owner`, 2026-10-01).

iMessage has no API and this machine is Windows-only, so the iPhone itself does the
messaging through Apple Shortcuts, talking to Mission Control over Tailscale:

  INBOUND   a "When I get a message" automation POSTs {from, name?, text} to
            /api/phone/inbound -> recorded in the thread log + one assistant ticket
            (lane owner, kind imessage_in). The operator drafts a reply as an EXEC card.
  OUTBOUND  an Approve tap on that card mints an `imessage_send` executor ticket; drain.py
            hands the EXACT payload to queue_outbound() -> the phone outbox. The phone's
            "Send approved texts" shortcut GETs /api/phone/outbox, sends each with the
            Send Message action, and POSTs /api/phone/ack {id}.

Delivery is AT MOST ONCE: an item is marked `handed` the moment the phone fetches it and is
never offered again, so a shortcut that dies after sending cannot cause a double text. A
handed item with no ack after HANDED_STALE_MIN is surfaced by sentinels.py for a human look.

Every phone call carries the shared token (header X-example-Token or ?token=) from
assistant/data/phone_token.txt (git-ignored, minted on first use, `phone_bridge.py token`).
Tailscale is still the outer door; the token stops anything else on the tailnet from
reading owner's texts or creating tickets.

  python assistant/scripts/phone_bridge.py token [--rotate]
  python assistant/scripts/phone_bridge.py thread <phone|email> [--n 20]   (operator: read-only)
  python assistant/scripts/phone_bridge.py outbox [--all]
  python assistant/scripts/phone_bridge.py stale
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alib import DATA, QueueLock, load_json, save_json  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TOKEN_FILE = DATA / "phone_token.txt"
OUTBOX = DATA / "phone_outbox.json"
THREADS = DATA / "phone_threads.json"
# Every phone that runs the Shortcuts, with the lane its incoming texts belong to and the
# owner named on cards. the owner's iPhone added 2026-10-05 ("do everything": a texting line for
# couples from his own number). A text from a known lead's number lands in lane wedding.

def _orgcfg():
    """assistant/orgcfg.py (WP5): the organization's own values from assistant/org.json; the literal below is the fallback."""
    import sys as _sys
    from pathlib import Path as _P
    root = str(_P(__file__).resolve().parents[1])
    if root not in _sys.path:
        _sys.path.insert(0, root)
    import orgcfg
    return orgcfg


# A phone with no org.json entry: one unnamed line on the personal lane, so the bridge still answers.
_NO_PHONES = {"phone": {"lane": "personal", "owner": "the owner"}}
try:   # assistant/org.json -> phone_accounts / default_phone_account / phone_token_header (WP5)
    ACCOUNTS = dict(_orgcfg().get("phone_accounts") or {}) or dict(_NO_PHONES)
    DEFAULT_ACCOUNT = str(_orgcfg().get("default_phone_account") or next(iter(ACCOUNTS)))
    TOKEN_HEADER = str(_orgcfg().get("phone_token_header") or "X-Sage-Token")
except Exception:  # noqa: BLE001
    ACCOUNTS = dict(_NO_PHONES)
    DEFAULT_ACCOUNT = next(iter(ACCOUNTS))
    TOKEN_HEADER = "X-Sage-Token"
if DEFAULT_ACCOUNT not in ACCOUNTS:
    DEFAULT_ACCOUNT = next(iter(ACCOUNTS))
THREAD_KEEP = 60            # messages kept per contact (context for drafting)
OUTBOX_MAX_AGE_H = 3        # an approved text not fetched within this long is not sent late
HANDED_STALE_MIN = 30       # fetched by the phone but never acked
HANDED_STALE_NOACK_MIN = 240  # ...on a phone whose Shortcut has NEVER acked (every text looks unconfirmed there; 2026-10-23)
HANDED_GIVEUP_H = 72        # older than this, a handed text is not chased any more (example's Oct 18 text was still being raised Oct 23)
TEXT_MAX = 4000


# ------------------------------------------------------------------ identity helpers
def norm_handle(h: str) -> str:
    """One key per contact: digits-only US numbers become +1XXXXXXXXXX; emails lowercase."""
    h = str(h or "").strip()
    if "@" in h:
        return h.lower()
    d = re.sub(r"\D", "", h)
    if len(d) == 10:
        d = "1" + d
    return "+" + d if d else ""


def valid_handle(h: str) -> bool:
    h = norm_handle(h)
    if "@" in h:
        return bool(re.fullmatch(r"[\w.+-]+@[\w-]+\.[\w.-]+", h)) and "example.com" not in h
    return bool(re.fullmatch(r"\+\d{11,15}", h))


# ------------------------------------------------------------------ token
def token(rotate=False) -> str:
    if not rotate:
        try:
            t = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if t:
                return t
        except OSError:
            pass
    t = secrets.token_urlsafe(24)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    return t


def check_token(given: str) -> bool:
    try:
        want = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return bool(want) and hmac.compare_digest(str(given or "").strip().encode("utf-8"), want.encode("utf-8"))


# ------------------------------------------------------------------ threads
def _log_thread(handle, direction, text, name="", ref=""):
    db = load_json(THREADS, {})
    th = db.setdefault(handle, {"name": "", "messages": []})
    if name:
        th["name"] = name[:80]
    # Several iPhone automations (one per trigger letter) can forward the same text; log it once.
    last = th["messages"][-1] if th["messages"] else {}
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if (direction == "in" and last.get("dir") == "in" and last.get("text") == text[:TEXT_MAX]
            and last.get("ts", "")[:15] == now[:15]):
        return
    th["messages"].append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "dir": direction, "text": text[:TEXT_MAX], "ref": ref})
    th["messages"] = th["messages"][-THREAD_KEEP:]
    save_json(THREADS, db)


def thread(handle, n=20) -> dict:
    th = load_json(THREADS, {}).get(norm_handle(handle)) or {"name": "", "messages": []}
    if not th.get("name"):
        th["name"] = _who_name(handle)
    return {"handle": norm_handle(handle), "name": th.get("name", ""), "messages": th["messages"][-n:]}


def _who_name(handle, text: str = "") -> str:
    """The best name for a sender: whois.py (contacts, memory, the wiki, HoneyBook, the leads and the
    texts themselves; 2026-10-23), the plain phone book before that."""
    try:
        import whois
        return whois.name_for(handle, text or None)
    except Exception:  # noqa: BLE001
        try:
            import contacts
            return contacts.name_for(handle)
        except Exception:  # noqa: BLE001
            return ""


# ------------------------------------------------------------------ inbound
def _lead_phones() -> set:
    """Digits of every quiz lead's phone (assistant/data/leads/*.json): a text from one of them
    is wedding work, whichever phone it reached."""
    out = set()
    try:
        for f in (DATA / "leads").glob("*.json"):
            ph = str((load_json(f, {}).get("lead") or {}).get("phone") or "")
            d = re.sub(r"\D", "", ph)
            if len(d) >= 10:
                out.add(d[-10:])
    except OSError:
        pass
    return out


def _lead_line() -> str:
    """The phone line that texts leads (config.json -> leads.text_account): a known lead's text there is business."""
    try:
        from alib import config
        return str((config().get("leads") or {}).get("text_account") or "")
    except BaseException:  # noqa: BLE001  (config() sys.exits on a broken file)
        return ""


def lane_for(account: str, handle: str) -> str:
    acct = ACCOUNTS.get(account) or ACCOUNTS[DEFAULT_ACCOUNT]
    if account and account == _lead_line() and re.sub(r"\D", "", handle)[-10:] in _lead_phones():
        from alib import business_lane
        return business_lane()
    return acct["lane"]


def _own_nudge(handle: str, text: str) -> bool:
    """phone_nudge.py waking the phone: its marker phrase, or a sender on the organization's own mail domain."""
    try:
        import phone_nudge
        mark = str(phone_nudge.MARK or "")
    except Exception:  # noqa: BLE001
        mark = ""
    if mark and text.startswith(mark):
        return True
    try:
        own = [str(e) for e in (_orgcfg().get("send_identities") or []) + (_orgcfg().get("internal_emails") or [])]
    except Exception:  # noqa: BLE001
        own = []
    domains = {e.split("@", 1)[1].lower() for e in own if "@" in e
               and not e.lower().endswith(("gmail.com", "outlook.com", "yahoo.com", "icloud.com", "hotmail.com"))}
    return any(handle.lower().endswith("@" + d) for d in domains)


def inbound(sender, text, name="", received="", account=DEFAULT_ACCOUNT):
    """One incoming text from a phone -> thread log + ONE ticket (deduped on content)."""
    handle, text = norm_handle(sender), str(text or "").strip()
    if not handle or not text:
        return {"ok": False, "reason": "from and text are required"}
    account = account if account in ACCOUNTS else DEFAULT_ACCOUNT
    if _own_nudge(handle, text):
        return {"ok": True, "queued": 0, "skipped": "own nudge"}      # phone_nudge.py waking the phone
    who = {}
    try:                                       # who is this (whois.py): name, relation, kind, how sure, why
        import whois
        who = whois.who_block(handle, text)
    except Exception:  # noqa: BLE001
        who = {}
    if not name:                               # the Shortcut sends no name: the records and the texts know it
        name = who.get("name", "") if float(who.get("confidence") or 0) >= 0.6 or who.get("kind") in ("automated", "business") else ""
        if not name:
            name = _who_name(handle, text)
    lane = lane_for(account, handle)
    received = str(received or time.strftime("%Y-%m-%dT%H:%M:%S"))[:25]
    with QueueLock("phone_bridge") as ok:
        if not ok:
            return {"ok": False, "reason": "queue busy, retry"}
        _log_thread(handle, "in", text, name)
    import tickets
    sid = hashlib.sha1(f"{handle}|{received[:16]}|{text}".encode("utf-8")).hexdigest()[:16]
    shown = who.get("label") or name or handle
    if name and who.get("label") and float(who.get("confidence") or 0) < 0.85 and who.get("kind") not in ("automated", "business"):
        shown = who["label"]                   # "probably example (+1 ...)" says how sure the name is
    elif name:
        shown = name
    item = {"source": "imessage", "source_id": sid, "lane": lane, "kind": "imessage_in",
            "title": f"Text from {shown}: {text[:90]}", "summary": text[:600],
            "payload": {"from": handle, "name": name[:80], "text": text[:TEXT_MAX],
                        "received": received, "account": account,
                        # who they are, with the reason, so the drafter and Sage never have to guess (whois.py)
                        **({"who": who} if who else {}),
                        # the conversation so far (both directions), so the drafter has context without a lookup
                        "thread": [{"ts": m.get("ts", "")[:16], "dir": m.get("dir", ""), "text": str(m.get("text", ""))[:240]}
                                   for m in thread(handle, 8)["messages"][:-1]]}}
    added = tickets.add_many([item], "phone_bridge")
    if added < 0:
        # QUEUE_BUSY. The iPhone Shortcut does not retry, so the text is parked here and the
        # next card sweep (10 min) turns it into its ticket.
        try:
            import cards
            cards.spill_items([item])
            return {"ok": True, "queued": 0, "spilled": 1}
        except Exception:
            return {"ok": False, "reason": "queue busy, retry"}
    return {"ok": True, "queued": added}


# ------------------------------------------------------------------ outbound
def queue_outbound(payload: dict, ticket_id: str) -> tuple[bool, str]:
    """drain.py's imessage_send executor. Idempotent per executor ticket."""
    to, body = norm_handle(payload.get("to")), str(payload.get("body") or "")
    if not valid_handle(to) or not body.strip():
        return False, "PERMANENT: imessage_send needs a valid phone/email `to` and a body"
    with QueueLock("phone_bridge") as ok:
        if not ok:
            return False, "outbox busy"
        box = load_json(OUTBOX, {"items": []})
        if any(i.get("ticket") == ticket_id for i in box["items"]):
            return True, f"PHONE_QUEUED (already) {ticket_id}"
        oid = "m-" + secrets.token_hex(6)
        acct = str(payload.get("account") or DEFAULT_ACCOUNT)
        box["items"].append({"id": oid, "ticket": ticket_id, "to": to, "body": body, "status": "ready", "account": acct,
                             "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "ts_handed": "", "ts_sent": ""})
        save_json(OUTBOX, box)
        n_ready = sum(1 for i in box["items"] if i.get("status") == "ready")
    try:   # wake the phone so its Shortcut drains the outbox now (phone_nudge.py); never fatal
        import phone_nudge
        phone_nudge.fire(n_ready)
    except Exception:  # noqa: BLE001
        pass
    return True, f"PHONE_QUEUED {oid} -> ...{to[-4:]}"


def _quiet() -> bool:
    import exec_store as es
    h = time.localtime().tm_hour
    return h >= es.QUIET_START or h < es.QUIET_END


def fetch_outbox(account: str = DEFAULT_ACCOUNT) -> list:
    """ONE phone asks for work: only the items approved for that account (an item with no
    account is owner's, from before 2026-10-05). Every ready item is handed over exactly once. Nothing is
    handed over during quiet hours: drain holds new tickets then, but a text that was already
    in the outbox could still be fetched (and sent) by a late-night Shortcut run."""
    if _quiet():
        return []
    with QueueLock("phone_bridge") as ok:
        if not ok:
            return []
        box = load_json(OUTBOX, {"items": []})
        out, late, now = [], [], time.strftime("%Y-%m-%dT%H:%M:%S")
        for i in box["items"]:
            if i["status"] != "ready" or str(i.get("account") or DEFAULT_ACCOUNT) != account:
                continue
            try:
                age_h = (time.time() - time.mktime(time.strptime(i["ts"], "%Y-%m-%dT%H:%M:%S"))) / 3600
            except (KeyError, ValueError):
                age_h = 0
            if age_h > OUTBOX_MAX_AGE_H:
                # "running 10 min late" must not go out the next morning: a text that waited
                # this long is not sent; it comes back as a ticket to re-card if still wanted
                i["status"] = "expired"
                late.append(i)
                continue
            i["status"], i["ts_handed"] = "handed", now
            out.append({"id": i["id"], "to": i["to"], "body": i["body"]})
        if out or late:
            save_json(OUTBOX, box)
    if late:                                   # outside the lock: add_many takes the same lock
        items = [{"source": "phone_outbox", "source_id": i["id"], "lane": ACCOUNTS.get(str(i.get("account") or DEFAULT_ACCOUNT), ACCOUNTS[DEFAULT_ACCOUNT])["lane"], "kind": "exec_exception",
                  "title": f"Approved text to ...{i['to'][-4:]} was NOT sent (waited over {OUTBOX_MAX_AGE_H} h)",
                  "summary": "It sat in the outbox past its moment (quiet hours, or the phone never fetched it). "
                             "Card it again only if it is still wanted. Text: " + i["body"][:300],
                  "payload": {"outbox_id": i["id"], "to": i["to"], "body": i["body"], "approved_ts": i.get("ts")}}
                 for i in late]
        try:
            import tickets
            added = tickets.add_many(items, "phone_bridge")
        except Exception:
            added = -1
        if added < 0:
            # The outbox already says "expired", so a ticket that could not be queued (QUEUE_BUSY)
            # must be parked for the next card sweep: otherwise an approved text is never sent
            # and nobody is told.
            try:
                import cards
                cards.spill_items(items)
            except Exception:
                pass
    return out


def recall(ticket_id: str) -> bool:
    """An Undo on an approved text: take it back out of the outbox while it is still 'ready' (the
    phone has not fetched it). True when pulled back; a handed or sent text cannot be. Never raises."""
    if not ticket_id:
        return False
    try:
        with QueueLock("phone_bridge") as ok:
            if not ok:
                return False
            box = load_json(OUTBOX, {"items": []})
            it = next((i for i in box["items"] if i.get("ticket") == ticket_id), None)
            if not it or it.get("status") != "ready":
                return False
            it["status"], it["ts_recalled"] = "recalled", time.strftime("%Y-%m-%dT%H:%M:%S")
            save_json(OUTBOX, box)
            return True
    except Exception:  # noqa: BLE001
        return False


def ack(oid) -> dict:
    with QueueLock("phone_bridge") as ok:
        if not ok:
            return {"ok": False, "reason": "busy"}
        box = load_json(OUTBOX, {"items": []})
        it = next((i for i in box["items"] if i["id"] == oid), None)
        if not it:
            return {"ok": False, "reason": "unknown id"}
        if it["status"] != "sent":
            it["status"], it["ts_sent"] = "sent", time.strftime("%Y-%m-%dT%H:%M:%S")
            box["items"] = [i for i in box["items"] if i["status"] != "sent" or i is it or
                            i["ts_sent"] >= time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))]
            save_json(OUTBOX, box)
            _log_thread(it["to"], "out", it["body"], ref=it["ticket"])
    return {"ok": True, "id": oid}


def acks_ever(account: str, items=None) -> bool:
    """Has this phone's Shortcut ever POSTed /api/phone/ack? (A `sent` item with no `confirmed_by` came from
    an ack; `confirmed_by: reply` came from text_confirm.) 2026-10-23: 123 outbox fetches, 0 acks, so every
    text on the owner's line was chased as 'never confirmed' 30 min after it went out."""
    items = load_json(OUTBOX, {"items": []})["items"] if items is None else items
    return any(i.get("status") == "sent" and not i.get("confirmed_by") and i.get("ts_sent")
               and str(i.get("account") or DEFAULT_ACCOUNT) == account for i in items)


def stale(minutes=None, giveup_h=HANDED_GIVEUP_H) -> list:
    """Handed texts with no ack and no reply: chased after `minutes` (HANDED_STALE_MIN, or
    HANDED_STALE_NOACK_MIN on a phone that has never acked), and no longer after `giveup_h`."""
    items = load_json(OUTBOX, {"items": []})["items"]
    now = time.time()
    rows = []
    for i in items:
        if i["status"] != "handed":
            continue
        try:
            t = time.mktime(time.strptime(i["ts_handed"], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        acct = str(i.get("account") or DEFAULT_ACCOUNT)
        win = minutes if minutes is not None else (HANDED_STALE_MIN if acks_ever(acct, items) else HANDED_STALE_NOACK_MIN)
        if t < now - win * 60 and (giveup_h is None or now - t < giveup_h * 3600):
            rows.append(i)
    return rows


# ------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("token"); p.add_argument("--rotate", action="store_true")
    p = sub.add_parser("thread"); p.add_argument("handle"); p.add_argument("--n", type=int, default=20)
    p = sub.add_parser("outbox"); p.add_argument("--all", action="store_true")
    sub.add_parser("stale")
    a = ap.parse_args()
    if a.cmd == "token":
        print(token(a.rotate))
    elif a.cmd == "thread":
        print(json.dumps(thread(a.handle, a.n), ensure_ascii=False, indent=1))
    elif a.cmd == "outbox":
        items = load_json(OUTBOX, {"items": []})["items"]
        print(json.dumps(items if a.all else [i for i in items if i["status"] != "sent"], ensure_ascii=False, indent=1))
    elif a.cmd == "stale":
        print(json.dumps(stale(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
