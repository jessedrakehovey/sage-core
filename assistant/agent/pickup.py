"""pickup.py - an ask is carried to its end: when a browser step ends, the desk picks the ask back up.

Why (an owner, 2026-11-10: "[the agent] should be reliable for these types of tasks and never just stop without
following all the way through"). The record of that morning: he asked for an upload link texted to a helper and a
finished film's link texted to a client. The desk (desk.py) took the first line, queued the browser step and its
turn ended, as designed. From then on every browser step that ended (a sign-in check, "the link is made but the
film has not uploaded yet") ended the WORK too: nothing read the step's result against what had been asked. The
link existed at 11:30; the card that texts it went up at 12:23, only because he asked again; the film's link was
never made, because the step that found the upload unfinished simply stopped and nobody looked again.

What this adds, by code, around the unchanged desk and doer:
  * on_job_end(ticket, marker, arg)  the doer calls this when a browser or PC step that came from an owner's chat
    ask ends for any reason but "waiting on the owner's tap" or "the owner stopped it". It starts a desk turn in
    the ask's thread (a PICK-UP): the step, how it ended, the thread so far. That turn does the next deed now (the
    card with the link in it, the next step, the other route) and is silent when nothing new is in their hands.
  * later(job, minutes, what)         a pick-up that finds something still cooking (an upload, a sync, a render)
    sets a check-back instead of telling the owner to ask again:  python assistant/agent/desk.py later <job> <min> "<what>"
  * tick()                            the bridge's minute tick starts the check-backs that are due.
Bounded so it can never become a loop: at most `max_rounds` pick-ups per conversation thread per `window_h` hours,
the same ending is picked up once, and the last round is told to hand the rest to the owner in one line.
Nothing here sends anything: a pick-up is a desk turn, under the desk's belt (desk_policy.py).

Switch: profile.json -> "pickup": {"enabled", "max_rounds", "window_h", "later_min", "later_max"}; env AGENT_PICKUP=off.
Generic: no organization, person or service is named here.

  python assistant/agent/pickup.py            # the chains and the check-backs waiting
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import core  # noqa: E402

LEDGER = core.DATA / "pickup.json"
DEFAULTS = {"enabled": True, "max_rounds": 12, "window_h": 6, "later_min": 2, "later_max": 180}
# endings that are not the end of the work: the owner's tap comes next (the approved step's own ending is picked
# up), or the owner stopped the job with their own hand
_WAITS_ON_TAP = ("DOER_CHECKPOINT",)
_STOPPED = re.compile(r"\bstopped by the owner\b|\bthe owner stopped\b", re.I)
SILENT = re.compile(r"^\W*NO[_ ]REPLY\W*$", re.I)


def cfg() -> dict:
    d = dict(DEFAULTS)
    try:
        d.update({k: v for k, v in (core.profile().get("pickup") or {}).items() if v is not None})
    except Exception:  # noqa: BLE001
        pass
    if os.environ.get("AGENT_PICKUP", "").lower() in ("off", "0", "false"):
        d["enabled"] = False
    return d


def _log(msg: str) -> None:
    try:
        import desk
        desk.log("pickup " + msg)
    except Exception:  # noqa: BLE001
        pass


def _load() -> dict:
    d = core.load(LEDGER, {}) or {}
    d.setdefault("chains", {})
    d.setdefault("seen", {})
    d.setdefault("later", [])
    return d


def _save(d: dict) -> None:
    now = time.time()
    d["seen"] = {k: v for k, v in d["seen"].items() if now - float(v) < 86400}
    d["chains"] = {k: [t for t in v if now - float(t) < 86400] for k, v in d["chains"].items()}
    d["chains"] = {k: v for k, v in d["chains"].items() if v}
    d["later"] = [x for x in d["later"] if not x.get("fired") or now - float(x.get("fired") or 0) < 86400][-60:]
    core.save(LEDGER, d)


def _chain(convo: str, thread: str) -> str:
    return str(thread or convo or "")


def rounds(convo: str, thread: str, now: float | None = None) -> int:
    """Pick-ups already started on this thread inside the window."""
    now = now or time.time()
    win = float(cfg().get("window_h") or 6) * 3600
    return len([t for t in _load()["chains"].get(_chain(convo, thread), []) if now - float(t) < win])


def _take_round(d: dict, convo: str, thread: str, now: float) -> int:
    """Count one round; returns its number (1-based), or 0 when the thread has used its rounds."""
    c = cfg()
    win = float(c.get("window_h") or 6) * 3600
    key = _chain(convo, thread)
    used = [t for t in d["chains"].get(key, []) if now - float(t) < win]
    if len(used) >= int(c.get("max_rounds") or 12):
        return 0
    d["chains"][key] = used + [now]
    return len(used) + 1


def _owner(handle: str) -> dict | None:
    try:
        p = core.person_for(handle=str(handle or ""))
    except Exception:  # noqa: BLE001
        p = None
    return p if p and core.is_owner(p) else None


def _short(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "..."


def _start(person: dict, convo: str, thread: str, space: str, text: str, resume: dict, spawn: bool = True) -> dict:
    import desk
    msg = {"channel": "gchat" if (space or str(convo).startswith("spaces/")) else "local"}
    return desk.start(msg, person, text, {"convo": convo, "thread": thread, "space": space, "channel": msg["channel"],
                                          "why": "pick-up", "resume": resume}, spawn=spawn)


# ------------------------------------------------------------------ a step ended
def on_job_end(t: dict, marker: str, arg: str, spawn: bool = True, now: float | None = None) -> dict:
    """The doer's step for ticket `t` ended with `marker` / `arg`. Start the desk's pick-up when the step came from
    an owner's chat ask. Never raises; returns {"ok", "job" | "reason"}."""
    try:
        return _on_job_end(t, marker, arg, spawn, now or time.time())
    except Exception as e:  # noqa: BLE001
        _log(f"crashed: {type(e).__name__}: {str(e)[:160]}")
        return {"ok": False, "reason": f"crashed: {type(e).__name__}"}


def _on_job_end(t: dict, marker: str, arg: str, spawn: bool, now: float) -> dict:
    c = cfg()
    marker, arg = str(marker or ""), str(arg or "")
    if not c.get("enabled", True):
        return {"ok": False, "reason": "switched off"}
    try:
        import desk
        if not desk.cfg().get("enabled", True):
            return {"ok": False, "reason": "the desk is off"}
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "no desk"}
    p = (t or {}).get("payload") or {}
    convo, thread, space = str(p.get("convo") or ""), str(p.get("thread") or ""), str(p.get("space") or "")
    person = _owner(p.get("from"))
    if not person or not convo:
        return {"ok": False, "reason": "not an owner's chat ask"}
    if str(p.get("via") or "") != str(core.profile().get("slug", "agent")):
        return {"ok": False, "reason": "not started from chat"}
    if marker in _WAITS_ON_TAP:
        return {"ok": False, "reason": "waiting on the owner's tap"}
    if _STOPPED.search(arg):
        return {"ok": False, "reason": "the owner stopped it"}
    if not space and convo.startswith("spaces/"):
        space = convo
    d = _load()
    sig = hashlib.sha1(f"{t.get('id')}|{marker}|{arg[:400]}".encode("utf-8", "replace")).hexdigest()[:16]
    if sig in d["seen"]:
        return {"ok": False, "reason": "this ending was already picked up"}
    n = _take_round(d, convo, thread, now)
    if not n:
        d["seen"][sig] = now
        _save(d)
        _log(f"{t.get('id')}: no rounds left on {_chain(convo, thread)}; the step's own report stands")
        return {"ok": False, "reason": "no rounds left"}
    d["seen"][sig] = now
    _save(d)
    how = {"DOER_DONE": "done", "DOER_ANSWER": "answered", "DOER_BLOCKED": "blocked"}.get(marker, "no result")
    resume = {"kind": "step", "ticket": str(t.get("id") or ""), "how": how, "step": _short(p.get("text"), 900),
              "result": _short(arg, 1400), "round": n, "of": int(c.get("max_rounds") or 12)}
    job = _start(person, convo, thread, space, f"Pick up the ask after a browser step ended ({how}): {_short(p.get('text'), 140)}",
                 resume, spawn)
    _log(f"{t.get('id')} ended {how}: desk {job.get('id')} picks the ask up (round {n})")
    return {"ok": job.get("status") == "running", "job": job.get("id"), "round": n}


# ------------------------------------------------------------------ check back later
def later(jid: str, minutes, what: str, now: float | None = None) -> dict:
    """A desk turn sets a check-back on its own ask: woken in `minutes` to look at `what` and finish."""
    import desk
    now = now or time.time()
    job = desk.load_job(jid)
    what = _short(what, 400)
    if not job:
        return {"ok": False, "reason": "no such job"}
    if not what:
        return {"ok": False, "reason": "say what to look at when you are woken"}
    c = cfg()
    if not c.get("enabled", True):
        return {"ok": False, "reason": "check-backs are switched off; tell the owner what is still waiting"}
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "minutes must be a number"}
    m = max(float(c.get("later_min") or 2), min(float(c.get("later_max") or 180), m))
    d = _load()
    key = _chain(job.get("convo") or "", job.get("thread") or "")
    waiting = [x for x in d["later"] if not x.get("fired") and x.get("chain") == key]
    if any(_short(x.get("what"), 80).lower() == what[:80].lower() for x in waiting):
        return {"ok": True, "reason": "that check-back is already set", "minutes": m}
    if len(waiting) >= 3:
        return {"ok": False, "reason": "three check-backs already wait on this thread; fold this into one of them"}
    win = float(c.get("window_h") or 6) * 3600
    used = len([t for t in d["chains"].get(key, []) if now - float(t) < win])
    if used + len(waiting) >= int(c.get("max_rounds") or 12):
        return {"ok": False, "reason": "no check-backs left on this thread: tell the owner in one line what is still waiting "
                                       "and the shortest way to finish it from the phone"}
    row = {"id": "w" + hashlib.sha1(f"{jid}|{now}|{what}".encode("utf-8", "replace")).hexdigest()[:10], "due": now + m * 60,
           "set": now, "what": what, "chain": key, "job": jid, "person": job.get("person") or "", "convo": job.get("convo") or "",
           "thread": job.get("thread") or "", "space": job.get("space") or "", "ask": _short((job.get("resume") or {}).get("step") or job.get("text"), 900)}
    d["later"].append(row)
    _save(d)
    _log(f"{jid}: check-back {row['id']} in {m:g} min: {what[:100]!r}")
    return {"ok": True, "id": row["id"], "minutes": m}


def waiting(convo: str = "") -> list:
    return [x for x in _load()["later"] if not x.get("fired") and (not convo or x.get("convo") == convo)]


def tick(spawn: bool = True, now: float | None = None) -> list:
    """Start the check-backs that are due (the bridge's minute tick). Returns the desk jobs started."""
    now = now or time.time()
    if not cfg().get("enabled", True):
        return []
    try:
        d = _load()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for x in d["later"]:
        if x.get("fired") or float(x.get("due") or 0) > now:
            continue
        x["fired"] = now
        person = _owner(x.get("person"))
        n = _take_round(d, x.get("convo") or "", x.get("thread") or "", now) if person else 0
        if not n:
            x["dropped"] = "no owner" if not person else "no rounds left"
            continue
        resume = {"kind": "later", "what": x.get("what") or "", "step": x.get("ask") or "", "round": n,
                  "of": int(cfg().get("max_rounds") or 12), "waited_min": round((now - float(x.get("set") or now)) / 60)}
        try:
            job = _start(person, x.get("convo") or "", x.get("thread") or "", x.get("space") or "",
                         f"Check back, as set: {_short(x.get('what'), 140)}", resume, spawn)
            x["started"] = job.get("id")
            out.append(job)
            _log(f"check-back {x.get('id')} due: desk {job.get('id')} (round {n})")
        except Exception as e:  # noqa: BLE001
            x["dropped"] = f"{type(e).__name__}: {str(e)[:80]}"
    if any(x.get("fired") == now for x in d["later"]):
        _save(d)
    return out


# ------------------------------------------------------------------ the block in the desk's prompt
def block(job: dict) -> str:
    """What a pick-up turn is told (desk.render puts it under the ask)."""
    r = job.get("resume") or {}
    if not r:
        return ""
    last = int(r.get("round") or 1) >= int(r.get("of") or 12)
    jid = job.get("id") or "<job>"
    if r.get("kind") == "later":
        head = (f"THIS IS A CHECK-BACK you set {r.get('waited_min', '?')} minutes ago, not a new ask. You were waiting on: "
                f"{r.get('what')!r}. It belongs to this work: {r.get('step')!r}.\nLook now.")
    else:
        head = ("THIS IS A PICK-UP, not a new ask. A browser step that was started for the owner's ask in this thread has "
                f"just ended, and nobody but you carries the ask on from here.\nThe step: {r.get('step')!r}\n"
                f"How it ended ({r.get('how')}): {r.get('result')!r}")
    steps = [
        "Read the thread below and list for yourself every single thing the owner asked for there that is not in their hands yet "
        "(each person to message, each link, each file). The ask is finished only when every one of them is.",
        "A piece this ending unblocked: do its next deed NOW. A link or an answer that was wanted for a message means the card "
        "with the full draft, in this thread, in this turn. Never leave a link sitting in a report for the owner to ask about.",
        "A piece waiting on something that changes by itself (an upload, a sync, a render, a site still processing): check it "
        "yourself on this PC when you can (the file, the sync folder, the status). Ready: carry on. Not ready: run "
        f"`python assistant/agent/desk.py later {jid} <minutes> \"<what to look at>\"` and you are woken then to finish it. "
        "Never tell the owner to ask again later.",
        "A piece only the owner can unblock (a tap on their phone, a code, a choice that is theirs): the step's own report "
        "already told them. Do not start that same browser step again and do not repeat the report.",
        "A piece that failed for a reason you can route around: take the other route, once.",
    ]
    if last:
        steps.append("This is the LAST round on this thread: set no more check-backs. Whatever is still open, tell the owner in "
                     "one line what is waiting and the shortest way to finish it from the phone.")
    tail = ("Your reply: only what is NEW in the owner's hands from this turn (a card that went up, a link, a file), in one or "
            "two lines. When nothing new is in their hands, your whole reply is exactly NO_REPLY.")
    return (head + f"\nRound {r.get('round')} of {r.get('of')}.\nWhat to do, in order:\n"
            + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) + "\n" + tail + "\n")


def render() -> str:
    d = _load()
    now = time.time()
    lines = [f"{k}: {len(v)} pick-up(s) in the last day" for k, v in d["chains"].items()]
    for x in d["later"]:
        if not x.get("fired"):
            lines.append(f"check-back {x['id']} in {max(0, round((float(x['due']) - now) / 60))} min: {x.get('what', '')[:100]}")
    return "\n".join(lines) or "no pick-ups, no check-backs waiting"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if "--json" in sys.argv:
        print(json.dumps(_load(), ensure_ascii=False, indent=1))
    else:
        print(render())
