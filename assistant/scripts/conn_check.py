"""conn_check.py - zero-LLM health check of every connection in assistant/connections.json.

The property OS works because something proves each login is alive; here a dead credential
used to show up only as a sensor that quietly found nothing. This reads the registry, runs
each row's check, and writes data/connections_status.json:

    live            proven working just now
    dead            configured but failing (sentinels.py turns this into a finding)
    not_configured  the credential / id / remote has never been set (shown on Mission Control,
                    never a finding: it is known backlog, see CONNECTIONS.md)
    manual          cannot be checked without opening a browser; listed for completeness

Read-only: it logs in and out, fetches, or reads a status file. It never sends, never writes
to an account, and never prints or stores a secret value (details carry error TYPES only).

  python assistant/scripts/conn_check.py            -> check everything, print a summary
  python assistant/scripts/conn_check.py --only id  -> one connection
  python assistant/scripts/conn_check.py --list     -> last recorded states, no checks
"""
from __future__ import annotations

import argparse
import json
import re
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alib import DATA, ROOT, VAULT, config, ensure_venv, load_json, now_iso, save_json, secret  # noqa: E402

REGISTRY = ROOT / "connections.json"
STATUS = DATA / "connections_status.json"
LIVE, DEAD, NOT_CONFIGURED, MANUAL = "live", "dead", "not_configured", "manual"


def _path(spec):
    """'data:x.json' -> assistant/data, 'vault:a/b' -> repo root, '~/...' -> home."""
    spec = str(spec)
    if spec.startswith("data:"):
        return DATA / spec[5:]
    if spec.startswith("vault:"):
        return VAULT / spec[6:]
    return Path(os.path.expanduser(spec))


def _age_h(iso):
    try:
        t = datetime.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def _user_env(name):
    v = os.environ.get(name, "")
    if not v and os.name == "nt":       # Task Scheduler children do not always inherit User env
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v = winreg.QueryValueEx(k, name)[0]
        except OSError:
            v = ""
    return v


def _run(argv, timeout=40):
    exe = shutil.which(argv[0])
    if not exe:
        return None, ""
    r = subprocess.run([exe] + list(argv[1:]), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, cwd=str(VAULT), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode, (r.stdout or "").strip()


# ---- one function per check type: returns (state, detail). Details never hold a secret.

def check_env(c):
    return (LIVE, "set") if _user_env(c["name"]) else (NOT_CONFIGURED, f"User env {c['name']} is not set")


def check_imap(c):
    acct = next((a for a in config()["mail"]["accounts"] if a["name"] == c["account"]), None)
    if not acct:
        return NOT_CONFIGURED, f"no mail account '{c['account']}' in config.json"
    pw = secret(acct["secret"])
    if not pw:
        return NOT_CONFIGURED, f"app password not stored ({acct['secret']})"
    import imaplib
    try:
        m = imaplib.IMAP4_SSL(acct["host"], timeout=30)
        try:
            m.login(acct["address"], pw)
        finally:
            try:
                m.logout()
            except Exception:
                pass
        return LIVE, f"IMAP login ok as {acct['address']}"
    except imaplib.IMAP4.error:
        return DEAD, f"IMAP login refused for {acct['address']} (app password revoked or wrong?)"
    except OSError as e:
        return DEAD, f"cannot reach {acct['host']}: {type(e).__name__}"


def check_ics(c):
    feed = next((f for f in config()["calendar"]["feeds"] if f["name"] == c["feed"]), None)
    url = secret(feed["secret"]) if feed else None
    if not url:
        return NOT_CONFIGURED, "secret iCal URL not stored"
    try:        # the URL is the secret: report the error type / HTTP code only, never the exception text
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "owner1-assistant"}), timeout=30) as r:
            head = r.read(200).decode("utf-8", "replace")
        return (LIVE, "feed fetched") if "BEGIN:VCALENDAR" in head else (DEAD, "the URL did not return a calendar")
    except urllib.error.HTTPError as e:
        return DEAD, f"calendar feed returned HTTP {e.code}"
    except (OSError, ValueError) as e:
        return DEAD, f"calendar feed unreachable: {type(e).__name__}"


def check_status_file(c):
    """A sensor already talks to the service: trust its status file instead of calling again."""
    d = load_json(_path(c["path"]), None)
    if not isinstance(d, dict) or not d:
        return NOT_CONFIGURED, f"{Path(str(c['path']).split(':')[-1]).name} not written yet"
    e = load_json(_path(c["error_path"]), {}) if c.get("error_path") else d
    err = str((e or {}).get(c.get("error_field", "error")) or "")
    if err:
        return DEAD, err[:140]
    age = _age_h(d.get(c.get("ts_field", "ts")))
    if age is None:
        return DEAD, "no timestamp in the status file"
    if age > float(c.get("max_age_h", 24)):
        return DEAD, f"last good {age:.0f}h ago (limit {c.get('max_age_h', 24)}h)"
    return LIVE, f"last good {age * 60:.0f} min ago" if age < 2 else f"last good {age:.0f}h ago"


def check_cmd(c):
    code, _ = _run(c["argv"], int(c.get("timeout", 40)))
    if code is None:
        return NOT_CONFIGURED, f"{c['argv'][0]} is not installed or not on PATH"
    return (LIVE, "ok") if code == 0 else (DEAD, f"`{' '.join(c['argv'])}` exited {code}")


def check_cmd_output(c):
    code, out = _run(c["argv"], int(c.get("timeout", 40)))
    if code is None:
        return NOT_CONFIGURED, f"{c['argv'][0]} is not installed or not on PATH"
    if code != 0:
        return DEAD, f"`{' '.join(c['argv'])}` exited {code}"
    return (LIVE, out.splitlines()[0][:80]) if out else (NOT_CONFIGURED, "none set up")


def check_cmd_match(c):
    """A command whose OUTPUT says the state (its exit code alone does not): `live` / `missing` are
    regexes tried on the output in that order; neither -> dead with the first output line. Used for
    the doer vault (`vault.py has <url>` prints ENTRY ... or NO_ENTRY ...)."""
    code, out = _run(c["argv"], int(c.get("timeout", 40)))
    if code is None:
        return NOT_CONFIGURED, f"{c['argv'][0]} is not installed or not on PATH"
    first = (out.splitlines() or [""])[0][:80]
    if c.get("live") and re.search(c["live"], out, re.M):
        return LIVE, first or "ok"
    if c.get("missing") and re.search(c["missing"], out, re.M):
        return NOT_CONFIGURED, str(c.get("missing_detail") or first or "none set up")
    return DEAD, first or f"`{' '.join(c['argv'])}` exited {code}"


def check_http(c):
    try:
        with urllib.request.urlopen(c["url"], timeout=10) as r:
            return LIVE, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return (LIVE, f"HTTP {e.code}") if e.code < 500 else (DEAD, f"HTTP {e.code}")
    except (OSError, ValueError) as e:
        return DEAD, f"not answering: {type(e).__name__}"


def check_mcp(c):
    """Registered for this folder in ~/.claude.json AND its credential folder is not empty.
    Reads server NAMES only."""
    cj = load_json(Path.home() / ".claude.json", {})
    want = str(VAULT).replace("\\", "/").lower()
    names = set()
    for proj, pv in (cj.get("projects") or {}).items():
        if str(proj).replace("\\", "/").lower() == want and isinstance(pv, dict):
            names |= set((pv.get("mcpServers") or {}).keys())
    cred = _path(c["cred_dir"])
    has_cred = cred.is_dir() and any(cred.iterdir())
    if c["server"] not in names:
        return NOT_CONFIGURED, "server not registered" + ("" if has_cred else " and no OAuth files yet")
    return (LIVE, "registered, OAuth files present") if has_cred else (DEAD, f"registered but {c['cred_dir']} is empty")


def _dotted(d, key):
    for part in key.split("."):
        d = d.get(part) if isinstance(d, dict) else None
    return d


def _unset(v):
    return v is None or str(v).strip() == "" or str(v).strip().upper().startswith("TODO")


def check_json_fields(c):
    d = load_json(_path(c["path"]), {})
    missing = [f for f in c["fields"] if _unset(_dotted(d, f))]
    return (NOT_CONFIGURED, "not filled in: " + ", ".join(missing)) if missing else (LIVE, "configured")


def check_meta_ads(c):
    cfg = load_json(VAULT / "ads" / "config.json", {})
    tok = _user_env("META_ADS_TOKEN") or str(load_json(VAULT / "ads" / "secrets.json", {}).get("META_ADS_TOKEN") or "")
    missing = [f for f in ("meta.ad_account_id", "meta.page_id") if _unset(_dotted(cfg, f))]
    if _unset(tok) or len(tok) < 60:        # real Meta tokens run 150+ chars; secrets.json ships a placeholder
        return NOT_CONFIGURED, "no real token yet (placeholder)" + (" and no ids" if missing else "")
    ver = _dotted(cfg, "meta.graph_version") or "v25.0"
    try:        # token goes in a header, never the URL
        req = urllib.request.Request(f"https://graph.facebook.com/{ver}/me?fields=id", headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read(200)
    except urllib.error.HTTPError as e:
        return DEAD, f"Meta rejected the token (HTTP {e.code}): expired or revoked?"
    except (OSError, ValueError) as e:
        return DEAD, f"graph.facebook.com unreachable: {type(e).__name__}"
    if missing:
        return NOT_CONFIGURED, "token valid; not filled in: " + ", ".join(missing)
    return LIVE, "token valid, account and page ids set"


def check_manual(c):
    return MANUAL, "not machine-checkable"


CHECKS = {"env": check_env, "imap": check_imap, "ics": check_ics, "status_file": check_status_file, "cmd": check_cmd,
          "cmd_output": check_cmd_output, "cmd_match": check_cmd_match, "http": check_http, "mcp": check_mcp,
          "json_fields": check_json_fields, "meta_ads": check_meta_ads, "manual": check_manual}


def run(registry=REGISTRY, only=""):
    reg = load_json(registry, {}).get("connections", [])
    prev = load_json(STATUS, {}).get("connections", {})
    out = dict(prev) if only else {}
    for row in reg:
        cid = row["id"]
        if only and cid != only:
            continue
        chk = row.get("check") or {"type": "manual"}
        try:
            state, detail = CHECKS[chk.get("type", "manual")](chk)
        except subprocess.TimeoutExpired:
            state, detail = DEAD, "check timed out"
        except Exception as e:      # one bad row must not hide the others
            state, detail = DEAD, f"check crashed: {type(e).__name__}"
        was = prev.get(cid, {})
        if state == DEAD and was.get("state") == LIVE and not was.get("strike"):
            # a single failed check after a live one is a blip (imap timeouts, a slow start-up): keep it live with a
            # strike; a second consecutive failure is what turns it dead and makes a card (2026-10-25)
            out[cid] = {**was, "strike": now_iso(), "detail": f"one failed check ({detail}); still counted live", "checked": now_iso()}
            continue
        renew, owner = row.get("renew", ""), row.get("owner", "owner1")
        # A sensor error that is not about sign-in is a code fault: say so, and do not ask an owner
        # to re-authorise. the CRM was "down, re-authorise it" for days on 10/9-10/13 while the
        # grant was fine (a slow start-up, a reworded transcript, an SDK rename).
        if state == DEAD and row.get("auth_errors") and row.get("fault_renew") and detail                 and not detail.startswith(("last good", "no timestamp", "check timed out"))                 and not re.search(row["auth_errors"], detail, re.I):
            renew, owner = row["fault_renew"], "claude"
            detail = "code fault, not a sign-in: " + detail
        out[cid] = {"name": row.get("name", cid), "lane": row.get("lane", "system"), "owner": owner,
                    "state": state, "detail": detail, "renew": renew, "checked": now_iso(),
                    "last_live": now_iso() if state == LIVE else was.get("last_live", ""),
                    "since": was.get("since", now_iso()) if was.get("state") == state else now_iso()}
    save_json(STATUS, {"ts": now_iso(), "connections": out})
    return out


def summary(conns):
    n = {s: sum(1 for c in conns.values() if c["state"] == s) for s in (LIVE, DEAD, NOT_CONFIGURED, MANUAL)}
    return f"CONN live={n[LIVE]} dead={n[DEAD]} not_configured={n[NOT_CONFIGURED]} manual={n[MANUAL]}"


if __name__ == "__main__":
    ensure_venv()       # keyring (mail / calendar secrets) lives in the wedding-web venv
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    conns = load_json(STATUS, {}).get("connections", {}) if a.list else run(only=a.only)
    for cid, c in conns.items():
        print(f"{c['state']:15} {cid:20} {c['detail']}")
    print(summary(conns))       # last line = what the spine log shows
