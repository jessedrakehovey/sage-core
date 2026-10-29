"""dashboard.py - the organization mission control, the simplified wedding edition.

One static HTML page (assistant/web/index.html) rebuilt by every operator tick.
Zero LLM calls, read-only: it only reads state files the sensors / operator already
write, plus the two scheduled-task states. Replaces Telegram as the place the owner looks.

  dashboard.py            -> rebuild assistant/web/index.html
  dashboard.py --open     -> rebuild, then open it in the default browser
  dashboard.py --hb-check -> accepted, does nothing (the old HoneyBook browser-login
                             probe is retired: HoneyBook signs in through Google, so
                             the connector is the path and conn_check.py its health)

Ticket titles/summaries came from email: rendered escaped, as data.
"""
import html
import json
import os
import subprocess
import sys
import time
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from alib import DATA, OUT, ROOT, VAULT, config, load_json, now_iso, secret

WEB = ROOT / "web" / "index.html"
CARDS = DATA / "cards.jsonl"
LOG = Path.home() / ".claude" / "assistant-operator.log"
TASKS = ["example-org", "example-org", "example-org", "example-org",
         "example-org"]
_task_cache = {"ts": 0.0, "states": {}}
LANE_ORDER = ["wedding", "personal", "home", "money", "owner6"]
# owner's iPhone Shortcut that pulls approved texts and sends them (phone_bridge.py)
PHONE_SEND_URL = "shortcuts://run-shortcut?name=example%20Send%20Texts"

# brand: example.invalid (2026-09-22) - near-black green, off-white, sage
CSS = """
:root{--bg:#050b07;--panel:rgba(251,255,250,.05);--line:rgba(251,255,250,.12);--ink:#fbfffa;
--muted:rgba(251,255,250,.62);--sage:#4a5c43;--sage2:#8fa386;--warn:#d9a441;--bad:#d96c5f;--ok:#7fb27a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Poppins,system-ui,sans-serif;padding:0 16px 48px}
h1,h2{font-family:"Playfair Display",Georgia,serif;font-weight:500;letter-spacing:.01em}
h1{font-size:26px;margin:0}h2{font-size:18px;margin:0 0 10px;color:var(--sage2)}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 18px;padding:26px 0 14px;border-bottom:1px solid var(--line)}
header .brand{font-size:12px;letter-spacing:.28em;text-transform:uppercase;color:var(--sage2)}
header .ts{margin-left:auto;color:var(--muted);font-size:12px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:18px 0}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi b{display:block;font-family:"Playfair Display",serif;font-size:28px;font-weight:500;line-height:1.1}
.kpi span{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}
.kpi.hot{border-color:var(--warn)}
section{margin:22px 0}.btns{margin:8px 0 4px}button.ans,button.undo,button.adskill,#ask button{font:inherit;padding:8px 12px;margin:3px 6px 3px 0;border-radius:8px;border:1px solid var(--line);background:var(--accent,#4A5C43);color:#FBFFFA;cursor:pointer}input.note,#asktext{font:inherit;width:70%;padding:7px 9px;border-radius:8px;border:1px solid var(--line);background:transparent;color:inherit}.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.item{display:grid;grid-template-columns:auto 1fr auto;gap:10px;padding:9px 0;border-top:1px solid var(--line);align-items:baseline}
.item:first-child{border-top:0}.item .t{font-weight:500}.item .n{color:var(--muted);font-size:13px;grid-column:2}
.item .age{color:var(--muted);font-size:12px;white-space:nowrap}
.lane{font-size:10px;letter-spacing:.12em;text-transform:uppercase;padding:3px 7px;border-radius:999px;background:var(--sage);color:var(--ink);white-space:nowrap}
.lane.personal{background:#3b4a5c}.lane.home{background:#5c4a3b}.lane.money{background:#5c3b4a}.lane.owner6{background:#6b5a3e}
.empty{color:var(--muted);font-style:italic}
table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:var(--muted);font-weight:500;font-size:12px;letter-spacing:.06em;text-transform:uppercase;padding:6px 8px 6px 0}
td{padding:7px 8px 7px 0;border-top:1px solid var(--line);vertical-align:top}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}.mono{font-family:Consolas,monospace;font-size:12px}
pre{white-space:pre-wrap;font-family:Consolas,monospace;font-size:12px;color:var(--muted);margin:0;max-height:260px;overflow:auto}
details summary{cursor:pointer;color:var(--sage2);font-family:"Playfair Display",serif;font-size:18px;margin-bottom:10px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
@media (max-width:600px){.item{grid-template-columns:auto 1fr}.item .age{grid-column:2}}
"""


def esc(s):
    return html.escape(str(s if s is not None else ""))


def age(iso):
    try:
        t = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return ""
    s = int((datetime.now(timezone.utc) - t).total_seconds())
    if s < 3600:
        return f"{max(1, s // 60)}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def local_ts(iso):
    try:
        t = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
        return t.strftime("%a %b %d, %H:%M")
    except (TypeError, ValueError):
        return "never"


def task_states():
    # cached 5 min: Mission Control rebuilds the page on every card tap while holding its
    # lock, and four schtasks calls per tap made the phone wait. No console window either.
    if time.time() - _task_cache["ts"] < 300 and _task_cache["states"]:
        return _task_cache["states"]
    out = {}
    for name in TASKS:
        try:
            r = subprocess.run(["schtasks", "/query", "/tn", name, "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0 and r.stdout.strip():
                out[name] = r.stdout.strip().splitlines()[0].split('","')[2].strip('"')
            else:
                out[name] = "not registered"
        except Exception:
            out[name] = "?"
    _task_cache.update(ts=time.time(), states=out)
    return out


def machinery_rows():
    """System rows for the things that silently broke before 2026-10-01: the HoneyBook
    snapshot's age, the refresher's last error, and every spine job that is failing."""
    rows = []
    snap = load_json(VAULT / "dashboard" / "data" / "honeybook.json", {})
    hs = load_json(DATA / "hb_snapshot_status.json", {})
    ts = str(snap.get("ts", "")) if isinstance(snap, dict) else ""
    if ts:
        a = age(ts[:19] + "Z")
        try:
            hours = (datetime.now(timezone.utc) - datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)).total_seconds() / 3600
        except ValueError:
            hours = 999
        err = str(hs.get("error") or "") if isinstance(hs, dict) else ""
        rows.append(("HoneyBook snapshot", f"{a} old" + (f" · refresher: {err[:90]}" if err else ""), "ok" if hours < 14 and not err else "bad"))
    else:
        rows.append(("HoneyBook snapshot", "missing", "bad"))
    sp = load_json(DATA / "spine_state.json", {})
    jobs = [(n, s) for n, s in (sp.items() if isinstance(sp, dict) else []) if isinstance(s, dict)]
    bad = [(n, s) for n, s in jobs if not s.get("ok")]
    newest = max((str(s.get("last", "")) for _, s in jobs), default="")
    rows.append(("day spine", f"{len(jobs) - len(bad)}/{len(jobs)} jobs ok · last run {newest[11:16] or 'never'}", "ok" if jobs and not bad else "bad"))
    for n, s in bad:
        rows.append(("spine · " + n, f"failing x{s.get('fails', '?')}: {str(s.get('result', ''))[:90]}", "bad"))
    tf = load_json(DATA / "typeform_status.json", {})
    if isinstance(tf, dict) and tf:
        rows.append(("quiz-lead sensor", ("ERROR " + str(tf.get("error"))[:90]) if tf.get("error") else f"ok · checked {age(str(tf.get('ts', '')))} ago", "bad" if tf.get("error") else "ok"))
    return rows


def connections_html():
    """Connections panel from conn_check.py's status file (assistant/connections.json is the
    registry). Dead first, then what still needs setting up, then live."""
    cs = load_json(DATA / "connections_status.json", {})
    conns = cs.get("connections") if isinstance(cs, dict) else None
    if not conns:
        return 0, '<div class="empty">no connection check has run yet (python assistant/scripts/conn_check.py)</div>'
    order = {"dead": 0, "not_configured": 1, "live": 2, "manual": 3}
    look = {"dead": ("bad", "DOWN"), "not_configured": ("warn", "needs setup"), "live": ("ok", "live"), "manual": ("muted", "check by hand")}
    rows = []
    for c in sorted((c for c in conns.values() if isinstance(c, dict)), key=lambda c: (order.get(c.get("state"), 9), str(c.get("name")))):
        k, label = look.get(c.get("state"), ("muted", str(c.get("state"))))
        fix = f'<br><span class="muted">{esc(c.get("owner", ""))}: {esc(c.get("renew", ""))}</span>' if c.get("state") in ("dead", "not_configured") and c.get("renew") else ""
        rows.append(f'<tr><td>{esc(c.get("name"))}</td><td class="{k}">{label}</td><td>{esc(c.get("detail"))}{fix}</td></tr>')
    n = {s: sum(1 for c in conns.values() if isinstance(c, dict) and c.get("state") == s) for s in order}
    head = (f'<div class="muted" style="margin-bottom:8px">{n["live"]} live · <span class="{"bad" if n["dead"] else "muted"}">{n["dead"]} down</span> · '
            f'{n["not_configured"]} need setup · {n["manual"]} by hand · checked {age(str(cs.get("ts", "")))} ago</div>')
    return n["dead"], head + "<table>" + "".join(rows) + "</table>"


def ads_html():
    """Ads control panel (ads/scripts/panel.py). Its own try: a broken ad ledger must never
    take Mission Control down."""
    sys.path.insert(0, str(VAULT / "ads" / "scripts"))
    import panel
    return panel.html()


def last_lines(path, n):
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def latest_brief():
    d = OUT / "briefs"
    files = sorted(d.glob("*.md")) if d.exists() else []
    if not files:
        return None, ""
    return files[-1].stem, files[-1].read_text(encoding="utf-8", errors="replace")


def cards(n=12):
    rows = []
    for line in last_lines(CARDS, n):
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return list(reversed(rows))


def lane_pill(lane):
    return f'<span class="lane {esc(lane)}">{esc(lane)}</span>'


MC_JS = r'''<script>
(function(){
  var served = location.protocol.indexOf("http") === 0;
  document.querySelectorAll(".cli").forEach(function(e){ if (served) e.style.display = "none"; });
  if (!served) return;
  function post(url, body){ return fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)}).then(function(r){ return r.json(); }); }
  document.querySelectorAll("button.ans").forEach(function(b){
    b.addEventListener("click", function(){
      var qid = b.dataset.qid, opt = b.dataset.option;
      var note = document.querySelector('input.note[data-qid="' + qid + '"]');
      var text = note ? note.value : "";
      b.disabled = true; b.textContent = "…";
      post("/api/cards/answer", {qid: qid, option: opt, text: text}).then(function(r){
        var el = document.getElementById("card-" + qid);
        if (!r.ok) { alert("not answered: " + (r.reason || "")); b.disabled = false; b.textContent = opt; return; }
        var box = document.createElement("div"); box.className = "t";
        var b1 = document.createElement("b"); b1.textContent = opt;
        box.appendChild(document.createTextNode("answered ")); box.appendChild(b1);
        if (r.ticket) box.appendChild(document.createTextNode(" · ticket " + r.ticket + " queued (Undo works until it runs)"));
        if (r.mint_error) { var s1 = document.createElement("span"); s1.className = "bad"; s1.textContent = " · " + r.mint_error; box.appendChild(s1); }
        box.appendChild(document.createTextNode(" · "));
        var ub = document.createElement("button"); ub.className = "undo"; ub.textContent = "undo"; box.appendChild(ub);
        el.textContent = ""; el.appendChild(box);
        ub.addEventListener("click", function(){
          post("/api/cards/undo", {qid: qid}).then(function(u){ box.textContent = u.ok ? "undone - reload to answer again" : "undo failed: " + (u.reason || ""); });
        });
      });
    });
  });
  document.querySelectorAll("button.adskill").forEach(function(b){
    b.addEventListener("click", function(){
      var on = b.dataset.on === "1";
      if (on && !confirm("Pause the ad system? Learning and approval cards stop until you resume.")) return;
      b.disabled = true;
      post("/api/ads/kill", {on: on}).then(function(r){ if (r.ok) location.reload(); else { alert("failed: " + (r.reason || "")); b.disabled = false; } });
    });
  });
  var ask = document.getElementById("ask");
  if (ask) ask.addEventListener("submit", function(ev){
    ev.preventDefault();
    var t = document.getElementById("asktext");
    post("/api/ask", {text: t.value}).then(function(r){ t.value = ""; document.getElementById("askstatus").textContent = r.ok ? "queued for the operator (next tick within 30 min)" : "not queued"; });
  });
})();
</script>
'''


def _brand() -> str:
    """The organization's name (org.json), never a literal: the same page serves any organization (sage-core)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import orgcfg
        return str(orgcfg.get("name") or "").strip() or "Sage"
    except Exception:  # noqa: BLE001
        return "Sage"


def _page_title() -> str:
    """profile.json page_title when the organization set one, else the agent's own name (a friendly default)."""
    try:
        prof = json.loads((Path(__file__).resolve().parent.parent / "agent" / "profile.json").read_text(encoding="utf-8-sig"))
        return str(prof.get("page_title") or prof.get("name") or "").strip() or "Home"
    except (OSError, ValueError):
        return "Home"


def _esc(s: str) -> str:
    import html
    return html.escape(str(s))


def build(hb_check=False):
    cfg = config()
    today = date.today()
    tickets = load_json(DATA / "tickets.json", {})
    tickets = {k: t for k, t in (tickets.items() if isinstance(tickets, dict) else [])
               if isinstance(t, dict) and t.get("status") and t.get("updated")}
    for t in tickets.values():
        t.setdefault("lane", "personal"); t.setdefault("title", "(untitled)"); t.setdefault("kind", "item")
    staged = sorted((t for t in tickets.values() if t["status"] == "staged"), key=lambda t: t["updated"])
    queued = [t for t in tickets.values() if t["status"] in ("new", "claimed")]
    recent_closed = sorted((t for t in tickets.values() if t["status"] in ("dismissed", "done")), key=lambda t: t["updated"], reverse=True)[:10]
    def _staged_note(t):
        return next((h.get("note") or "" for h in reversed(t.get("history", [])) if h.get("to") == "staged"), "")
    # a real Gmail draft is staged with the note "draft in Gmail: ..."; counting any note that
    # contains the word "draft" showed "4 drafts in Gmail" when there were none
    drafts = [t for t in staged if _staged_note(t).lower().lstrip().startswith("draft in gmail")]
    # A staged ticket whose card is still open is the SAME decision as that card: it is answered on
    # the card, so it is not listed a second time under "needs you".
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import cards as _cards_early
        _live_qids = {c["qid"] for c in _cards_early.pending()}
    except Exception:
        _live_qids = set()
    _carded = [t for t in staged if any(q in _staged_note(t) for q in _live_qids)]
    staged_nocard = [t for t in staged if t not in _carded]
    cal = load_json(DATA / "calendar.json", {}).get("events", [])
    projects = load_json(VAULT / cfg["projects"]["state_file"], {}).get("projects", {})
    mail_status = load_json(DATA / "mail_status.json", {})
    budget = load_json(DATA / ".budget.json", {})
    # hb_check is accepted and ignored: it used to open a Playwright profile to probe a HoneyBook
    # browser login, which Google sign-in makes unscriptable and nothing uses (the connector is
    # the path; conn_check.py reports its health)
    tasks = task_states()
    try:
        conn_dead, conn_html = connections_html()
    except Exception as e:
        conn_dead, conn_html = 0, f'<div class="empty">connections unreadable: {esc(str(e)[:100])}</div>'
    try:
        ads_block = ads_html()
    except Exception as e:
        ads_block = f'<div class="empty">ads panel unreadable: {esc(str(e)[:120])}</div>'
    brief_day, brief_text = latest_brief()
    log_tail = last_lines(LOG, 25)
    last_tick = next((l[:19] for l in reversed(log_tail) if "===" in l and "start" in l), "never")

    # ---- KPIs
    active = {k: p for k, p in projects.items() if isinstance(p, dict) and p.get("stage") not in ("delivered", "archived", "cancelled")}
    def next_date(field):
        ds = []
        for k, p in active.items():
            try:
                d = date.fromisoformat(str(p.get(field, ""))[:10])
                if d >= today:
                    ds.append((d, p.get("couple") or k))
            except ValueError:
                pass
        return min(ds) if ds else None
    nshoot, ndeliv = next_date("wedding_date"), next_date("promised_delivery")
    kp = [
        (len(_live_qids) + len(staged_nocard), "needs you", "hot" if (_live_qids or staged_nocard) else ""),
        (len(drafts), "drafts in gmail", ""),
        (len(queued), "queued for operator", ""),
        (len(active), "active bookings", ""),
        (f"{(nshoot[0] - today).days}d" if nshoot else "—", "next shoot" + (f" · {nshoot[1]}" if nshoot else ""), ""),
        (f"{(ndeliv[0] - today).days}d" if ndeliv else "—", "next delivery" + (f" · {ndeliv[1]}" if ndeliv else ""), "hot" if ndeliv and (ndeliv[0] - today).days < 0 else ""),
    ]
    kpis = "".join(f'<div class="kpi {c}"><b>{esc(v)}</b><span>{esc(l)}</span></div>' for v, l, c in kp)

    # ---- needs you
    if staged_nocard:
        rows = []
        for t in staged_nocard:
            note = next((h["note"] for h in reversed(t.get("history", [])) if h.get("to") == "staged" and h.get("note")), "")
            rows.append(f'<div class="item">{lane_pill(t["lane"])}<div class="t">{esc(t["title"])}</div>'
                        f'<div class="age">{age(t["updated"])} · <span class="mono">{esc(t["id"])}</span></div>'
                        f'<div class="n">{esc(note or t.get("summary", ""))}</div></div>')
        needs = "".join(rows)
    else:
        needs = '<div class="empty">nothing else is waiting on you</div>'

    # ---- calendar
    cal_rows = []
    for e in cal:
        try:
            d = date.fromisoformat(e["date"])
        except (KeyError, ValueError):
            continue
        if d < today:
            continue
        when = "Today" if d == today else ("Tomorrow" if d == today + timedelta(days=1) else d.strftime("%a %b %d"))
        cal_rows.append(f'<tr><td>{esc(when)}</td><td>{esc(e.get("time") or "all day")}</td><td>{esc(e.get("title"))}'
                        f'{" · <span class=muted>" + esc(e["location"]) + "</span>" if e.get("location") else ""}</td></tr>')
    cal_html = f'<table><tr><th>When</th><th>Time</th><th>What</th></tr>{"".join(cal_rows)}</table>' if cal_rows else \
        '<div class="empty">calendar not connected or nothing in the next %d days</div>' % cfg["calendar"]["lookahead_days"]

    # ---- bookings
    if active:
        prow = []
        for k, p in sorted(active.items(), key=lambda kv: str(kv[1].get("wedding_date", ""))):
            def days(field):
                try:
                    n = (date.fromisoformat(str(p.get(field, ""))[:10]) - today).days
                    if field == "wedding_date":
                        cls = "muted" if n < 0 else ("warn" if n <= 14 else "")   # a past wedding is just shot, not late
                    else:
                        cls = "bad" if n < 0 else ("warn" if n <= 14 else "")
                    return f'<span class="{cls}">{p.get(field)} ({n:+d}d)</span>'
                except ValueError:
                    return "—"
            prow.append(f'<tr><td>{esc(p.get("couple") or k)}</td><td>{days("wedding_date")}</td><td>{esc(p.get("stage", "?"))}</td>'
                        f'<td>{days("promised_delivery")}</td><td class="muted">{esc(p.get("note", ""))}</td></tr>')
        bookings = f'<table><tr><th>Couple</th><th>Wedding</th><th>Stage</th><th>Promised delivery</th><th>Note</th></tr>{"".join(prow)}</table>'
    else:
        bookings = '<div class="empty">no active bookings in the tracker (scripts/projects/project_state.json)</div>'

    # ---- queue
    lane_counts = {l: sum(1 for t in queued if t["lane"] == l) for l in LANE_ORDER}
    qrows = "".join(f'<div class="item">{lane_pill(l)}<div class="t">{n} queued</div><div class="age"></div></div>' for l, n in lane_counts.items() if n)
    closed = "".join(f'<div class="item">{lane_pill(t["lane"])}<div class="t">{esc(t["title"])}</div><div class="age">{esc(t["status"])} · {age(t["updated"])}</div>'
                     f'<div class="n">{esc(next((h.get("note") for h in reversed(t.get("history", [])) if h.get("note")), ""))}</div></div>' for t in recent_closed)

    # ---- system
    def cls(ok):
        return "ok" if ok else "bad"
    sys_rows = []
    for a in cfg["mail"]["accounts"]:
        st = mail_status.get("accounts", {}).get(a["name"], "no run yet")
        sys_rows.append(("mail · " + a["name"], st, cls(str(st).startswith("ok"))))
    for f in cfg["calendar"]["feeds"]:
        sys_rows.append(("calendar · " + f["name"], "secret set" if secret(f["secret"]) else "NOT_CONFIGURED", cls(bool(secret(f["secret"])))))
    for a in cfg["mail"]["accounts"]:
        sys_rows.append(("app password · " + a["name"], "set" if secret(a["secret"]) else "not set", cls(bool(secret(a["secret"])))))
    # (the old "HoneyBook session" row probed a browser login nothing uses any more: HoneyBook
    # is read through the connector, so the snapshot's age is the real health signal)
    try:
        sys_rows += machinery_rows()
    except Exception as e:
        sys_rows.append(("machinery", f"unreadable: {e}"[:100], "bad"))
    for name, st in tasks.items():
        sys_rows.append(("task · " + name, st, "ok" if st == "Ready" else ("warn" if st == "Running" else "bad")))
    sys_rows.append(("last tick", last_tick, ""))
    if budget.get("date") == today.isoformat():
        sys_rows.append(("operator runs today", f'{budget.get("count", 0)} / {cfg["operator"]["runs_per_day_max"]}', ""))
    system = "<table>" + "".join(f'<tr><td>{esc(k)}</td><td class="{c}">{esc(v)}</td></tr>' for k, v, c in sys_rows) + "</table>"

    cards_html = "".join(f'<div class="item"><span class="age">{local_ts(c.get("ts", ""))}</span><div class="t" style="white-space:pre-wrap">{esc(c.get("text", ""))}</div><div class="age"></div></div>' for c in cards()) \
        or '<div class="empty">no notifications yet</div>'

    # ---- open decision cards + executor queue (Phase 2 approval chain)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import cards as _cards
        import exec_store as _es
        open_cards = _cards.pending()
        exec_counts = _es.counts()
        exec_pending = _es.pending()
    except Exception as e:  # the page must build even if the chain is broken
        open_cards, exec_counts, exec_pending = [], {"error": str(e)[:80]}, []

    def _card_html(c):
        # Buttons work when the page is SERVED by mc_server.py (they POST /api/cards/answer);
        # opened as a file they fall back to showing the CLI command.
        btns = "".join(f'<button class="ans" data-qid="{esc(c["qid"])}" data-option="{esc(o)}">{esc(o)}</button>' for o in c.get("options") or [])
        cli = f'python assistant/scripts/cards.py answer {esc(c["qid"])} --option "&lt;label&gt;"'
        if c.get("exec_kind") == "imessage_send":   # owner's iPhone: run the send Shortcut after the 30 s undo
            btns += f' <a href="{PHONE_SEND_URL}" style="display:inline-block;padding:8px 12px;margin:3px 0;border-radius:8px;border:1px solid var(--line);color:#FBFFFA;text-decoration:none" title="after approving, wait 30 s, then tap">📱 Send now</a>'
        # 2026-10-20: a held card waits for the weekly digest (answer it here any time); an urgent one is on the phone now
        tag = (" · <span style=\"color:#DFE0E1\">waits for the weekly approvals</span>" if c.get("held")
               else (" · <b>URGENT</b>" if c.get("urgent") else ""))
        return (f'<div class="item card-item" id="card-{esc(c["qid"])}">{lane_pill(c.get("lane") or "personal")}<div class="t"><b>{esc(c.get("header") or c["qid"])}</b>'
                f'{" · EXEC" if c.get("exec") else ""} · {c["age_min"]:.0f}m{tag}<br><span style="white-space:pre-wrap">{esc(c.get("question", ""))}</span>'
                + (f'<br><a href="{esc(c["image"])}" target="_blank"><img src="{esc(c["image"])}" alt="review page" style="max-width:100%;border-radius:8px;margin-top:6px;border:1px solid var(--line)"></a>' if c.get("image") else "")
                + f'<div class="btns">{btns}</div><input class="note" placeholder="or type an answer / a note" data-qid="{esc(c["qid"])}">'
                f'<small class="cli">{cli}</small></div><div class="age"></div></div>')
    open_cards_html = "".join(_card_html(c) for c in open_cards) or '<div class="empty">no open cards</div>'
    exec_html = ("<table>" + "".join(f'<tr><td>{esc(k)}</td><td>{esc(str(v))}</td></tr>' for k, v in sorted(exec_counts.items())) + "</table>"
                 + "".join(f'<div class="item"><div class="t">{esc(t["kind"])} {esc(t["id"])} · queued {esc(t["ts"])} · {esc(t.get("approval_ref", ""))}</div></div>' for t in exec_pending))

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>{_esc(_brand())} · {_esc(_page_title())}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@500&family=Poppins:wght@400;500&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body>
<header><span class="brand">{_esc(_brand())}</span><h1>{_esc(_page_title())}</h1><a class="navlink" href="/desk" style="margin-left:14px;color:#9DB392;font-size:14px">Desk &rarr;</a><a class="navlink" href="/chat" style="margin-left:14px;color:#9DB392;font-size:14px">Chat</a><a class="navlink" href="/setup" style="margin-left:14px;color:#9DB392;font-size:14px">Setup</a><span class="ts">built {datetime.now().strftime("%a %b %d, %H:%M")} · refreshes every 5 min</span></header>
<div class="kpis">{kpis}</div>
<section><h2>Open cards ({len(open_cards)})</h2><div class="card">{open_cards_html}</div></section>
<section><h2>Also waiting on you (no card)</h2><div class="card">{needs}</div></section>
<section><h2>Ask the assistant</h2><div class="card"><form id="ask"><input id="asktext" placeholder="Tell the operator what you need (goes into the queue; nothing is sent without your tap)" style="width:78%"> <button>Queue it</button> <span id="askstatus" class="age"></span></form></div></section>
{'<section><h2>Morning brief · ' + esc(brief_day) + '</h2><div class="card"><pre>' + esc(brief_text) + '</pre></div></section>' if brief_text else ''}
<div class="grid2">
<section><h2>Next {cfg["calendar"]["lookahead_days"]} days</h2><div class="card">{cal_html}</div></section>
<section><h2>Bookings</h2><div class="card">{bookings}</div></section>
</div>
<div class="grid2">
<section><h2>Queue</h2><div class="card">{qrows or '<div class="empty">queue empty</div>'}</div>
{'<details style="margin-top:10px"><summary>Recently closed</summary><div class="card">' + closed + '</div></details>' if closed else ''}</section>
<section><h2>System</h2><div class="card">{system}</div></section>
<section><h2>Executor</h2><div class="card">{exec_html}</div></section>
</div>
<section><h2>Ads</h2><div class="card">{ads_block}</div></section>
<section><h2>Connections{f' · <span class="bad">{conn_dead} down</span>' if conn_dead else ''}</h2><div class="card">{conn_html}</div></section>
<section><h2>Notifications</h2><div class="card">{cards_html}</div></section>
<details><summary>Operator log (last 25 lines)</summary><div class="card"><pre>{esc(chr(10).join(log_tail)) or "no log yet"}</pre></div></details>
{MC_JS}
</body></html>"""
    WEB.parent.mkdir(parents=True, exist_ok=True)
    tmp = WEB.with_suffix(".tmp%d" % os.getpid())      # atomic: the spine, the operator tick and the server all rebuild
    tmp.write_text(page, encoding="utf-8")
    os.replace(tmp, WEB)
    return WEB


def safe_page(err):
    """When build() raises, Mission Control serves THIS instead of a bare error: the open
    cards with working buttons. A broken ticket or state file must never stop the owner from
    answering a card on his phone."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import cards as _cards
        open_cards = _cards.pending()
    except Exception:
        open_cards = []
    items = ""
    for c in open_cards:
        btns = "".join(f'<button class="ans" data-qid="{esc(c["qid"])}" data-option="{esc(o)}">{esc(o)}</button>' for o in c.get("options") or [])
        items += (f'<div class="item card-item" id="card-{esc(c["qid"])}"><div class="t"><b>{esc(c.get("header") or c["qid"])}</b><br>'
                  f'<span style="white-space:pre-wrap">{esc(c.get("question", ""))}</span><div class="btns">{btns}</div>'
                  f'<input class="note" placeholder="or type an answer / a note" data-qid="{esc(c["qid"])}"></div></div>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{_esc(_page_title())} (safe mode)</title><style>{CSS}</style></head><body><header><h1>{_esc(_page_title())}</h1>'
            f'<span class="ts">safe mode</span></header><section><div class="card"><div class="item"><div class="t bad">The full page failed to build: '
            f'{esc(str(err)[:300])}. Your open cards are below and still work.</div></div></div></section>'
            f'<section><h2>Open cards ({len(open_cards)})</h2><div class="card">{items or "<div class=empty>no open cards</div>"}</div></section>{MC_JS}</body></html>')


if __name__ == "__main__":
    p = build(hb_check="--hb-check" in sys.argv)
    print(f"DASHBOARD {p}")
    if "--open" in sys.argv:
        webbrowser.open(p.as_uri())
