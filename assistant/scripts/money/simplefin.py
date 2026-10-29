"""simplefin.py - the read-only bank feed (SimpleFIN Bridge) -> data/money/ledger.json.

SimpleFIN (https://beta-bridge.simplefin.org, $15/yr) has exactly two calls: a ONE-TIME POST that
turns a setup token into an access URL (done by setup_creds.py `money simplefin`, which stores the
access URL in Windows Credential Manager as the owner-assistant / simplefin:access_url) and GET
{access_url}/accounts, which returns every linked account with balances and transactions. There is
no write endpoint in the protocol, and this module refuses to issue anything but that GET (`_get`
asserts the method and the path). No bank credential ever touches this PC: the banks are linked on
SimpleFIN's own site.

Incremental: each pull asks for [last_ok - overlap_days, today] in windows of at most window_days
(the protocol caps a request at 90 days); the first pull walks back feed.initial_days. Transactions
merge by id (the bridge's id, or a hash when it gives none); balances replace. Quota: SimpleFIN asks
for about 24 requests a day, feed.max_pulls_per_day caps us well under it. Status for /setup and
conn_check: data/money/simplefin_status.json {last_ok, error, accounts, transactions}.

  python assistant/scripts/money/simplefin.py            # pull (the spine runs this)
  python assistant/scripts/money/simplefin.py status     # what the ledger holds, every feed (no network)
  python assistant/scripts/money/simplefin.py --dry-run  # fetch, print counts, write nothing
  python assistant/scripts/money/simplefin.py --full     # ignore last_ok, walk back initial_days
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
from alib import ensure_venv, now_iso, save_json, secret  # noqa: E402

STATUS = C.DIR / "simplefin_status.json"
USER_AGENT = "sage-money-lane/1 (read-only)"


# ------------------------------------------------------------------ protocol
def claim(setup_token: str, opener=None) -> str:
    """Setup token (base64 of the claim URL) -> access URL. The ONE POST in the protocol; the token
    is single-use, so the caller must store the result. Called only by setup_creds.py."""
    tok = str(setup_token or "").strip()
    if not tok:
        raise ValueError("empty setup token")
    try:
        claim_url = base64.b64decode(tok + "=" * (-len(tok) % 4)).decode("utf-8").strip()
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"not a SimpleFIN setup token: {type(e).__name__}") from e
    if not claim_url.startswith("https://"):
        raise ValueError("the decoded claim URL is not https")
    req = urllib.request.Request(claim_url, data=b"", method="POST", headers={"User-Agent": USER_AGENT, "Content-Length": "0"})
    with (opener or urllib.request.urlopen)(req, timeout=30) as r:
        access = r.read().decode("utf-8").strip()
    if not access.startswith("https://") or "@" not in access:
        raise ValueError("the bridge did not return an access URL")
    return access


def _split(access_url: str) -> tuple:
    """(base URL without userinfo, Authorization header value)."""
    u = urllib.parse.urlsplit(access_url)
    if u.scheme != "https" or not u.username:
        raise ValueError("access URL must be https with embedded credentials")
    auth = base64.b64encode(f"{urllib.parse.unquote(u.username)}:{urllib.parse.unquote(u.password or '')}".encode()).decode()
    host = u.hostname + (f":{u.port}" if u.port else "")
    base = urllib.parse.urlunsplit((u.scheme, host, u.path.rstrip("/"), "", ""))
    return base, "Basic " + auth


def _get(access_url: str, start: date, end: date, pending: bool, opener=None) -> dict:
    """The only request this module makes: GET /accounts. Anything else is refused by construction."""
    base, auth = _split(access_url)
    q = {"start-date": int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()),
         "end-date": int(datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc).timestamp())}
    if pending:
        q["pending"] = 1
    url = base + "/accounts?" + urllib.parse.urlencode(q)
    assert urllib.parse.urlsplit(url).path.endswith("/accounts"), "money lane: only /accounts is ever requested"
    req = urllib.request.Request(url, method="GET", headers={"Authorization": auth, "User-Agent": USER_AGENT, "Accept": "application/json"})
    assert req.get_method() == "GET"
    with (opener or urllib.request.urlopen)(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


# ------------------------------------------------------------------ merge
def _ts_date(v) -> str:
    try:
        return datetime.fromtimestamp(int(v), tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return str(v or "")[:10]


def merge(led: dict, payload: dict, c: dict | None = None) -> dict:
    """Fold one /accounts payload into the ledger. Returns counts. Pure: no network, no disk."""
    c = c or C.cfg()
    new_t = upd_t = 0
    for a in payload.get("accounts") or []:
        aid = str(a.get("id") or "")
        if not aid:
            continue
        name = str(a.get("name") or aid)
        org = (a.get("org") or {}).get("name") or (a.get("org") or {}).get("domain") or ""
        kind, side = C.account_kind(name, aid, c)
        prev = led["accounts"].get(aid) or {}
        try:
            bal = float(a.get("balance"))
        except (TypeError, ValueError):
            bal = prev.get("balance")
        led["accounts"][aid] = {"id": aid, "name": name, "org": str(org), "kind": kind, "side": side,
                               "currency": str(a.get("currency") or prev.get("currency") or "USD"),
                               "balance": bal, "available": a.get("available-balance", prev.get("available")),
                               "balance_date": _ts_date(a.get("balance-date")) if a.get("balance-date") else prev.get("balance_date"),
                               "first_seen": prev.get("first_seen") or now_iso()}
        for t in a.get("transactions") or []:
            posted = _ts_date(t.get("posted") or t.get("transacted_at"))
            try:
                amt = round(float(t.get("amount")), 2)
            except (TypeError, ValueError):
                continue
            desc = str(t.get("description") or t.get("payee") or "").strip()
            tid = str(t.get("id") or "") or C.txn_id(aid, posted, amt, desc)
            old = led["transactions"].get(tid)
            row = {"id": tid, "account_id": aid, "posted": posted, "amount": amt, "description": desc,
                   "payee": str(t.get("payee") or ""), "memo": str(t.get("memo") or ""), "pending": bool(t.get("pending"))}
            if old:
                changed = any(old.get(k) != v for k, v in row.items())
                row = {**old, **row}                      # keeps bucket / bucket_src / rule_id
                upd_t += changed
            else:
                row.update({"bucket": "", "bucket_src": "none", "rule_id": "", "seen": now_iso()})
                new_t += 1
            led["transactions"][tid] = row
    errors = [str(e) for e in (payload.get("errors") or payload.get("errlist") or [])]
    return {"accounts": len(payload.get("accounts") or []), "new": new_t, "updated": upd_t, "errors": errors}


def _windows(start: date, end: date, size: int) -> list:
    out, s = [], start
    while s <= end:
        e = min(end, s + timedelta(days=max(1, size) - 1))
        out.append((s, e))
        s = e + timedelta(days=1)
    return out


def _status(**kw) -> None:
    st = C.load_json(STATUS, {}) or {}
    st.update(kw)
    save_json(STATUS, st)


def pull(dry: bool = False, full: bool = False, opener=None, today: date | None = None) -> dict:
    c = C.cfg()
    f = c["feed"]
    access = secret(str(f.get("secret") or "simplefin:access_url"))
    if not access:
        _status(error="", last_ok=(C.load_json(STATUS, {}) or {}).get("last_ok", ""), configured=False)
        print("NOT_CONFIGURED: run  python assistant/scripts/setup_creds.py money simplefin")
        return {"ok": False, "reason": "not_configured"}
    today = today or date.today()
    st = C.state()
    pulls = [p for p in st.get("pulls") or [] if p[:10] == today.isoformat()]
    if not dry and len(pulls) >= int(f.get("max_pulls_per_day") or 6):
        print(f"QUOTA: {len(pulls)} pulls today already (feed.max_pulls_per_day)")
        return {"ok": False, "reason": "quota"}
    led = C.ledger()
    last_ok = C.pdate(led["pulled"].get("last_ok"))
    if full or not last_ok:
        start = today - timedelta(days=int(f.get("initial_days") or 365))
    else:
        start = last_ok - timedelta(days=int(f.get("overlap_days") or 5))
    totals = {"accounts": 0, "new": 0, "updated": 0, "errors": [], "windows": 0}
    try:
        for s, e in _windows(start, today, int(f.get("window_days") or 60)):
            payload = _get(access, s, e, bool(f.get("include_pending", True)), opener=opener)
            r = merge(led, payload, c)
            totals["windows"] += 1
            totals["accounts"] = max(totals["accounts"], r["accounts"])
            totals["new"] += r["new"]
            totals["updated"] += r["updated"]
            totals["errors"] += r["errors"]
    except urllib.error.HTTPError as e:
        msg = f"SimpleFIN HTTP {e.code}" + (" (access token revoked or expired)" if e.code in (401, 403) else "")
        _status(error=msg, configured=True)
        C.log("pull_error", error=msg)
        print("PULL_ERROR " + msg)
        return {"ok": False, "reason": msg}
    except (OSError, ValueError, AssertionError) as e:
        msg = f"SimpleFIN unreachable: {type(e).__name__}: {str(e)[:120]}"
        _status(error=msg, configured=True)
        C.log("pull_error", error=msg)
        print("PULL_ERROR " + msg)
        return {"ok": False, "reason": msg}
    if dry:
        print("DRY " + json.dumps(totals))
        return {"ok": True, "dry": True, **totals}
    led["pulled"] = {"last_ok": today.isoformat(), "at": now_iso(), "provider": "simplefin", "errors": totals["errors"][:5]}
    C.save_ledger(led)
    st.setdefault("pulls", [])
    st["pulls"] = [p for p in st["pulls"] if p[:10] >= (today - timedelta(days=2)).isoformat()] + [today.isoformat() + "T" + time.strftime("%H:%M:%S")]
    C.save_state(st)
    _status(last_ok=now_iso(), error="; ".join(totals["errors"][:3]), configured=True,
            accounts=len(led["accounts"]), transactions=len(led["transactions"]))
    C.log("pull", **{k: v for k, v in totals.items() if k != "errors"}, errors=len(totals["errors"]))
    print("PULL " + json.dumps({k: (v if k != "errors" else len(v)) for k, v in totals.items()}))
    return {"ok": True, **totals}


def status() -> dict:
    led = C.ledger()
    s = C.load_json(STATUS, {}) or {}
    out = {"configured": bool(s.get("configured")), "last_ok": s.get("last_ok", ""), "error": s.get("error", ""),
           "accounts": [{"name": a["name"], "kind": a["kind"], "balance": a.get("balance"), "as_of": a.get("balance_date"), "feed": a.get("feed") or "simplefin"}
                        for a in led["accounts"].values()],
           "transactions": len(led["transactions"]),
           "unplaced": sum(1 for t in led["transactions"].values() if not t.get("bucket"))}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="pull", choices=["pull", "status"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--full", action="store_true")
    a = ap.parse_args()
    if a.cmd == "status":
        print(json.dumps(status(), indent=1))
        return 0
    ensure_venv()
    r = pull(dry=a.dry_run, full=a.full)
    return 0 if r.get("ok") or r.get("reason") in ("not_configured", "quota") else 1


if __name__ == "__main__":
    sys.exit(main())
