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
                     + (" It is open on your screen now, maximized on your main monitor."
                        if ok else f" I could not confirm the browser is on your screen ({detail[:80]})."))
        lines.append(f"Tap '{HANDOFF_DONE}' when done, or Cancel.")
    elif ask_code:
        lines.append("A verification code was sent to you. Type it in the note box, then tap Approve; the doer types it and continues.")
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


def checkpoint(tid: str, spec_file: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{16}", tid or ""):
        return "REFUSED ticket id must be the 16-hex assistant ticket id"
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
    if on_screen is not None:            # tie the read-back to this card, so doer.py words only this one
        hf = OUT / tid / "handoff.json"
        try:
            hf.write_text(json.dumps(dict(json.loads(hf.read_text(encoding="utf-8")), qid=r["qid"])), encoding="utf-8")
        except (OSError, ValueError):
            pass
    return f"CHECKPOINT={r['qid']}"


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("checkpoint"); p.add_argument("--ticket", required=True); p.add_argument("--file", required=True)
    n = sub.add_parser("note"); n.add_argument("--ticket", required=True)
    n.add_argument("--kind", default="progress", choices=["progress", "blocked", "done"]); n.add_argument("text", nargs="+")
    a = ap.parse_args()
    if a.cmd == "note":
        import notes
        line = notes.record(a.ticket, " ".join(a.text), a.kind)
    else:
        line = checkpoint(a.ticket, a.file)
    print(line)
    return 0 if line.startswith(("CHECKPOINT=", "NOTE=")) else 1


if __name__ == "__main__":
    sys.exit(main())
