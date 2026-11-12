"""chat_forms.py - every bit of data entry happens in Google Chat, never on Mission Control (the owner, 2026-11-01:
"i had to use a mission control app for collecting plaid keys. Make all data entry for anything go through google
chat never mission control.").

What Chat carries is a CARD with a button; the button opens a Chat DIALOG whose fields go to the app as formInputs,
never into the conversation (the same shape as the vault login, vault_chat.py). The submit is answered on the spot
by gchat_events.click (form_open / form_submit / form_action) and stored on this PC: Windows Credential Manager, a
User environment variable, or the connection's own claim. Nothing typed in a dialog is logged, said or kept in chat.

Which connections get a form is read from connections.json + setup_actions.recipe, so a new row needs no code here:
  bank-plaid             the two Plaid keys, then Plaid's own Hosted Link page (secure.plaid.com) for the bank sign-in
  setup_creds mail X     the mailbox's Google app password   -> keyring imap:X
  setup_creds calendar X the calendar's secret iCal address   -> keyring ics:X
  setup_creds money simplefin  a SimpleFIN setup token        -> claimed (connect.claim), never kept
  kind env NAME          a token for the crons                -> User environment variable NAME

Who posts the card: bridge.py when an owner asks to set one up (offer(), before any model turn), and actions.py
when Sage's own `blocked` deed names a connection (post_for). Zero LLM. Org-free: names come from the registry.

  python assistant/scripts/chat_forms.py list              # every connection that has a Chat form
  python assistant/scripts/chat_forms.py match "<text>"    # which form a message would bring (nothing posted)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_STARTUP = (r"\b(?:finish|complete|continue|resume|do|run)\b.{0,20}\b(?:set(?:ting)?[ -]?up|setup|start[ -]?up)\b(?!.{0,12}\b(?:for|of|the \w+ (?:tab|drive|login))\b)"
            r"|\bset (?:everything|it all|the rest) up\b|\bget (?:me |us )?(?:all )?set up\b|\bstart[ -]?up (?:card|sequence|setup)\b")
_CONNECT = r"\b(set ?up|connect|re-?connect|link|re-?link|hook ?up)\b"
_VERB = r"\b(fix|renew|update|enter|add|change|new|paste|reset)\b"
_SECRET = r"\b(token|keys?|password|app password|credentials?|secret|ical|connection|feed|login)\b"
_GENERIC = {"feed", "bank", "read", "only", "via", "the", "and", "for", "account", "accounts", "personal", "business",
            "mail", "calendar", "token", "with", "from"}


# ------------------------------------------------------------------ which connection takes what
def _registry() -> list:
    import connect
    return connect._rows()


def spec(rid: str) -> dict | None:
    """-> {"rid", "name", "store", "fields", "title", "intro", "link"} for a connection whose setup is typing
    something in; None when it is a sign-in window, a console job or plain text."""
    import connect
    import setup_actions
    r = connect.row(rid)
    if not r:
        return None
    name = str(r.get("name") or rid)
    if rid == "bank-plaid":
        return {"rid": rid, "name": name, "store": "plaid", "title": "Plaid keys",
                "intro": "Paste the client_id and the Production secret from Plaid's Keys page. They go straight into the PC's "
                         "Credential Manager and are never posted in this chat.",
                "link": ("Open Plaid Keys page", "https://dashboard.plaid.com/developers/keys"),
                "fields": [("client_id", "client_id"), ("secret", "Secret (the Production one)")]}
    rc = setup_actions.recipe(r)
    if rc.get("kind") == "job_form":
        # a job that needs something typed first (the Control PC PIN, 2026-11-04): the dialog's values go to module.func on
        # this PC and are never stored by the form itself
        return {"rid": rid, "name": name, "store": "job_form", "title": str(rc.get("label") or "Set it up"), "intro": str(rc.get("how") or ""),
                "link": None, "fields": [(str(n), str(label)) for n, label in (rc.get("fields") or [])], "module": str(rc.get("module") or ""),
                "func": str(rc.get("func") or ""), "watch": False}
    if rc.get("kind") == "job":
        # a job the agent runs itself from a Chat button (chat-home: the Chat app's Google-side setup, 2026-11-04)
        return {"rid": rid, "name": name, "store": "job", "title": str(rc.get("label") or "Run it"), "intro": str(rc.get("how") or ""),
                "link": None, "fields": [], "watch": bool(rc.get("watch"))}
    argv = [str(a) for a in (rc.get("argv") or [])]
    also = str(rc.get("also_url") or "")
    link = ("Open page", also) if also.startswith("https://") else None
    if "setup_creds.py" in " ".join(argv):
        i = next(n for n, a in enumerate(argv) if a.endswith("setup_creds.py"))
        what = argv[i + 1:i + 3]
        if len(what) == 2 and not what[1]:
            return None                # a row with no account named: nothing to store under
        if what[:1] == ["mail"] and len(what) == 2:
            return {"rid": rid, "name": name, "store": f"keyring:imap:{what[1]}", "title": "Google app password",
                    "intro": "Make one at myaccount.google.com/apppasswords, then paste it here. It goes straight into the PC's "
                             "Credential Manager, never into this chat.",
                    "link": ("Open app passwords", "https://myaccount.google.com/apppasswords"), "fields": [("value", "App password")]}
        if what[:1] == ["calendar"] and len(what) == 2:
            return {"rid": rid, "name": name, "store": f"keyring:ics:{what[1]}", "title": "Calendar secret address",
                    "intro": "Google Calendar > Settings > the calendar > Secret address in iCal format. Paste it here; it goes "
                             "straight into the PC's Credential Manager.", "link": link,
                    "fields": [("value", "Secret iCal address")]}
        if what == ["money", "simplefin"]:
            return {"rid": rid, "name": name, "store": "simplefin", "title": "SimpleFIN setup token",
                    "intro": "In SimpleFIN Bridge tap New App and copy the setup token. It works once: I claim the read-only "
                             "access with it and keep only that.", "link": link, "fields": [("value", "Setup token")]}
    if rc.get("kind") == "env" and rc.get("name"):
        return {"rid": rid, "name": name, "store": f"env:{rc['name']}", "title": name if name.lower().endswith("token") else f"{name} token",
                "intro": "Paste it here. It is stored on the PC for the jobs that use it, never in this chat.", "link": link,
                "fields": [("value", "Token")]}
    return None


def rows() -> list:
    out = []
    for r in _registry():
        try:
            s = spec(str(r.get("id") or ""))
        except Exception:  # noqa: BLE001
            s = None
        if s:
            out.append(s)
    return out


def match(text: str) -> str:
    """The connection id a setup ask is about, '' when the message is not one. The registry's own chat
    ask_words first (connect.asked), then any form row whose distinctive name words sit next to a setup verb."""
    import connect
    t = str(text or "")
    if re.search(_STARTUP, t, re.I):
        return "startup"                # "finish setting up" / "set everything up": the start-up sequence's one card
    rid = connect.asked(t)
    if rid:
        to = connect._deferred(rid, t)
        rid = to or rid
        return rid if spec(rid) else ""
    for s in rows():                   # a job row may name its own chat phrases (setup.words, a regex): "I forgot my Control PC PIN"
        if s.get("store") in ("job", "job_form"):
            pat = str(((connect.row(s["rid"]) or {}).get("setup") or {}).get("words") or "")
            try:
                if pat and re.search(pat, t, re.I):
                    return s["rid"]
            except re.error:
                pass
    if re.search(_CONNECT, t, re.I) or re.search(_VERB, t, re.I) or re.search(r"\b(run|redo|again)\b", t, re.I):
        for s in rows():               # a job row ("set up the home tab", "redo the chat commands") needs no secret word
            if s.get("store") != "job":
                continue
            words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", s["name"]) if w.lower() not in _GENERIC]
            if words and any(re.search(rf"\b{re.escape(w)}\b", t, re.I) for w in words):
                return s["rid"]
    if not ((re.search(_CONNECT, t, re.I) or re.search(_VERB, t, re.I)) and re.search(_SECRET, t, re.I)):
        return ""                      # "add a form question" / "link Sam to the doc" are not setup asks; "fix the form token" is
    for s in rows():
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", s["name"]) if w.lower() not in _GENERIC]
        if words and any(re.search(rf"\b{re.escape(w)}\b", t, re.I) for w in words):
            return s["rid"]
    return ""


# ------------------------------------------------------------------ the Chat bodies
def _btn(label: str, fn: str, params: dict, dialog: bool = False, filled: bool = False) -> dict:
    import chat_push
    return chat_push._button(label, fn, params, interaction="OPEN_DIALOG" if dialog else "", filled=filled)


def _link_btn(label: str, url: str) -> dict:
    return {"text": label[:40], "onClick": {"openLink": {"url": url}}}


def card(rid: str) -> dict | None:
    """The message for a connection: Plaid with keys stored is the bank sign-in card, everything else the form card."""
    if rid == "startup":
        import startup
        return startup.card()
    s = spec(rid)
    if not s:
        return None
    if s["store"] == "plaid" and _plaid_keys():
        return plaid_connect_card()
    if s["store"] in ("job", "job_form"):
        return job_card(rid, s)
    buttons = [_btn(f"Enter {s['title'][0].lower() + s['title'][1:]}" if s["store"] != "plaid" else "Enter Plaid keys",
                    "form_open", {"rid": rid}, dialog=True, filled=True)]
    if s.get("link"):
        buttons.append(_link_btn(*s["link"]))
    return {"text": f"{s['name']}: tap the button to type it in.",
            "cardsV2": [{"cardId": f"form-{rid}", "card": {
                "header": {"title": s["name"][:200], "subtitle": "typed in a form on top of Chat, never posted here"},
                "sections": [{"widgets": [{"textParagraph": {"text": s["intro"]}}, {"buttonList": {"buttons": buttons}}]}]}}]}


def dialog(rid: str, error: str = "", values: dict | None = None, params: dict | None = None) -> dict:
    import html
    s = spec(rid) or {"title": "Form", "intro": "", "fields": []}
    top = []
    if error:
        top.append({"textParagraph": {"text": f'<font color="#B3261E"><b>{html.escape(error[:300])}</b></font>'}})
    top.append({"textParagraph": {"text": s["intro"]}})
    vals = values or {}
    fields = [{"textInput": {"name": n, "label": label, "type": "SINGLE_LINE", "value": str(vals.get(n) or "")[:300]}}
              for n, label in s["fields"]]
    return {"header": {"title": s["title"], "subtitle": str(s.get("name") or "")[:200]},
            "sections": [{"widgets": top + fields + [
                {"buttonList": {"buttons": [_btn("Save", "form_submit", {"rid": rid, **{k: str(v) for k, v in (params or {}).items()}}, filled=True)]}}]}]}


def plaid_connect_card(note: str = "") -> dict:
    """Plaid's own sign-in page for the next bank, minted now (good for four hours)."""
    sys.path.insert(0, str(HERE / "money"))
    import plaid_wizard
    st = plaid_wizard.status()
    linked = ", ".join(i.get("institution") or "a bank" for i in st.get("items") or [])
    r = plaid_wizard.hosted_start()
    lines = [note] if note else []
    if linked:
        lines.append(f"Linked so far: {linked}.")
    if not r.get("ok"):
        lines.append(r.get("message") or "Plaid would not start the sign-in.")
        widgets = [{"textParagraph": {"text": " ".join(lines)}},
                   {"buttonList": {"buttons": [_btn("Try again", "form_action", {"rid": "bank-plaid", "do": "connect"}, filled=True)]}}]
    else:
        lines.append("Tap Sign in to your bank: Plaid's own page opens, pick your bank, sign in there and choose the "
                     "accounts. Read-only; nobody can move money with it. I'll say here what I see the moment it's linked.")
        widgets = [{"textParagraph": {"text": " ".join(lines)}},
                   {"buttonList": {"buttons": [_link_btn("Sign in to your bank", r["url"]),
                                               _btn("Another bank", "form_action", {"rid": "bank-plaid", "do": "connect"})]}}]
        relink = st.get("relink") if any(i.get("error") for i in st.get("items") or []) else ""
        if relink:
            widgets[-1]["buttonList"]["buttons"].append(_btn("Re-sign-in", "form_action", {"rid": "bank-plaid", "do": "relink", "item": relink}))
    return {"text": "Connect a bank through Plaid.",
            "cardsV2": [{"cardId": "plaid-connect", "card": {
                "header": {"title": "Connect a bank", "subtitle": "Plaid's sign-in page, read-only"},
                "sections": [{"widgets": widgets}]}}]}


# ------------------------------------------------------------------ storing what was typed
def _plaid_keys() -> bool:
    sys.path.insert(0, str(HERE / "money"))
    import plaid
    cid, sec = plaid.creds()
    return bool(cid and sec)


def _keyring_set(key: str, value: str) -> None:
    import keyring
    from alib import KEYRING_SERVICE
    keyring.set_password(KEYRING_SERVICE, key, value)


def _env_set(name: str, value: str) -> None:
    if os.name == "nt":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        try:   # tell running apps the environment changed
            import ctypes
            ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 2, 5000, None)
        except Exception:  # noqa: BLE001
            pass
    os.environ[name] = value


def submit(rid: str, form: dict) -> dict:
    """-> {"ok", "message", "next": a Chat message body to post after the dialog closes, or None}. Never logs a value."""
    s = spec(rid)
    if not s:
        return {"ok": False, "message": "That form is not known any more."}
    vals = {n: str(form.get(n) or "").strip() for n, _ in s["fields"]}
    if not all(vals.values()):
        return {"ok": False, "message": "Every box is needed."}
    store = s["store"]
    try:
        if store == "plaid":
            sys.path.insert(0, str(HERE / "money"))
            import plaid_wizard
            r = plaid_wizard.save_keys(vals["client_id"], vals["secret"])
            if not r.get("ok"):
                return {"ok": False, "message": r.get("message") or "Plaid did not accept those keys."}
            return {"ok": True, "message": "Plaid keys stored and accepted.",
                    "next": plaid_connect_card("Your Plaid keys are stored and Plaid accepts them.")}
        v = vals["value"]
        if store.startswith("keyring:"):
            key = store.split(":", 1)[1]
            if key.startswith("imap:"):
                v = v.replace(" ", "")
            if key.startswith("ics:") and not v.startswith("https://"):
                return {"ok": False, "message": "That doesn't look like the secret address (it starts with https://)."}
            _keyring_set(key, v)
            return {"ok": True, "message": f"Stored for {s['name']}. It's checked again on the next pass."}
        if store.startswith("env:"):
            _env_set(store.split(":", 1)[1], v)
            return {"ok": True, "message": f"Stored for {s['name']}. The jobs pick it up on their next run."}
        if store == "simplefin":
            import connect
            line = connect.claim(rid, v)
            connect.scrub(v)
            ok = not line.startswith(("That token didn't take", "I recognised"))
            return {"ok": ok, "message": line}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Not stored: {type(e).__name__}."}
    return {"ok": False, "message": "That form has nowhere to store it yet."}


# ------------------------------------------------------------------ posting
def _remote_desktop() -> str:
    try:
        import home_tab
        return home_tab.remote_desktop()
    except Exception:  # noqa: BLE001
        return "https://remotedesktop.google.com/access"


def job_card(rid: str, s: dict | None = None, result: dict | None = None, running: bool = False) -> dict:
    """The card for a job row: one button that runs it, Control PC when the job may need the owner at the PC, and
    the last result's line once there is one. `running`: the tap was taken and the card redraws when the job ends."""
    s = s or spec(rid) or {}
    name, title = str(s.get("name") or rid), str(s.get("title") or "Run it")
    widgets = [{"textParagraph": {"text": str(s.get("intro") or "")}}] if s.get("intro") else []
    sub = "I do the clicks myself"
    if running:
        widgets.append({"textParagraph": {"text": "<b>Working on it.</b> This card updates itself when it is done (a few minutes)."}})
        sub = "working"
    elif result:
        st = str(result.get("state") or ("ok" if result.get("ok") else "failed"))
        icon = {"ok": "✅", "needs_login": "🔑", "failed": "❌", "needs_admin": "🛡"}.get(st, "•")
        widgets.append({"textParagraph": {"text": f"{icon} {str(result.get('message') or '')[:600]}"}})
        sub = {"ok": "done", "needs_login": "a login for my vault", "failed": "not done", "needs_admin": "one approval from Windows"}.get(st, st)
    form = s.get("store") == "job_form"          # its button opens the form; a plain job's button runs it
    def run_btn(label: str, filled: bool = True) -> dict:
        return (_btn(label, "form_open", {"rid": rid}, dialog=True, filled=filled) if form
                else _btn(label, "form_action", {"rid": rid, "do": "run"}, filled=filled))
    if running:
        buttons = []
    elif result and isinstance(result.get("action"), dict) and result["action"].get("do"):
        # the result names the next tap (Control PC: "Ask Windows now"): that button first, the form again second
        buttons = [_btn(str(result["action"].get("label") or "Go on"), "form_action", {"rid": rid, "do": str(result["action"]["do"])}, filled=True),
                   run_btn(title, filled=False)]
    elif result and str(result.get("state")) in ("needs_login", "failed") and result.get("site"):
        # the site wants a login the vault lacks (or refused): the vault DIALOG, saved -> the job runs again by itself
        buttons = [_btn("Enter login", "vault_add", {"site": str(result["site"])[:200], "rid": rid}, dialog=True, filled=True),
                   run_btn(f"{title} again", filled=False)]
    else:
        buttons = [run_btn(title if not result or form else f"{title} again")]
    if s.get("watch"):
        buttons.append(_link_btn("Control PC", _remote_desktop()))
    if buttons:
        widgets.append({"buttonList": {"buttons": buttons}})
    return {"text": f"{name}: {sub}."[:300],
            "cardsV2": [{"cardId": f"job-{rid}", "card": {"header": {"title": name[:200], "subtitle": sub[:200]}, "sections": [{"widgets": widgets}]}}]}


def post(rid: str, space: str, thread: str = "") -> bool:
    body = card(rid)
    if not body or not str(space or "").startswith("spaces/"):
        return False
    import gchat
    return bool(gchat.post_body(space, body, thread))


def offer_line(rid: str) -> str:
    if rid == "startup":
        import startup
        return startup.offer_line()
    s = spec(rid) or {}
    if s.get("store") == "plaid" and _plaid_keys():
        return "Your Plaid keys are in. Tap Sign in to your bank below; it opens Plaid's own page."
    if s.get("store") == "job":
        return f"Tap {s.get('title') or 'the button'} below and I do it myself; the card says how it went."
    return f"Tap the button below to type it in. It goes straight to the PC, not into this chat."


def post_for(b: dict, ctx: dict) -> bool:
    """Sage's `blocked` deed named a connection or a sign-in: post that connection's form where the ask came from."""
    if str(b.get("kind") or "").lower() not in ("connection", "signin"):
        return False
    rid = str(b.get("connection") or "").strip()
    if not spec(rid):                  # the brain may give the name from CONNECTIONS rather than the id
        rid = next((s["rid"] for s in rows() if rid and rid.lower() in (s["name"].lower(), s["rid"].lower())), "")
    if not rid:
        rid = match("set up " + " ".join(str(b.get(k) or "") for k in ("connection", "what", "why")))
    return bool(rid) and post(rid, str(ctx.get("space") or ""), str(ctx.get("thread") or ""))


def main() -> int:
    a = sys.argv[1:]
    if a[:1] == ["list"]:
        for s in rows():
            print(f"{s['rid']:<24} {s['store']:<28} {s['title']}")
        return 0
    if a[:1] == ["match"] and len(a) == 2:
        print(match(a[1]) or "(no form)")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
