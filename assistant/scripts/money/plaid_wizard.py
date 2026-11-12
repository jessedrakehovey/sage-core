"""plaid_wizard.py - connect Relay (and any other bank) through Plaid, entirely in Google Chat.

History: 2026-10-16 a phone page on Mission Control (/setup/plaid) took the keys and ran Plaid Link. 2026-11-01
the owner: "i had to use a mission control app for collecting plaid keys. Make all data entry for anything go through
google chat never mission control." So now (scripts/chat_forms.py posts the cards, gchat_events.py answers them):
  1. Plaid keys       a Chat card's button opens a dialog with client_id + secret; save_keys() stores both in
                      Windows Credential Manager and proves them by minting a Link token (setup-only, no bank data);
                      bad keys are removed again
  2. Sign in to bank  the next card's button opens Plaid's own Hosted Link page (secure.plaid.com, OAuth banks
                      included, no redirect address of ours); hosted_start() mints it and a watcher thread reads the
                      result back (/link/token/get), exchanges the public token, runs the first pull and says in
                      chat what it sees; the spine job money-plaid-hosted is the safety net. "Another bank" mints a
                      fresh page; "Re-sign-in" opens one in update mode for a bank that needs it.
The /setup/plaid address now only says this moved to Chat; status stays readable.

Read-only by construction: everything here goes through plaid.Client (allow-listed paths) and the setup paths
are used only inside this flow.

  python assistant/scripts/money/plaid_wizard.py status         # what is stored and linked (no network)
  python assistant/scripts/money/plaid_wizard.py hosted-check   # finish any bank sign-in done on Plaid's page
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import plaid  # noqa: E402

PAGE_PATH = "/setup/plaid"
API = "/api/setup/plaid"
DASHBOARD = "https://dashboard.plaid.com/"
SIGNUP = "https://dashboard.plaid.com/signup"
KEYS_PAGE = "https://dashboard.plaid.com/developers/keys"
API_PAGE = "https://dashboard.plaid.com/developers/api"     # Allowed redirect URIs live here


def page_url() -> str:
    """This page's full https address: the redirect_uri for Plaid Link on the phone ('' when unknown)."""
    u = mc_url()
    return (u + PAGE_PATH) if u.startswith("https://") else ""


def link_feed(f: dict | None = None) -> dict:
    """The feed with redirect_uri defaulted to this page (money.json may still pin another)."""
    f = dict(f or plaid.feed())
    if not str(f.get("redirect_uri") or "").strip():
        f["redirect_uri"] = page_url()
    return f


def mint(client, f: dict, access_token: str = "") -> tuple:
    """-> (link_token, oauth). Tries with the page as redirect_uri (the phone's OAuth return); when Plaid
    says that address is not allowed yet, mints without it so every non-OAuth bank still links, and
    reports oauth=False so the page shows the one-time Copy address + Open API page fix."""
    lf = link_feed(f)
    if not lf.get("redirect_uri"):
        tok = plaid.link_token(client, lf, access_token=access_token)
        plaid._status(oauth_ready=False)
        return tok, False
    try:
        tok = plaid.link_token(client, lf, access_token=access_token)
    except plaid.PlaidError as e:
        if not plaid.redirect_refused(e):
            raise
        lf["redirect_uri"] = ""
        tok = plaid.link_token(client, lf, access_token=access_token)
        plaid._status(oauth_ready=False)
        return tok, False
    plaid._status(oauth_ready=True)
    return tok, True


def mc_url() -> str:
    try:
        sys.path.insert(0, str(C.SCRIPTS))
        import chat_push
        return str(chat_push.MC_URL).rstrip("/")
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------ status
def status() -> dict:
    f = plaid.feed()
    cid, sec = plaid.creds(f)
    st = plaid.state()
    led = C.ledger()
    s = C.load_json(plaid.STATUS, {}) or {}
    items = [{"item_id": k, "institution": v.get("institution") or "bank", "linked": v.get("linked", ""),
              "accounts": v.get("accounts") or [], "error": v.get("error", "")} for k, v in st["items"].items()]
    err = str(s.get("error", ""))
    relink = next((i["item_id"] for i in items if i["error"] or (i["item_id"] in err and "ITEM_LOGIN_REQUIRED" in err)),
                  items[-1]["item_id"] if items else "")
    if "plaid.py setup" in err:  # the pull names a PC command; on the phone it is the Re-sign-in button
        err = err.split(" - sign in again")[0] + " - tap Re-sign-in below."
    accts = [{"name": a.get("name"), "kind": a.get("kind"), "balance": a.get("balance"), "as_of": a.get("balance_date")}
             for a in led["accounts"].values() if a.get("feed") == "plaid"]
    keys = bool(cid and sec)
    step = 1 if not keys else (2 if not items else 3)
    return {"keys": keys, "env": str(f.get("env") or "production"), "items": items, "accounts": accts,
            "last_ok": s.get("last_ok", ""), "error": err, "step": step, "relink": relink,
            "redirect_uri": str(link_feed(f).get("redirect_uri") or ""), "oauth_ready": s.get("oauth_ready"),
            "api_page": API_PAGE, "page_url": (mc_url() + PAGE_PATH) if mc_url() else PAGE_PATH}


# ------------------------------------------------------------------ step 2: the keys
def save_keys(client_id: str, secret_: str, opener=None) -> dict:
    """Store both keys, then prove them with /link/token/create (setup-only, returns no bank data).
    Bad keys are removed again so a wrong paste never lingers."""
    cid, sec = str(client_id or "").strip(), str(secret_ or "").strip()
    if not cid or not sec:
        return {"ok": False, "message": "Both the client id and the secret are needed."}
    if len(cid) < 16 or len(sec) < 16 or " " in cid or " " in sec:
        return {"ok": False, "message": "That doesn't look like a Plaid client id + secret (each is a long string of letters and digits, no spaces)."}
    f = plaid.feed()
    plaid.store_secret(f["secrets"]["client_id"], cid)
    plaid.store_secret(f["secrets"]["secret"], sec)
    client = plaid.Client(str(f.get("env") or "production"), cid, sec, opener=opener)
    try:
        _tok, oauth = mint(client, f)
    except plaid.PlaidError as e:
        if e.code in ("INVALID_API_KEYS", "INVALID_INPUT", "UNAUTHORIZED") or e.http in (401, 403):
            plaid.clear_secret(f["secrets"]["client_id"])
            plaid.clear_secret(f["secrets"]["secret"])
            return {"ok": False, "message": f"Plaid rejected those keys ({e.code}). On the Keys page copy the client_id and the {client.env} secret again (tap the eye to show it), then paste both here. Nothing was kept."}
        if plaid.redirect_refused(e):
            return {"ok": True, "message": "Keys stored. Plaid wants this page's address registered first: tap Copy address, tap Open API page, paste it under Allowed redirect URIs and save, then come back and tap Connect Relay.",
                    "warn": "redirect_uri", **_redirect_fix()}
        return {"ok": False, "message": f"Keys stored but Plaid answered {e.code}: {e.message[:120]}"}
    except (OSError, ValueError) as e:
        return {"ok": True, "message": f"Keys stored. Plaid could not be reached to prove them right now ({type(e).__name__}); try Connect Relay in a minute.", "warn": "offline"}
    plaid._status(configured=True, error="")
    C.log("plaid_keys_stored", env=client.env, oauth=oauth)
    if not oauth:
        return {"ok": True, "message": f"Keys verified and stored ({client.env}). Tap Connect Relay now. One more tap for banks that sign you in on their own site (Chase, Amex, Capital One...): tap Copy address, tap Open API page, paste it under Allowed redirect URIs and save. Relay works without it.",
                "warn": "redirect_uri", **_redirect_fix()}
    return {"ok": True, "message": f"Keys verified and stored ({client.env}). Now tap Connect Relay."}


def _redirect_fix() -> dict:
    """What the page needs to show the phone fix: a link to Plaid's API page + the address to copy."""
    return {"link": API_PAGE, "link_label": "Open API page", "copy": (mc_url() + PAGE_PATH) if mc_url() else PAGE_PATH}


# ------------------------------------------------------------------ step 3: Link on the phone
def link_token(opener=None, update_item: str = "") -> dict:
    f = plaid.feed()
    client = plaid.make_client(f, opener=opener)
    if client is None:
        return {"ok": False, "message": "Paste the Plaid keys first (step 2)."}
    access = ""
    if update_item:
        access = plaid.secret(f["secrets"]["access_token_prefix"] + update_item) or ""
    try:
        tok, oauth = mint(client, f, access_token=access)
    except plaid.PlaidError as e:
        fix = plaid.redirect_refused(e)
        hint = " Tap Copy address, tap Open API page, paste it under Allowed redirect URIs and save, then try again." if fix else ""
        return {"ok": False, "message": f"Plaid would not start Link ({e.code}).{hint}", **(_redirect_fix() if fix else {})}
    except (OSError, ValueError) as e:
        return {"ok": False, "message": f"Plaid could not be reached ({type(e).__name__})."}
    r = {"ok": True, "token": tok, "env": client.env, "update": bool(update_item), "oauth": oauth,
         "redirect_uri": link_feed(f).get("redirect_uri", "") if oauth else ""}
    if not oauth:
        r.update(_redirect_fix(), hint="Relay links now. Banks that sign you in on their own site need this page's address allowed once in Plaid: Copy address, Open API page, Allowed redirect URIs.")
    return r


def _after_link(opener=None) -> None:
    """First pull, then one line into the owners' space. Runs in a thread; never raises."""
    try:
        plaid.pull(full=True, opener=opener)
    except Exception as e:  # noqa: BLE001
        C.log("plaid_first_pull_error", error=f"{type(e).__name__}: {str(e)[:120]}")
    try:
        text = linked_text()
        sys.path.insert(0, str(C.ROOT / "agent"))
        import bridge
        bridge.post("owner1", text, channel="setup")
    except Exception as e:  # noqa: BLE001
        C.log("plaid_linked_post_error", error=f"{type(e).__name__}: {str(e)[:120]}")


def linked_text() -> str:
    s = status()
    if s["accounts"]:
        parts = [f"{a['name']} {C.money(a['balance'])}" for a in s["accounts"][:6] if a.get("balance") is not None]
        inst = (s["items"][-1]["institution"] if s["items"] else "Relay") or "Relay"
        return (f"{inst} is connected, read-only. I can see {len(s['accounts'])} account{'s' if len(s['accounts']) != 1 else ''}"
                + (": " + ", ".join(parts) if parts else "") + ". Categorizing starts on the next pass; the first unplaced-transactions card comes Friday.")
    inst = (s["items"][-1]["institution"] if s["items"] else "Relay") or "Relay"
    return f"{inst} is linked, read-only. The first pull is still running; I'll name the accounts on the next pass."


def exchange(public_token: str, metadata: dict | None = None, update_item: str = "", opener=None, background: bool = True) -> dict:
    f = plaid.feed()
    client = plaid.make_client(f, opener=opener)
    if client is None:
        return {"ok": False, "message": "Paste the Plaid keys first (step 2)."}
    st = plaid.state()
    try:
        r = plaid.on_public_token(client, f, st, str(public_token or ""), metadata or {}, update_item)
    except (plaid.PlaidError, AssertionError, ValueError, OSError) as e:
        return {"ok": False, "message": f"Could not finish the link: {str(e)[:160]}"}
    plaid.save_state(st)
    plaid._status(error="", configured=True, items=len(st["items"]))
    inst = r.get("institution") or "the bank"
    if background:
        threading.Thread(target=_after_link, kwargs={"opener": opener}, daemon=True).start()
    else:
        _after_link(opener=opener)
    return {"ok": True, "institution": inst, "item_id": r.get("item_id", ""),
            "message": f"{inst} is linked. Pulling the first year of transactions now; I'll say what I see in chat. Another bank to add? Tap Connect another bank."}


# ------------------------------------------------------------------ Google Chat: Hosted Link (2026-11-01)
# the owner: "Make all data entry for anything go through google chat never mission control." The keys come in
# through a Chat dialog (scripts/chat_forms.py) and the bank sign-in is Plaid's own Hosted Link page at
# secure.plaid.com, opened from a Chat button. Plaid keeps the result; hosted_check() reads it back with
# /link/token/get, exchanges the public token and says in chat what it sees. A watcher thread checks every
# few seconds for the first half hour; the spine's money-plaid-hosted job covers the rest of the token's life.
HOSTED_TTL_S = 4 * 3600          # a link token lives four hours
WATCH_S, WATCH_EVERY_S = 1800, 10


def hosted_start(update_item: str = "", opener=None, watch: bool = True) -> dict:
    """-> {"ok", "url", "message"}: a fresh Hosted Link for a new bank, or for re-sign-in of update_item."""
    f = plaid.feed()
    client = plaid.make_client(f, opener=opener)
    if client is None:
        return {"ok": False, "message": "The Plaid keys come first: tap Enter Plaid keys."}
    access = plaid.secret(f["secrets"]["access_token_prefix"] + update_item) or "" if update_item else ""
    try:
        tok, url = plaid.link_token(client, f, access_token=access, hosted=True)
    except plaid.PlaidError as e:
        return {"ok": False, "message": f"Plaid would not start the bank sign-in ({e.code}: {e.message[:120]})."}
    except (OSError, ValueError) as e:
        return {"ok": False, "message": f"Plaid could not be reached ({type(e).__name__}). Try again in a minute."}
    st = plaid.state()
    now = __import__("time").time()
    st["hosted"] = [h for h in (st.get("hosted") or []) if now - float(h.get("at") or 0) < HOSTED_TTL_S] + \
                   [{"token": tok, "update": update_item, "at": now}]
    plaid.save_state(st)
    C.log("plaid_hosted_started", env=client.env, update=bool(update_item))
    if watch:
        threading.Thread(target=_watch, args=(tok,), kwargs={"opener": opener}, daemon=True).start()
    return {"ok": True, "url": url, "message": "Plaid's sign-in is ready."}


def hosted_check(opener=None, post: bool = True, only: str = "") -> dict:
    """Read back every pending Hosted Link session; exchange what finished. -> {"linked": [...], "pending": n}."""
    f = plaid.feed()
    client = plaid.make_client(f, opener=opener)
    st = plaid.state()
    pend = list(st.get("hosted") or [])
    if client is None or not pend:
        return {"linked": [], "pending": len(pend)}
    import time as _t
    now, keep, linked = _t.time(), [], []
    for h in pend:
        if only and h.get("token") != only:
            keep.append(h)
            continue
        if now - float(h.get("at") or 0) >= HOSTED_TTL_S:
            continue
        try:
            r = plaid.hosted_results(client, h["token"])
        except (plaid.PlaidError, OSError, ValueError) as e:
            C.log("plaid_hosted_check_error", error=f"{type(e).__name__}: {str(e)[:120]}")
            keep.append(h)
            continue
        if h.get("update") and r["done"] and not r["exited"]:
            plaid.on_public_token(client, f, st, "", {}, h["update"])
            linked.append({"item_id": h["update"], "update": True})
            continue
        for pub, meta in r["public"]:
            try:
                x = plaid.on_public_token(client, f, st, pub, meta, "")
                linked.append(x)
            except (plaid.PlaidError, AssertionError, ValueError, OSError) as e:
                C.log("plaid_hosted_exchange_error", error=f"{type(e).__name__}: {str(e)[:120]}")
        if not r["done"]:
            keep.append(h)
    st["hosted"] = keep
    plaid.save_state(st)
    if linked:
        plaid._status(error="", configured=True, items=len(st["items"]))
        if post:
            _after_link(opener=opener)
    return {"linked": linked, "pending": len(keep)}


def _watch(tok: str, opener=None) -> None:
    import time as _t
    end = _t.time() + WATCH_S
    while _t.time() < end:
        _t.sleep(WATCH_EVERY_S)
        try:
            r = hosted_check(opener=opener, only=tok)
            if not any(h.get("token") == tok for h in (plaid.state().get("hosted") or [])) or r["linked"]:
                return
        except Exception as e:  # noqa: BLE001
            C.log("plaid_hosted_watch_error", error=f"{type(e).__name__}: {str(e)[:120]}")


# ------------------------------------------------------------------ the page + routing for mc_server
def handle(method: str, path: str, body: dict | None = None, opener=None) -> tuple:
    """-> (http code, body str, content type) or None when the path is not ours."""
    # 2026-11-01 (the owner: "Make all data entry for anything go through google chat never mission control"): the keys come
    # in through a Chat dialog (chat_forms.py) and the bank sign-in is Plaid's Hosted Link page, so this page only
    # points there and its key / link / exchange endpoints are off. status stays: a read, nothing typed.
    if method == "GET" and path in (PAGE_PATH, PAGE_PATH + "/"):
        return 200, MOVED, "text/html"
    if method == "GET" and path == API + "/status":
        return 200, json.dumps(status(), ensure_ascii=False), "application/json"
    if path.startswith(API + "/"):
        return 410, json.dumps({"ok": False, "message": "Plaid is set up in Google Chat now: ask the agent to set up Plaid."}), "application/json"
    return None


MOVED = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Plaid setup moved</title><style>body{margin:0;background:#050B07;color:#FBFFFA;font:16px/1.5 Poppins,system-ui,sans-serif;padding:32px 16px}
main{max-width:520px;margin:0 auto}h1{font:500 24px "Playfair Display",Georgia,serif}</style></head><body><main>
<h1>Plaid is set up in Google Chat now</h1><p>Ask the agent in Chat to set up Plaid. A form opens right there for the two keys,
then a button opens Plaid's own sign-in page for your bank. Nothing is typed on this page any more.</p></main></body></html>"""


if __name__ == "__main__":
    if sys.argv[1:2] == ["hosted-check"]:       # the spine's safety net for a Chat bank sign-in the watcher missed
        r = hosted_check()
        print(f"plaid hosted: {len(r['linked'])} linked, {r['pending']} pending")
    else:
        print(json.dumps(status(), indent=1))
