"""clock.py - the agent's own idea of the time, measured against Google's, so a wrong PC clock never silences it.

2026-11-01: the PC never moved its clock back for the end of daylight time, so its UTC ran an hour fast. Every
Google sign-in token is stamped with the local clock; Google refused them all ("Invalid JWT ... Check your iat and
exp values") and Chat went dark from 14:55 - the owner's message was never even read.

  offset()   seconds to ADD to the local clock to get true time, from the Date header of a Google response
             (one HEAD, re-measured every RECHECK_S; the last good measurement is kept on disk for a failed check)
  now()      time.time() + offset()
  install()  google-auth reads the time through google.auth._helpers.utcnow; it is swapped for the corrected
             clock, so token stamps and expiry checks are right whatever the PC clock says. Idempotent.
  skew()     the last measurement {offset, ts} - reflex d_clock reports a skew past ALARM_S to the owner.
  reset()    measure again on the next call (a 401 from Google: a token stamped from a stale offset).

The PC clock being SET (an owner fixing it, a sync) is noticed at once: the wall clock moving apart from the
monotonic clock by more than JUMP_S since the last measurement re-measures, so the old correction is never laid
on top of a now-right clock (2026-11-01 15:16: the clock was fixed, the cached -1 h made every token expired, 401s).

Offsets under TRUST_S are treated as zero (the Date header has one-second resolution, a request takes time).
"""
from __future__ import annotations

import time
import urllib.request
from email.utils import parsedate_to_datetime

import core

STATE = core.DATA / "clock.json"
PROBES = ("https://www.google.com", "https://chat.googleapis.com")
RECHECK_S = 600
TRUST_S = 5
ALARM_S = 120
JUMP_S = 30
_MEM = {"offset": None, "ts": 0.0, "mono": 0.0}
_INSTALLED = {"v": False}


def _measure() -> float | None:
    for url in PROBES:
        try:
            t0 = time.time()
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=8) as r:
                date = r.headers.get("Date")
            t1 = time.time()
        except Exception as e:  # noqa: BLE001
            date = getattr(e, "headers", None) and e.headers.get("Date")   # an HTTP error still carries the Date
            t1 = time.time()
            t0 = t1
        if date:
            return parsedate_to_datetime(date).timestamp() - (t0 + t1) / 2
    return None


def _jumped() -> bool:
    if not _MEM["ts"]:                                # never measured in this process: nothing to compare
        return False
    return abs((time.time() - _MEM["ts"]) - (time.monotonic() - _MEM["mono"])) > JUMP_S


def reset() -> None:
    _MEM["force"] = True


def offset(force: bool = False) -> float:
    force = _MEM.pop("force", False) or force
    if (not force and _MEM["offset"] is not None and 0 <= time.time() - _MEM["ts"] < RECHECK_S
            and not _jumped()):
        return _MEM["offset"]
    got = _measure()
    if got is None:                                   # offline: the last good measurement, else trust the PC
        saved = core.load(STATE, {})
        _MEM["offset"] = float(_MEM["offset"] if _MEM["offset"] is not None else saved.get("offset") or 0.0)
        if _jumped():                                 # the PC clock was set: the old correction no longer holds
            _MEM["offset"] = 0.0
        _MEM.update(ts=time.time(), mono=time.monotonic())
        return _MEM["offset"]
    off = 0.0 if abs(got) < TRUST_S else round(got, 1)
    _MEM.update(offset=off, ts=time.time(), mono=time.monotonic())
    try:
        core.save(STATE, {"offset": off, "ts": core.stamp()})
    except Exception:  # noqa: BLE001
        pass
    return off


def now() -> float:
    return time.time() + offset()


def skew() -> dict:
    return core.load(STATE, {})


def install() -> None:
    if _INSTALLED["v"]:
        return
    import datetime
    from google.auth import _helpers

    def utcnow():
        return datetime.datetime.fromtimestamp(now(), datetime.timezone.utc).replace(tzinfo=None)

    _helpers.utcnow = utcnow
    _INSTALLED["v"] = True
