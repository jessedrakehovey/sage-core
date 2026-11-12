"""gchat_events.py - Google Chat's HTTP events for the Sage app: BUTTON TAPS ANSWERED ON THE SPOT.

Why this exists (2026-10-05, evening): cards were mirrored into the owners' Chat DMs with a
button per option, and every tap showed "could not process request". A Chat app built in
add-on mode gets interactive events (button clicks) only through a synchronous HTTPS reply;
the Pub/Sub pull the bridge uses never sees them (zero events arrived). So the app's
connection is an HTTP endpoint: Mission Control's `/gchat/events`, reached through Tailscale
Funnel on :8443. Every event carries Google's signed token; nothing is acted on until it
verifies (issuer chat@system.gserviceaccount.com, audience = the Chat project number or the
endpoint URL).

  message event  -> queued for the bridge (assistant/data/agent/gchat_inbox.jsonl); the
                    reply still happens through the bridge + brain + REST post (no model
                    turn inside Google's 30 s window). Response: {} (nothing posted here).
  button tap     -> cards.answer / cards.undo, by="gchat:<owner>", the SAME guarded path as
                    Mission Control; the reply IS the updated card (buttons -> the answer).
  anyone else    -> a one-line refusal, no action.

Pure functions; mc_server.py is the only caller. Tests: assistant/tests/test_chat_push.py.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
for p in (str(HERE.parent / "agent"),):
    if p not in sys.path:
        sys.path.append(p)

CHAT_ISSUER = "chat@system.gserviceaccount.com"
CERTS_URL = f"https://www.googleapis.com/service_accounts/v1/metadata/x509/{CHAT_ISSUER}"
QID = re.compile(r"q[0-9a-f]{12}")


def _core():
    import core
    return core


def peek_claims(auth_header: str) -> dict:
    """The UNVERIFIED claims of a bearer JWT (iss/aud/email only), for the refusal log line.
    Never trusted for anything else; never raises."""
    try:
        tok = str(auth_header or "").split(None, 1)[1].strip()
        seg = tok.split(".")[1]
        import base64
        claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)).decode("utf-8"))
        return {k: str(claims.get(k, ""))[:120] for k in ("iss", "aud", "email")}
    except Exception:  # noqa: BLE001
        return {}


def verify_relay(header_value: str, secret: str) -> bool:
    """The Apps Script relay (assistant/agent/gchat_relay/) forwards every Chat event with
    X-Sage-Relay: <shared secret>. Google Chat itself only calls port 443 and this machine's
    public path is on 8443 (2026-10-06), so the relay is how taps reach here. Constant-time
    compare; an unset secret accepts nothing."""
    import hmac
    sec = str(secret or "")
    return bool(sec) and hmac.compare_digest(str(header_value or ""), sec)


def verify_bearer(auth_header: str, audiences: list, _verify=None) -> dict | None:
    """Google's ID token on every Chat event, or None. `_verify` is the test seam."""
    tok = str(auth_header or "")
    if not tok.lower().startswith("bearer "):
        return None
    tok = tok[7:].strip()
    try:
        if _verify is None:
            from google.auth.transport import requests as g_requests
            from google.oauth2 import id_token

            def _verify(t):
                return id_token.verify_token(t, g_requests.Request(), certs_url=CERTS_URL)
        claims = _verify(tok)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(claims, dict) or claims.get("iss") != CHAT_ISSUER:
        return None
    aud = str(claims.get("aud") or "")
    if aud not in [str(a) for a in audiences if a]:
        return None
    return claims


def _addon(ev: dict) -> bool:
    return isinstance(ev.get("chat"), dict)


def _reply_text(ev: dict, text: str) -> dict:
    if _addon(ev):
        return {"hostAppDataAction": {"chatDataAction": {"createMessageAction": {"message": {"text": text}}}}}
    return {"text": text}


def _reply_update(ev: dict, body: dict) -> dict:
    # the card only: the message's plain `text` (the phone-notification line added 2026-10-07) is
    # fine on a new message but Chat refuses it inside a tap's update, showing "Sage is unable to
    # process your request" AFTER the tap had already registered (the owner 2026-10-08)
    body = {k: v for k, v in body.items() if k != "text"}
    if _addon(ev):
        return {"hostAppDataAction": {"chatDataAction": {"updateMessageAction": {"message": body}}}}
    return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **body}


def _reply_dialog(ev: dict, body: dict) -> dict:
    if _addon(ev):
        return {"action": {"navigations": [{"pushCard": body}]}}
    return {"actionResponse": {"type": "DIALOG", "dialogAction": {"dialog": {"body": body}}}}


def _reply_close(ev: dict, text: str) -> dict:
    if _addon(ev):
        return {"action": {"navigations": [{"endNavigation": {"action": "CLOSE_DIALOG"}}], "notification": {"text": text}}}
    return {"actionResponse": {"type": "DIALOG", "dialogAction": {"actionStatus": {"statusCode": "OK", "userFacingMessage": text}}}}


def handle(ev: dict, log=lambda s: None, late: float | None = None) -> dict:
    """One verified Chat event -> the JSON Chat expects back. Never raises. `late`: the epoch the
    relay queued this event at while Mission Control was unreachable (header X-Sage-Late); nobody
    is waiting on the reply, so the outcome goes to Chat by REST instead."""
    import gchat
    try:
        common = (ev.get("commonEventObject") or {}) if isinstance(ev, dict) else {}
        # what kind of event this was, so a tap or dialog that goes nowhere can be traced
        # (2026-10-07: "Edit & send" opened its dialog but the submit never showed up in any log)
        log("gchat event detail: fn=" + str(ev.get("relayFunction") or common.get("invokedFunction") or "")
            + " dialog=" + str(common.get("dialogEventType") or "") + " form=" + ",".join(sorted((common.get("formInputs") or {}).keys()))
            + " params=" + json.dumps(common.get("parameters") or {})[:160]
            + " platform=" + str(ev.get("clientPlatform") or common.get("platform") or "")
            + " chat_keys=" + ",".join(sorted((ev.get("chat") or {}).keys())[:6]))
        h = gchat.parse_home(ev)
        if h:                       # the owner opened the app's Home tab (2026-11-04)
            return home_open(ev, h, log)
        c = gchat.parse_command(ev)
        if c:                       # a registered slash or quick command
            return command_event(ev, c, log, late)
        m = gchat.parse_event(ev)
        if m:
            gchat.queue_inbound(m)
            log(f"gchat http message from {m.get('email') or m.get('chat_user')} queued")
            return {}
        a = gchat.parse_added(ev)
        if a:                       # an owner added Sage: their DM is recorded, cards reach them from now on
            who = gchat.record_added(a)
            log(f"gchat app added in {'a DM' if a.get('dm') else 'a space'} by {who or 'a non-owner'}")
            if who and a.get("dm") and a.get("space"):
                # the start-up sequence (startup.py, 2026-11-04): what the agent can set up by itself arrives as ONE card
                # the moment an owner first adds it; nothing is a step at a computer
                try:
                    import startup
                    if startup.offer(str(a["space"])):
                        log(f"start-up card offered to {who}")
                except Exception as e:  # noqa: BLE001
                    log(f"start-up offer failed: {type(e).__name__}: {str(e)[:120]}")
            return _reply_text(ev, "Connected. Cards for you will arrive here, with buttons to answer them.") if who else {}
        k = gchat.parse_click(ev)
        if not k:
            gchat._note_unparsed(ev if isinstance(ev, dict) else {})
            return {}
        return click(ev, k, log, late)
    except Exception as e:  # noqa: BLE001
        log(f"gchat http handle crashed: {type(e).__name__}: {str(e)[:200]}")
        return {}


# ------------------------------------------------------------------ taps that never make Chat wait
# A tap is answered in well under a second on a good day, but Chat gives the whole round trip (Chat ->
# Apps Script relay -> Funnel -> here) only a few seconds before it shows "Sage is unable to process
# your request" - and then the answer often lands anyway, much later, with nothing said (the owner
# 2026-10-19). So the work runs on its own thread: if it is done within TAP_WAIT_S the reply is the
# exact outcome as before; if not, Chat gets the chosen answer straight away and the thread finishes
# on its own; a failure then comes back as a fresh card (report_failed), never as silence.
TAP_WAIT_S = 3.0
LATE_MAX_S = 2 * 3600          # a tap the relay held longer than this is not acted on: fresh card instead
_DUP = re.compile(r"being answered|already answered|already been answered", re.I)


def _plain(option: str) -> str:
    return re.sub(r"\s*\(Recommended\)\s*", "", str(option or "")).strip()


def _answer_soon(qid: str, option: str, q_index: int, person: dict, before, log, grant: str = "") -> dict:
    """cards.answer on a worker thread. Returns {"r": result} when it finished within TAP_WAIT_S,
    else {} (the worker reports any failure itself when it is done)."""
    import threading
    import cards
    box: dict = {}
    replied = threading.Event()

    def work():
        try:
            r = cards.answer(qid, option, q_index=q_index, by=f"gchat:{person['key']}", grant=grant)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "reason": f"{type(e).__name__}: {str(e)[:160]}"}
        box["r"] = r
        if replied.is_set():                      # Chat already shows the choice: only a failure is news
            log(f"late result answer {qid} {option!r}: ok={r.get('ok')} {r.get('reason') or r.get('ticket') or ''}")
            _after(qid, option, person, before, r, log)

    t = threading.Thread(target=work, daemon=True, name=f"tap-{qid}")
    t.start()
    t.join(TAP_WAIT_S)
    if "r" in box:
        return box
    replied.set()
    if "r" in box:                                # finished in the instant between join and set
        return box
    return {}


def _after(qid: str, option: str, person: dict, before, r: dict, log) -> None:
    """The outcome of a tap whose card Chat already shows as working (⏳): when it worked, the card
    flips to the real outcome by REST with nothing else said; a fresh card / plain line when it did not."""
    import cards
    import chat_push
    if r.get("ok"):
        card = cards._read(cards.LIVE / f"{qid}.json") or before or {}
        if not r.get("mint_error"):
            chat_push.mark_answered(card, {"option": option, "by": person["key"], "ticket": r.get("ticket")})
        else:
            chat_push.mark_answered(card, {"option": option, "by": person["key"], "mint_error": r.get("mint_error")})
            chat_push.push_result(card, f"I recorded \"{_plain(option)}\" but couldn't set it up: {str(r['mint_error'])[:200]}. "
                                        "Nothing went out. Ask me here and I'll redo it.")
        return
    reason = str(r.get("reason") or "")
    if _DUP.search(reason) and (cards._answers_index().get(qid) or {}).get("option"):
        return                                    # another delivery of the same tap got there first: it worked
    report_failed(qid, option, reason, before, log)


def _late_answer(qid: str, option: str, q_index: int, person: dict, before, late: float, log, grant: str = "") -> dict:
    """A tap the relay held while this server was unreachable. Nobody waits on the reply."""
    import cards
    import chat_push
    core = _core()
    age = time.time() - float(late or 0)
    core.say(chat_push.agent_convo(person["key"]), person["name"], chat_push.tap_line(before, option, qid),
             channel="gchat", meta={"click": True, "late_s": int(age)})
    if age > LATE_MAX_S:
        log(f"late tap {qid} {option!r} held {int(age)}s: too old to act on, reposting")
        report_failed(qid, option, f"your tap reached me {int(age // 60)} minutes late", before, log)
        return {}
    r = cards.answer(qid, option, q_index=q_index, by=f"gchat:{person['key']}", grant=grant)
    log(f"late tap {qid} {option!r} by {person['key']} (held {int(age)}s): ok={r.get('ok')} {r.get('reason') or r.get('ticket') or ''}")
    if r.get("ok"):
        card = cards._read(cards.LIVE / f"{qid}.json") or before
        if card:                                  # the relay showed a placeholder; put the real answered card back
            chat_push.mark_answered(card, {"option": option, "by": person["key"], "ticket": r.get("ticket"),
                                           "mint_error": r.get("mint_error")})
    if not r.get("ok") or r.get("mint_error"):
        _after(qid, option, person, before, r, log)
    return {}


def _taken(ev: dict, qid: str, option: str, person: dict, before) -> dict:
    """The reply to a second delivery of a tap that is already being (or has been) answered. Never {}:
    Chat shows an empty reply to a button as "Sage is unable to process your request", and when the
    relay delivers one tap twice that is the reply the phone may show while the first one is sending
    (the owner 2026-10-25, Corwin's email: the error at 5:36, the send at 5:40). So: the card as it stands."""
    import cards
    import chat_push
    card = cards._read(cards.LIVE / f"{qid}.json") or cards._read(cards.ARCHIVE / f"{qid}.json") or before
    if not card:
        return _reply_text(ev, f"Got it: {_plain(option)}. Working on it.")
    done = cards._answers_index().get(qid) or {}
    if card.get("status") != "open" and done.get("option") and not done.get("undone"):
        a = {"option": done["option"], "by": str(done.get("by") or person["key"]), "ticket": done.get("ticket"),
             "mint_error": done.get("mint_error")}
    else:                                         # the first delivery still holds the card: it is working
        a = {"option": option, "by": person["key"], "working": True}
    return _reply_update(ev, chat_push.body_for(dict(card, status="answered"), "answered", a))


def report_failed(qid: str, option: str, reason: str, before, log) -> None:
    """A tap that did not go through, told later: the same decision again as a fresh card with
    working buttons, and one plain line saying why. Never raises."""
    import cards
    import chat_push
    try:
        card = cards._read(cards.LIVE / f"{qid}.json")
        q = ((card or before or {}).get("questions") or [{}])[0]
        lane = str(q.get("lane") or "")
        what = str(q.get("header") or "that decision")
        why = re.sub(r"\s*-\s*(reload the page|undo within 30 s, or ask for a new card|tap Undo first to change it)\s*$", "", reason).strip() or "it didn't go through"
        fresh = ""
        done = cards._answers_index().get(qid) or {}
        if card and card.get("status") == "answered" and done.get("option"):
            # decided already (on Mission Control, or by the other owner): nothing to re-ask
            if _plain(done["option"]) != _plain(option):
                chat_push.push_say(f"Your \"{_plain(option)}\" on {what} came too late: it was already answered "
                                   f"\"{_plain(done['option'])}\". Tell me here if that should change.", lane=lane)
            return
        if card and card.get("status") == "open":
            chat = chat_push.push_card(card)      # same card, new message, buttons that work
            if chat:
                card["chat"] = chat
                cards._write(cards.LIVE / f"{qid}.json", card)
                fresh = qid
        elif before and before.get("questions"):
            r = cards.post(before["questions"], before.get("owner") or "gchat-retry", lane=lane,
                           thread=str(before.get("chat_thread") or ""), replaces=qid)
            fresh = r.get("qid", "") if r.get("posted") else ""
            if not fresh:
                log(f"report_failed {qid}: fresh card refused: {str(r.get('reason') or '')[:160]}")
        tail = " Here it is again, tap it once more." if fresh else " Ask me here and I'll set it up again."
        chat_push.push_say(f"Your \"{_plain(option)}\" on {what} didn't go through ({why}).{tail}", lane=lane)
        log(f"report_failed {qid} {option!r}: {why[:120]} fresh={fresh or '-'}")
    except Exception as e:  # noqa: BLE001
        log(f"report_failed {qid} crashed: {type(e).__name__}: {str(e)[:200]}")


def click(ev: dict, k: dict, log=lambda s: None, late: float | None = None) -> dict:
    core = _core()
    import cards
    import chat_push
    person = core.person_for(email=k.get("email", ""), chat_user=k.get("chat_user", ""))
    fn, p = k.get("function", ""), k.get("params") or {}
    if not person or not core.can(person, "approve"):
        log(f"ignored click {fn} from unknown {k.get('email') or k.get('chat_user')!r}")
        return _reply_text(ev, "These cards are for the owners.")
    if fn == "home_action":
        return home_click(ev, k, person, log)
    if fn in ("vault_add", "vault_add_submit"):
        return vault_click(ev, k, person, log)
    if fn in ("form_open", "form_submit", "form_action"):
        return form_click(ev, k, person, log, late=late)
    if p.get("job") and not p.get("qid"):
        return job_click(ev, k, person, log)
    qid = str(p.get("qid") or "")
    if not QID.fullmatch(qid):
        return _reply_text(ev, "That card id is not valid.")
    convo = chat_push.agent_convo(person["key"])
    if fn == "card_answer":
        option = str(p.get("option") or "")
        q_index = int(p.get("q") or 0)
        grant = str(p.get("grant") or "")                 # the Always button (scripts/policy.py): the tap stands
        before = cards._read(cards.LIVE / f"{qid}.json")
        if late is not None:
            return _late_answer(qid, option, q_index, person, before, late, log, grant=grant)
        box = _answer_soon(qid, option, q_index, person, before, log, grant=grant)
        r = box.get("r")
        if r is None:
            # still working after TAP_WAIT_S: the tap is taken; the card shows the choice now and a
            # failure comes back later as a fresh card (the owner 2026-10-19: "make the buttons instantly
            # work and then only tell me later if the requests didn't work with a fresh approval")
            log(f"click answer {qid} {option!r} by {person['key']}: still working after {TAP_WAIT_S}s, answered optimistically")
            core.say(convo, person["name"], chat_push.tap_line(before, option, qid), channel="gchat", meta={"click": True})
            if not before:
                return _reply_text(ev, f"Got it: {_plain(option)}.")
            return _reply_update(ev, chat_push.body_for(dict(before, status="answered"), "answered",
                                                        {"option": option, "by": person["key"], "working": True}))
        log(f"click answer {qid} {option!r} by {person['key']}: ok={r.get('ok')} {r.get('reason') or r.get('ticket') or ''}")
        if not r.get("ok") and re.search(r"being answered|already answered|already been answered|not open", str(r.get("reason") or ""), re.I):
            return _taken(ev, qid, option, person, before)   # the relay re-delivered the tap: no error, no new line
        core.say(convo, person["name"], chat_push.tap_line(before, option, qid), channel="gchat", meta={"click": True})
        if not r.get("ok"):
            return _reply_text(ev, f"That card could not be answered: {r.get('reason')}")
        card = cards._read(cards.LIVE / f"{qid}.json")
        _wake_doer(card or before)
        if not card:
            return _reply_text(ev, f"Answered: {option}.")
        body = chat_push.body_for(card, "answered", {"option": option, "by": person["key"], "ticket": r.get("ticket"),
                                                      "mint_error": r.get("mint_error")})   # a stacked card re-renders the whole stack
        return _reply_update(ev, body)
    if fn == "card_undo":
        r = cards.undo(qid, by=f"gchat:{person['key']}")
        log(f"click undo {qid} by {person['key']}: ok={r.get('ok')} {r.get('reason') or ''}")
        if late is not None:
            if not r.get("ok"):
                card = cards._read(cards.LIVE / f"{qid}.json") or {}
                chat_push.push_result(card, "Your Undo reached me too late, so it didn't take: "
                                            f"{r.get('reason')}. Tell me here if that needs reversing.")
            else:
                card = cards._read(cards.LIVE / f"{qid}.json")
                if card:
                    chat_push.mark_undone(card)
            return {}
        core.say(convo, person["name"], f"[undid the answer on card {qid}]", channel="gchat", meta={"click": True})
        if not r.get("ok"):
            return _reply_text(ev, f"Undo did not go through: {r.get('reason')}")
        card = cards._read(cards.LIVE / f"{qid}.json")
        return _reply_update(ev, chat_push.body_for(card, "undone")) if card else _reply_text(ev, "Undone.")
    if fn == "card_edit":
        card = cards._read(cards.LIVE / f"{qid}.json")
        if not card or card.get("status") != "open":
            return _reply_text(ev, "That card is no longer open.")
        if p.get("mode") == "later":
            log(f"click send-later {qid} by {person['key']}: picker opened")
            return _reply_dialog(ev, chat_push.later_dialog(card, int(p.get("q") or 0), tz_offset_min=_tz_offset_min(ev)))
        log(f"click edit {qid} by {person['key']}: dialog opened")
        return _reply_dialog(ev, chat_push.edit_dialog(card, int(p.get("q") or 0)))
    if fn == "card_edit_submit" and p.get("mode") == "later":
        return later_submit(ev, k, person, qid, log)
    if fn == "card_edit_submit":
        revised = str((k.get("form") or {}).get("body") or "").strip()
        if not revised:
            return _reply_close(ev, "Nothing sent: the message was empty.")
        card = cards._read(cards.LIVE / f"{qid}.json")
        q = ((card or {}).get("questions") or [{}])[int(p.get("q") or 0)] if card else {}
        approve = str(((q.get("exec") or {}).get("approve_label")) or "Approve")
        if ((q.get("exec") or {}).get("payload") or {}).get("ask_code"):
            # the code dialog (chat_push.code_dialog): the digits are the note-box text, minted into the step, never kept
            digits = re.sub(r"\D", "", revised)[:10]
            if not 4 <= len(digits) <= 10:
                return _reply_dialog(ev, chat_push.code_dialog(card, int(p.get("q") or 0), error="A code is 4 to 10 digits."))
            r = cards.answer(qid, approve, text=digits, q_index=int(p.get("q") or 0), by=f"gchat:{person['key']}")
            log(f"click code-submit {qid} by {person['key']}: ok={r.get('ok')} {r.get('reason') or r.get('ticket') or ''} (code not logged)")
            core.say(convo, person["name"], f"[typed the verification code into card {qid}]", channel="gchat", meta={"click": True})
            if not r.get("ok"):
                return _reply_close(ev, f"The code did not go in: {r.get('reason')}")
            card = cards._read(cards.LIVE / f"{qid}.json") or card
            chat_push.mark_answered(card, {"option": approve, "by": person["key"], "ticket": r.get("ticket"), "mint_error": r.get("mint_error")})
            _wake_doer(card)
            return _reply_close(ev, "Got the code. Signing in now; the card and this thread say when it is through.")
        r = cards.answer(qid, approve, revised=revised, q_index=int(p.get("q") or 0), by=f"gchat:{person['key']}")
        log(f"click edit-submit {qid} by {person['key']}: ok={r.get('ok')} {r.get('reason') or r.get('ticket') or ''} revised={len(revised)} chars")
        if r.get("blanks") and card and late is None:
            # a blank is still in the text: keep the dialog open with his words in it (Marisa, 2026-10-18)
            return _reply_dialog(ev, chat_push.edit_dialog(card, int(p.get("q") or 0), text=revised,
                                                           error=f"Not sent yet: {r['blanks'][0][:60]} is still a blank. Replace it with your words."))
        core.say(convo, person["name"], f"[edited and approved card {qid}: {revised[:200]}]", channel="gchat", meta={"click": True})
        if late is not None and not r.get("ok"):
            report_failed(qid, approve, str(r.get("reason") or ""), card, log)
            return {}
        if not r.get("ok"):
            return _reply_close(ev, f"Not sent: {r.get('reason')}")
        card = cards._read(cards.LIVE / f"{qid}.json") or card
        chat_push.mark_answered(card, {"option": approve, "by": person["key"], "ticket": r.get("ticket"),
                                       "mint_error": r.get("mint_error"), "revised": revised})
        if r.get("mint_error"):
            return _reply_close(ev, f"Recorded, but not minted: {str(r['mint_error'])[:150]}")
        return _reply_close(ev, "Approved with your text. Undo on the card works until it goes out.")
    log(f"ignored click function {fn!r} from {person['key']}")
    return _reply_text(ev, "That button is not known.")


def _tz_offset_min(ev: dict) -> int | None:
    """The owner's time zone offset in minutes from the event (commonEventObject.timeZone.offset is in ms), or None."""
    try:
        off = ((ev.get("commonEventObject") or {}).get("timeZone") or {}).get("offset")
        return int(off) // 60000 if off is not None else None
    except (TypeError, ValueError):
        return None


def later_submit(ev: dict, k: dict, person: dict, qid: str, log=lambda s: None) -> dict:
    """The Send later dialog's submit: the picked time -> cards.answer(send_at=...), the not_before an owner's words
    would mint. The dialog closes with the time in words."""
    import cards
    import chat_push
    import exec_store as es
    core = _core()
    p = k.get("params") or {}
    card = cards._read(cards.LIVE / f"{qid}.json")
    if not card or card.get("status") != "open":
        return _reply_close(ev, "That card is no longer open.")
    ms = str((k.get("form") or {}).get("when") or "")
    try:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ms) / 1000))
        send_at = es.parse_not_before(when)
    except (TypeError, ValueError) as e:
        return _reply_dialog(ev, chat_push.later_dialog(card, int(p.get("q") or 0), tz_offset_min=_tz_offset_min(ev),
                                                        error=f"Pick a time that works: {str(e)[:120] or 'that one is not valid'}."))
    q = (card.get("questions") or [{}])[int(p.get("q") or 0)]
    approve = str(((q.get("exec") or {}).get("approve_label")) or "Approve")
    r = cards.answer(qid, approve, q_index=int(p.get("q") or 0), by=f"gchat:{person['key']}", send_at=send_at)
    log(f"click send-later {qid} by {person['key']} at {send_at}: ok={r.get('ok')} {r.get('reason') or r.get('ticket') or ''}")
    core.say(chat_push_convo(person), person["name"], f"[approved card {qid} for {es.when_text(send_at)}]", channel="gchat", meta={"click": True})
    if not r.get("ok"):
        return _reply_close(ev, f"Not scheduled: {r.get('reason')}")
    card = cards._read(cards.LIVE / f"{qid}.json") or card
    chat_push.mark_answered(card, {"option": approve, "by": person["key"], "ticket": r.get("ticket"), "mint_error": r.get("mint_error")})
    if r.get("mint_error"):
        return _reply_close(ev, f"Recorded, but not set up: {str(r['mint_error'])[:150]}")
    return _reply_close(ev, f"Goes out {es.when_text(send_at)}. Undo on the card works until then.")


# ------------------------------------------------------------------ the Home tab and the registered commands (home_tab.py, 2026-11-04)
def _home_tab():
    import home_tab
    return home_tab


def home_open(ev: dict, h: dict, log=lambda s: None) -> dict:
    core = _core()
    ht = _home_tab()
    person = core.person_for(email=h.get("email", ""), chat_user=h.get("chat_user", ""))
    if not person or not core.can(person, "approve"):
        log(f"home tab opened by non-owner {h.get('email') or h.get('chat_user')!r}")
        return ht.render({"sections": [{"widgets": [{"textParagraph": {"text": "This desk is for the owners."}}]}]}, push=True)
    log(f"home tab opened by {person['key']}")
    return ht.render(ht.desk("home"), push=True)


def home_click(ev: dict, k: dict, person: dict, log=lambda s: None) -> dict:
    ht = _home_tab()
    p = k.get("params") or {}
    do = str(p.get("do") or "desk")
    ctx = str(p.get("ctx") or ("msg" if k.get("msg_name") else "home"))
    card, toast = ht.act(do, dict(p, ctx=ctx), k.get("form") or {}, person)
    log(f"home {do} by {person['key']} ({ctx}): {toast or 'ok'}")
    if do in ("answer", "stop", "show", "switch"):
        try:
            _core().say(chat_push_convo(person), person["name"], f"[{do} from the desk: {toast or do}]", channel="gchat", meta={"click": True})
        except Exception:  # noqa: BLE001
            pass
    if ctx == "home":
        return ht.render(card, push=False, toast=toast)
    return _reply_update(ev, ht.message(card))


def command_event(ev: dict, c: dict, log=lambda s: None, late: float | None = None) -> dict:
    """A registered command: ours are answered on the spot with a card; any other goes to the brain as a message."""
    import gchat
    core = _core()
    ht = _home_tab()
    person = core.person_for(email=c.get("email", ""), chat_user=c.get("chat_user", ""))
    view = ht.command_for(c.get("id"), c.get("text", ""))
    if not view:
        if c.get("text") and c.get("space"):
            gchat.queue_inbound({"email": c.get("email", ""), "chat_user": c.get("chat_user", ""), "name": c.get("name", ""),
                                 "space": c["space"], "dm": bool(c.get("dm")), "thread": c.get("thread", ""), "text": c["text"],
                                 "msg_name": c.get("msg_name", ""), "attachments": []})
            log(f"gchat command {c.get('text')!r} from {c.get('email') or c.get('chat_user')} queued for the brain")
        return {}
    if not person or not core.can(person, "approve"):
        return _reply_text(ev, "That one is for the owners.")
    body = ht.command(view, c.get("args", ""), person)
    log(f"gchat command {view} by {person['key']}" + (" (late: posted by REST)" if late is not None else ""))
    if late is not None and c.get("space"):
        try:
            gchat.post_body(c["space"], body, c.get("thread", ""))
        except Exception as e:  # noqa: BLE001
            log(f"late command post failed: {type(e).__name__}")
        return {}
    if _addon(ev):
        return {"hostAppDataAction": {"chatDataAction": {"createMessageAction": {"message": body}}}}
    return body


def _wake_doer(card: dict | None) -> None:
    """An agent_run card answered: the doer picks the answer up now, not at its next 10-minute tick (2026-11-03).
    Two doors, because an answer travels one of two ways: an answer the doer reads by itself ("I'll sign in myself",
    "No code came") needs a doer tick; an APPROVAL minted an exec ticket, which only drain hands to the doer, so that
    lane is drained once the undo window has passed (2026-11-04: a login saved from chat waited ten minutes)."""
    try:
        ex = ((card or {}).get("questions") or [{}])[0].get("exec") or {}
        if ex.get("kind") != "agent_run":
            return
        sys.path.insert(0, str(HERE.parent / "doer"))
        import doer
        doer.wake()
    except Exception:  # noqa: BLE001
        pass
    try:
        import drain
        import exec_store
        drain.kick(str(exec_store.KIND_LANES.get("agent_run") or "doer"))
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ the live job card (doer/livecard.py, 2026-11-03)
# Its buttons carry {"job": <ticket id>, "option": "Stop" | "Show on screen"} through the same card_answer relay function
# as every other card, so the Apps Script relay needs no new function; the tap is answered with the redrawn card.
def job_click(ev: dict, k: dict, person: dict, log=lambda s: None) -> dict:
    p = k.get("params") or {}
    tid, option = str(p.get("job") or ""), str(p.get("option") or "")
    if not re.fullmatch(r"[0-9a-f]{16}", tid):
        return _reply_text(ev, "That job is not known.")
    try:
        sys.path.insert(0, str(HERE.parent / "doer"))
        import livecard
        if option == "Do later":                 # a page left open for the owner (doer/leftopen.py, 2026-11-08)
            import leftopen
            body = leftopen.later(tid, person)
        else:
            body = livecard.action(tid, option, person)
    except Exception as e:  # noqa: BLE001
        log(f"job click {tid} {option!r} crashed: {type(e).__name__}: {str(e)[:160]}")
        body = {}
    log(f"job click {tid} {option!r} by {person['key']}: {'redrawn' if body else 'no live card'}")
    try:
        _core().say(chat_push_convo(person), person["name"], f"[tapped \"{option}\" on the live card of job {tid}]", channel="gchat", meta={"click": True})
    except Exception:  # noqa: BLE001
        pass
    if not body:
        return _reply_text(ev, "That job is not running any more.")
    return _reply_update(ev, body)


# ------------------------------------------------------------------ the vault dialog (sage-core, 2026-10-28)
# "Enter login" opens a dialog; its submit carries site / username / password / totp as formInputs, which go to
# doer/vault.py add-stdin in the keyring interpreter. The values live in this process for the length of the
# subprocess call and are never logged, said or stored anywhere but the vault.
def _vault_add(fields: dict) -> str:
    """-> the vault script's one line (ADDED ... | VAULT_ERROR ...). Replaceable in tests."""
    import subprocess
    from alib import VENV_PY, ROOT as _ROOT
    py = str(VENV_PY) if VENV_PY.exists() else sys.executable
    r = subprocess.run([py, str(_ROOT / "doer" / "vault.py"), "add-stdin"], input=json.dumps(fields), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=90, cwd=str(_ROOT.parent))
    out = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    return out[-1] if out else f"VAULT_ERROR exit {r.returncode}"


def vault_click(ev: dict, k: dict, person: dict, log=lambda s: None) -> dict:
    import chat_push
    core = _core()
    fn, p, form = k.get("function", ""), k.get("params") or {}, k.get("form") or {}
    site = str(p.get("site") or "")
    qid = str(p.get("qid") or "")                 # set when the dialog came from a sign-in card (report.py login)
    if not QID.fullmatch(qid):
        qid = ""
    rid = str(p.get("rid") or "")                 # set when it came from a setup job's card (chat_forms.job_card): the job runs again
    if not re.fullmatch(r"[\w-]{2,64}", rid):
        rid = ""
    if fn == "vault_add":
        log(f"vault dialog opened by {person['key']} for {site or 'an unnamed site'}" + (f" (card {qid})" if qid else "") + (f" (job {rid})" if rid else ""))
        return _reply_dialog(ev, chat_push.vault_dialog(site, qid=qid, rid=rid))
    fields = {"site": str(form.get("site") or site).strip(), "username": str(form.get("username") or "").strip(),
              "password": str(form.get("password") or ""), "totp": str(form.get("totp") or "")}
    if not (fields["site"] and fields["username"] and fields["password"]):
        return _reply_dialog(ev, chat_push.vault_dialog(fields["site"], error="Site, username and password are all needed.", username=fields["username"], qid=qid, rid=rid))
    try:
        line = _vault_add(fields)
    except Exception as e:  # noqa: BLE001
        line = f"VAULT_ERROR {type(e).__name__}"
    finally:
        fields["password"] = fields["totp"] = ""
    if not line.startswith("ADDED"):
        log(f"vault add by {person['key']} failed: {line[:160]}")
        return _reply_dialog(ev, chat_push.vault_dialog(fields["site"], error=line.replace("VAULT_ERROR", "Not saved:").strip(), username=fields["username"], qid=qid, rid=rid))
    parts = line.split()
    host, user = (parts[2] if len(parts) > 2 else fields["site"]), (parts[3] if len(parts) > 3 else fields["username"])
    # the sign-in name never goes into the log or the transcript (2026-11-09: it was written to both in plain text)
    log(f"vault add by {person['key']}: {host} (totp={'yes' if 'totp=yes' in line else 'no'})")
    try:
        core.say(chat_push.agent_convo(person["key"]), person["name"], f"[saved a login for {host} in the vault]", channel="gchat", meta={"click": True})
    except Exception:  # noqa: BLE001
        pass
    if rid == "startup":
        # the start-up sequence stopped at a sign-in: with the login saved it carries on from that job by itself
        import gchat
        import startup
        name = str(k.get("msg_name") or "")

        def on_update(body: dict) -> None:
            if name:
                try:
                    gchat.patch_body(name, body)
                except Exception as e:  # noqa: BLE001
                    log(f"start-up card edit failed: {type(e).__name__}: {str(e)[:120]}")

        started = _run_startup(person["key"], on_update)
        log(f"vault add by {person['key']} carries the start-up sequence on: {started}")
        return _reply_close(ev, f"Saved the {host} login. Carrying on with the setup now; the card shows each step." if started
                            else f"Saved the {host} login for {user}.")
    if rid:
        # a setup job stopped at this site's sign-in: with the login saved it runs again by itself, and its card follows
        import chat_forms
        import gchat
        import setup_actions
        name = str(k.get("msg_name") or "")

        def done(res: dict) -> None:
            log(f"job {rid} after the vault login for {person['key']}: {res.get('state') or ('ok' if res.get('ok') else 'failed')}: {str(res.get('message') or '')[:160]}")
            if name:
                try:
                    gchat.patch_body(name, chat_forms.job_card(rid, result=res))
                except Exception as e:  # noqa: BLE001
                    log(f"job {rid} result card edit failed: {type(e).__name__}: {str(e)[:120]}")

        if name:
            try:                                      # the working card first, so the result (minutes away) is the last edit
                gchat.patch_body(name, chat_forms.job_card(rid, running=True))
            except Exception as e:  # noqa: BLE001
                log(f"job {rid} working card edit failed: {type(e).__name__}: {str(e)[:120]}")
        started = _run_job(setup_actions, rid, person["key"], done)
        log(f"vault add by {person['key']} restarts job {rid}: {started}")
        what = str((chat_forms.spec(rid) or {}).get("name") or rid)
        return _reply_close(ev, f"Saved the {host} login. Running {what} again now; its card says how it went." if started
                            else f"Saved the {host} login for {user}. Tap the job's button to run it again.")
    if qid:
        # the sign-in card: the saved login IS its approval; the job carries on from where it stopped (doer.run_login_resume)
        import cards
        card = cards._read(cards.LIVE / f"{qid}.json")
        q = ((card or {}).get("questions") or [{}])[0]
        approve = str(((q.get("exec") or {}).get("approve_label")) or "Enter login")
        r = cards.answer(qid, approve, by=f"gchat:{person['key']}") if card else {"ok": False, "reason": "that card is no longer open"}
        log(f"vault add answered sign-in card {qid}: ok={r.get('ok')} {r.get('reason') or r.get('ticket') or ''}")
        if r.get("ok") and card:
            card = cards._read(cards.LIVE / f"{qid}.json") or card
            chat_push.mark_answered(card, {"option": approve, "by": person["key"], "ticket": r.get("ticket"), "mint_error": r.get("mint_error")})
            _wake_doer(card)
            return _reply_close(ev, f"Saved the {host} login. The job picks back up in under a minute; this thread says how it went.")
        return _reply_close(ev, f"Saved the {host} login for {user} in the vault, but the job did not restart ({r.get('reason') or r.get('mint_error')}). Ask me here to try it again.")
    return _reply_close(ev, f"Saved the {host} login for {user} in the vault. Nothing was kept in chat.")


# ------------------------------------------------------------------ data-entry forms (chat_forms.py, 2026-11-01)
# the owner: "Make all data entry for anything go through google chat never mission control." A connection's card has
# one button that opens a dialog; the submit stores the values on this PC and closes the dialog with one line. What
# comes next (Plaid: the bank sign-in card) is posted as a fresh message in the same thread. Values are never logged.
def form_click(ev: dict, k: dict, person: dict, log=lambda s: None, late: float | None = None) -> dict:
    import chat_forms
    import gchat
    core = _core()
    fn, p, form = k.get("function", ""), k.get("params") or {}, k.get("form") or {}
    rid = str(p.get("rid") or "")
    if rid == "startup":                          # the start-up sequence's own card (startup.py)
        return startup_click(ev, k, person, log, late=late)
    if not chat_forms.spec(rid):
        return _reply_text(ev, "That form is not known any more.")
    thread = gchat.thread_of(k.get("msg_name", "")) if k.get("msg_name") else ""
    if (chat_forms.spec(rid) or {}).get("store") == "job" and (fn == "form_open" or p.get("do") == "run"):
        return job_run_click(ev, k, person, rid, log, late=late)
    if fn == "form_open":
        log(f"form {rid} opened by {person['key']}")
        return _reply_dialog(ev, chat_forms.dialog(rid, params=({"from": str(p["from"])} if p.get("from") else None)))
    if fn == "form_action" and (chat_forms.spec(rid) or {}).get("store") == "job_form":
        return job_form_click(ev, k, person, rid, log, late=late)
    if fn == "form_action":
        if rid == "bank-plaid":
            log(f"form {rid} action {p.get('do')} by {person['key']}")
            if p.get("do") == "relink" and p.get("item"):
                sys.path.insert(0, str(HERE / "money"))
                import plaid_wizard
                r = plaid_wizard.hosted_start(update_item=str(p["item"]))
                if not r.get("ok"):
                    return _reply_text(ev, r.get("message") or "Plaid would not start the re-sign-in.")
                body = {"text": "Re-sign-in through Plaid.", "cardsV2": [{"cardId": "plaid-relink", "card": {
                    "header": {"title": "Sign in to your bank again", "subtitle": "Plaid's sign-in page, read-only"},
                    "sections": [{"widgets": [{"buttonList": {"buttons": [chat_forms._link_btn("Sign in again", r["url"])]}}]}]}}]}
                return _reply_update(ev, body)
            return _reply_update(ev, chat_forms.plaid_connect_card())
        return _reply_text(ev, "That button is not known.")
    if (chat_forms.spec(rid) or {}).get("store") == "job_form":
        return job_form_click(ev, k, person, rid, log, late=late)
    values = {n: str(form.get(n) or "") for n, _ in (chat_forms.spec(rid) or {}).get("fields", [])}
    r = chat_forms.submit(rid, form)
    log(f"form {rid} submitted by {person['key']}: ok={r.get('ok')} (values not logged)")
    if not r.get("ok"):
        keep = {n: v for n, v in values.items() if n in ("client_id",)}   # never echo a secret back into the dialog
        return _reply_dialog(ev, chat_forms.dialog(rid, error=r.get("message") or "Not stored.", values=keep))
    try:
        core.say(chat_push_convo(person), person["name"], f"[entered {chat_forms.spec(rid)['title']} for {chat_forms.spec(rid)['name']} in a Chat form]",
                 channel="gchat", meta={"click": True})
    except Exception:  # noqa: BLE001
        pass
    if r.get("next") and str(k.get("space") or "").startswith("spaces/"):
        try:
            gchat.post_body(k["space"], r["next"], thread)
        except Exception as e:  # noqa: BLE001
            log(f"form {rid} follow-up card not posted: {type(e).__name__}: {str(e)[:120]}")
    return _reply_close(ev, r.get("message") or "Stored.")


def job_run_click(ev: dict, k: dict, person: dict, rid: str, log=lambda s: None, late: float | None = None) -> dict:
    """A job row's button (chat_forms.job_card, 2026-11-04): the job runs on a thread (minutes), the tap is answered now
    with the card in its working state, and the card is edited by REST with the result when the job ends. A tap the
    relay held (late): Chat already shows the relay's own "got it" card, so the working card goes up by REST too."""
    import chat_forms
    import gchat
    import setup_actions
    core = _core()
    name = str(k.get("msg_name") or "")
    if late is not None and name:
        try:
            gchat.patch_body(name, chat_forms.job_card(rid, running=True))
        except Exception as e:  # noqa: BLE001
            log(f"job {rid} working card edit failed: {type(e).__name__}: {str(e)[:120]}")

    def done(res: dict) -> None:
        log(f"job {rid} for {person['key']}: {res.get('state') or ('ok' if res.get('ok') else 'failed')}: {str(res.get('message') or '')[:160]}")
        try:
            core.say(chat_push_convo(person), person["name"], f"[{rid}: {str(res.get('message') or '')[:200]}]", channel="gchat", meta={"click": True})
        except Exception:  # noqa: BLE001
            pass
        if name:
            try:
                gchat.patch_body(name, chat_forms.job_card(rid, result=res))
            except Exception as e:  # noqa: BLE001
                log(f"job {rid} result card edit failed: {type(e).__name__}: {str(e)[:120]}")

    started = _run_job(setup_actions, rid, person["key"], done)
    log(f"job {rid} started by {person['key']}: {started}")
    if not started:
        return _reply_text(ev, "That job is not known any more.")
    return _reply_update(ev, chat_forms.job_card(rid, running=True))


def _run_job(setup_actions, rid: str, by: str, done) -> bool:
    """Replaceable in tests."""
    return setup_actions.run_job_async(rid, by, done)


def _job_form_call(spec: dict, what: str, values: dict, by: str) -> dict:
    """module.func(**values, by=) for a submit, module.on_action(do, by=) for a result card's button. Replaceable in tests."""
    import importlib
    mod = importlib.import_module(str(spec.get("module") or ""))
    if what == "submit":
        return getattr(mod, str(spec.get("func") or "run"))(**values, by=by)
    return mod.on_action(what, by=by)


def job_form_click(ev: dict, k: dict, person: dict, rid: str, log=lambda s: None, late: float | None = None) -> dict:
    """A job that needs something typed first (setup kind job_form, 2026-11-04). The form's submit hands the values to the
    row's module on a thread and closes the dialog at once; a button on the result card (form_action do=<action>) runs the
    module's on_action the same way. Either way the card that carried the button is edited with the result. The values
    are never logged, said or kept by this module."""
    import threading
    import chat_forms
    import gchat
    spec = chat_forms.spec(rid) or {}
    fn, p, form = k.get("function", ""), k.get("params") or {}, k.get("form") or {}
    name = str(k.get("msg_name") or "")
    if fn == "form_submit":
        values = {n: str(form.get(n) or "").strip() for n, _ in spec.get("fields", [])}
        if not all(values.values()):
            return _reply_dialog(ev, chat_forms.dialog(rid, error="Every box is needed.", params={"from": str(p.get("from") or "")}))
        what = "submit"
    else:
        values, what = {}, str(p.get("do") or "")

    def work():
        try:
            res = _job_form_call(spec, what, values, person["key"])
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "state": "failed", "message": f"{type(e).__name__}: {str(e)[:160]}"}
        log(f"job form {rid} {what} for {person['key']}: {res.get('state') or ('ok' if res.get('ok') else 'failed')} (values not logged)")
        if name:
            try:
                body = chat_forms.job_card(rid, result=res)
                if res.get("ok") and str(p.get("from") or "") == "startup":
                    import startup
                    body = startup.card()                # back to the sequence's own card, with this step done
                gchat.patch_body(name, body)
            except Exception as e:  # noqa: BLE001
                log(f"job form {rid} result card edit failed: {type(e).__name__}: {str(e)[:120]}")

    threading.Thread(target=work, daemon=True, name=f"jobform-{rid}").start()
    try:
        _core().say(chat_push_convo(person), person["name"], f"[{spec.get('title') or rid}: {'filled in the form' if what == 'submit' else what}]", channel="gchat", meta={"click": True})
    except Exception:  # noqa: BLE001
        pass
    if fn == "form_submit":
        return _reply_close(ev, "Got it. Working on it now; the card says how it went.")
    if late is not None:
        return {}
    return _reply_update(ev, chat_forms.job_card(rid, running=True))


def _run_startup(by: str, on_update) -> bool:
    """The whole start-up sequence on a thread; `on_update(card body)` after each step. Replaceable in tests."""
    import startup
    return startup.run_async(by, on_update)


def startup_click(ev: dict, k: dict, person: dict, log=lambda s: None, late: float | None = None) -> dict:
    """"Set up everything" on the start-up card: every job still to do runs in order on this PC and the card redraws
    after each step (REST edits of the same message). The tap itself is answered at once with the working card."""
    import gchat
    import startup
    name = str(k.get("msg_name") or "")

    def on_update(body: dict) -> None:
        if name:
            try:
                gchat.patch_body(name, body)
            except Exception as e:  # noqa: BLE001
                log(f"start-up card edit failed: {type(e).__name__}: {str(e)[:120]}")

    started = _run_startup(person["key"], on_update)
    log(f"start-up sequence started by {person['key']}: {started}")
    try:
        _core().say(chat_push_convo(person), person["name"], "[tapped Set up everything on the start-up card]", channel="gchat", meta={"click": True})
    except Exception:  # noqa: BLE001
        pass
    if not started:
        return _reply_update(ev, startup.card())
    todo = startup.pending(fresh=False)
    body = startup.card(running=(todo[0]["rid"] if todo else ""), st=startup.status(fresh=False))
    if late is not None:
        on_update(body)
        return {}
    return _reply_update(ev, body)


def chat_push_convo(person: dict) -> str:
    import chat_push
    return chat_push.agent_convo(person["key"])


def audiences() -> list:
    import gchat
    c = gchat.cfg()
    return [str(c.get("project_number") or ""), str(c.get("endpoint_url") or "")]


def relay_secret() -> str:
    import gchat
    return str(gchat.cfg().get("relay_secret") or "")


if __name__ == "__main__":
    print(json.dumps({"audiences": audiences(), "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}))
