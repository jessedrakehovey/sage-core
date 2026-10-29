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
        m = gchat.parse_event(ev)
        if m:
            gchat.queue_inbound(m)
            log(f"gchat http message from {m.get('email') or m.get('chat_user')} queued")
            return {}
        a = gchat.parse_added(ev)
        if a:                       # an owner added Sage: their DM is recorded, cards reach them from now on
            who = gchat.record_added(a)
            log(f"gchat app added in {'a DM' if a.get('dm') else 'a space'} by {who or 'a non-owner'}")
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


def _answer_soon(qid: str, option: str, q_index: int, person: dict, before, log) -> dict:
    """cards.answer on a worker thread. Returns {"r": result} when it finished within TAP_WAIT_S,
    else {} (the worker reports any failure itself when it is done)."""
    import threading
    import cards
    box: dict = {}
    replied = threading.Event()

    def work():
        try:
            r = cards.answer(qid, option, q_index=q_index, by=f"gchat:{person['key']}")
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


def _late_answer(qid: str, option: str, q_index: int, person: dict, before, late: float, log) -> dict:
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
    r = cards.answer(qid, option, q_index=q_index, by=f"gchat:{person['key']}")
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
    (the owner 2026-10-25, example's email: the error at 5:36, the send at 5:40). So: the card as it stands."""
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
    if fn in ("vault_add", "vault_add_submit"):
        return vault_click(ev, k, person, log)
    qid = str(p.get("qid") or "")
    if not QID.fullmatch(qid):
        return _reply_text(ev, "That card id is not valid.")
    convo = chat_push.agent_convo(person["key"])
    if fn == "card_answer":
        option = str(p.get("option") or "")
        q_index = int(p.get("q") or 0)
        before = cards._read(cards.LIVE / f"{qid}.json")
        if late is not None:
            return _late_answer(qid, option, q_index, person, before, late, log)
        box = _answer_soon(qid, option, q_index, person, before, log)
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
        log(f"click edit {qid} by {person['key']}: dialog opened")
        return _reply_dialog(ev, chat_push.edit_dialog(card, int(p.get("q") or 0)))
    if fn == "card_edit_submit":
        revised = str((k.get("form") or {}).get("body") or "").strip()
        if not revised:
            return _reply_close(ev, "Nothing sent: the message was empty.")
        card = cards._read(cards.LIVE / f"{qid}.json")
        q = ((card or {}).get("questions") or [{}])[int(p.get("q") or 0)] if card else {}
        approve = str(((q.get("exec") or {}).get("approve_label")) or "Approve")
        r = cards.answer(qid, approve, revised=revised, q_index=int(p.get("q") or 0), by=f"gchat:{person['key']}")
        log(f"click edit-submit {qid} by {person['key']}: ok={r.get('ok')} {r.get('reason') or r.get('ticket') or ''} revised={len(revised)} chars")
        if r.get("blanks") and card and late is None:
            # a blank is still in the text: keep the dialog open with his words in it (example, 2026-10-18)
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
    if fn == "vault_add":
        log(f"vault dialog opened by {person['key']} for {site or 'an unnamed site'}")
        return _reply_dialog(ev, chat_push.vault_dialog(site))
    fields = {"site": str(form.get("site") or site).strip(), "username": str(form.get("username") or "").strip(),
              "password": str(form.get("password") or ""), "totp": str(form.get("totp") or "")}
    if not (fields["site"] and fields["username"] and fields["password"]):
        return _reply_dialog(ev, chat_push.vault_dialog(fields["site"], error="Site, username and password are all needed.", username=fields["username"]))
    try:
        line = _vault_add(fields)
    except Exception as e:  # noqa: BLE001
        line = f"VAULT_ERROR {type(e).__name__}"
    finally:
        fields["password"] = fields["totp"] = ""
    if not line.startswith("ADDED"):
        log(f"vault add by {person['key']} failed: {line[:160]}")
        return _reply_dialog(ev, chat_push.vault_dialog(fields["site"], error=line.replace("VAULT_ERROR", "Not saved:").strip(), username=fields["username"]))
    parts = line.split()
    host, user = (parts[2] if len(parts) > 2 else fields["site"]), (parts[3] if len(parts) > 3 else fields["username"])
    log(f"vault add by {person['key']}: {host} for {user} (totp={'yes' if 'totp=yes' in line else 'no'})")
    try:
        core.say(chat_push.agent_convo(person["key"]), person["name"], f"[saved a login for {host} ({user}) in the vault]", channel="gchat", meta={"click": True})
    except Exception:  # noqa: BLE001
        pass
    return _reply_close(ev, f"Saved the {host} login for {user} in the vault. Nothing was kept in chat.")


def audiences() -> list:
    import gchat
    c = gchat.cfg()
    return [str(c.get("project_number") or ""), str(c.get("endpoint_url") or "")]


def relay_secret() -> str:
    import gchat
    return str(gchat.cfg().get("relay_secret") or "")


if __name__ == "__main__":
    print(json.dumps({"audiences": audiences(), "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}))
