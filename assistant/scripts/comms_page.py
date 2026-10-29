"""comms_page.py - the Comms board on Mission Control (/comms): every thread, its state, its SLA.

The wedding-side twin of the property OS's #/comms page, read straight from
assistant/data/comms_state.json (comms_state.py). Zero LLM, read-only; the only actions are
links to the card that already waits on the owner. Built 2026-10-06.
"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

from alib import DATA, load_json


def _orgcfg():
    """assistant/orgcfg.py (WP5): the organization's own values from assistant/org.json; the literal below is the fallback."""
    import sys as _sys
    from pathlib import Path as _P
    root = str(_P(__file__).resolve().parents[1])
    if root not in _sys.path:
        _sys.path.insert(0, root)
    import orgcfg
    return orgcfg


try:
    MC = str(_orgcfg().get("mission_control_url") or "https://this-pc.tailnet.ts.net:9443")
except Exception:  # noqa: BLE001
    MC = "https://this-pc.tailnet.ts.net:9443"
CSS = """
:root{--bg:#050b07;--panel:rgba(251,255,250,.05);--line:rgba(251,255,250,.12);--ink:#fbfffa;--muted:rgba(251,255,250,.62);
--sage:#4a5c43;--sage2:#8fa386;--warn:#d9a441;--bad:#d96c5f;--ok:#7fb27a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Poppins,system-ui,sans-serif;padding:0 16px 48px}
h1{font-family:"Playfair Display",Georgia,serif;font-weight:500;font-size:26px;margin:0}
h2{font-family:"Playfair Display",Georgia,serif;font-weight:500;font-size:18px;margin:22px 0 8px;color:var(--sage2)}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 18px;padding:26px 0 14px;border-bottom:1px solid var(--line)}
header a{color:var(--sage2);text-decoration:none;font-size:13px}header .ts{margin-left:auto;color:var(--muted);font-size:12px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin:16px 0}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.kpi b{display:block;font-family:"Playfair Display",serif;font-size:26px;font-weight:500}.kpi span{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
.kpi.hot{border-color:var(--bad)}.kpi.warm{border-color:var(--warn)}
.row{display:grid;grid-template-columns:auto 1fr auto;gap:10px;padding:10px 0;border-top:1px solid var(--line);align-items:baseline}
.row:first-child{border-top:0}.who{font-weight:500}.snip{color:var(--muted);font-size:13px;grid-column:2}
.pill{font-size:10px;letter-spacing:.1em;text-transform:uppercase;padding:3px 7px;border-radius:999px;background:var(--sage);white-space:nowrap}
.pill.lead{background:#6b3e3e}.pill.couple{background:#3e5a6b}.pill.personal{background:#3b4a5c}.pill.owner6{background:#6b5a3e}.pill.vendor{background:#5c4a3b}
.sla{font-size:12px;white-space:nowrap}.sla.green{color:var(--ok)}.sla.amber{color:var(--warn)}.sla.red,.sla.breached{color:var(--bad);font-weight:600}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 16px}.empty{color:var(--muted);font-style:italic}
a.btn{display:inline-block;padding:6px 10px;border-radius:8px;border:1px solid var(--line);color:#FBFFFA;text-decoration:none;font-size:13px;background:var(--sage)}
"""


def esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _row(th: dict) -> str:
    li = th.get("last_inbound") or {}
    sla = th.get("sla") or {}
    band = sla.get("band", "")
    sla_txt = ""
    if sla:
        hl = float(sla.get("hours_left", 0))
        sla_txt = f'<span class="sla {band}">{"BREACHED " + str(abs(round(hl * 60))) + " min ago" if hl < 0 else f"{round(hl * 60)} min left"}</span>'
    who = th.get("name") or th.get("handle")
    chan = "✉" if th.get("channel") == "email" else "💬"
    act = f'<a class="btn" href="{MC}/#card-{esc(th["card"])}">card {esc(th["card"])}</a>' if th.get("card") else ""
    age = f'{th.get("age_min", 0)} min' if th.get("age_min") is not None and th["age_min"] < 180 else (li.get("ts", "")[5:16].replace("T", " ") if li else "")
    return (f'<div class="row"><span class="pill {esc(th.get("class"))}">{esc(th.get("class"))}</span>'
            f'<div><span class="who">{chan} {esc(who)}</span> <span class="snip" style="display:inline">{esc(th.get("handle")) if who != th.get("handle") else ""}</span></div>'
            f'<div>{sla_txt} {act}</div>'
            f'<div class="snip">{esc(li.get("text", ""))} <span style="opacity:.6">· {esc(age)}</span></div></div>')


def page() -> str:
    st = load_json(DATA / "comms_state.json", {}) or {}
    threads, lanes, counts = st.get("threads") or {}, st.get("lanes") or {}, st.get("counts") or {}
    probe = load_json(DATA / "comms_probe_state.json", {}) or {}

    def section(title, keys, empty):
        rows = "".join(_row(threads[k]) for k in keys if k in threads)
        return f'<h2>{title}</h2><div class="card">{rows or f"<div class=empty>{empty}</div>"}</div>'

    kpis = "".join(f'<div class="kpi {cls}"><b>{counts.get(k, 0)}</b><span>{label}</span></div>' for k, label, cls in (
        ("breached", "past SLA", "hot" if counts.get("breached") else ""), ("needs_reply", "needs reply", "warm" if counts.get("needs_reply") else ""),
        ("awaiting_owner1", "waiting on you", ""), ("awaiting_party", "waiting on them", ""), ("closed", "closed", "")))
    gen = str(st.get("_generated", ""))[:16].replace("T", " ")
    wake = probe.get("last_wake_at", "never")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Comms</title><link href="https://fonts.googleapis.com/css2?family=Playfair+Display&family=Poppins:wght@400;500&display=swap" rel="stylesheet"><style>{CSS}</style></head><body>
<header><h1>Comms</h1><a href="{MC}/">Mission Control</a><a href="{MC}/chat">Sage</a><span class="ts">board {esc(gen)} · last wake {esc(str(wake)[:16].replace("T", " "))}</span></header>
<div class="kpis">{kpis}</div>
{section("Past SLA", lanes.get("breached", []), "nothing is late")}
{section("Needs a reply", [k for k in lanes.get("needs_reply", []) if k not in set(lanes.get("breached", []))], "nothing waiting")}
{section("Waiting on you (card open)", lanes.get("awaiting_owner1", []), "no drafts waiting")}
{section("Waiting on them", lanes.get("awaiting_party", []), "none")}
{section("Closed recently", lanes.get("closed", [])[:12], "none")}
<p class="empty" style="margin-top:20px">Classes and SLAs: assistant/data/comms_law.json. A one-minute probe wakes the operator when a human message lands; this board is rebuilt every minute by that probe and every 10 by the Day Spine.</p>
</body></html>"""
