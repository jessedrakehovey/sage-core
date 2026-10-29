"""chat_page.py - the phone chat with the agent, served by Mission Control at /chat.

Mission Control (assistant/scripts/mc_server.py) is already reachable from both owners'
phones over Tailscale, so the chat lives there: no new account, no new port. This module
is everything the server needs; it runs no model itself.

  GET  /chat                      the page
  GET  /api/agent/thread?as=KEY   that person's conversation (from the agent's transcript)
  POST /api/agent/say {as, text}  one line into the agent's inbox; the bridge answers it

A message is only ever queued for the bridge (the same `local` channel chat.py uses), so the
page can do nothing the terminal cannot. `as` must name a person in people.json. Both owners
are equals, so the page lets the phone say which of them is typing and remembers it; the
tailnet login is logged with every message.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

INBOX = core.DATA / "inbox.jsonl"
ALIVE = core.DATA / "bridge_alive.json"
MAX_TEXT = 4000
KEEP = 80


def who_for(login: str) -> str:
    """The person a tailnet login belongs to, or '' when it matches nobody."""
    p = core.person_for(email=str(login or "").strip())
    return p["key"] if p else ""


def thread(key: str) -> dict:
    person = core.person_for(handle=key)
    if not person:
        return {"ok": False, "reason": "unknown person"}
    rows = [r for r in core.rows(core.TRANSCRIPT) if r.get("convo") == f"dm:{person['key']}"]
    total = len(rows)
    a = core.load(ALIVE, {})
    up = bool(a) and time.time() - float(a.get("ts", 0)) < 1800
    msgs = [{"n": total - len(rows[-KEEP:]) + i, "ts": r.get("ts", ""), "who": r.get("who", ""),
             "mine": r.get("who") == person["name"], "text": r.get("text", "")} for i, r in enumerate(rows[-KEEP:])]
    return {"ok": True, "person": person["key"], "name": person["name"], "agent": core.profile().get("name", "Agent"),
            "bridge": up, "waiting": bool(msgs) and msgs[-1]["mine"], "messages": msgs}


def say(key: str, text: str, login: str = "") -> dict:
    person = core.person_for(handle=key)
    text = str(text or "").strip()[:MAX_TEXT]
    if not person or not core.can(person, "ask"):
        return {"ok": False, "reason": "unknown person"}
    if not text:
        return {"ok": False, "reason": "empty"}
    core.append(INBOX, {"ts": core.stamp(), "from": person["key"], "text": text, "via": "mission-control", "login": login[:80]})
    return {"ok": True}


def page(default_key: str = "") -> str:
    people = [{"key": k, "name": u["name"]} for k, u in (core.owners() or core.people().get("users") or {}).items()]
    name = core.profile().get("name", "Agent")
    return (PAGE.replace("__AGENT__", name).replace("__PEOPLE__", json.dumps(people))
            .replace("__DEFAULT__", json.dumps(default_key)))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="__AGENT__"><meta name="theme-color" content="#050B07">
<title>__AGENT__</title>
<style>
:root{--bg:#050B07;--panel:#0d1610;--ink:#FBFFFA;--mute:#9aa69a;--sage:#4A5C43;--sage2:#3F503A;--line:#1c2a1f}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--ink);font:16px/1.45 Poppins,system-ui,-apple-system,Segoe UI,sans-serif;
 display:flex;flex-direction:column;height:100dvh}
header{display:flex;align-items:center;gap:10px;padding:12px 16px;padding-top:calc(12px + env(safe-area-inset-top));
 border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font:600 20px "Playfair Display",Georgia,serif;margin:0;flex:1}
header a{color:var(--mute);font-size:13px;text-decoration:none;margin-left:10px}
#attach{background:none;border:1px solid var(--line);color:var(--mute);border-radius:14px;min-width:44px;font-size:18px}
#attach.busy{opacity:.5}
.dot{width:9px;height:9px;border-radius:50%;background:#7a2f2f}.dot.up{background:#7fbf6a}
.who{display:flex;border:1px solid var(--line);border-radius:999px;overflow:hidden}
.who button{background:none;border:0;color:var(--mute);padding:6px 12px;font:inherit;font-size:13px}
.who button.on{background:var(--sage);color:var(--ink)}
#log{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px;-webkit-overflow-scrolling:touch}
.m{max-width:86%;padding:10px 13px;border-radius:16px;white-space:pre-wrap;overflow-wrap:anywhere}
.m.me{align-self:flex-end;background:var(--sage2);border-bottom-right-radius:4px}
.m.it{align-self:flex-start;background:var(--panel);border:1px solid var(--line);border-bottom-left-radius:4px}
.m small{display:block;color:var(--mute);font-size:11px;margin-top:4px}
.note{align-self:center;color:var(--mute);font-size:13px;text-align:center;padding:6px 12px}
form{display:flex;gap:8px;padding:10px 12px;padding-bottom:calc(10px + env(safe-area-inset-bottom));
 border-top:1px solid var(--line);background:var(--panel)}
textarea{flex:1;resize:none;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:14px;
 padding:10px 12px;font:inherit;max-height:140px;min-height:44px}
textarea:focus{outline:2px solid var(--sage)}
form button{background:var(--sage);color:var(--ink);border:0;border-radius:14px;padding:0 18px;font:600 15px inherit;min-height:44px}
form button:disabled{opacity:.5}
</style></head><body>
<header><span class="dot" id="dot" title="agent loop"></span><h1>__AGENT__</h1>
<div class="who" id="who"></div><a href="/setup">Setup</a><a href="/">Cards</a></header>
<div id="log" aria-live="polite"></div>
<form id="f"><button id="attach" type="button" title="Upload a file to the library (a PDF, a document, a photo, a spreadsheet)" aria-label="Upload a file">&#128206;</button>
<input type="file" id="file" multiple hidden accept=".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.md,.csv,.json,.png,.jpg,.jpeg,.heic,.webp,.gif,.eml,.ics,.rtf">
<textarea id="t" rows="1" placeholder="Message __AGENT__" autocomplete="off"></textarea>
<button id="send" type="submit">Send</button></form>
<script>
const PEOPLE = __PEOPLE__, DEF = __DEFAULT__;
let me = DEF;
try { me = localStorage.getItem("sage_me") || DEF; } catch (e) {}
if (!PEOPLE.some(p => p.key === me)) me = "";
const log = document.getElementById("log"), who = document.getElementById("who"), t = document.getElementById("t");
let lastKey = "", busy = false;
function drawWho() {
  who.innerHTML = "";
  for (const p of PEOPLE) {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = p.name; if (p.key === me) b.className = "on";
    b.onclick = () => { me = p.key; try { localStorage.setItem("sage_me", me); } catch (e) {} lastKey = ""; drawWho(); poll(); };
    who.appendChild(b);
  }
}
function el(cls, text) { const d = document.createElement("div"); d.className = cls; d.textContent = text; return d; }
function draw(d) {
  document.getElementById("dot").className = "dot" + (d.bridge ? " up" : "");
  const key = me + "|" + d.messages.length + "|" + (d.messages.length ? d.messages[d.messages.length - 1].n : 0) + "|" + d.waiting + "|" + d.bridge;
  if (key === lastKey) return;
  lastKey = key;
  const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  log.innerHTML = "";
  if (!d.messages.length) log.appendChild(el("note", "Ask anything, or tell " + d.agent + " what to remember, schedule or note."));
  for (const m of d.messages) {
    const b = el("m " + (m.mine ? "me" : "it"), m.text);
    const s = document.createElement("small"); s.textContent = (m.mine ? "" : m.who + " · ") + m.ts.slice(5); b.appendChild(s);
    log.appendChild(b);
  }
  if (d.waiting) log.appendChild(el("note", d.bridge ? d.agent + " is working on it…" : d.agent + " is offline. Your message is saved and will be answered when it is back (within 10 minutes)."));
  if (atEnd) log.scrollTop = log.scrollHeight;
}
async function poll() {
  if (!me) { log.innerHTML = ""; log.appendChild(el("note", "Who is this? Tap your name above.")); return; }
  try {
    const r = await fetch("/api/agent/thread?as=" + encodeURIComponent(me), {cache: "no-store"});
    const d = await r.json(); if (d.ok) draw(d);
  } catch (e) { document.getElementById("dot").className = "dot"; }
}
document.getElementById("f").addEventListener("submit", async ev => {
  ev.preventDefault();
  const text = t.value.trim(); if (!text || !me || busy) return;
  busy = true; document.getElementById("send").disabled = true;
  try {
    const r = await fetch("/api/agent/say", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({as: me, text})});
    const d = await r.json();
    if (d.ok) { t.value = ""; t.style.height = "auto"; log.scrollTop = log.scrollHeight; }
    else alert("Not sent: " + (d.reason || "error"));
  } catch (e) { alert("Not sent. Check the connection and try again."); }
  busy = false; document.getElementById("send").disabled = false; poll();
});
t.addEventListener("input", () => { t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 140) + "px"; });
t.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !/Mobi|Android|iPhone/i.test(navigator.userAgent)) { e.preventDefault(); document.getElementById("f").requestSubmit(); } });
const fileIn = document.getElementById("file"), attach = document.getElementById("attach");
attach.addEventListener("click", () => { if (!me) { alert("Tap your name first."); return; } fileIn.click(); });
fileIn.addEventListener("change", async () => {
  if (!fileIn.files.length || busy) return;
  busy = true; attach.className = "busy"; document.getElementById("send").disabled = true;
  const fd = new FormData(); fd.append("as", me); fd.append("text", t.value.trim());
  for (const f of fileIn.files) fd.append("file", f, f.name);
  try {
    const r = await fetch("/api/agent/upload", {method: "POST", body: fd});
    const d = await r.json();
    if (d.ok) { t.value = ""; t.style.height = "auto"; } else alert("Not uploaded: " + (d.reason || "error"));
  } catch (e) { alert("Not uploaded. Check the connection and try again."); }
  fileIn.value = ""; busy = false; attach.className = ""; document.getElementById("send").disabled = false; poll();
});
drawWho(); poll(); setInterval(poll, 2500);
</script></body></html>
"""
