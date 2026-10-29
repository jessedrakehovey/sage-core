"""plaid_wizard.py - connect Relay through Plaid from the phone, in three taps (the owner 2026-10-16:
"make setting up Plaid stupid simple from inside the Sage chat, completely set up from there").

What the owner does, all from chat: say "set up Plaid" -> Sage answers with the steps and ONE link,
Mission Control's /setup/plaid page (tailnet HTTPS, phone-sized). That page is a wizard:
  1. Create your Plaid account   a button to dashboard.plaid.com (Plaid's free tier; the owner's own
                                 sign-up, email and terms: never done for them)
  2. Paste the two keys          client id + secret, sent only to this PC over the tailnet and stored
                                 in Windows Credential Manager; the page verifies them by minting a
                                 Link token (a setup-only call that returns no bank data)
  3. Connect Relay               Plaid Link opens right on the phone; the owner signs in to Relay inside
                                 Plaid's own window; the page hands back only the public token, this
                                 module exchanges it (plaid.on_public_token), runs the first pull and
                                 posts "Relay is connected: ..." into the shared chat space. Then
                                 "Connect another bank" opens Link again for each other bank (2026-10-18).
Once keys are stored, step 1 is hidden and the chat steps skip it too (connect.py reads status()).
Phone only, end to end (the owner 2026-10-20: "Your instructions involve using a PC."): direct links to
Plaid's Keys and API pages, a Paste button on each key box, a Copy button for the redirect address,
and Re-sign-in on the page for a bank that needs it, so no step points at the PC or a terminal.
Plaid Link's mobile web flow (2026-10-21): the Link token is minted with this page's own address as
redirect_uri (banks that sign in on their own site bounce back here), the page keeps that token in
localStorage and re-opens Link with it + receivedRedirectUri when the bank sends the phone back
(Plaid requires the SAME token), and when Plaid has not been told the address yet the token is minted
without it (Relay and other non-OAuth banks still link) and the page shows Copy address + Open API page.
Long-lived secrets never go through the chat record (that rule stays with connect.py); the page is
the one place they are typed, and it is the owner's own PC answering.

Read-only by construction: everything here goes through plaid.Client (allow-listed paths) and the
two setup paths are used only inside this flow. Served by mc_server.py (routes /setup/plaid and
/api/setup/plaid/*) through handle(); nothing else imports this.

  python assistant/scripts/money/plaid_wizard.py status   # what the wizard would show (no network)
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
        if "redirect_uri" not in e.message.lower():
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
        if "redirect_uri" in e.message.lower():
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
        fix = "redirect_uri" in e.message.lower()
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


# ------------------------------------------------------------------ the page + routing for mc_server
def page() -> str:
    return (PAGE.replace("__LINK_JS__", plaid.LINK_JS).replace("__SIGNUP__", SIGNUP).replace("__KEYS__", KEYS_PAGE)
            .replace("__API__", API))


def handle(method: str, path: str, body: dict | None = None, opener=None) -> tuple:
    """-> (http code, body str, content type) or None when the path is not ours."""
    body = body or {}
    if method == "GET" and path in (PAGE_PATH, PAGE_PATH + "/"):
        return 200, page(), "text/html"
    if method == "GET" and path == API + "/status":
        return 200, json.dumps(status(), ensure_ascii=False), "application/json"
    if method == "GET" and path == API + "/link-token":
        return 200, json.dumps(link_token(opener=opener, update_item=str(body.get("update") or "")), ensure_ascii=False), "application/json"
    if method == "POST" and path == API + "/keys":
        return 200, json.dumps(save_keys(body.get("client_id", ""), body.get("secret", ""), opener=opener), ensure_ascii=False), "application/json"
    if method == "POST" and path == API + "/exchange":
        return 200, json.dumps(exchange(body.get("public_token", ""), body.get("metadata") or {}, str(body.get("update") or ""), opener=opener), ensure_ascii=False), "application/json"
    return None


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#050B07"><title>Connect Relay · Plaid</title>
<style>
:root{--bg:#050B07;--panel:#0d1610;--ink:#FBFFFA;--mute:#9aa69a;--sage:#4A5C43;--line:#1c2a1f;--bad:#b4453f;--ok:#7fbf6a;--warn:#c9a44a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 Poppins,system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;align-items:center;gap:10px;padding:12px 16px;padding-top:calc(12px + env(safe-area-inset-top));border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:2}
header h1{font:600 20px "Playfair Display",Georgia,serif;margin:0;flex:1}header a{color:var(--mute);font-size:13px;text-decoration:none;margin-left:10px}
main{padding:12px 16px 40px;max-width:560px;margin:0 auto}
.intro{color:var(--mute);font-size:13px;margin:4px 0 14px}
.step{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin-bottom:10px;opacity:.55}
.step.on{opacity:1;border-color:var(--sage)}.step.done{opacity:.8}
.step h2{font:500 16px inherit;margin:0 0 6px;display:flex;align-items:center;gap:8px}
.num{width:24px;height:24px;border-radius:50%;background:var(--sage);display:inline-flex;align-items:center;justify-content:center;font-size:13px;flex:none}
.done .num{background:var(--ok);color:#050B07}
.step p{margin:4px 0 8px;font-size:14px;color:#d8e2d5}
label{display:block;font-size:12px;color:var(--mute);margin:8px 0 3px}
input{width:100%;background:#111c14;border:1px solid var(--line);color:var(--ink);border-radius:10px;padding:10px 12px;font:inherit;font-size:14px}
button,a.btn{display:inline-block;background:var(--sage);color:var(--ink);border:0;border-radius:10px;padding:10px 16px;font:500 14px inherit;min-height:40px;text-decoration:none;margin:6px 8px 0 0;cursor:pointer}
button.sec,a.sec{background:none;border:1px solid var(--line);color:var(--mute)}button:disabled{opacity:.5}
.res{font-size:13px;margin-top:8px;color:#cfe3c7;white-space:pre-wrap}.res.err{color:#e8a39f}
.acct{display:flex;justify-content:space-between;border-top:1px solid var(--line);padding:6px 0;font-size:14px}
.foot{color:var(--mute);font-size:12px;margin-top:16px}
.row{display:flex;gap:6px;align-items:center;min-width:0}.row input{flex:1;min-width:0}.row button{margin:0;flex:none}
.res,.step p,.foot,.intro{overflow-wrap:anywhere}a{overflow-wrap:anywhere}
.hint{font-size:13px;color:var(--mute);margin-top:8px}
@media (max-width:420px){button,a.btn{width:100%;margin:8px 0 0;text-align:center}.row button{width:auto;margin:0}}
</style></head><body>
<header><h1>Connect Relay</h1><a href="/setup">Setup</a><a href="/chat">Chat</a></header>
<main>
<div class="intro">Read-only. Sage will see balances and transactions; it cannot move money, and your Relay login is typed only inside Plaid's own window.</div>

<div class="step" id="s1"><h2><span class="num">1</span>Create your Plaid account</h2>
<p>Plaid's free tier. Sign up with your own email, then tap <b>Open Keys page</b>: tap the copy icon next to <b>client_id</b>, come back to this tab and tap Paste in step 2; then the same for the production secret. All on this phone.</p>
<a class="btn" href="__SIGNUP__" target="_blank" rel="noopener">Open Plaid sign-up</a>
<a class="btn sec" href="__KEYS__" target="_blank" rel="noopener">Open Keys page</a>
<button class="sec" onclick="goto(2)">I have an account</button></div>

<div class="step" id="s2"><h2><span class="num">2</span>Paste the two keys</h2>
<p>They go straight to the PC and into Windows Credential Manager, never into chat.</p>
<label>client_id</label><div class="row"><input id="cid" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"><button class="sec" onclick="pasteInto('cid')">Paste</button></div>
<label>secret (the <span id="envname">production</span> one)</label><div class="row"><input id="sec" type="password" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"><button class="sec" onclick="pasteInto('sec')">Paste</button></div>
<button class="sec" onclick="toggleSecret()">Show secret</button>
<a class="btn sec" href="__KEYS__" target="_blank" rel="noopener">Open Keys page</a>
<button id="savekeys" onclick="saveKeys()">Store and verify</button>
<div class="res" id="r2"></div><div id="fix2"></div></div>

<div class="step" id="s3"><h2><span class="num">3</span>Connect your banks</h2>
<p>Plaid Link opens here. Pick Relay, sign in there, choose the accounts (Operating, Tax, Savings). Then tap Connect another bank for each other bank; Plaid opens again each time.</p>
<div class="res" id="linked"></div>
<button id="link" onclick="openLink('')">Connect Relay</button>
<button class="sec" id="relink" style="display:none" onclick="openLink(RELINK)">Re-sign-in</button>
<div class="res" id="r3"></div><div id="fix3"></div><div class="hint" id="oauth"></div>
<div id="accts"></div></div>

<div class="foot">Something off? Say "plaid" in chat and Sage walks you through it. Keys: <a href="__KEYS__" target="_blank" rel="noopener" style="color:var(--mute)">dashboard.plaid.com</a>.<br>Opened this from Google Chat and Plaid's window will not come back? Tap the compass / "Open in Safari" icon and carry on there.</div>
</main>
<script src="__LINK_JS__"></script>
<script>
const API = "__API__"; let S = null, RELINK = "";
function $(id){return document.getElementById(id)}
function goto(n){ for (let i=1;i<=3;i++){ const d=$("s"+i); d.classList.toggle("on", i===n); d.classList.toggle("done", i<n); } }
async function pasteInto(id){
  try { $(id).value = (await navigator.clipboard.readText()).trim(); }
  catch(e){ $(id).focus(); $("r2").className="res"; $("r2").textContent="Your phone blocked the Paste button: long-press the box and choose Paste."; }
}
function toggleSecret(){ const s=$("sec"); s.type = s.type==="password" ? "text" : "password"; }
function showFix(where, d){
  const f=$(where); f.innerHTML=""; if (!d || !d.link) return;
  if (d.copy){ const b=document.createElement("button"); b.className="sec"; b.textContent="Copy address";
    b.onclick=async()=>{ try{ await navigator.clipboard.writeText(d.copy); b.textContent="Copied"; }catch(e){ window.prompt("Copy this address", d.copy); } }; f.appendChild(b); }
  const a=document.createElement("a"); a.className="btn"; a.href=d.link; a.target="_blank"; a.rel="noopener"; a.textContent=d.link_label||"Open Plaid"; f.appendChild(a);
}
function money(v){ if (v===null||v===undefined) return ""; const n=Number(v); return (n<0?"-$":"$")+Math.abs(n).toLocaleString(undefined,{maximumFractionDigits:0}); }
async function load(){
  try { const r = await fetch(API+"/status",{cache:"no-store"}); S = await r.json(); } catch(e){ $("r3").textContent="Could not reach the PC."; return; }
  $("envname").textContent = S.env || "production";
  goto(S.step || 1);
  $("s1").style.display = S.keys ? "none" : "";
  $("linked").textContent = (S.items && S.items.length) ? "Linked: " + S.items.map(i=>i.institution||"bank").join(", ") : "";
  const a = $("accts"); a.innerHTML = "";
  if (S.items && S.items.length){ RELINK = S.relink || S.items[S.items.length-1].item_id; $("relink").style.display="inline-block"; $("link").textContent="Connect another bank"; }
  for (const x of (S.accounts||[])){ const d=document.createElement("div"); d.className="acct"; d.innerHTML="<span>"+(x.name||"?")+"</span><span>"+money(x.balance)+"</span>"; a.appendChild(d); }
  if (S.items && S.items.length && !(S.accounts||[]).length) $("r3").textContent = "Linked. The first pull is running; balances appear here in a minute.";
  if (S.error) { $("r3").textContent = S.error; $("r3").className="res err"; }
  if (S.keys && S.oauth_ready === false){ $("oauth").textContent = "Banks that sign you in on their own site (Chase, Amex, Capital One...) need this page's address allowed once in Plaid: Copy address, then Open API page, then Allowed redirect URIs. Relay works without it."; showFix("fix3", {link:S.api_page, link_label:"Open API page", copy:S.page_url}); }
  else $("oauth").textContent = "";
}
// Plaid's mobile web flow: a bank that signs you in on its own site sends the phone back to this page
// with oauth_state_id=...; Link must then re-open with the SAME token, so it is kept here meanwhile.
const TOK = "plaid_link_token";
function keepToken(tok, update){ try { localStorage.setItem(TOK, JSON.stringify({tok:tok, update:update||"", at:Date.now()})); } catch(e){} }
function savedToken(){ try { const d = JSON.parse(localStorage.getItem(TOK)||"null"); return (d && d.tok && Date.now()-d.at < 4*3600*1000) ? d : null; } catch(e){ return null; } }
function dropToken(){ try { localStorage.removeItem(TOK); } catch(e){} }
function returning(){ return window.location.href.indexOf("oauth_state_id=")>=0; }
async function saveKeys(){
  const b=$("savekeys"), r=$("r2"); b.disabled=true; r.className="res"; r.textContent="Storing and checking with Plaid…";
  try { const x = await fetch(API+"/keys",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({client_id:$("cid").value,secret:$("sec").value})});
    const d = await x.json(); r.textContent = d.message||(d.ok?"Stored.":"Failed."); r.className = d.ok?"res":"res err"; showFix("fix2", d);
    if (d.ok){ $("cid").value=""; $("sec").value=""; goto(3); }
  } catch(e){ r.textContent="Could not reach the PC."; r.className="res err"; }
  b.disabled=false;
}
let handler=null;
async function openLink(update){
  const r=$("r3"); r.className="res"; r.textContent="Starting Plaid Link…";
  let d;
  const back = returning() ? savedToken() : null;
  if (back){ d = {ok:true, token:back.tok}; update = back.update || ""; }
  else {
    try { const x = await fetch(API+"/link-token"+(update?"?update="+encodeURIComponent(update):""),{cache:"no-store"}); d = await x.json(); }
    catch(e){ r.textContent="Could not reach the PC."; r.className="res err"; return; }
    showFix("fix3", d);
    if (d.ok && d.hint) $("oauth").textContent = d.hint;
  }
  if (!d.ok){ r.textContent=d.message||"Plaid would not start."; r.className="res err"; return; }
  keepToken(d.token, update);
  const cfg = {token:d.token,
    onSuccess:function(public_token, metadata){
      dropToken(); if (returning()) history.replaceState(null, "", window.location.pathname);
      r.textContent="Linked. Storing the read-only access token on the PC…";
      fetch(API+"/exchange",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({public_token:public_token,metadata:metadata,update:update||""})})
        .then(x=>x.json()).then(j=>{ r.textContent=j.message||(j.ok?"Done.":"Failed."); r.className=j.ok?"res":"res err"; setTimeout(load, 4000); setTimeout(load, 20000); })
        .catch(e=>{ r.textContent="Could not reach the PC: "+e; r.className="res err"; });
    },
    onExit:function(err){ dropToken(); if (returning()) history.replaceState(null, "", window.location.pathname); r.textContent = err ? ("Plaid Link closed: "+(err.display_message||err.error_code||"")+". Tap Connect to try again.") : "Closed without linking. Tap Connect when ready."; }};
  if (returning()) cfg.receivedRedirectUri = window.location.href;
  try { handler = Plaid.create(cfg); handler.open(); r.textContent="Plaid's window is open: sign in to your bank there."; }
  catch(e){ r.textContent="Plaid Link could not load: "+e; r.className="res err"; }
}
load();
if (returning()) { if (savedToken()) openLink(""); else { $("r3").textContent="The bank sent you back, but this page lost its place (a different browser than the one you started in?). Tap Connect once more."; $("r3").className="res err"; } }
</script></body></html>
"""


if __name__ == "__main__":
    print(json.dumps(status(), indent=1))
