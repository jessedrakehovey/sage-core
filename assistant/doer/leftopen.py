"""leftopen.py - a step the agent cannot finish in its browser is left open for the owner, with Control PC and Do later.

The owner, 2026-11-08: "if sage gets to a step it can't complete in browser it should leave the browser open for the
user to do it themself unless the user selects a button to do it later. Have control pc and do later buttons
presented in chat."

So when a browser job stops at a step only the owner can take (doer._owner_only: a password, a code, a captcha, a
payment, identity), the doer does NOT close the browser. The page stays where the job left it, in front unless
someone used the PC in the last few minutes (browser.want_front), and ONE message goes into the job's own thread:

    One step needs you on "<the job>": <why>.
    I left it open in my browser, right where I stopped. ...
    [Control PC]  [Do later]

* **Control PC** opens Chrome Remote Desktop (doer.remote_desktop_url, the same link as the live card), so the owner
  finishes the step from a phone. It is a link: nothing comes back to the agent and nothing closes.
* **Do later** (card_answer {"job": <ticket>, "option": "Do later"}; gchat_events.job_click routes it here) is the
  only thing that closes it: the browser quits (only the held tab when another job is using the browser), the job is
  held, and the tapped message says when it comes back. At that time (doer.do_later_at, default 09:00, at least
  doer.do_later_min_wait_h hours on, never in quiet hours) `sweep()` (doer.tick, every ten minutes) opens the page
  again in front and posts the same two buttons in the same thread. The owner can say Do later again; each offer is
  one message, nothing is retried by the agent and nothing loops on its own.

While a page is held: browser.close_browser() refuses to quit (force=True is Do later), prune_tabs() keeps the held
tab, and a new job works in a fresh tab and is told never to touch the held one. A held page settles by itself when
its tab is gone (the owner finished and closed it), when its job runs again, or when the browser went down (then it
waits for the next slot like a Do later). The words go through voice.gate (one voice, no ids, no internal names) and
the chat-first rule. Org-free: the agent's name and spaces come from the pack. Switch: config doer.leave_open.
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (ROOT / "scripts", ROOT / "agent", str(HERE)):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from alib import config, load_json, save_json  # noqa: E402

import browser  # noqa: E402

CFG = (config().get("doer") or {})
CONTROL = "Control PC"
LATER = "Do later"
STORE = browser.LEFT_OPEN
_TID = re.compile(r"[0-9a-f]{16}")
CHANNEL = None            # tests set a stand-in with post_body / patch_body
NOW = None                # tests set a clock


def on() -> bool:
    return bool(CFG.get("leave_open", True))


def _now() -> float:
    return float(NOW() if callable(NOW) else time.time())


def _log(msg: str) -> None:
    try:
        import doer
        doer.log("left open: " + msg)
    except Exception:  # noqa: BLE001
        pass


def _load() -> dict:
    return load_json(STORE, {}) or {}


def _save(d: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    save_json(STORE, d)


def entry(tid: str) -> dict:
    return dict(_load().get(str(tid)) or {})


def held_targets() -> set:
    """The tab ids of pages left open for an owner right now (browser.py reads the same file)."""
    return {str(e.get("target") or "") for e in _load().values() if e.get("state") == "open" and e.get("target")}


def holding(except_tid: str = "") -> list:
    """Tickets whose page is open for an owner now (other than `except_tid`)."""
    return [k for k, e in _load().items() if e.get("state") == "open" and k != except_tid]


# ------------------------------------------------------------------ when it comes back
def next_slot(now: float | None = None) -> float:
    """The epoch a Do later comes back: the next doer.do_later_at (default 09:00) at least min_wait_h away."""
    now = _now() if now is None else now
    at = str(CFG.get("do_later_at") or "09:00")
    try:
        hh, mm = (int(x) for x in at.split(":", 1))
    except ValueError:
        hh, mm = 9, 0
    wait = float(CFG.get("do_later_min_wait_h", 3)) * 3600
    lt = time.localtime(now)
    for d in range(0, 8):
        cand = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + d, hh, mm, 0, 0, 0, -1))
        if cand - now >= wait:
            return cand
    return now + 86400


def when_words(ts: float, now: float | None = None) -> str:
    now = _now() if now is None else now
    clock = time.strftime("%I:%M %p", time.localtime(ts)).lstrip("0").replace(":00 ", " ")
    days = (time.localtime(ts).tm_yday - time.localtime(now).tm_yday) % 366
    if days == 0:
        return f"today at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    return f"{time.strftime('%A', time.localtime(ts))} at {clock}"


# ------------------------------------------------------------------ the message
def _channel():
    if CHANNEL is not None:
        return CHANNEL
    try:
        import livecard
        return livecard._channel()
    except Exception:  # noqa: BLE001
        return None


def _where(t: dict) -> tuple:
    try:
        import livecard
        return livecard._space_for(t)
    except Exception:  # noqa: BLE001
        return "", ""


def _remote() -> str:
    return str(CFG.get("remote_desktop_url") or "https://remotedesktop.google.com/access")


def _gate(text: str, space: str = "") -> str:
    """One voice and the chat-first rule; a gate that empties the line falls back to the plain words (the buttons
    still need a sentence over them)."""
    out = str(text or "")
    try:
        import chatfirst
        out = chatfirst.clean(out, "")
    except Exception:  # noqa: BLE001
        pass
    try:
        import voice
        out = voice.gate(out, channel="doer", space="") or voice.polish(voice.one_voice(out))
    except Exception:  # noqa: BLE001
        pass
    return " ".join(out.split()) or "One step needs you."


def body(tid: str, text: str, mode: str = "open", when: str = "") -> dict:
    """The message: the line, then [Control PC] [Do later]; after a Do later tap, the line about when, no buttons."""
    if mode == "later":
        line = f"Okay, it waits. I closed it and I'll bring it back {when}, with the same two buttons."
        return {"text": text[:3800], "cardsV2": [{"cardId": f"later-{tid}", "card": {"sections": [
            {"widgets": [{"textParagraph": {"text": html.escape(line)}}]}]}}]}
    if mode == "done":
        return {"text": text[:3800], "cardsV2": [{"cardId": f"later-{tid}", "card": {"sections": [
            {"widgets": [{"textParagraph": {"text": "This one is closed now."}}]}]}}]}
    import chat_push
    btns = [{"text": CONTROL, "onClick": {"openLink": {"url": _remote()}}},
            chat_push._button(LATER, "card_answer", {"job": tid, "option": LATER})]
    return {"text": text[:3800], "cardsV2": [{"cardId": f"later-{tid}", "card": {"sections": [
        {"widgets": [{"buttonList": {"buttons": btns}}]}]}}]}


def _record(t: dict, text: str) -> None:
    """The line on the job's transcript and on Mission Control (the message itself is the Chat copy)."""
    p = (t or {}).get("payload") or {}
    try:
        import core
        name = str((load_json(ROOT / "agent" / "profile.json", {}) or {}).get("name") or "Agent")
        core.say(str(p.get("convo") or f"dm:{p.get('from') or 'owner'}"), name, text, channel="doer", meta={"job": str(t.get("id") or "")})
    except Exception:  # noqa: BLE001
        pass
    try:
        import doer
        doer._mc_note(text)
    except Exception:  # noqa: BLE001
        pass


def _bring_front() -> bool:
    """In front unless someone is at the PC (the 2026-11-06 rule). -> True when code read back a visible window."""
    try:
        if not browser.want_front():
            return False
        ok, _ = browser.show_on_screen()
        return bool(ok)
    except Exception:  # noqa: BLE001
        return False


def _line(say: str, job: str, again: bool, when: str) -> str:
    head = (f"Back to {job}, as you asked. " if again and job else "Back to this one, as you asked. " if again else "")
    return (head + str(say or "One step needs you.").rstrip() + " I left it open in my browser, right where I stopped. "
            f"Tap Control PC to finish it from your phone, or Do later and I'll close it and bring it back {when}.")


def offer(t: dict, say: str, why: str = "", again: bool = False) -> str:
    """Hold the page the job stopped at for the owner and post the one message. -> the message name, '' when it could
    not be offered (no page up, no owner-only chat to put it in, switched off): the caller then closes and says it
    the old way."""
    tid = str((t or {}).get("id") or "")
    if not on() or not _TID.fullmatch(tid):
        return ""
    page = browser.active_page()
    url = str(page.get("url") or "")
    if not page or url.startswith(("about:", "chrome:", "devtools:")):
        return ""
    space, thread = _where(t)
    g = _channel()
    if not g or not space:
        return ""
    in_front = _bring_front()
    try:
        import doer
        job = doer.job_line(t)
    except Exception:  # noqa: BLE001
        job = ""
    text = _gate(_line(say, job, again, when_words(next_slot())), space)
    try:
        name = g.post_body(space, body(tid, text), thread)
    except Exception as e:  # noqa: BLE001
        _log(f"{tid}: post failed: {type(e).__name__}: {str(e)[:120]}")
        name = ""
    if not name:
        return ""
    d = _load()
    old = d.get(tid) or {}
    d[tid] = {"state": "open", "target": str(page.get("id") or ""), "url": url, "why": str(why or say)[:400], "say": str(say)[:600],
              "text": text, "space": space, "thread": thread, "name": str(name), "epoch": _now(), "in_front": in_front,
              "offers": int(old.get("offers") or 0) + 1, "due": 0}
    _save(d)
    _record(t, text)
    _log(f"{tid}: page held for the owner at {url[:100]} (in front={in_front}), message {name}")
    return str(name)


# ------------------------------------------------------------------ Do later
def _doer_busy() -> bool:
    """A browser job is running right now (doer's one-at-a-time lock, held by a live pid)."""
    try:
        import doer
        st = load_json(doer.LOCK, {}) or {}
        if not st:
            return False
        from drain import _pid_alive
        return bool(_pid_alive(st.get("pid")))
    except Exception:  # noqa: BLE001
        return False


def _close_tab(target: str) -> bool:
    if not target:
        return False
    try:
        urllib.request.urlopen(browser.cdp_url() + "/json/close/" + target, timeout=5).read()
        return True
    except Exception:  # noqa: BLE001
        return False


def later(tid: str, person: dict | None = None) -> dict:
    """The owner tapped Do later: close it, hold the job, say when it comes back. -> the body to show ({} = unknown)."""
    tid = str(tid or "")
    d = _load()
    e = d.get(tid)
    if not e:
        return {}
    if e.get("state") == "later":
        return body(tid, str(e.get("text") or ""), "later", when_words(float(e.get("due") or next_slot())))
    if e.get("state") != "open":
        return body(tid, str(e.get("text") or ""), "done")
    who = str((person or {}).get("key") or "owner")
    e.update({"state": "later", "due": next_slot(), "later_by": who, "later_at": _now()})
    d[tid] = e
    _save(d)                                       # saved first: the close below must not see this page as held
    if _doer_busy() or holding():
        _close_tab(str(e.get("target") or ""))     # another job (or another held page) lives in this browser: only this tab
    else:
        try:
            browser.close_browser(force=True)
        except Exception as ex:  # noqa: BLE001
            _log(f"{tid}: close failed: {type(ex).__name__}")
    _log(f"{tid}: Do later by {who}; back {time.strftime('%Y-%m-%d %H:%M', time.localtime(e['due']))}")
    try:
        import tickets
        tickets._transition(tid, "staged", "doer", f"held: Do later by {who}, offered again "
                            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(e['due']))}", {"staged", "claimed", "new"})
    except Exception:  # noqa: BLE001
        pass
    return body(tid, str(e.get("text") or ""), "later", when_words(e["due"]))


def resume(tid: str, person: dict | None = None) -> dict:
    """The owner says they finished the step themselves (agent/resume.py, 2026-11-09: "I'm done" in chat): the hold is
    released, the job goes back to staged so the browser agent carries on from its notes in the same page, and the
    message's buttons go. -> {"ok", "job"}; {"ok": False} when nothing of this job is held."""
    tid = str(tid or "")
    d = _load()
    e = d.get(tid)
    if not e or e.get("state") not in ("open", "later"):
        return {"ok": False}
    who = str((person or {}).get("key") or "owner")
    e.update({"state": "done", "done_by": who, "done_at": _now()})
    d[tid] = e
    _save(d)
    job = ""
    try:
        import tickets
        t = tickets._load().get(tid) or {}
        tickets._transition(tid, "staged", "doer", f"the owner finished the step themselves ({who} said so in chat); carry on from the notes",
                            {"staged", "claimed", "new"})
        import doer
        job = doer.job_line(t)
    except Exception as ex:  # noqa: BLE001
        _log(f"{tid}: resume transition failed: {type(ex).__name__}")
    g = _channel()
    if g and e.get("name") and hasattr(g, "patch_body"):
        try:
            g.patch_body(str(e["name"]), body(tid, str(e.get("text") or ""), "done"))
        except Exception:  # noqa: BLE001
            pass
    _log(f"{tid}: the owner ({who}) finished the step; job staged again")
    d.pop(tid, None)
    _save(d)
    return {"ok": True, "job": job}


def clear(tid: str) -> None:
    """The job runs again (or is over): its held page is no longer the owner's."""
    d = _load()
    if str(tid) in d:
        d.pop(str(tid), None)
        _save(d)


# ------------------------------------------------------------------ the tick: settle and come back
def _pages() -> list | None:
    """The browser's pages, or None when it is down."""
    if not browser.alive():
        return None
    try:
        with urllib.request.urlopen(browser.cdp_url() + "/json/list", timeout=5) as r:
            return [x for x in json.loads(r.read().decode("utf-8", "replace")) if x.get("type") == "page"]
    except Exception:  # noqa: BLE001
        return None


def _open_tab(url: str) -> dict:
    """A new tab at `url` (PUT /json/new, GET on old builds) and in front of the others. {} on failure."""
    base = browser.cdp_url() + "/json/new?" + urllib.parse.quote(url, safe=":/?&=%#")
    for method in ("PUT", "GET"):
        try:
            req = urllib.request.Request(base, method=method)
            with urllib.request.urlopen(req, timeout=8) as r:
                tab = json.loads(r.read().decode("utf-8", "replace"))
            try:
                urllib.request.urlopen(browser.cdp_url() + "/json/activate/" + str(tab.get("id")), timeout=5).read()
            except Exception:  # noqa: BLE001
                pass
            return tab
        except Exception:  # noqa: BLE001
            continue
    return {}


def _job_over(tid: str) -> bool:
    try:
        import tickets
        t = tickets._load().get(tid) or {}
    except Exception:  # noqa: BLE001
        return False
    if not t:
        return True
    try:
        import livecard
        if livecard.stopped(tid):
            return True
    except Exception:  # noqa: BLE001
        pass
    return t.get("status") not in ("staged", "new", "claimed")


def reoffer(tid: str) -> str:
    """Bring a Do later back: the page in front again and the same two buttons in the same thread. -> message name."""
    e = entry(tid)
    try:
        import tickets
        t = tickets._load().get(tid) or {}
    except Exception:  # noqa: BLE001
        t = {}
    if not e or not t:
        return ""
    try:
        browser.open_browser()
    except Exception as ex:  # noqa: BLE001
        _log(f"{tid}: could not open the browser to bring it back: {type(ex).__name__}")
        return ""
    tab = _open_tab(str(e.get("url") or ""))
    if not tab:
        return ""
    name = offer(t, str(e.get("say") or ""), str(e.get("why") or ""), again=True)
    if not name:
        _close_tab(str(tab.get("id") or ""))
    return name


def sweep(quiet: bool = False, now: float | None = None) -> list:
    """doer.tick, every ten minutes: settle held pages that are gone, and bring back ONE Do later that is due (never
    in quiet hours, never while a job runs). -> what it did, for the log."""
    now = _now() if now is None else now
    did = []
    d = _load()
    if not d:
        return did
    pages = _pages()
    ids = {str(p.get("id") or "") for p in (pages or [])}
    changed = False
    for tid, e in list(d.items()):
        if _job_over(tid) and e.get("state") != "closed":
            d.pop(tid, None)
            changed = True
            did.append(f"{tid}: job is over, forgotten")
            continue
        if e.get("state") != "open":
            continue
        if pages is None:                          # the browser went down (a restart): it waits for the next slot
            e.update({"state": "later", "due": next_slot(now), "later_by": "browser closed"})
            changed = True
            did.append(f"{tid}: browser down, back at the next slot")
        elif e.get("target") and str(e["target"]) not in ids:
            d.pop(tid, None)                       # the owner closed the tab (finished it, or let it go)
            changed = True
            did.append(f"{tid}: tab closed by the owner, settled")
    if changed:
        _save(d)
    if quiet or _doer_busy():
        return did
    due = sorted((float(e.get("due") or 0), k) for k, e in d.items() if e.get("state") == "later" and float(e.get("due") or 0) <= now)
    if due:
        tid = due[0][1]
        name = reoffer(tid)
        did.append(f"{tid}: brought back" + ("" if name else " FAILED"))
        if not name:                               # could not bring it back now: the next slot, never a tight loop
            d = _load()
            if tid in d:
                d[tid]["due"] = next_slot(now)
                _save(d)
    return did


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    if a[:1] == ["status"]:
        print(json.dumps(_load(), indent=1, ensure_ascii=False))
    elif a[:1] == ["sweep"]:
        print("\n".join(sweep()) or "nothing to do")
    else:
        print(__doc__)
