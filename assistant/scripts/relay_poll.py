"""relay_poll.py - card taps and dialogs reach this PC with no public address (sage-core, 2026-10-28).

the owner: "Can we make approval cards work without tailscale for orgs in sage-core? Let's do that." Google Chat
delivers button taps only to a public HTTPS endpoint, so the Apps Script relay (assistant/agent/gchat_relay/)
now has a PULL mode: it queues every event in its cache and THIS script fetches them through the relay's web app
URL every few seconds, hands each to Mission Control's own /gchat/events on loopback (the same verified door, the
same cards.answer), and posts Mission Control's reply back so the relay can turn the tap into the answered card
inside Chat's wait. An event older than LATE_AFTER_S is handed over with X-Sage-Late: the relay already gave Chat
the optimistic card, and Mission Control finishes by REST (gchat_events, v5). Zero LLM. Stdlib only. Nothing here
reads a secret except the relay's shared token; a vault submit's fields pass through in memory and are never logged.

Config (assistant/agent/gchat.local.json): relay_webapp_url (Deploy -> Web app URL), relay_secret.
  python assistant/scripts/relay_poll.py --seconds 50 [--interval 3]   # one serve job run (serve.json: every minute)
  python assistant/scripts/relay_poll.py --once | --health | --loop
Last line: RELAY_POLL polls=<n> events=<n> replied=<n> late=<n> errors=<n>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import orgcfg  # noqa: E402
import ports  # noqa: E402

CFG = Path(os.environ.get("AGENT_GCHAT_CONFIG") or (ROOT / "agent" / "gchat.local.json"))
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "relay-poll.log"
LATE_AFTER_S = 20.0           # the relay answers Chat by itself at 22 s (WAIT_MS): an event picked up later than this cannot make it
POLL_TIMEOUT_S = 8.0          # one poll's wait. A warm relay answers in about a second; a request that hangs is dropped and asked again
HEDGE_AFTER_S = 2.5           # a poll still unanswered after this gets a second one beside it; the first answer wins
WORKER_GRACE_S = 45.0         # how long a run waits for a hand-over still in flight (Mission Control may take half a minute)
RUN_S = 56.0                  # one run of the every-minute job: long enough that the next run starts as this one ends


def _log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg + "\n")
    except OSError:
        pass


def cfg() -> dict:
    try:
        return json.loads(CFG.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def configured() -> bool:
    c = cfg()
    return bool(c.get("relay_webapp_url") and c.get("relay_secret"))


def mc_events_url() -> str:
    port = ports.current("mission_control")
    return f"http://127.0.0.1:{port}/gchat/events"


def _get(url: str, timeout: float = POLL_TIMEOUT_S) -> dict:
    """Apps Script answers in about a second; now and then one request hangs, before or after the script ran
    (2026-11-04: about one poll in forty sat for the full 30 s, the PC heard nothing meanwhile, and a tap that landed
    in that window came back as "could not process request"). The relay leases events until the PC acks them, so a
    dropped poll loses nothing: the wait is short and the next poll goes out at once."""
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "sage-relay-poll"}), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _post(url: str, body: dict, headers: dict | None = None, timeout: float = 30) -> tuple:
    data = json.dumps(body).encode("utf-8")
    h = {"Content-Type": "application/json", "User-Agent": "sage-relay-poll"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method="POST", headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode("utf-8")
        return r.status, (json.loads(txt) if txt.strip() else {})


def health() -> dict:
    c = cfg()
    if not configured():
        return {"ok": False, "error": "relay_webapp_url / relay_secret missing in gchat.local.json"}
    try:
        return _get(c["relay_webapp_url"] + "?" + urllib.parse.urlencode({"op": "health", "secret": c["relay_secret"]}))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def _relay_post(base: str, body: dict, tries: int = 2) -> None:
    """A reply or an ack to the relay. Both are safe to send twice, and either can hang like a poll can: a short wait,
    then once more. Raises when neither try got through."""
    last: Exception | None = None
    for _ in range(max(1, tries)):
        try:
            _post(base, body, timeout=POLL_TIMEOUT_S)
            return
        except Exception as e:  # noqa: BLE001
            last = e
    raise last if last else RuntimeError("not sent")


def _handle(item: dict, c: dict, stats: dict) -> None:
    """One event, start to finish: Mission Control, the reply, the ack. Runs on its own thread so the next poll is not
    kept waiting (2026-11-04: one tap at a time meant a slow hand-over held the taps behind it for over a minute)."""
    base = c["relay_webapp_url"]
    eid = str(item.get("id") or "")
    ev = item.get("event") or {}
    fn = str(item.get("fn") or ev.get("relayFunction") or "")
    age = max(0.0, time.time() - float(item.get("at") or 0) / 1000.0)
    headers = {"X-Sage-Relay": c["relay_secret"]}
    late = age > LATE_AFTER_S
    if late and fn != "vault_add_submit":
        headers["X-Sage-Late"] = str(int(float(item.get("at") or time.time() * 1000)))
        stats["late"] += 1
    reply: dict = {}
    try:
        status, reply = _post(mc_events_url(), ev, headers)
        if status != 200:
            stats["errors"] += 1
    except Exception as e:  # noqa: BLE001
        stats["errors"] += 1
        _log(f"mission control refused event {eid} ({fn}): {type(e).__name__}: {str(e)[:160]}")
        reply = {}
    stats["events"] += 1
    how = "replied"
    try:
        _relay_post(base, {"op": "reply", "secret": c["relay_secret"], "id": eid, "body": reply})
        stats["replied"] += 1
    except Exception as e:  # noqa: BLE001
        stats["errors"] += 1
        how = "reply NOT delivered"
        _log(f"reply for {eid} not delivered: {type(e).__name__}: {str(e)[:160]}")
    _log(f"event {eid} fn={fn or 'message'} age={age:.1f}s{' LATE' if late else ''} -> mission control -> {how}")
    _ack(base, c, [eid], stats)


def _ack(base: str, c: dict, ids: list, stats: dict) -> None:
    """Only now does the relay drop them (a lost poll loses nothing, 2026-11-04)."""
    try:
        _relay_post(base, {"op": "ack", "secret": c["relay_secret"], "ids": ids})
    except Exception as e:  # noqa: BLE001
        stats["errors"] += 1
        _log(f"ack for {len(ids)} event(s) not delivered: {type(e).__name__}: {str(e)[:120]}")


_TAKE_LOCK = threading.Lock()


def _take(events: list, c: dict, stats: dict) -> None:
    """Start each fetched event on its own thread; one already taken (the relay hands an un-acked event over again)
    is acked and skipped. Under a lock: two polls may answer at once (the hedge)."""
    again = []
    with _TAKE_LOCK:
        for item in events:
            eid = str(item.get("id") or "")
            if eid and _seen(eid):
                again.append(eid)
                continue
            _mark_seen(eid)                   # before the hand-over: this run or the next never takes the same event twice
            th = threading.Thread(target=_handle, args=(item, c, stats), daemon=True)
            stats.setdefault("workers", []).append(th)
            th.start()
    if again and not _busy(stats):
        _ack(c["relay_webapp_url"], c, again, stats)   # handled by an earlier run whose ack was lost; a live worker acks its own


def _poll_get(url: str, timeout: float, late=None) -> dict:
    """The poll, hedged: when the first request has not answered in HEDGE_AFTER_S a second goes out beside it and the
    first answer wins (a request that hangs no longer costs its whole wait). An answer that comes in after the other
    was returned is handed to `late`, so events it carries are not left waiting for their lease to run out."""
    q: queue.Queue = queue.Queue()
    state = {"returned": False}

    def go() -> None:
        try:
            r = _get(url, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            q.put(("err", e))
            return
        if state["returned"]:
            if late and (r.get("events") or []):
                late(r.get("events") or [])
            return
        q.put(("ok", r))

    threading.Thread(target=go, daemon=True).start()
    started, got, err = 1, 0, None
    end = time.time() + timeout + 0.5
    hedge_at = time.time() + HEDGE_AFTER_S
    while got < started and time.time() < end:
        until = hedge_at if started == 1 and hedge_at < end - 1.0 else end
        try:
            kind, val = q.get(timeout=max(0.05, until - time.time()))
        except queue.Empty:
            if started == 1 and time.time() < end - 1.0:
                started = 2
                threading.Thread(target=go, daemon=True).start()
            continue
        got += 1
        if kind == "ok":
            state["returned"] = True
            return val
        err = val
    state["returned"] = True
    raise err or TimeoutError("The read operation timed out")


def poll_once(stats: dict) -> int:
    """One poll: fetch queued events and start each on its own thread (Mission Control, the reply, the ack).
    Returns the events fetched; -1 when the poll itself was dropped."""
    c = cfg()
    base = c["relay_webapp_url"]
    try:
        r = _poll_get(base + "?" + urllib.parse.urlencode({"op": "poll", "secret": c["relay_secret"], "max": 10}),
                      max(2.0, min(POLL_TIMEOUT_S, float(stats.get("left") or POLL_TIMEOUT_S))),
                      late=lambda evs: _take(evs, c, stats))
    except Exception as e:  # noqa: BLE001
        stats["errors"] += 1
        stats["dropped"] = stats.get("dropped", 0) + 1
        if stats["dropped"] <= 1:             # one line per run: a slow Apps Script is not news every three seconds
            _log(f"poll failed: {type(e).__name__}: {str(e)[:160]}")
        return -1                             # asked again at once: nothing is heard while a poll hangs
    stats["polls"] += 1
    events = r.get("events") or []
    _take(events, c, stats)
    return len(events)


def _busy(stats: dict) -> bool:
    return any(th.is_alive() for th in stats.get("workers") or [])


SEEN = Path(os.environ.get("AGENT_DATA") or (ROOT / "data" / "agent")) / "relay_seen.json"
SEEN_MAX = 500


def _seen_ids() -> list:
    try:
        return list(json.loads(SEEN.read_text(encoding="utf-8")).get("ids") or [])
    except (OSError, ValueError, AttributeError):
        return []


def _seen(eid: str) -> bool:
    return eid in _seen_ids()


def _mark_seen(eid: str) -> None:
    if not eid:
        return
    ids = [x for x in _seen_ids() if x != eid] + [eid]
    try:
        SEEN.parent.mkdir(parents=True, exist_ok=True)
        SEEN.write_text(json.dumps({"ids": ids[-SEEN_MAX:]}), encoding="utf-8")
    except OSError:
        pass


def run(seconds: float, interval: float, once: bool = False) -> dict:
    stats = {"polls": 0, "events": 0, "replied": 0, "late": 0, "errors": 0}
    if not configured():
        stats["errors"] = 1
        stats["note"] = "not configured"
        return stats
    t_end = time.time() + seconds
    while True:
        stats["left"] = POLL_TIMEOUT_S if once else t_end - time.time()   # the last poll of a run never outlasts the run
        n = poll_once(stats)
        if once:
            break
        wait = 0.0 if n < 0 else (0.2 if n else interval)   # a dropped poll goes again at once; a burst of taps drains fast
        if time.time() + wait + 1.0 > t_end and not (_busy(stats) and time.time() < t_end + WORKER_GRACE_S):
            break                                   # time is up and nothing is mid-hand-over (a busy run keeps listening)
        time.sleep(wait)
    for th in stats.pop("workers", []):
        th.join(WORKER_GRACE_S)
    return stats


def main() -> int:
    if sys.stdout is None:                          # pythonw (the scheduled task): no console, the log file is the record
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=RUN_S)
    ap.add_argument("--interval", type=float, default=float(os.environ.get("RELAY_POLL_INTERVAL") or 3))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--health", action="store_true")
    a = ap.parse_args()
    if a.health:
        h = health()
        print(json.dumps(h))
        return 0 if h.get("ok") else 1
    # an every-minute job that stops at 50 s leaves ten seconds a minute in which no tap is heard: never shorter than RUN_S
    t0 = time.time()
    s = run(10 ** 9 if a.loop else (a.seconds if a.once else max(a.seconds, RUN_S)), a.interval, once=a.once)
    minute = 1
    while not a.once and not a.loop and time.time() - t0 > 60.0 * minute - 1.0 and minute < 30:
        # this run outlasted its minute (an event that took long to hand over): the next start found it still running
        # and was skipped, so nobody would hear a tap until the start after that. This run covers that minute too.
        more = run(max(0.0, t0 + 60.0 * minute + RUN_S - time.time()), a.interval)
        for k in ("polls", "events", "replied", "late", "errors"):
            s[k] += more[k]
        minute += 1
    print(f"RELAY_POLL polls={s['polls']} events={s['events']} replied={s['replied']} late={s['late']} errors={s['errors']}" + (f" ({s['note']})" if s.get("note") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
