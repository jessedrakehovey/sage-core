"""livecard.py - the one Chat card that follows a job while it runs: its steps, a picture of the screen, and three buttons.

The owner asked (2026-11-03) for Chat to feel like a window into the agent's desk, and to see what the PC is doing
while they are away. So every browser or computer job gets ONE card in the chat it was asked in, posted when the job
starts and REDRAWN in place (messages.update) at each milestone note, at a checkpoint, and when it ends:

    <the job in the owner's words>
    on it since 10:41 · step 3
    ✓ signed in to the rental site
    ✓ dates set, 7 to 10 November
    ▶ adding the 35mm to the cart
    [picture of the page as it is now]
    [Control PC]  [Show on screen]  [Stop]

The picture is taken by code (the browser's own CDP for a browser job, the main screen for a computer job), hosted
for a few minutes through scripts/shots.py (a shared drive the owner made; nothing public on the PC) and shown by
Chat as an image widget. Without that drive the card still redraws, just with no picture.

"Control PC" opens Chrome Remote Desktop (config doer.remote_desktop_url): Google's own live view of the PC, where
the owner can also take the wheel. "Show on screen" brings the agent's browser onto the main monitor (it works
off-screen otherwise). "Stop" writes a stop flag the browser-tool hook reads: the next tool call is refused and the
model turn ends with DOER_BLOCKED, the ticket is staged as stopped by the owner, nothing is retried.

Where the card goes: the job's own thread when it was asked in one, else the owners' home space or the requester's
DM; never a space with staff in it (milestones were pulled from the team space on 2026-10-28). Switch: config
doer.live_card (default on). Org-free: the agent's name from the profile, spaces from the chat config.
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (ROOT / "scripts", ROOT / "agent", str(HERE)):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from alib import config, load_json, save_json  # noqa: E402

CFG = (config().get("doer") or {})
OUT = ROOT / "out" / "doer"
STOP_FILE = "stop.json"
STOPPED_FILE = "stopped.json"
LIVE_FILE = "live.json"
SHOT_EVERY_S = int(CFG.get("live_shot_every_s", 20))
STEPS_MAX = 8
REMOTE_DESKTOP = str(CFG.get("remote_desktop_url") or "https://remotedesktop.google.com/access")
WATCH, SHOW, STOP = "Control PC", "Bring it to the front", "Stop"
CHANNEL = None            # tests set a stand-in with post_body / patch_body
_TID = re.compile(r"[0-9a-f]{16}")


def on() -> bool:
    return bool(CFG.get("live_card", True))


def _log(msg: str) -> None:
    try:
        import doer
        doer.log("live card: " + msg)
    except Exception:  # noqa: BLE001
        pass


def _agent() -> str:
    try:
        return str((load_json(ROOT / "agent" / "profile.json", {}) or {}).get("name") or "Agent")
    except Exception:  # noqa: BLE001
        return "Agent"


def _channel():
    if CHANNEL is not None:
        return CHANNEL
    try:
        import chat_push
        if not chat_push.enabled():
            return None
        return chat_push._g()
    except Exception:  # noqa: BLE001
        return None


def _clock(ts: float | None = None) -> str:
    s = time.strftime("%I:%M %p", time.localtime(ts or time.time()))
    return s.lstrip("0")


# ------------------------------------------------------------------ state
def _path(tid: str) -> Path:
    return OUT / tid / LIVE_FILE


def state(tid: str) -> dict:
    return load_json(_path(tid), {}) or {}


def _save(tid: str, st: dict) -> None:
    _path(tid).parent.mkdir(parents=True, exist_ok=True)
    save_json(_path(tid), st)


def stop_requested(out_dir) -> dict:
    """The stop flag the owner's tap wrote (click_guard reads it before every browser tool). {} when none."""
    return load_json(Path(out_dir) / STOP_FILE, {}) or {}


def request_stop(tid: str, by: str = "owner") -> None:
    (OUT / tid).mkdir(parents=True, exist_ok=True)
    save_json(OUT / tid / STOP_FILE, {"by": by, "epoch": time.time()})
    close_followups(tid, by)


# An owner's Stop also ends what would carry the job on (dream review 2026-10-17: "stop" left its follow-ups queued
# and a new action-preview card could still post). stopped.json outlasts stop.json (which a come-back start clears):
# report.py posts no checkpoint / sign-in / carry-on card for the job, doer.py runs no approved step, code fallback or
# hand sign-in for it, and the job's queued agent_run exec tickets are cancelled here. A fresh claim of the job clears it.
def stopped(tid: str) -> dict:
    """The lasting stop mark of a job, {} when the owner has not stopped it."""
    return load_json(OUT / str(tid) / STOPPED_FILE, {}) or {}


def clear_stopped(tid: str) -> None:
    try:
        (OUT / str(tid) / STOPPED_FILE).unlink()
    except OSError:
        pass


def close_followups(tid: str, by: str = "owner") -> int:
    """Mark the job stopped for good and cancel its queued approved steps. -> how many were cancelled."""
    tid = str(tid or "")
    if not _TID.fullmatch(tid):
        return 0
    (OUT / tid).mkdir(parents=True, exist_ok=True)
    save_json(OUT / tid / STOPPED_FILE, {"by": by, "epoch": time.time()})
    n = 0
    try:
        import exec_store as es
        for x in es.pending():
            if x.get("kind") == "agent_run" and str((x.get("payload") or {}).get("ticket") or "") == tid:
                try:
                    es.set_status(x["id"], "cancelled", f"the owner stopped this job ({by})")
                    n += 1
                except ValueError:
                    pass                    # it started running meanwhile: the stop flag halts it at its next tool
    except Exception as e:  # noqa: BLE001
        _log(f"{tid}: closing follow-ups failed: {type(e).__name__}")
    if n:
        _log(f"{tid}: stop by {by}: {n} queued follow-up step(s) cancelled")
    return n


# ------------------------------------------------------------------ where
def _space_for(t: dict) -> tuple:
    """(space, thread) the card goes to, or ('', '') when the job has no owner-only chat to live in."""
    p = t.get("payload") or {}
    thread = str(p.get("thread") or "")
    space = str(p.get("space") or (thread.split("/threads/")[0] if "/threads/" in thread else ""))
    try:
        import chat_push
        import core
        home = core.home_space()
        dms = {str(v) for v in chat_push._spaces().values() if str(v).startswith("spaces/")}
    except Exception:  # noqa: BLE001
        home, dms = "", set()
    allowed = ({home} if home else set()) | dms
    if not space:
        key = str(p.get("from") or "")
        try:
            import chat_push
            space = home or str(chat_push._spaces().get(key) or "")
        except Exception:  # noqa: BLE001
            space = home
    if not space.startswith("spaces/") or space not in allowed:
        return "", ""                       # a staff space, or nowhere owner-only: no live card
    return space, thread


# ------------------------------------------------------------------ the picture
def _page_ws() -> str:
    """The CDP websocket of the page the job is working on (the visible, focused tab: browser.active_page), '' when
    the browser is not up. Until 2026-11-06 this took the first tab in Chrome's list, so a job that opened a second
    tab sent pictures of the one it had left."""
    try:
        import browser
        return browser.front_page_ws()
    except Exception:  # noqa: BLE001
        return ""


def _flat(values: list) -> bool:
    if len(values) < 16:
        return False
    m = sum(values) / len(values)
    return (sum((v - m) ** 2 for v in values) / len(values)) ** 0.5 < 2.0


def png_gray(png: bytes) -> list:
    """Grey levels of a small 8-bit RGB/RGBA/grey PNG, stdlib only (the agent's venvs have no Pillow). [] when the
    PNG is of a kind this does not read."""
    import struct
    import zlib
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        return []
    i, idat, w = 8, b"", 0
    while i + 8 <= len(png):
        n, kind = struct.unpack(">I4s", png[i:i + 8])
        chunk = png[i + 8:i + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype, _c, _f, inter = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or inter or ctype not in (0, 2, 6):
                return []
            bpp = {0: 1, 2: 3, 6: 4}[ctype]
        elif kind == b"IDAT":
            idat += chunk
        i += 12 + n
    if not w or not idat:
        return []
    raw, stride, out, prev = zlib.decompress(idat), w * bpp, [], bytearray(w * bpp)
    for r in range(h):
        f, line = raw[r * (stride + 1)], bytearray(raw[r * (stride + 1) + 1:(r + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b, c = prev[x], (prev[x - bpp] if x >= bpp else 0)
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += [sum(line[x:x + min(bpp, 3)]) // min(bpp, 3) for x in range(0, stride, bpp)]
        prev = line
    return out


def blank(img: bytes) -> bool:
    """True for a picture that is one flat colour (a window that did not paint: all black, all white, or the page's
    bare background). Reads a small PNG with the stdlib; a JPEG only when Pillow is there (else never blank)."""
    if img[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            return _flat(png_gray(img))
        except Exception:  # noqa: BLE001
            return False
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(img)).convert("L")
        im.thumbnail((64, 64))
        return _flat(list(im.getdata()))
    except Exception:  # noqa: BLE001
        return False


def _page_blank(browser, ws: str) -> bool:
    """A 1/20-size PNG of the page, checked for one flat colour."""
    try:
        r = browser._cdp_calls(ws, [("Page.getLayoutMetrics", {})])[0] or {}
        vp = r.get("cssLayoutViewport") or r.get("layoutViewport") or {}
        w, h = float(vp.get("clientWidth") or 0), float(vp.get("clientHeight") or 0)
        if not w or not h:
            return False
        r = browser._cdp_calls(ws, [("Page.captureScreenshot", {"format": "png", "clip": {
            "x": 0, "y": 0, "width": w, "height": h, "scale": 0.05}})])[0] or {}
        return bool(r.get("data")) and blank(base64.b64decode(r["data"]))
    except Exception:  # noqa: BLE001
        return False


def capture(t: dict, out_path: Path) -> bool:
    """A JPEG of what the job is looking at: the browser page over CDP, or the main screen for a computer job."""
    where = str((t.get("payload") or {}).get("where") or "").lower()
    try:
        import shots
        if where == "computer":
            if not shots.capture_desktop(out_path):
                return False
            if blank(Path(out_path).read_bytes()):    # a locked or sleeping screen reads back solid black
                _log("desktop capture skipped: the screen read back blank")
                Path(out_path).unlink(missing_ok=True)
                return False
            return True
        import browser
        page = browser.active_page()
        ws = str(page.get("webSocketDebuggerUrl") or "")
        if not ws:
            return False
        browser.ready_to_paint(page)                  # a minimized window paints nothing (the capture hangs)
        r = browser._cdp_calls(ws, [("Page.captureScreenshot", {"format": "jpeg", "quality": 60})])[0] or {}
        data = r.get("data") if isinstance(r, dict) else None
        if not data:
            return False
        raw = base64.b64decode(data)
        if blank(raw) or _page_blank(browser, ws):
            _log("capture skipped: the page drew a blank frame")
            return False                              # keep the last real picture rather than show a blank one
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(raw)
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"capture failed: {type(e).__name__}")
        return False


def _shot(t: dict, st: dict, force: bool = False) -> str:
    """Take and host a fresh picture when one is due; -> the url to show (the last one when not due)."""
    now = time.time()
    if not force and now - float(st.get("shot_at") or 0) < SHOT_EVERY_S:
        return str(st.get("shot_url") or "")
    tid = str(t.get("id") or "")
    path = OUT / tid / "live.jpg"
    if not capture(t, path):
        return str(st.get("shot_url") or "")
    st["shot_at"] = now
    try:
        import shots
        url = shots.publish(path) if shots.enabled() else ""
    except Exception:  # noqa: BLE001
        url = ""
    if url:
        st["shot_url"] = url
    return str(st.get("shot_url") or "")


# ------------------------------------------------------------------ the card
def _job(t: dict) -> str:
    try:
        import doer
        return doer.job_line(t, 90).strip('"') or str(t.get("title") or "a job")
    except Exception:  # noqa: BLE001
        return str(t.get("title") or "a job")


def _steps(tid: str) -> list:
    try:
        import notes
        rows = [r for r in notes.read(tid) if r.get("by", "turn") == "turn"]
    except Exception:  # noqa: BLE001
        rows = []
    return rows[-STEPS_MAX:]


def _button(label: str, tid: str, filled: bool = False) -> dict:
    import chat_push
    return chat_push._button(label, "card_answer", {"job": tid, "option": label}, filled=filled)


def body(t: dict, st: dict) -> dict:
    """The message body for the card as it stands: state running | waiting | done | stopped | blocked | failed."""
    tid = str(t.get("id") or "")
    job = _job(t)
    mode = str(st.get("state") or "running")
    started = float(st.get("started") or time.time())
    steps = _steps(tid)
    n = len(steps)
    sub = {"running": f"on it since {_clock(started)}" + (f" · step {n}" if n else ""),
           "waiting": "waiting for your tap on the card below",
           "done": f"done at {_clock(st.get('ended'))}",
           "stopped": f"stopped by you at {_clock(st.get('ended'))}",
           "blocked": "stopped: one step needs you",
           "failed": "could not finish"}.get(mode, mode)
    widgets = []
    if steps:
        for i, r in enumerate(steps):
            last = i == n - 1 and mode == "running"
            icon = "▶" if last else ("⚠" if r.get("kind") == "blocked" else "✓")
            widgets.append({"decoratedText": {"text": f"{icon} {html.escape(str(r.get('text') or '')[:200])}", "wrapText": True}})
    elif mode == "running":
        widgets.append({"textParagraph": {"text": html.escape(str(st.get("phase") or "Opening the browser"))}})
    if st.get("final"):
        widgets.append({"textParagraph": {"text": f"<b>{html.escape(str(st['final'])[:600])}</b>"}})
    sections = [{"widgets": widgets}] if widgets else []
    if st.get("shot_url"):
        sections.append({"widgets": [{"image": {"imageUrl": str(st["shot_url"]), "altText": "what is on the screen now"}}]})
    btns = []
    if mode == "running":
        btns = [{"text": WATCH, "onClick": {"openLink": {"url": REMOTE_DESKTOP}}}, _button(SHOW, tid), _button(STOP, tid)]
    elif mode == "waiting":
        btns = [{"text": WATCH, "onClick": {"openLink": {"url": REMOTE_DESKTOP}}}, _button(SHOW, tid)]
    foot = []
    if st.get("on_screen"):
        foot.append("It is in front now: Control PC shows it.")
    if st.get("stopping"):
        foot.append("Stopping at the next step.")
    if st.get("shot_at"):
        foot.append(f"Picture from {_clock(st['shot_at'])}.")
    if btns or foot:
        w = ([{"buttonList": {"buttons": btns}}] if btns else []) + \
            ([{"textParagraph": {"text": f'<font color="#777777">{html.escape(" ".join(foot))}</font>'}}] if foot else [])
        sections.append({"widgets": w})
    text = {"running": f"On it: {job}", "waiting": f"Waiting for you: {job}", "done": f"Done: {job}",
            "stopped": f"Stopped: {job}", "blocked": f"Needs you: {job}", "failed": f"Could not finish: {job}"}.get(mode, job)
    return {"text": text[:300],
            "cardsV2": [{"cardId": f"live-{tid}", "card": {"header": {"title": job[:200], "subtitle": sub[:200]}, "sections": sections}}]}


def _redraw(t: dict, st: dict) -> bool:
    g = _channel()
    name = str(st.get("name") or "")
    if not g or not name:
        return False
    try:
        ok = bool(g.patch_body(name, body(t, st)))
    except Exception as e:  # noqa: BLE001
        _log(f"redraw failed: {type(e).__name__}: {str(e)[:120]}")
        ok = False
    return ok


# ------------------------------------------------------------------ the hooks the doer calls
def start(t: dict, phase: str = "") -> str:
    """Post the card for a job that is starting. -> the message name ('' when no card goes out)."""
    if not on() or not isinstance(t, dict) or not _TID.fullmatch(str(t.get("id") or "")):
        return ""
    tid = t["id"]
    space, thread = _space_for(t)
    g = _channel()
    if not g or not space:
        return ""
    old = state(tid)
    st = {"state": "running", "started": time.time(), "space": space, "thread": thread, "phase": phase,
          "shot_url": "", "shot_at": 0, "name": str(old.get("name") or "")}
    try:                                         # in front by default (browser.want_front): the card says so when it is
        import browser
        bs = browser.status()
        if bs.get("up") and bs.get("offscreen") is False:
            st["on_screen"] = True
    except Exception:  # noqa: BLE001
        pass
    try:
        (OUT / tid / STOP_FILE).unlink()          # a fresh start forgets an old stop
    except OSError:
        pass
    _shot(t, st, force=True)                     # the first picture, when the browser is already up
    if st["name"]:                               # the job came back (a login, an approved step): the same card, redrawn
        _save(tid, st)
        _redraw(t, st)
        return st["name"]
    try:
        name = g.post_body(space, body(t, st), thread)
    except Exception as e:  # noqa: BLE001
        _log(f"post failed: {type(e).__name__}: {str(e)[:120]}")
        name = ""
    st["name"] = str(name or "")
    _save(tid, st)
    return st["name"]


def touch(tid: str, phase: str = "") -> bool:
    """A milestone or a phase change: a fresh picture when due, and the card redrawn. Cheap when there is no card."""
    st = state(str(tid))
    if not st.get("name") or st.get("state") not in ("running", "waiting"):
        return False
    t = _ticket(tid)
    if not t:
        return False
    if phase:
        st["phase"] = phase
    _shot(t, st)
    _save(tid, st)
    return _redraw(t, st)


def finish(tid: str, marker: str, final: str = "", quiet: bool = False) -> bool:
    """The job ended (or stopped at a checkpoint): the card's last state. The job's report goes up as its own message
    right under the card, so the card does not repeat it (2026-11-04: the same paragraph stood twice in the thread);
    `quiet` = nothing was said in chat, and then `final` is the card's last line. A job waiting on a tap keeps one last
    picture of the page it stopped at; a job that is over drops its picture (the hosted file is deleted minutes later
    and would stand on the card as a broken image)."""
    st = state(str(tid))
    if not st.get("name"):
        return False
    t = _ticket(tid)
    if not t:
        return False
    stopped = bool(stop_requested(OUT / tid)) and marker in ("DOER_BLOCKED", "", None)
    st["state"] = ("stopped" if stopped else "done" if marker in ("DOER_DONE", "DOER_ANSWER") else
                   "waiting" if marker == "DOER_CHECKPOINT" else "blocked" if marker == "DOER_BLOCKED" else "failed")
    st["ended"] = time.time()
    st["final"] = " ".join(str(final or "").split())[:600] if quiet else ""
    st.pop("stopping", None)
    if st["state"] == "waiting":
        _shot(t, st, force=True)
    else:
        st["shot_url"], st["shot_at"] = "", 0
    _save(tid, st)
    return _redraw(t, st)


def expire_pictures(ttl_s: float | None = None, now: float | None = None) -> int:
    """A card whose picture is past its lifetime (the hosted file is gone) is redrawn without it. Called by the
    picture sweep (scripts/shots.py sweep, every ten minutes). -> how many cards were redrawn."""
    try:
        import shots
        ttl = float(ttl_s if ttl_s is not None else (shots._cfg().get("ttl_s") or shots.DEFAULT_TTL_S))
    except Exception:  # noqa: BLE001
        ttl = float(ttl_s or 900)
    now = time.time() if now is None else now
    n = 0
    for p in sorted(OUT.glob(f"*/{LIVE_FILE}")):
        st = load_json(p, {}) or {}
        if not st.get("shot_url") or now - float(st.get("shot_at") or 0) < ttl or st.get("state") == "running":
            continue
        tid = p.parent.name
        st["shot_url"], st["shot_at"] = "", 0
        _save(tid, st)
        t = _ticket(tid)
        try:
            if t and st.get("name") and _redraw(t, st):
                n += 1
        except Exception as e:  # noqa: BLE001
            _log(f"{tid}: picture expiry redraw failed: {type(e).__name__}: {str(e)[:100]}")
    return n


def action(tid: str, option: str, person: dict | None = None) -> dict:
    """A tap on the card's own buttons -> the body to show now ({} when the card is not live)."""
    tid = str(tid or "")
    st = state(tid)
    t = _ticket(tid)
    if not st.get("name") or not t:
        return {}
    who = str((person or {}).get("key") or "owner")
    if option == STOP:
        request_stop(tid, who)
        st["stopping"] = True
        _log(f"{tid}: stop asked by {who}")
    elif option == SHOW:
        try:
            import browser
            ok, detail = browser.show_on_screen()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, type(e).__name__
        st["on_screen"] = bool(ok)
        if not ok:
            st["phase"] = f"Could not bring the browser to the front ({str(detail)[:80]})"
        _log(f"{tid}: show on screen by {who}: {ok} {detail}")
    else:
        return {}
    _save(tid, st)
    b = body(t, st)
    _redraw(t, st)
    return b


def _ticket(tid: str) -> dict:
    try:
        import tickets
        return tickets._load().get(str(tid)) or {}
    except Exception:  # noqa: BLE001
        return {}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    if len(a) == 2 and a[0] == "state":
        print(json.dumps(state(a[1]), indent=1))
    elif len(a) == 2 and a[0] == "body":
        print(json.dumps(body(_ticket(a[1]), state(a[1])), indent=1, ensure_ascii=False))
    else:
        print(__doc__)
