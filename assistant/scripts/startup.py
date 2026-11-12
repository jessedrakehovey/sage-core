"""startup.py - the agent's start-up sequence, run from ONE card in Google Chat (2026-11-04).

The owner's rule: the agent is 100% inside Google Chat, a portal into the PC. So the Google-side setup a new
organization needs after the PC install is not a runbook: the moment an owner first adds the agent in Chat (and any
time they say "finish setting up"), ONE card arrives, "Getting set up", listing the jobs the agent can do by itself,
with one button. The tap runs them in order on this PC, in the agent's own browser, and the card redraws after each:

    Getting set up · 1 of 2 done
    ✅ Chat app Home tab and commands
    ⏳ Screenshots shared drive (pictures on the job cards)
    [Set up everything]

What is in the sequence is DATA: every row of connections.json whose setup is kind `job` with `"startup": true`, in
registry order (today: chat-home, shots-drive). A job that meets a sign-in the vault cannot answer stops the
sequence at that row with **Enter login** (the vault dialog, in Chat); saved, the sequence carries on by itself.
Nothing here ever sends the owner to the PC. A row whose own check already passes is shown done and skipped.

  python assistant/scripts/startup.py status         # the sequence and what is still to do
  python assistant/scripts/startup.py card           # the card as it would be posted now
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (HERE, ROOT / "agent"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from alib import DATA, load_json, save_json  # noqa: E402

RID = "startup"
STATE = Path(os.environ.get("STARTUP_STATE") or (DATA / "startup_state.json"))
TITLE = "Getting set up"
BUTTON = "Set up everything"
_ICON = {"ok": "✅", "running": "⏳", "needs_login": "🔑", "failed": "❌", "todo": "○"}


def _registry() -> list:
    import setup_actions
    return setup_actions._registry()


def rows() -> list:
    """The start-up jobs, in registry order: [{"rid", "name", "label", "how"}]."""
    out = []
    for r in _registry():
        s = r.get("setup") or {}
        if isinstance(s, dict) and s.get("kind") in ("job", "job_form") and s.get("startup"):
            out.append({"rid": str(r.get("id")), "name": str(r.get("name") or r.get("id")), "label": str(s.get("label") or "Run it"), "kind": str(s.get("kind")),
                        "how": str(s.get("how") or ""), "check": r.get("check") or {}})
    return out


def _passes(row: dict) -> bool:
    """The row's own connection check, run in-process (conn_check's functions). Replaceable in tests."""
    try:
        import conn_check
        c = row.get("check") or {}
        fn = conn_check.CHECKS.get(str(c.get("type") or ""))
        if not fn:
            return False
        state, _detail = fn(c)
        return state == conn_check.LIVE
    except Exception:  # noqa: BLE001
        return False


def status(fresh: bool = True) -> list:
    """Every start-up job with done True/False. `fresh` False reuses a result a few minutes old (the Home tab)."""
    st = load_json(STATE, {}) or {}
    if not fresh and time.time() - float(st.get("checked_at") or 0) < 300 and isinstance(st.get("done"), dict):
        return [dict(r, done=bool(st["done"].get(r["rid"]))) for r in rows()]
    out = [dict(r, done=_passes(r)) for r in rows()]
    st.update({"checked_at": time.time(), "done": {r["rid"]: r["done"] for r in out}})
    try:
        save_json(STATE, st)
    except Exception:  # noqa: BLE001
        pass
    return out


def pending(fresh: bool = True) -> list:
    return [r for r in status(fresh) if not r["done"]]


# ------------------------------------------------------------------ the card
def _btn(label: str, fn: str, params: dict, dialog: bool = False, filled: bool = False) -> dict:
    import chat_push
    return chat_push._button(label, fn, params, interaction="OPEN_DIALOG" if dialog else "", filled=filled)


def card(results: dict | None = None, running: str = "", st: list | None = None) -> dict:
    """The message body. `results`: {rid: the job's result dict} from this run; `running`: the rid at work now."""
    import html
    results = results or {}
    st = st if st is not None else status()
    widgets, n_done, stuck = [], 0, None
    for r in st:
        res = results.get(r["rid"])
        state = ("running" if r["rid"] == running else str(res.get("state") or ("ok" if res.get("ok") else "failed")) if res
                 else "ok" if r["done"] else "todo")
        if state == "ok":
            n_done += 1
        line = f"{_ICON.get(state, '•')} <b>{html.escape(r['name'])}</b>"
        if state in ("needs_login", "failed") and res:
            line += f"<br>{html.escape(str(res.get('message') or '')[:400])}"
            stuck = stuck or (r, res)
        elif state == "todo":
            line += f"<br><font color=\"#777777\">{html.escape(r['how'].split('. ')[0][:160])}.</font>"
        widgets.append({"textParagraph": {"text": line}})
    total = len(st)
    buttons = []
    if running:
        widgets.append({"textParagraph": {"text": "<font color=\"#777777\">Working on it. This card updates itself after each step.</font>"}})
    elif stuck and stuck[1].get("site"):
        buttons = [_btn("Enter login", "vault_add", {"site": str(stuck[1]["site"])[:200], "rid": RID}, dialog=True, filled=True),
                   _btn("Try again", "form_action", {"rid": RID, "do": "run"})]
    elif n_done < total:
        auto = [r for r in st if not r["done"] and r.get("kind", "job") == "job" and not (results.get(r["rid"]) or {}).get("ok")]
        typed = [r for r in st if not r["done"] and r.get("kind") == "job_form"]
        if auto:
            buttons.append(_btn(BUTTON if not results else "Try again", "form_action", {"rid": RID, "do": "run"}, filled=True))
        for r in typed[:3]:                           # a step that needs one thing typed: its own form, opened from here
            buttons.append(_btn(r["label"], "form_open", {"rid": r["rid"], "from": RID}, dialog=True, filled=not auto))
    else:
        widgets.append({"textParagraph": {"text": "All set. Everything from here happens in this chat."}})
    if buttons:
        widgets.append({"buttonList": {"buttons": buttons}})
    sub = "working" if running else (f"{n_done} of {total} done" if total else "nothing to set up")
    return {"text": f"{TITLE}: {sub}."[:300],
            "cardsV2": [{"cardId": "startup", "card": {"header": {"title": TITLE, "subtitle": sub}, "sections": [{"widgets": widgets or [
                {"textParagraph": {"text": "Nothing to set up here."}}]}]}}]}


def offer_line() -> str:
    n = len(pending(fresh=False))
    return (f"There {'is one thing' if n == 1 else f'are {n} things'} I can set up myself so this chat works like an app. "
            f"Tap {BUTTON} on the card and I do them; nothing for you to do at a computer.") if n else "Everything is already set up."


def offer(space: str, thread: str = "", force: bool = False) -> bool:
    """Post the card where the owner is, once, when something is still to do (`force`: they asked for it). -> posted."""
    st = load_json(STATE, {}) or {}
    todo = pending()
    if not str(space or "").startswith("spaces/") or (not todo and not force) or (st.get("offered_at") and not force):
        return False
    try:
        import gchat
        name = gchat.post_body(space, card(), thread)
    except Exception:  # noqa: BLE001
        name = ""
    if name:
        st = load_json(STATE, {}) or {}
        st.update({"offered_at": time.strftime("%Y-%m-%d %H:%M"), "offered_in": space})
        save_json(STATE, st)
    return bool(name)


# ------------------------------------------------------------------ the run
def _run_row(rid: str, by: str) -> dict:
    """One job, through the same runner as its own button. Replaceable in tests."""
    import setup_actions
    return setup_actions.run(rid, by=by)


def run_all(by: str, on_update) -> dict:
    """Every job still to do, in order; `on_update(card body)` after each change. Stops at the first job that needs a
    login the vault lacks (the card then carries Enter login) or fails. -> {rid: result}."""
    results: dict = {}
    for r in status():
        if r["done"] or r.get("kind") == "job_form":      # a typed step has its own button; it is not run from here
            continue
        on_update(card(results, running=r["rid"], st=_overlay(results)))
        try:
            res = _run_row(r["rid"], by)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "state": "failed", "message": f"{type(e).__name__}: {str(e)[:160]}"}
        results[r["rid"]] = res
        if not res.get("ok"):
            break
    on_update(card(results, st=_overlay(results)))
    st = load_json(STATE, {}) or {}
    st.update({"last_run": time.strftime("%Y-%m-%d %H:%M"), "last_by": by, "checked_at": 0})
    save_json(STATE, st)
    return results


def _overlay(results: dict) -> list:
    """The status list with this run's finished jobs marked done, without re-running every check for each redraw."""
    st = load_json(STATE, {}) or {}
    done = dict(st.get("done") or {})
    return [dict(r, done=bool(done.get(r["rid"])) or bool((results.get(r["rid"]) or {}).get("ok"))) for r in rows()]


def run_async(by: str, on_update) -> bool:
    if not rows():
        return False
    threading.Thread(target=lambda: run_all(by, on_update), daemon=True, name="startup").start()
    return True


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    if a[:1] == ["status"]:
        for r in status():
            print(("done  " if r["done"] else "to do ") + r["rid"].ljust(14) + r["name"])
    elif a[:1] == ["card"]:
        print(json.dumps(card(), indent=1, ensure_ascii=False))
    else:
        print(__doc__)
