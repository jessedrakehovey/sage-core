"""report.py - the doer's two ways to talk mid-task: a CHECKPOINT card, and a milestone NOTE.

The doer's model turn runs `report.py checkpoint --ticket <id> --file <json>` when it reaches
an irreversible step (tier C in policy.json) or needs a verification code. This script, with
no model, reads the JSON the turn wrote, builds the card so that the step sentence and every
value are visible on it (cards.py refuses otherwise: EXEC_VERBATIM), attaches the review-page
screenshot and posts it. The owner's Approve mints an `agent_run` exec ticket; drain hands it
to `doer.py --approved`, which resumes the doer's own session to perform exactly that step.

`report.py note --ticket <id> [--kind progress|blocked|done] "<one line>"` (2026-10-28) is the
milestone note: appended to the job's notes.jsonl (the checkpoint a continued turn reads back),
written to the live status line, and held on the job's transcript, off Chat (notes.py).

`report.py evidence --ticket <id> --what .. --detail .. [--file <path>] [--failed]` (2026-11-10) writes the
job's evidence.json by code, so the turn never has to name a path (the vault's has a space).

A HAND-OFF checkpoint ("handoff": true, 2026-10-28) is a last step the owner does by hand (types the
payment, signs): code brings the doer's browser on screen, maximized and in front, BEFORE the card
posts; the card says "open on your screen" only when code read the window back as visible, and it has
no Approve (the doer never presses that last button). The read-back is kept in handoff.json.

Prints CHECKPOINT=<card id> / NOTE=<n> on success, else REFUSED <why> (exit 1).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))
import cards  # noqa: E402
import tickets  # noqa: E402

OUT = ROOT / "out" / "doer"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")
# a code card's other buttons (2026-10-07: the Facebook text never arrived and the card had no way
# out). doer.tick reads these answers and resumes the task on the chosen sign-in method.
NO_CODE = "No code came"
USE_INSTEAD = "Use instead: "
HANDOFF_DONE = "I finished it"
# the sign-in card (2026-11-03): its approve ("Enter login") is a dialog button in chat (chat_push renders it as the vault
# dialog, whose submit answers the card), SELF puts the browser on the owner's screen (doer.self_signin), STOP ends the job
ENTER_LOGIN = "Enter login"
SELF = "I'll sign in myself"
CARRY_ON = "Carry on"
STOP = "Stop"
_HUMAN = re.compile(r"captcha|robot|human|verify you|wants a person|by hand", re.I)
_HOST = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$", re.I)


def _host_of(host: str, url: str) -> str:
    h = str(host or "").strip().lower()
    if not _HOST.fullmatch(h):
        from urllib.parse import urlparse
        h = (urlparse(str(url or "")).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _stopped(tid: str) -> bool:
    """The owner stopped this job (livecard.close_followups): no new action-preview card for it."""
    try:
        import livecard
        return bool(livecard.stopped(tid))
    except Exception:  # noqa: BLE001
        return False


def build_login_question(tid: str, host: str, url: str, why: str) -> dict:
    """The sign-in card: ONE card in the job's thread when login_fill came back NO_ENTRY / FAILED / a captcha. Its exec
    is the usual agent_run with payload.login, so either approval (the vault dialog's Save, or Carry on after a hand
    sign-in) mints the ticket drain hands to doer.run_login_resume."""
    h = _host_of(host, url)
    if not h:
        raise ValueError("host is required: the site's web address (stripe.com)")
    why = " ".join(str(why or "").split())[:300]
    human = bool(_HUMAN.search(why))
    t = tickets._load().get(tid) or {}
    step = f"Sign in to {h} and carry on with the job"
    if human:
        reason = f"{h} wants a person at its sign-in ({why or 'a check only a person can pass'})."
        what = (f"'{SELF}' lets you pass it from your phone: Control PC opens my browser at the sign-in page; "
                f"tap Carry on when you are in. '{STOP}' ends the job.")
        options = [{"label": SELF}, {"label": STOP}]
        approve = CARRY_ON
    else:
        reason = (f"My vault has no login for {h}." if "NO_ENTRY" in why.upper() or "no login" in why.lower()
                  else f"The {h} login did not work ({why or 'the site refused it'}).")
        what = (f"Tap '{ENTER_LOGIN}': the form opens on top of chat and goes straight to my vault, never into this "
                f"conversation; I carry on from where I stopped. '{SELF}' is there if you would rather do it through Control PC. '{STOP}' ends the job.")
        options = [{"label": ENTER_LOGIN}, {"label": SELF}, {"label": STOP}]
        approve = ENTER_LOGIN
    lines = ["[ACTION PREVIEW]", step, f"On: {url}" if url else "", reason, what]
    payload = {"ticket": tid, "step": step, "values": {}, "url": str(url or ""), "host": h, "login": True, "human": human,
               "resume": "sign in (login_fill.py from the vault, or the owner's own sign-in), then carry on from the last milestone note"}
    return {"question": "\n".join(x for x in lines if x), "header": f"Sign-in needed: {h}"[:60],
            "lane": t.get("lane") or "personal", "audience": "owner", "urgent": True,
            "options": options, "exec": {"kind": "agent_run", "approve_label": approve, "payload": payload}}


def login(tid: str, host: str, url: str, why: str) -> str:
    """-> 'LOGIN=<card id>' or 'REFUSED <why>'. The card lands in the thread the job was asked in."""
    if not re.fullmatch(r"[0-9a-f]{16}", tid or ""):
        return "REFUSED ticket id must be the 16-hex assistant ticket id"
    if _stopped(tid):
        return "REFUSED the owner stopped this job; end with DOER_BLOCKED: stopped by the owner"
    try:
        q = build_login_question(tid, host, url, why)
    except ValueError as e:
        return f"REFUSED {e}"
    thread = str(((tickets._load().get(tid) or {}).get("payload") or {}).get("thread") or "")
    r = cards.post([q], owner="doer", lane=q["lane"], thread=thread, asked=True)
    if not r.get("posted"):
        return f"REFUSED {r.get('reason')}"
    return f"LOGIN={r['qid']}"


def carry_on_card(tid: str, payload: dict, on_screen: tuple | None = None) -> str:
    """After 'I'll sign in myself': the card whose Carry on resumes the job (exec payload login + self). -> card id or ''."""
    if _stopped(tid):
        return ""
    h = str(payload.get("host") or "the site")
    url = str(payload.get("url") or "")
    ok, detail = on_screen or (False, "not checked")
    where = ("My browser is at the sign-in page; Control PC opens it from your phone." if ok else
             "My browser is at the sign-in page (I could not confirm it is in front); Control PC opens it either way.")
    step = f"Carry on with the job once you have signed in to {h}"
    lines = ["[ACTION PREVIEW]", step, f"On: {url}" if url else "", where,
             f"Once you are in, tap '{CARRY_ON}'. '{STOP}' ends the job."]
    p = {"ticket": tid, "step": step, "values": {}, "url": url, "host": h, "login": True, "self": True,
         "resume": "check the page is signed in (login_fill.py says ALREADY), then carry on from the last milestone note"}
    q = {"question": "\n".join(x for x in lines if x), "header": f"Signed in to {h}?"[:60],
         "lane": (tickets._load().get(tid) or {}).get("lane") or "personal", "audience": "owner", "urgent": True,
         "options": [{"label": CARRY_ON}, {"label": STOP}], "exec": {"kind": "agent_run", "approve_label": CARRY_ON, "payload": p}}
    thread = str(((tickets._load().get(tid) or {}).get("payload") or {}).get("thread") or "")
    r = cards.post([q], owner="doer", lane=q["lane"], thread=thread, asked=True)
    return str(r.get("qid") or "") if r.get("posted") else ""


def build_question(tid: str, spec: dict, on_screen: tuple | None = None) -> dict:
    step = " ".join(str(spec.get("step") or "").split())
    if not step:
        raise ValueError("step is required: one sentence, the exact final action")
    values = spec.get("values") or {}
    if not isinstance(values, dict):
        raise ValueError("values must be an object of label -> value")
    url = str(spec.get("url") or "").strip()
    resume = " ".join(str(spec.get("resume") or "").split())
    if not resume:
        raise ValueError("resume is required: what to do once approved")
    ask_code = bool(spec.get("ask_code"))
    shot = _SAFE.sub("", str(spec.get("screenshot") or "").replace("\\", "/").rsplit("/", 1)[-1])
    if shot and not (OUT / tid / shot).is_file():
        shot = ""                       # a wrong name must not block the card; the owner still sees the words
    command = " ".join(str(spec.get("command") or "").split())
    lines = ["[ACTION PREVIEW]", step]
    if url:
        lines.append(f"On: {url}")
    if command:
        lines.append(f"Runs exactly: {command}")
    if values:
        lines.append("Values this commits:")
        lines += [f"- {k}: {v}" for k, v in values.items()]
    other = spec.get("other_methods") if isinstance(spec.get("other_methods"), list) else []
    methods = [" ".join(str(m).split())[:80] for m in other if str(m).strip()][:4] if ask_code else []
    handoff = bool(spec.get("handoff")) and not ask_code
    if handoff:
        ok, detail = on_screen or (False, "not checked")
        lines.append("This last step is yours; the doer stopped before it."
                     + (" It is waiting in my browser; Control PC opens it from your phone."
                        if ok else " It is waiting in my browser (I could not confirm it is in front); Control PC opens it either way."))
        lines.append(f"Tap '{HANDOFF_DONE}' when done, or Cancel.")
    elif ask_code:
        lines.append("A verification code was sent to you. Tap Enter code and type it there, or reply here with the digits; I type it in and carry on.")
        lines.append(f"No code? Tap '{NO_CODE}' and the doer switches to another way on its own"
                     + (", or pick one of the site's other ways." if methods else "."))
    else:
        lines.append("Tap Approve and the doer performs exactly this step, nothing more. Cancel stops it; the task is closed.")
    if shot:
        lines.append(f"Screenshot: {shot}")
    payload = {"ticket": tid, "step": step, "values": values, "url": url, "resume": resume, "ask_code": ask_code}
    if shot:
        payload["screenshot"] = shot
    if command:
        payload["command"] = command
    t = tickets._load().get(tid) or {}
    if handoff:                          # no exec: an Approve would have the doer press the owner's own button
        return {"question": "\n".join(lines), "header": f"Doer: {str(t.get('title') or 'task')[:60]}",
                "lane": t.get("lane") or "personal", "audience": "owner",
                "options": [{"label": HANDOFF_DONE}, {"label": "Cancel"}],
                "no_recommendation": "a status tap on the owner's own last step, nothing to recommend"}
    return {"question": "\n".join(lines), "header": f"Doer: {str(t.get('title') or 'task')[:60]}",
            "lane": t.get("lane") or "personal", "audience": "owner",
            "options": [{"label": "Approve"}] + ([{"label": USE_INSTEAD + m} for m in methods] + [{"label": NO_CODE}] if ask_code else [])
                       + [{"label": "Cancel"}],
            "exec": {"kind": "agent_run", "approve_label": "Approve", "payload": payload}}


def _bring_on_screen(tid: str) -> tuple:
    """The doer's browser on the main monitor, in front; (ok, detail) as code read it back, kept in
    out/doer/<ticket>/handoff.json for doer.py (keep the browser open, say 'open on your screen')."""
    try:
        import browser
        ok, detail = browser.show_on_screen()
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"{type(e).__name__}"
    try:
        (OUT / tid).mkdir(parents=True, exist_ok=True)
        (OUT / tid / "handoff.json").write_text(json.dumps({"on_screen": bool(ok), "detail": detail, "epoch": time.time()}), encoding="utf-8")
    except OSError:
        pass
    return bool(ok), detail


# a checkpoint whose step is the owner answering the site's own check on another device: the page must not move
_WAIT_RE = re.compile(r"(?:verif\w*|security code|one[- ]time (?:code|passcode)|passcode|two[- ]step|2fa|authenticator|"
                      r"(?:push|sign[- ]in) (?:notification|prompt)|prompt (?:sent|on your phone)|"
                      r"(?:tap|approve|press|choose)\s+\W?(?:yes|approve|allow|it.?s me)\b[^.]{0,60}\b(?:phone|app|device|notification|prompt))", re.I)


def is_wait(spec: dict) -> bool:
    """True when the checkpoint waits on the owner passing a sign-in check (a code, a tap on their phone)."""
    if not isinstance(spec, dict) or spec.get("handoff"):
        return False
    return bool(spec.get("ask_code") or spec.get("wait_on_page") or _WAIT_RE.search(str(spec.get("step") or "")))


def checkpoint(tid: str, spec_file: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{16}", tid or ""):
        return "REFUSED ticket id must be the 16-hex assistant ticket id"
    if _stopped(tid):
        return "REFUSED the owner stopped this job; end with DOER_BLOCKED: stopped by the owner"
    try:
        spec = json.loads(Path(spec_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f"REFUSED cannot read the JSON file ({type(e).__name__})"
    on_screen = None
    if spec.get("handoff") and not spec.get("ask_code"):
        on_screen = _bring_on_screen(tid)                 # before the card posts, so the card can say it truthfully
    try:
        q = build_question(tid, spec, on_screen)
    except ValueError as e:
        return f"REFUSED {e}"
    thread = str(((tickets._load().get(tid) or {}).get("payload") or {}).get("thread") or "")
    r = cards.post([q], owner="doer", lane=q["lane"], thread=thread)      # in the thread the job was asked in
    if not r.get("posted"):
        return f"REFUSED {r.get('reason')}"
    if is_wait(spec):                    # the tap or the code answers THIS page: it stays, untouched, until they answer
        try:
            import browser
            browser.hold_wait(tid)
        except Exception:  # noqa: BLE001
            pass
    if on_screen is not None:            # tie the read-back to this card, so doer.py words only this one
        hf = OUT / tid / "handoff.json"
        try:
            hf.write_text(json.dumps(dict(json.loads(hf.read_text(encoding="utf-8")), qid=r["qid"])), encoding="utf-8")
        except (OSError, ValueError):
            pass
    return f"CHECKPOINT={r['qid']}"


def evidence(tid: str, what: str = "", detail: str = "", ok: bool = True, files=(), note: str = "") -> str:
    """`report.py evidence` (2026-11-10): code writes the job's evidence.json, so the turn never names that path.
    Before this the turn had to write <out_dir>/evidence.json itself, and the vault path has a space in it:
    the 2026-10-18 admin-page job could not, and its DONE reached chat as an unchecked
    claim. Appends one check row (and a bytes + sha256 row per --file) to out/doer/<ticket>/evidence.json,
    the file computer.verify_evidence re-checks. Prints EVIDENCE=<rows> or REFUSED <why>."""
    if not re.fullmatch(r"[0-9a-f]{16}", tid or ""):
        return "REFUSED ticket id must be the 16-hex assistant ticket id"
    if not (str(what or "").strip() or str(detail or "").strip() or files):
        return "REFUSED give --what/--detail (what you read back) or --file"
    import hashlib
    f = OUT / tid / "evidence.json"
    try:
        ev = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    except (OSError, ValueError):
        ev = {}
    ev = ev if isinstance(ev, dict) else {}
    checks, rows, notes_ = list(ev.get("checks") or []), list(ev.get("files") or []), ev.get("notes") or []
    notes_ = [notes_] if isinstance(notes_, str) else list(notes_)
    what, detail = " ".join(str(what or "").split())[:200], " ".join(str(detail or "").split())[:400]
    if what or detail:
        checks.append({"what": what or "read back on the page", "ok": bool(ok), "detail": detail})
    for p in files or ():
        path = Path(str(p).strip().strip("\"'")).expanduser()
        if not path.is_absolute():
            path = ROOT.parent / path
        if not path.is_file():
            return f"REFUSED no such file: {p}"
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        rows.append({"path": str(path), "bytes": path.stat().st_size, "sha256": h.hexdigest()})
    if note:
        notes_.append(" ".join(str(note).split())[:300])
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({**ev, "checks": checks, "files": rows, "notes": notes_}, indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        return f"REFUSED cannot write the evidence file ({type(e).__name__})"
    return f"EVIDENCE={len(checks) + len(rows)}"


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("checkpoint"); p.add_argument("--ticket", required=True); p.add_argument("--file", required=True)
    n = sub.add_parser("note"); n.add_argument("--ticket", required=True)
    n.add_argument("--kind", default="progress", choices=["progress", "blocked", "done"]); n.add_argument("text", nargs="+")
    lg = sub.add_parser("login"); lg.add_argument("--ticket", required=True); lg.add_argument("--host", default="")
    lg.add_argument("--url", default=""); lg.add_argument("--why", default="")
    e = sub.add_parser("evidence"); e.add_argument("--ticket", required=True); e.add_argument("--what", default="")
    e.add_argument("--detail", default=""); e.add_argument("--failed", action="store_true")
    e.add_argument("--file", action="append", default=[]); e.add_argument("--note", default="")
    a = ap.parse_args()
    if a.cmd == "evidence":
        line = evidence(a.ticket, a.what, a.detail, not a.failed, a.file, a.note)
    elif a.cmd == "note":
        import notes
        line = notes.record(a.ticket, " ".join(a.text), a.kind)
    elif a.cmd == "login":
        line = login(a.ticket, a.host, a.url, a.why)
    else:
        line = checkpoint(a.ticket, a.file)
    print(line)
    return 0 if line.startswith(("CHECKPOINT=", "NOTE=", "LOGIN=", "EVIDENCE=")) else 1


if __name__ == "__main__":
    sys.exit(main())
