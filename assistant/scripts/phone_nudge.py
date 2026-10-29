"""phone_nudge.py - wake the iPhone the moment an approved text is ready, so nobody opens Shortcuts.

Why (2026-10-06): a text approved in Google Chat lands in the phone outbox, but the iPhone only
sends when the "the organization" Shortcut runs. the owner does not want to open Shortcuts. iOS
Shortcuts can run an automation *immediately* when an SMS containing a phrase arrives, so this
module sends one tiny SMS to the owner's own phone through his carrier's email-to-SMS gateway
(e.g. p952108@example.invalid). The automation "Message contains example-SEND -> run example Send
Texts" then drains the outbox within seconds.

What this is NOT: an outbound channel. The recipient is fixed to the owner's own gateway
address (config.json -> "phone": {"nudge_to": "..."}), the body is a constant marker with a
count, and nothing from the outgoing text is included. email_send.py stays the only path to
anyone else. Rate-limited to one nudge per NUDGE_EVERY_S; never raises.

  python assistant/scripts/phone_nudge.py --test     # send one nudge now
"""
from __future__ import annotations

import os
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if __name__ == "__main__":
    # keyring lives in ~/.venvs/wedding-web: re-exec there when RUN as a script. Never when imported:
    # phone_bridge.queue_outbound imports this module, and under system python the re-exec was a
    # SystemExit in the middle of pytest (test_phone_bridge "flaky" since 2026-10-11; it was this).
    try:
        from alib import ensure_venv
        ensure_venv()
    except Exception:  # noqa: BLE001
        pass



def _mark() -> str:
    """The SMS phrase the phone's automation watches for (org.json -> phone_nudge_mark)."""
    try:
        from alib import _org
        return str(_org("phone_nudge_mark", "") or "SAGE-SEND")
    except Exception:  # noqa: BLE001
        return "SAGE-SEND"


MARK = _mark()
NUDGE_EVERY_S = 45
RENUDGE_S = 120          # a text still "ready" (never fetched) this long after the last nudge gets another one
RENUDGE_MAX = 5          # ...up to this many per waiting spell, so a phone that is off is not spammed all day
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "phone-nudge.log"
STATE = Path(os.environ.get("ASSISTANT_DATA") or (HERE.parent / "data")) / "phone_nudge_state.json"


def _log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {msg}\n")
    except OSError:
        pass


def _cfg() -> dict:
    try:
        from alib import config
        return dict(config().get("phone") or {})
    except Exception:  # noqa: BLE001
        return {}


_DEFERRED = {"timer": None}


def _ready_count() -> int:
    try:
        import json
        box = json.loads((Path(os.environ.get("ASSISTANT_DATA") or (HERE.parent / "data")) / "phone_outbox.json").read_text(encoding="utf-8"))
        return sum(1 for i in box.get("items", []) if i.get("status") == "ready")
    except Exception:  # noqa: BLE001
        return 0


def _defer(seconds: float) -> None:
    """One pending re-nudge at a time; fires only if texts are still waiting."""
    try:
        import threading
        t = _DEFERRED.get("timer")
        if t is not None and t.is_alive():
            return
        def _go():
            n = _ready_count()
            if n:
                fire(n)
        t = threading.Timer(max(1.0, seconds), _go)
        t.daemon = True
        _DEFERRED["timer"] = t
        t.start()
        _log(f"re-nudge deferred {seconds:.0f}s")
    except Exception:  # noqa: BLE001
        pass


def configured() -> bool:
    return "@" in str(_cfg().get("nudge_to") or "")


def fire(n_ready: int = 1, force: bool = False) -> bool:
    """One SMS-gateway email to the owner's phone: '<MARK> <n> text(s) ready'. True if sent."""
    if os.environ.get("ASSISTANT_CARDS") or os.environ.get("ASSISTANT_DATA"):   # test runs
        return False
    c = _cfg()
    to = str(c.get("nudge_to") or "").strip()
    if "@" not in to:
        return False
    try:
        import json
        st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    except (OSError, ValueError):
        st = {}
    wait = NUDGE_EVERY_S - (time.time() - float(st.get("ts") or 0))
    if not force and wait > 0:
        # Rate-limited, but a text is waiting: nudge again the moment the window ends, as long
        # as something is still "ready" (two approvals 33 s apart lost the second one, 17:06).
        _defer(wait + 1)
        return False
    try:
        from alib import config, secret
        frm_name = str(c.get("nudge_from") or "personal")
        acct = next((a for a in config().get("mail", {}).get("accounts", []) if a.get("name") == frm_name), None)
        if not acct:
            _log(f"no mail account named {frm_name!r}")
            return False
        pw = secret(f"smtp:{acct['name']}") or secret(acct.get("secret", ""))
        if not pw:
            _log(f"no smtp secret for {acct['name']}")
            return False
        msg = EmailMessage()
        msg["From"] = acct["address"]
        msg["To"] = to
        msg["Subject"] = ""
        msg.set_content(f"{MARK} {n_ready} text{'s' if n_ready != 1 else ''} ready")
        s = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
        s.starttls()
        s.login(acct["address"], pw)
        s.send_message(msg)
        s.quit()
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({**st, "ts": time.time(), "to": to}), encoding="utf-8")
        _log(f"nudged {to} ({n_ready} ready)")
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"nudge failed: {type(e).__name__}: {str(e)[:160]}")
        return False


def _ready_oldest_age_s(now: float | None = None) -> float:
    """Seconds since the oldest text still waiting to be fetched was queued; 0 when none."""
    try:
        import json
        from datetime import datetime
        box = json.loads((Path(os.environ.get("ASSISTANT_DATA") or (HERE.parent / "data")) / "phone_outbox.json").read_text(encoding="utf-8"))
        ages = [(now or time.time()) - datetime.strptime(i["ts"][:19], "%Y-%m-%dT%H:%M:%S").timestamp()
                for i in box.get("items", []) if i.get("status") == "ready" and i.get("ts")]
        return max(ages) if ages else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def renudge_if_waiting(now: float | None = None) -> bool:
    """Called every minute by the comms probe (2026-10-24): a text the phone has not fetched RENUDGE_S
    after the last nudge gets another wake-up SMS, up to RENUDGE_MAX per spell. The deferred timer
    inside the answering process dies with it (a CLI answer exits at once), so this is the one path
    that survives. Nothing during quiet hours; nothing when the outbox is empty. True when it fired."""
    import json
    now = now or time.time()
    h = time.localtime(now).tm_hour
    try:
        st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    except (OSError, ValueError):
        st = {}
    n = _ready_count()
    if not n:
        if st.get("renudges"):
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps({**st, "renudges": 0}), encoding="utf-8")
        return False
    if h >= 21 or h < 7:                      # exec_store quiet hours; the morning fetch takes it
        return False
    last = max(float(st.get("ts") or 0), float(st.get("renudge_ts") or 0))
    if _ready_oldest_age_s(now) < RENUDGE_S or now - last < RENUDGE_S:
        return False
    if int(st.get("renudges") or 0) >= RENUDGE_MAX:
        return False
    st["renudges"] = int(st.get("renudges") or 0) + 1
    st["renudge_ts"] = now                    # counts even when the SMS itself fails: never more than one try per window
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st), encoding="utf-8")
    ok = fire(n, force=True)
    _log(f"re-nudge {st['renudges']}/{RENUDGE_MAX}: {n} ready, oldest {_ready_oldest_age_s(now) / 60:.0f} min -> {'sent' if ok else 'not sent'}")
    return ok


if __name__ == "__main__":
    if sys.argv[1:2] == ["--test"]:
        print("configured:", configured(), "sent:", fire(1, force=True))
    else:
        print(__doc__)
