"""home_tab.py - the agent's Home tab in Google Chat: the desk at a glance, its views, and the commands answered by code.

Phase 2 of the desk window (2026-11-04). Google Chat shows a Chat app's HOME tab in the owner's DM with it; when the
owner opens the tab Chat asks the app for a card (the Apps Script relay's onAppHome -> the PC -> home_tab.desk) and
every button on that card comes back to the app the same way (home_action), answered with the card redrawn
(RenderActions updateCard). Nothing here is a model turn: every card is built from the records on this PC in well
under a second, which is what Chat's wait allows.

Views:  desk       what is running now (with the live picture), what waits on the owner, today, the PC, chips
        jobs       running / waiting / recent jobs, with Show on screen and Stop
        approvals  every open card with its buttons (the same guarded cards.answer as the card itself)
        settings   the switchable jobs (agent/switches.json) as switches
        help       the commands and the buttons

Commands (registered in the Chat app's configuration, slash and quick): /desk /jobs /approvals /settings /help /watch
/stop /login. A command is answered on the spot with the same cards as a message in the space it was used in; any
other slash text (/rules, /memory, ...) still goes to the brain as before. The list to register is COMMANDS (ids are
fixed so a new organization registers the same table); `python assistant/scripts/home_tab.py commands` prints it.

Org-free: the agent's name from the profile, the jobs from switches.json, the calendars from google_api, the remote
desktop link from config doer.remote_desktop_url. Tests: assistant/tests/test_home_tab.py.
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (HERE, ROOT / "agent", ROOT / "doer"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from alib import DATA, config, load_json, save_json  # noqa: E402

FN = "home_action"
CARD_ID = "desk"
CACHE = Path(os.environ.get("HOME_TAB_CACHE") or (DATA / "home_cache.json"))
EVENTS_CACHE_S = 300
# id -> (name, type, description): the table registered in the Chat app's configuration (Commands). Quick commands
# have no text, so the id is what names them; slash commands carry their text too.
COMMANDS = [
    (1, "/desk", "SLASH", "The desk: what is running, what waits on you, today"),
    (2, "/jobs", "SLASH", "Running, waiting and recent jobs"),
    (3, "/approvals", "SLASH", "Every open card, with its buttons"),
    (4, "/watch", "SLASH", "A picture of the screen and the Remote Desktop link"),
    (5, "/stop", "SLASH", "Stop the running job at its next step"),
    (6, "/login", "SLASH", "Hand over a login: /login <site>"),
    (7, "/settings", "SLASH", "Switch the standing jobs on or off"),
    (8, "/help", "SLASH", "What the buttons and commands do"),
    (11, "Desk", "QUICK", "The desk at a glance"),
    (12, "Approvals", "QUICK", "Every open card"),
    (13, "Control PC", "QUICK", "A picture of the screen and the Remote Desktop link"),
    (14, "Stop the job", "QUICK", "Stop the running job at its next step"),
]
_BY_ID = {c[0]: c for c in COMMANDS}
_BY_NAME = {c[1].lower(): c for c in COMMANDS}
_VIEW_OF = {"/desk": "desk", "desk": "desk", "/jobs": "jobs", "/approvals": "approvals", "approvals": "approvals",
            "/settings": "settings", "/help": "help", "/watch": "watch", "control pc": "watch", "watch my pc": "watch", "/stop": "stop",
            "stop the job": "stop", "/login": "login"}
_TID = re.compile(r"[0-9a-f]{16}")
_QID = re.compile(r"q[0-9a-f]{12}")


# ------------------------------------------------------------------ small helpers
def agent_name() -> str:
    try:
        return str((load_json(ROOT / "agent" / "profile.json", {}) or {}).get("name") or "Agent")
    except Exception:  # noqa: BLE001
        return "Agent"


def remote_desktop() -> str:
    try:
        return str((config().get("doer") or {}).get("remote_desktop_url") or "https://remotedesktop.google.com/access")
    except Exception:  # noqa: BLE001
        return "https://remotedesktop.google.com/access"


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _clock(ts: float | None = None) -> str:
    return time.strftime("%I:%M %p", time.localtime(ts or time.time())).lstrip("0")


def _btn(label: str, do: str, ctx: str, filled: bool = False, **params) -> dict:
    import chat_push
    return chat_push._button(label, FN, {"do": do, "ctx": ctx, **{k: str(v) for k, v in params.items()}}, filled=filled)


def _link(label: str, url: str) -> dict:
    return {"text": label, "onClick": {"openLink": {"url": url}}}


def _chip(label: str, do: str, ctx: str, **params) -> dict:
    return {"label": label, "onClick": {"action": {"function": FN, "parameters": [{"key": "do", "value": do}, {"key": "ctx", "value": ctx}]
                                                   + [{"key": k, "value": str(v)} for k, v in params.items()]}}}


def chips(ctx: str, current: str = "") -> dict:
    out = []
    for label, do in (("Desk", "desk"), ("Jobs", "jobs"), ("Approvals", "approvals"), ("Settings", "settings"), ("Help", "help")):
        if do != current:
            out.append(_chip(label, do, ctx))
    out.append({"label": "Control PC", "onClick": {"openLink": {"url": remote_desktop()}}})
    return {"chipList": {"chips": out}}


def _para(text: str) -> dict:
    return {"textParagraph": {"text": text}}


def _row(text: str, top: str = "", bottom: str = "", button: dict | None = None) -> dict:
    w = {"decoratedText": {"text": text, "wrapText": True}}
    if top:
        w["decoratedText"]["topLabel"] = top[:40]
    if bottom:
        w["decoratedText"]["bottomLabel"] = bottom[:80]
    if button:
        w["decoratedText"]["button"] = button
    return w


# ------------------------------------------------------------------ the records
def _tickets() -> dict:
    try:
        import tickets
        return tickets._load()
    except Exception:  # noqa: BLE001
        return {}


def _job_title(t: dict) -> str:
    try:
        import doer
        return doer.job_line(t, 90).strip('"') or str(t.get("title") or "a job")
    except Exception:  # noqa: BLE001
        return str(t.get("title") or "a job")


def jobs() -> dict:
    """{"running": [...], "waiting": [...], "recent": [...]} of agent_run tickets, each with title / since / phase / shot."""
    out = {"running": [], "waiting": [], "recent": []}
    try:
        import livecard
    except Exception:  # noqa: BLE001
        livecard = None
    rows = [t for t in _tickets().values() if t.get("kind") == "agent_run"]
    for t in rows:
        hist = t.get("history") or [{}]
        last = hist[-1] if hist else {}
        st = (livecard.state(t["id"]) if livecard else {}) or {}
        row = {"id": t["id"], "title": _job_title(t), "status": t.get("status"), "note": str(last.get("note") or "")[:160],
               "ts": str(last.get("ts") or t.get("ts") or ""), "shot": str(st.get("shot_url") or ""), "state": str(st.get("state") or ""),
               "phase": str(st.get("phase") or ""), "started": float(st.get("started") or 0)}
        if t.get("status") == "claimed" and last.get("by") == "doer":
            out["running"].append(row)
        elif t.get("status") == "staged" and "checkpoint" in row["note"]:
            out["waiting"].append(row)
        elif t.get("status") in ("done", "staged", "new", "claimed"):
            out["recent"].append(row)
    out["recent"].sort(key=lambda r: r["ts"], reverse=True)
    out["recent"] = out["recent"][:5]
    return out


def _now_line(job: dict) -> str:
    """What the job is on right now: its newest milestone note, else the live status line."""
    try:
        import notes
        got = notes.last(job["id"])
        if got:
            return str(got.get("text") or "")
    except Exception:  # noqa: BLE001
        pass
    return job.get("phase") or "working"


def open_cards() -> list:
    try:
        import cards
        rows = cards.pending()
    except Exception:  # noqa: BLE001
        return []
    rows.sort(key=lambda c: (not c.get("urgent"), c.get("held", False), -float(c.get("age_min") or 0)))
    return rows


def today_events() -> list:
    """The next day-and-a-half of the owners' calendars, cached for a few minutes (a Home open must answer fast)."""
    c = load_json(CACHE, {}) or {}
    if time.time() - float(c.get("events_at") or 0) < EVENTS_CACHE_S:
        return list(c.get("events") or [])
    rows = []
    try:
        import google_api
        for e in google_api.events(days=2):
            if e.get("error"):
                continue
            rows.append({"title": str(e.get("title") or "")[:80], "start": str(e.get("start") or ""), "all_day": bool(e.get("all_day")),
                         "location": str(e.get("location") or "")[:60], "meet": str(e.get("meet") or "")})
    except Exception:  # noqa: BLE001
        rows = []
    c.update({"events_at": time.time(), "events": rows[:8]})
    try:
        save_json(CACHE, c)
    except Exception:  # noqa: BLE001
        pass
    return rows[:8]


def _when(start: str, all_day: bool) -> str:
    try:
        if all_day:
            d = datetime.strptime(start[:10], "%Y-%m-%d")
            return f"{d:%a} all day"
        d = datetime.fromisoformat(start)
        return f"{d:%a} " + d.strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return start[:16]


def pc_health() -> dict:
    """Uptime, CPU load, free disk, the browser, the last housekeeping run. Each piece guarded; never raises."""
    h = {}
    try:
        import ctypes
        ms = ctypes.windll.kernel32.GetTickCount64()
        h["uptime_h"] = round(ms / 3600000, 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        r = subprocess.run(["wmic", "cpu", "get", "loadpercentage"], capture_output=True, text=True, timeout=4,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        nums = [int(x) for x in re.findall(r"\b\d+\b", r.stdout or "")]
        if nums:
            h["cpu_pct"] = max(nums)
    except Exception:  # noqa: BLE001
        pass
    try:
        du = shutil.disk_usage(str(ROOT))
        h["disk_free_gb"] = round(du.free / 1e9)
    except Exception:  # noqa: BLE001
        pass
    try:
        import browser
        b = browser.status()
        h["browser"] = "up, off-screen" if b.get("up") and b.get("offscreen") else "up, on screen" if b.get("up") else "closed"
    except Exception:  # noqa: BLE001
        pass
    try:
        st = load_json(DATA / "spine_state.json", {}) or {}
        last = max((str(v.get("last") or "") for v in st.values() if isinstance(v, dict)), default="")
        if last:
            h["spine_last"] = last[11:16]
    except Exception:  # noqa: BLE001
        pass
    return h


# ------------------------------------------------------------------ the views
def desk(ctx: str = "home") -> dict:
    name = agent_name()
    j = jobs()
    sections = []
    # the start-up sequence, while anything in it is still to do (startup.py): one button, the card lands in the chat
    try:
        import startup
        todo = startup.pending(fresh=False)
    except Exception:  # noqa: BLE001
        todo = []
    if todo:
        sections.append({"header": "Finish setting up", "widgets": [
            _para(f"{len(todo)} thing{'s' if len(todo) != 1 else ''} I can set up myself: " + "; ".join(_esc(r["name"]) for r in todo[:4]) + "."),
            {"buttonList": {"buttons": [_btn("Set up everything", "startup", ctx, filled=True)]}}]})
    # right now
    w = []
    if j["running"]:
        r = j["running"][0]
        since = f" since {_clock(r['started'])}" if r.get("started") else ""
        w.append(_row(f"<b>{_esc(r['title'])}</b>", top=f"On it{since}", bottom=_now_line(r)[:80]))
        if r.get("shot"):
            w.append({"image": {"imageUrl": r["shot"], "altText": "what is on the screen now"}})
        w.append({"buttonList": {"buttons": [_link("Control PC", remote_desktop()), _btn("Bring it to the front", "show", ctx, job=r["id"]),
                                             _btn("Stop", "stop", ctx, job=r["id"])]}})
        for r2 in j["running"][1:3]:
            w.append(_row(_esc(r2["title"]), top="Also running"))
    elif j["waiting"]:
        r = j["waiting"][0]
        w.append(_row(f"<b>{_esc(r['title'])}</b>", top="Waiting for your tap", bottom="the card is in this chat"))
    else:
        last = j["recent"][0] if j["recent"] else None
        w.append(_para("Nothing running." + (f" Last: <b>{_esc(last['title'])}</b> ({_esc(last['note'][:90])})" if last else "")))
    sections.append({"header": "Right now", "widgets": w})
    # waiting on you
    oc = open_cards()
    w = []
    if not oc:
        w.append(_para("Nothing waits on you."))
    else:
        urgent = [c for c in oc if not c.get("held")]
        held = [c for c in oc if c.get("held")]
        if urgent:
            for c in urgent[:4]:
                w.append(_row(_esc(c.get("header") or "Decision"), top=str(c.get("lane") or "")[:20], bottom=str(c.get("question") or "").split("\n")[0][:80]))
        line = (f"{len(urgent)} to answer now" if urgent else "") + (" · " if urgent and held else "") + \
               (f"{len(held)} held for the weekly list" if held else "")
        w.append(_para(line))
        w.append({"buttonList": {"buttons": [_btn("Open approvals", "approvals", ctx, filled=bool(urgent))]}})
    sections.append({"header": "Waiting on you", "widgets": w})
    # today
    ev = today_events()
    if ev:
        w = [_row(_esc(e["title"]), top=_when(e["start"], e["all_day"]), bottom=e.get("location") or "",
                  button=_link("Join", e["meet"]) if e.get("meet") else None) for e in ev[:4]]
        sections.append({"header": "Coming up", "widgets": w, "collapsible": len(ev) > 2, "uncollapsibleWidgetsCount": 2})
    # the PC
    h = pc_health()
    bits = []
    if "cpu_pct" in h:
        bits.append(f"CPU {h['cpu_pct']}%")
    if "disk_free_gb" in h:
        bits.append(f"{h['disk_free_gb']} GB free")
    if "uptime_h" in h:
        bits.append(f"up {h['uptime_h']} h")
    if h.get("browser"):
        bits.append(f"browser {h['browser']}")
    if h.get("spine_last"):
        bits.append(f"housekeeping ran {h['spine_last']}")
    sections.append({"header": "The PC", "widgets": [_para(" · ".join(bits) or "no reading")]})
    sections.append({"widgets": [chips(ctx, "desk"), _para(f'<font color="#777777">{_esc(name)} · {_clock()} · tap Desk to refresh</font>')]})
    return {"header": {"title": f"{name}'s desk", "subtitle": time.strftime("%A, %B ") + str(int(time.strftime("%d")))},
            "sections": sections}


def jobs_view(ctx: str = "home") -> dict:
    j = jobs()
    sections = []
    for key, head in (("running", "Running"), ("waiting", "Waiting for your tap"), ("recent", "Recent")):
        rows = j[key]
        if not rows:
            continue
        w = []
        for r in rows:
            bottom = _now_line(r)[:80] if key == "running" else r["note"][:80]
            w.append(_row(f"<b>{_esc(r['title'])}</b>", top=(r["ts"][11:16] if key != "running" else f"since {_clock(r['started'])}" if r.get("started") else ""), bottom=bottom))
            if key == "running":
                w.append({"buttonList": {"buttons": [_btn("Bring it to the front", "show", ctx, job=r["id"]), _btn("Stop", "stop", ctx, job=r["id"])]}})
        sections.append({"header": head, "widgets": w})
    if not sections:
        sections.append({"widgets": [_para("No jobs yet. Ask me to do something on a site and it shows up here.")]})
    sections.append({"widgets": [chips(ctx, "jobs")]})
    return {"header": {"title": "Jobs", "subtitle": f"{len(j['running'])} running · {len(j['waiting'])} waiting"}, "sections": sections}


def approvals_view(ctx: str = "home") -> dict:
    oc = open_cards()
    sections = []
    for c in oc[:10]:
        opts = [o for o in (c.get("options") or []) if o][:3]
        q = str(c.get("question") or "")
        first = q.split("\n")[0][:300]
        w = [_para(f"<b>{_esc(c.get('header') or 'Decision')}</b><br>{_esc(first)}")]
        if c.get("image"):
            pass                                        # the picture lives on Mission Control; the card's own thread has it
        btns = [_btn(re.sub(r"\s*\(Recommended\)\s*", "", o).strip(), "answer", ctx, filled=(i == 0 and c.get("exec")), qid=c["qid"], option=o)
                for i, o in enumerate(opts)]
        if btns:
            w.append({"buttonList": {"buttons": btns}})
        label = ("now" if not c.get("held") else "held for the weekly list") + (" · urgent" if c.get("urgent") else "")
        sections.append({"header": f"{str(c.get('lane') or '')[:14]} · {label}", "widgets": w, "collapsible": True, "uncollapsibleWidgetsCount": 1})
    if not oc:
        sections.append({"widgets": [_para("Nothing waits on you.")]})
    elif len(oc) > 10:
        sections.append({"widgets": [_para(f"{len(oc) - 10} more on the weekly list.")]})
    sections.append({"widgets": [chips(ctx, "approvals")]})
    return {"header": {"title": "Approvals", "subtitle": f"{len(oc)} open"}, "sections": sections}


def settings_view(ctx: str = "home", toast: str = "") -> dict:
    try:
        import switches
        rows = switches.jobs()
    except Exception:  # noqa: BLE001
        switches, rows = None, []
    w = []
    for j in rows:
        on = switches.state(j) if switches else None
        ctl = {"name": f"sw:{j['key']}", "value": "on", "selected": bool(on), "controlType": "SWITCH",
               "onChangeAction": {"function": FN, "parameters": [{"key": "do", "value": "switch"}, {"key": "ctx", "value": ctx}, {"key": "key", "value": j["key"]}]}}
        w.append({"decoratedText": {"topLabel": str(j.get("name") or j["key"])[:40], "text": _esc(str(j.get("what") or "")[:120]) or " ",
                                    "wrapText": True, "switchControl": ctl}})
    if not w:
        w.append(_para("No switchable jobs are registered yet (agent/switches.json)."))
    sections = [{"header": "Standing jobs", "widgets": w}]
    if toast:
        sections.insert(0, {"widgets": [_para(f"<b>{_esc(toast)}</b>")]})
    sections.append({"widgets": [chips(ctx, "settings")]})
    return {"header": {"title": "Settings", "subtitle": "a switch takes effect the moment you flip it"}, "sections": sections}


def help_view(ctx: str = "home") -> dict:
    name = agent_name()
    lines = [f"<b>{_esc(c[1])}</b> · {_esc(c[3])}" for c in COMMANDS if c[2] == "SLASH"]
    quick = ", ".join(c[1] for c in COMMANDS if c[2] == "QUICK")
    w = [_para("<br>".join(lines)),
         _para(f"Quick commands (the ⚡ menu in the message box): {_esc(quick)}."),
         _para("On a job's card: <b>Control PC</b> opens Chrome Remote Desktop, the live view of the PC where you can also take the "
               "wheel; <b>Bring it to the front</b> puts my browser where that window shows it; <b>Stop</b> ends the job at its next step."),
         _para("On a sign-in card: <b>Enter login</b> opens a form that goes straight to my vault, never into this chat. "
               "On a code card: <b>Enter code</b> does the same for the code."),
         _para(f"Also: /rules, /memory, /schedules, /promises, /skills, /changes and /undo &lt;id&gt; ask {_esc(name)} itself.")]
    return {"header": {"title": "Help", "subtitle": f"what {name} can do from here"}, "sections": [{"widgets": w}, {"widgets": [chips(ctx, "help")]}]}


def watch_view(ctx: str = "msg") -> dict:
    j = jobs()
    w = []
    if j["running"]:
        r = j["running"][0]
        w.append(_row(f"<b>{_esc(r['title'])}</b>", top="On it", bottom=_now_line(r)[:80]))
        shot = r.get("shot")
        if not shot:
            shot = _fresh_shot(r["id"])
        if shot:
            w.append({"image": {"imageUrl": shot, "altText": "what is on the screen now"}})
        else:
            w.append(_para("No picture yet (the screenshot drive is not set up, or the browser just opened)."))
        w.append({"buttonList": {"buttons": [_link("Control PC", remote_desktop()), _btn("Bring it to the front", "show", ctx, job=r["id"]), _btn("Stop", "stop", ctx, job=r["id"])]}})
    else:
        w.append(_para("Nothing is running. Control PC opens the live view anyway."))
        w.append({"buttonList": {"buttons": [_link("Control PC", remote_desktop())]}})
    return {"header": {"title": "Watch", "subtitle": _clock()}, "sections": [{"widgets": w}]}


def _fresh_shot(tid: str) -> str:
    try:
        import livecard
        livecard.touch(tid)
        return str(livecard.state(tid).get("shot_url") or "")
    except Exception:  # noqa: BLE001
        return ""


VIEWS = {"desk": desk, "jobs": jobs_view, "approvals": approvals_view, "settings": settings_view, "help": help_view, "watch": watch_view}


def _owner_space(person_key: str) -> str:
    """Where the agent's chat with this owner lives: the owners' home space, else their DM. '' when unknown."""
    try:
        import chat_push
        import core
        return str(core.home_space() or chat_push._spaces().get(person_key) or "")
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------ the taps
def act(do: str, params: dict, form: dict, person: dict | None = None) -> tuple:
    """A tap on a desk card -> (the card to show now, a one-line toast or ''). Never raises."""
    ctx = str(params.get("ctx") or "home")
    who = str((person or {}).get("key") or "owner")
    try:
        if do in ("desk", "jobs", "approvals", "settings", "help", "watch"):
            return VIEWS[do](ctx), ""
        if do == "startup":
            # the Home tab cannot be edited from outside a tap, so the sequence runs on its own card in the chat
            import startup
            space = _owner_space(who)
            posted = startup.offer(space, force=True) if space else False
            return desk(ctx), ("The setup card is in our chat: tap Set up everything there." if posted
                               else "I could not post the setup card; say 'finish setting up' in our chat.")
        if do in ("stop", "show"):
            tid = str(params.get("job") or "")
            if not _TID.fullmatch(tid):
                return desk(ctx), "That job is not known."
            import livecard
            body = livecard.action(tid, livecard.STOP if do == "stop" else livecard.SHOW, person)
            toast = ("Stopping it at the next step." if do == "stop" else "Bringing my browser to the front; Control PC shows it.") if body else "That job is not running any more."
            return (jobs_view(ctx) if ctx == "home" else watch_view(ctx)), toast
        if do == "switch":
            import switches
            key = str(params.get("key") or "")
            on = bool(str(form.get(f"sw:{key}") or ""))
            r = switches.set_on(key, on, by=who)
            name = str((switches.find(key) or {}).get("name") or key)
            toast = {"applied": f"{name} is now {'on' if on else 'off'}.", "unchanged": f"{name} was already {'on' if on else 'off'}."}.get(
                str(r.get("status")), f"{name}: {r.get('reason') or 'not changed'}")
            return settings_view(ctx, toast if r.get("status") == "refused" else ""), toast
        if do == "answer":
            import cards
            qid, option = str(params.get("qid") or ""), str(params.get("option") or "")
            if not _QID.fullmatch(qid) or not option:
                return approvals_view(ctx), "That card is not known."
            r = cards.answer(qid, option, by=f"gchat:{who}")
            if r.get("ok"):
                try:
                    import chat_push
                    card = cards._read(cards.LIVE / f"{qid}.json")
                    if card:                              # the card's own message in the thread flips too
                        chat_push.mark_answered(card, {"option": option, "by": who, "ticket": r.get("ticket"), "mint_error": r.get("mint_error")})
                except Exception:  # noqa: BLE001
                    pass
                toast = f"{re.sub(r'\s*\(Recommended\)\s*', '', option).strip()}: done." + (" Undo on the card's own message." if r.get("ticket") else "")
            else:
                toast = f"Not answered: {r.get('reason')}"
            return approvals_view(ctx), toast
    except Exception as e:  # noqa: BLE001
        return desk(ctx), f"That did not work ({type(e).__name__})."
    return desk(ctx), ""


# ------------------------------------------------------------------ Chat's shapes
def render(card: dict, push: bool = False, toast: str = "") -> dict:
    """RenderActions for the Home tab: the first card is pushed, every later one updates in place."""
    out = {"action": {"navigations": [{("pushCard" if push else "updateCard"): card}]}}
    if toast:
        out["action"]["notification"] = {"text": toast[:200]}
    return out


def message(card: dict, text: str = "") -> dict:
    """The same card as a message body (a command's reply in a space)."""
    return {"text": (text or str((card.get("header") or {}).get("title") or ""))[:300], "cardsV2": [{"cardId": CARD_ID, "card": card}]}


def command_for(cmd_id, text: str = "") -> str:
    """Which view a registered command asks for: by its id, else by its slash word. '' when it is not one of ours."""
    try:
        c = _BY_ID.get(int(cmd_id or 0))
    except (TypeError, ValueError):
        c = None
    name = c[1].lower() if c else str(text or "").strip().split()[0].lower() if str(text or "").strip() else ""
    return _VIEW_OF.get(name, "")


def command(view: str, args: str = "", person: dict | None = None) -> dict:
    """The reply body for a command used in a space. `view` from command_for."""
    if view in ("desk", "jobs", "approvals", "settings", "help", "watch"):
        v = VIEWS[view]("msg")
        return message(v)
    if view == "stop":
        j = jobs()
        if not j["running"]:
            return {"text": "Nothing is running."}
        import livecard
        r = j["running"][0]
        body = livecard.action(r["id"], livecard.STOP, person)
        return {"text": f"Stopping {r['title']} at its next step." if body else f"{r['title']} is running but has no live card; it stops at its next tool call."}
    if view == "login":
        import vault_chat
        site = str(args or "").strip().split()[0] if str(args or "").strip() else ""
        return vault_chat.card(site, agent_name())
    return {"text": "That command is not known here."}


def commands_table() -> str:
    """What to type into the Chat app's configuration (Commands)."""
    lines = ["id  type   name            description"]
    for cid, name, typ, desc in COMMANDS:
        lines.append(f"{cid:<3} {typ:<6} {name:<15} {desc}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    if a[:1] == ["commands"]:
        print(commands_table())
    elif a[:1] in (["desk"], ["jobs"], ["approvals"], ["settings"], ["help"], ["watch"]):
        print(json.dumps(VIEWS[a[0]]("msg"), indent=1, ensure_ascii=False))
    else:
        print(__doc__)
