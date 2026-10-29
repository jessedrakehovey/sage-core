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
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import orgcfg  # noqa: E402

CFG = Path(os.environ.get("AGENT_GCHAT_CONFIG") or (ROOT / "agent" / "gchat.local.json"))
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "relay-poll.log"
LATE_AFTER_S = 25.0


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
    port = int(os.environ.get("MC_PORT") or orgcfg.get("mission_control_port", 9019) or 9019)
    return f"http://127.0.0.1:{port}/gchat/events"


def _get(url: str, timeout: float = 45) -> dict:
    """Apps Script answers in under a second when warm and in 20-40 s on a cold start (2026-10-28, first live hour: three
    read timeouts at 20 s, every event still delivered on the next poll). 45 s covers the cold start."""
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


def poll_once(stats: dict) -> int:
    """One poll: fetch queued events, hand each to Mission Control, post each reply back. Returns events handled."""
    c = cfg()
    base = c["relay_webapp_url"]
    try:
        r = _get(base + "?" + urllib.parse.urlencode({"op": "poll", "secret": c["relay_secret"], "max": 10}))
    except Exception as e:  # noqa: BLE001
        stats["errors"] += 1
        if stats["errors"] <= 1:              # one line per run: a slow Apps Script is not news every three seconds
            _log(f"poll failed: {type(e).__name__}: {str(e)[:160]}")
        return 0
    stats["polls"] += 1
    events = r.get("events") or []
    for item in events:
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
        try:
            _post(base, {"op": "reply", "secret": c["relay_secret"], "id": eid, "body": reply})
            stats["replied"] += 1
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            _log(f"reply for {eid} not delivered: {type(e).__name__}: {str(e)[:160]}")
        _log(f"event {eid} fn={fn or 'message'} age={age:.1f}s{' LATE' if late else ''} -> mission control -> replied")
    return len(events)


def run(seconds: float, interval: float, once: bool = False) -> dict:
    stats = {"polls": 0, "events": 0, "replied": 0, "late": 0, "errors": 0}
    if not configured():
        stats["errors"] = 1
        stats["note"] = "not configured"
        return stats
    t_end = time.time() + seconds
    while True:
        n = poll_once(stats)
        if once:
            break
        if time.time() + interval > t_end:
            break
        time.sleep(interval if not n else 0.2)      # a burst of taps drains fast; quiet time polls on the interval
    return stats


def main() -> int:
    if sys.stdout is None:                          # pythonw (the scheduled task): no console, the log file is the record
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=50)
    ap.add_argument("--interval", type=float, default=float(os.environ.get("RELAY_POLL_INTERVAL") or 3))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--health", action="store_true")
    a = ap.parse_args()
    if a.health:
        h = health()
        print(json.dumps(h))
        return 0 if h.get("ok") else 1
    s = run(10 ** 9 if a.loop else a.seconds, a.interval, once=a.once)
    print(f"RELAY_POLL polls={s['polls']} events={s['events']} replied={s['replied']} late={s['late']} errors={s['errors']}" + (f" ({s['note']})" if s.get("note") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
