"""setup_page.py - Mission Control's Setup page (/setup): connections and logins, one button each.

Served by mc_server.py to the owners' phones over Tailscale. The rows and the buttons come from
setup_actions.py; this module is only the HTML. Tapping a button POSTs /api/setup/run {id, action}
and shows the one-line result; "Check" re-proves the row; "What the PC shows" fetches a screenshot
of the agent's browser so a sign-in started from the phone can be followed without walking over.
"""
from __future__ import annotations

import json


def page(agent_name: str = "Sage", org_status: str = "") -> str:
    return (PAGE.replace("__AGENT__", agent_name).replace("__ORG__", json.dumps(org_status)))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#050B07"><title>Setup · __AGENT__</title>
<style>
:root{--bg:#050B07;--panel:#0d1610;--ink:#FBFFFA;--mute:#9aa69a;--sage:#4A5C43;--sage2:#3F503A;--line:#1c2a1f;--bad:#b4453f;--warn:#c9a44a;--ok:#7fbf6a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 Poppins,system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;align-items:center;gap:10px;padding:12px 16px;padding-top:calc(12px + env(safe-area-inset-top));border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0}
header h1{font:600 20px "Playfair Display",Georgia,serif;margin:0;flex:1}header a{color:var(--mute);font-size:13px;text-decoration:none;margin-left:10px}
main{padding:12px 16px 40px;max-width:720px;margin:0 auto}
.intro{color:var(--mute);font-size:13px;margin:4px 0 12px}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.filters button{background:none;border:1px solid var(--line);color:var(--mute);border-radius:999px;padding:5px 11px;font:inherit;font-size:12px}
.filters button.on{background:var(--sage);color:var(--ink);border-color:var(--sage)}
.row{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin-bottom:10px}
.top{display:flex;align-items:center;gap:10px}.dot{width:10px;height:10px;border-radius:50%;flex:none;background:var(--mute)}
.dot.live{background:var(--ok)}.dot.dead{background:var(--bad)}.dot.not_configured{background:var(--warn)}.dot.manual,.dot.unknown{background:#5c6b5e}
.name{font-weight:500;flex:1}.lane{color:var(--mute);font-size:11px;border:1px solid var(--line);border-radius:6px;padding:1px 6px}
.detail{color:var(--mute);font-size:13px;margin:4px 0 0 20px}
.how{font-size:13px;margin:8px 0 0 20px;display:none}.row.open .how{display:block}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 0 20px}
.btns button{background:var(--sage);color:var(--ink);border:0;border-radius:10px;padding:9px 14px;font:500 14px inherit;min-height:40px}
.btns button.sec{background:none;border:1px solid var(--line);color:var(--mute)}.btns button:disabled{opacity:.5}
.btns a.pagebtn{display:inline-flex;align-items:center;background:var(--sage);color:var(--ink);border-radius:10px;padding:9px 14px;font:500 14px inherit;min-height:40px;text-decoration:none}
.res{font-size:13px;margin:8px 0 0 20px;color:#cfe3c7;white-space:pre-wrap}.res.err{color:#e8a39f}
.shot{margin-top:10px}.shot img{max-width:100%;border-radius:10px;border:1px solid var(--line)}
.add{margin-top:18px;color:var(--mute);font-size:13px}
.add code{background:#111c14;padding:1px 5px;border-radius:5px}
</style></head><body>
<header><h1>Setup</h1><a href="/chat">Chat</a><a href="/">Cards</a></header>
<main>
<div class="intro" id="org"></div>
<div class="filters" id="filters"></div>
<div id="rows"></div>
<div class="shot" id="shot"></div>
<div class="add">Missing a service? Tell __AGENT__ in chat ("we use Square for payments, square.com") and it appears here with a Sign in button. Passwords are never typed in chat: every secret is entered on the PC, in the window a button opens.</div>
</main>
<script>
const ORG = __ORG__;
let rows = [], filter = "needs";
const F = {needs: r => r.state === "dead" || r.state === "not_configured", all: () => true, live: r => r.state === "live", manual: r => r.state === "manual" || r.state === "unknown"};
function el(t, c, txt) { const d = document.createElement(t); if (c) d.className = c; if (txt !== undefined) d.textContent = txt; return d; }
function drawFilters() {
  const f = document.getElementById("filters"); f.innerHTML = "";
  for (const [k, label] of [["needs", "Needs attention"], ["all", "All"], ["live", "Working"], ["manual", "By hand"]]) {
    const n = rows.filter(F[k]).length;
    const b = el("button", k === filter ? "on" : "", label + " · " + n);
    b.onclick = () => { filter = k; draw(); }; f.appendChild(b);
  }
}
function draw() {
  drawFilters();
  document.getElementById("org").textContent = ORG;
  const box = document.getElementById("rows"); box.innerHTML = "";
  const list = rows.filter(F[filter]);
  if (!list.length) box.appendChild(el("div", "intro", filter === "needs" ? "Everything that can be checked is working." : "Nothing here."));
  for (const r of list) {
    const d = el("div", "row"); d.id = "r-" + r.id;
    const top = el("div", "top"); top.appendChild(el("span", "dot " + r.state)); top.appendChild(el("span", "name", r.name));
    if (r.lane) top.appendChild(el("span", "lane", r.lane)); d.appendChild(top);
    top.onclick = () => d.classList.toggle("open");
    d.appendChild(el("div", "detail", (r.state.replace("_", " ")) + (r.detail ? " · " + r.detail : "") + (r.checked ? " · checked " + r.checked.slice(5, 16).replace("T", " ") : "")));
    const how = el("div", "how"); how.textContent = r.how || ""; if (r.credential) how.appendChild(el("div", "detail", "Where the secret lives: " + r.credential)); d.appendChild(how);
    const btns = el("div", "btns");
    if (r.page) { const a = el("a", "", r.page_label || "Set up from this phone"); a.href = r.page; a.className = "pagebtn"; btns.appendChild(a); }
    if (r.label) { const b = el("button", "", r.label); b.onclick = () => run(r, "", b); btns.appendChild(b); }
    if (r.also_url) { const b = el("button", "sec", "Open page"); b.onclick = () => run(r, "open", b); btns.appendChild(b); }
    if (r.checkable) { const b = el("button", "sec", "Check"); b.onclick = () => run(r, "check", b); btns.appendChild(b); }
    if (r.kind === "signin" || r.kind === "window") { const b = el("button", "sec", "What the PC shows"); b.onclick = () => shot(b); btns.appendChild(b); }
    d.appendChild(btns);
    const res = el("div", "res"); res.id = "res-" + r.id; d.appendChild(res);
    if (r.last_run && r.last_run.result) { res.textContent = (r.last_run.ts || "").slice(5, 16).replace("T", " ") + " · " + r.last_run.result; }
    box.appendChild(d);
  }
}
async function load() {
  try { const r = await fetch("/api/setup", {cache: "no-store"}); const d = await r.json(); rows = d.rows || []; draw(); }
  catch (e) { document.getElementById("rows").textContent = "Could not load. Check the connection."; }
}
async function run(r, action, btn) {
  const res = document.getElementById("res-" + r.id); res.className = "res"; res.textContent = "Working…"; btn.disabled = true;
  document.getElementById("r-" + r.id).classList.add("open");
  try {
    const x = await fetch("/api/setup/run", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: r.id, action})});
    const d = await x.json(); res.textContent = d.message || (d.ok ? "Done." : "Failed."); if (!d.ok) res.className = "res err";
    if (action === "check") load();
  } catch (e) { res.textContent = "Failed to reach the PC."; res.className = "res err"; }
  btn.disabled = false;
}
async function shot(btn) {
  btn.disabled = true; const s = document.getElementById("shot"); s.innerHTML = ""; s.appendChild(el("div", "intro", "Fetching what the PC shows…"));
  try {
    const x = await fetch("/api/setup/shot?t=" + Date.now(), {cache: "no-store"});
    if (x.ok && (x.headers.get("Content-Type") || "").startsWith("image")) {
      const blob = await x.blob(); const img = document.createElement("img"); img.src = URL.createObjectURL(blob); s.innerHTML = ""; s.appendChild(img);
    } else { const d = await x.json(); s.innerHTML = ""; s.appendChild(el("div", "res err", d.message || "No screenshot.")); }
  } catch (e) { s.innerHTML = ""; s.appendChild(el("div", "res err", "Could not fetch a screenshot.")); }
  btn.disabled = false;
}
load(); setInterval(load, 60000);
</script></body></html>
"""
