"""drain.py - zero-LLM executor: runs APPROVED exec tickets through registered scripts.

Ported from the property OS (scripts/executor/drain.py), trimmed to this OS's kinds.
exec_store.enqueue is the only way work gets here and it refuses anything without
the owner's per-item approval provenance. This worker composes nothing, decides nothing and
never calls an LLM: it re-verifies each ticket's payload sha and hands the EXACT payload
to a registered script.

Error policy: PERMANENT failures (bad payload, forbidden identity) go straight to
'exception'; TRANSIENT ones (SMTP down, login wall) retry up to MAX_ATTEMPTS. Every
exception becomes an assistant ticket (kind exec_exception) so the operator surfaces it
to the owner. Outbound kinds are held during quiet hours (21:00-07:00).

  python assistant/scripts/drain.py [--dry-run] [--lane email|local] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exec_store as es  # noqa: E402

HERE = Path(__file__).resolve().parent
VAULT = HERE.parent.parent
PY = sys.executable
MAX_ATTEMPTS = 2
STALE_RUNNING_MIN = 20
PERM = "PERMANENT: "
UNDO_FLOOR_S = 35          # cards.UNDO_S (30) + slack: a card approval is not executed while it can still be undone
UNVERIFIED = PERM + "DELIVERY UNVERIFIED: "
LOCK = es.DATA / ".drain.lock"
BREAKER = es.DATA / "breaker.json"
BREAKER_N = 2               # consecutive TRANSIENT failures on one channel (lane) trip it: one "paused" card, not N failures
PROBE_EVERY_S = 600         # while tripped, one ticket of that lane is tried per pass as the probe


def _breaker_load() -> dict:
    try:
        return json.loads(BREAKER.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _breaker_save(b: dict) -> None:
    try:
        BREAKER.parent.mkdir(parents=True, exist_ok=True)
        tmp = BREAKER.with_suffix(".tmp")
        tmp.write_text(json.dumps(b, indent=1), encoding="utf-8")
        os.replace(tmp, BREAKER)
    except OSError:
        pass


def breaker_tripped(lane: str) -> bool:
    return bool((_breaker_load().get(lane) or {}).get("tripped_ts"))


def breaker_reset(lane: str, by: str = "code") -> None:
    b = _breaker_load()
    if lane in b:
        b[lane] = {"fails": 0, "tripped_ts": 0, "reset_by": by, "reset_ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _breaker_save(b)
        es.ledger("breaker_reset", lane=lane, by=by)


def _breaker_note(lane: str, ok: bool, transient: bool, held: int) -> bool:
    """A success resets the channel; a transient failure counts; BREAKER_N in a row trips it and posts ONE card
    by code (cards.breaker_card) that clears itself when the channel works again. PERMANENT failures are the
    payload's fault, not the channel's, and never count."""
    b = _breaker_load()
    st = b.get(lane) or {"fails": 0, "tripped_ts": 0}
    if ok:
        if st.get("fails") or st.get("tripped_ts"):
            b[lane] = {"fails": 0, "tripped_ts": 0, "reset_by": "success", "reset_ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
            _breaker_save(b)
            if st.get("tripped_ts"):
                es.ledger("breaker_closed", lane=lane)
        return False
    if not transient:
        return False
    st["fails"] = int(st.get("fails") or 0) + 1
    st["last_fail"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tripped = False
    if st["fails"] >= BREAKER_N and not st.get("tripped_ts"):
        st["tripped_ts"] = time.time()
        tripped = True
        es.ledger("breaker_tripped", lane=lane, fails=st["fails"], held=held)
        try:
            import cards
            cards.breaker_card(lane, int(st["fails"]), held)
        except Exception:  # noqa: BLE001
            pass
    b[lane] = st
    _breaker_save(b)
    return tripped


def _quiet_hours(now=None) -> bool:
    h = (now or datetime.now()).hour
    return h >= es.QUIET_START or h < es.QUIET_END


def _run(cmd, timeout=180):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, cwd=str(VAULT))


# ------------------------------------------------------------------ executors
def _exec_local(p, dry):
    """op = a key in assistant/jobs.json -> that fixed command starts detached (jobs.launch; job_runner
    says the outcome in chat when it ends). Any other op is only recorded, as before."""
    import jobs
    j = jobs.find(str(p.get("op") or ""))
    if not j:
        return True, f"local op recorded: {str(p.get('op', ''))[:160]}"
    if dry:
        return True, f"DRY_RUN_OK job {j['key']}"
    try:
        res = jobs.launch(j, p.get("args") or {})
    except ValueError as e:
        return False, PERM + str(e)
    except OSError as e:
        return False, "transient: could not start " + j["key"] + f": {e}"
    return True, f"{jobs.describe(j, p.get('args') or {})} started (pid {res['pid']}); it reports in chat when done; log {res['log']}"


def _exec_tracking_fix(p, dry):
    """The weekly record check's approved fixes -> tracking/drift.py apply (internal writes only)."""
    fixes = p.get("fixes") or []
    if not isinstance(fixes, list) or not fixes:
        return False, PERM + "payload.fixes must be a non-empty list"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"fixes": fixes}, f)
        fixes_file = f.name
    cmd = [PY, "assistant/scripts/tracking/drift.py", "apply", "--fixes-file", fixes_file] + (["--dry-run"] if dry else [])
    try:
        r = _run(cmd, timeout=120)
    except subprocess.TimeoutExpired:
        return False, PERM + "drift.py apply timed out; check the ledgers before re-carding"
    finally:
        try:
            os.unlink(fixes_file)
        except OSError:
            pass
    out = (r.stdout or "").strip().splitlines()
    last = out[-1] if out else (r.stderr or "")[:200]
    return r.returncode == 0, last[:400]


def _exec_email_send(p, dry):
    for req in ("from", "to", "subject", "body"):
        if not str(p.get(req) or "").strip():
            return False, PERM + f"payload missing {req}"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(str(p["body"]))
        body_file = f.name
    to = p["to"]
    cmd = [PY, "assistant/scripts/email_send.py", "--from", str(p["from"]),
           "--to", ",".join(to) if isinstance(to, (list, tuple)) else str(to),
           "--subject", str(p["subject"]), "--body-file", body_file]
    if p.get("cc"):
        cc = p["cc"]
        cmd += ["--cc", ",".join(cc) if isinstance(cc, (list, tuple)) else str(cc)]
    if p.get("in_reply_to"):
        cmd += ["--in-reply-to", str(p["in_reply_to"])]
    if p.get("references"):
        r = p["references"]
        cmd += ["--references", " ".join(r) if isinstance(r, (list, tuple)) else str(r)]
    if dry:
        cmd += ["--dry-run"]
    try:
        r = _run(cmd)
    except subprocess.TimeoutExpired:
        return False, UNVERIFIED + "email_send.py timed out mid-run; check Sent mail before re-carding"
    finally:
        try:
            Path(body_file).unlink()
        except OSError:
            pass
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    # SENT=1 first: a send with a refused recipient prints "SENT=1 <id> PARTIAL_REFUSED=...", and
    # reading that as a permanent failure invited a re-card that double-sends to everyone else
    if "SENT=1" in out or (dry and r.returncode == 0 and "DRY_RUN_OK" in out):
        return True, out[-400:]
    if "REFUSED=" in out:
        return False, PERM + out[-300:]
    # Only failures that provably happened BEFORE the message was handed to the server are
    # retried. Anything else may have gone out: a blind retry is a double send.
    if any(m in out for m in ("NOT_SENT", "LOGIN_REQUIRED", "NOT_CONFIGURED")):
        return False, out[-400:]
    return False, UNVERIFIED + "check Sent mail before re-carding. " + out[-250:]


def _exec_calendar_hold(p, dry):
    """A tentative hold on OUR OWN calendar file (assistant/data/holds.json). Nobody is
    notified. Google Calendar write access comes with roadmap Phase 3; until then the
    morning brief reads holds.json."""
    if not str(p.get("title") or "").strip() or not str(p.get("date") or "").strip():
        return False, PERM + "payload needs title + date"
    if dry:
        return True, "DRY_RUN_OK hold"
    from alib import DATA, load_json, save_json, now_iso
    holds = load_json(DATA / "holds.json", {"holds": []})
    holds["holds"].append({"title": p["title"], "date": p["date"], "time": p.get("time"),
                           "duration_min": p.get("duration_min"), "note": p.get("note"), "created": now_iso()})
    save_json(DATA / "holds.json", holds)
    return True, f"HOLD_CREATED {p['date']} {p['title'][:60]}"


def _exec_imessage_send(p, dry):
    """Hand the approved text to owner's iPhone outbox; her Shortcut does the sending and
    acks it (phone_bridge.py). Delivery is at-most-once from here on."""
    if dry:
        return True, "DRY_RUN_OK imessage"
    import phone_bridge
    ok, msg = phone_bridge.queue_outbound(p, p.get("_ticket", ""))
    return ok, msg


def _exec_ads_apply(p, dry):
    """the owner tapped Approve on an ad-budget card. apply.py re-proves everything itself (ticket,
    sha, caps, kill file, the steps still equal today's plan) before one write to Meta. Money:
    any failure that is not a clean refusal is PERMANENT, never retried blind."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False)
        pf = f.name
    cmd = [PY, "ads/scripts/apply.py", str(p.get("proposal_id")), "--card-ticket", str(p.get("_ticket", "")),
           "--payload-file", pf] + (["--dry-run-card"] if dry else [])
    try:
        r = _run(cmd, timeout=240)
    except subprocess.TimeoutExpired:
        if dry:
            raise
        return False, PERM + "ad change unverified (timed out) - check Ads Manager before re-carding."
    finally:
        try:
            Path(pf).unlink()
        except OSError:
            pass
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode == 0 and ("ADS_APPLIED=" in out or (dry and "DRY_RUN_OK" in out)):
        return True, next((l for l in out.splitlines() if "ADS_APPLIED=" in l or "DRY_RUN_OK" in l), out[:200])[:300]
    return False, PERM + out[-300:]


def _exec_agent_run(p, dry):
    """One approved browser step (assistant/doer). doer.py --approved resumes the doer's own
    model session and performs exactly payload.step; it prints DOER_RESULT=<line>. Anything
    but DOER_DONE is UNVERIFIED: the step may have landed on the site, so no blind retry."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False)
        pf = f.name
    cmd = [PY, "assistant/doer/doer.py", "--approved", pf] + (["--dry-run"] if dry else [])
    try:
        r = _run(cmd, timeout=AGENT_RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, UNVERIFIED + "the doer timed out mid-step; check the site before re-carding"
    finally:
        try:
            Path(pf).unlink()
        except OSError:
            pass
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if dry and "DRY_RUN_OK" in out:
        return True, "DRY_RUN_OK agent_run"
    if "DOER_RESULT=DOER_DONE" in out:
        return True, out.split("DOER_RESULT=", 1)[1].strip()[:400]
    if "REFUSED" in out:
        return False, PERM + out[-300:]
    return False, UNVERIFIED + "the doer did not confirm the step; check the site before re-carding. " + out[-250:]


AGENT_RUN_TIMEOUT_S = 540    # the doer's own turn limit is shorter (config doer.approved_timeout_s)


def _exec_sheet_set(p, dry):
    """Cells on a Google Sheet (agent/google_api.py, the account's own OAuth token with the Sheets
    scope). Two passes: FIRST every cell is re-read and compared with the `before` the owner saw on the
    card; one difference refuses the whole ticket and nothing is written (the sheet changed under
    him; a new card shows the new values). THEN each cell is written with RAW input (never a
    formula) and read back. A failure between writes is PERMANENT (no blind retry): the result
    says exactly which cells landed."""
    g = es._google_api()
    ok, why = g.sheet_ready(str(p.get("account") or ""))
    if not ok:
        return False, PERM + why
    try:
        cells = es.sheet_cells(p)
    except ValueError as e:
        return False, PERM + str(e)
    acct, sid = str(p["account"]), str(p["spreadsheet_id"])

    def _current(ref):
        rows = g.sheet_get(acct, sid, ref)
        return (rows[0][0] if rows and rows[0] else "")

    stale = []
    for ref, _value, before in cells:
        try:
            cur = _current(ref)
        except Exception as e:  # noqa: BLE001
            return False, f"could not read {ref} before writing ({type(e).__name__}: {str(e)[:120]})"      # transient: retried once
        if g.cell_text(cur) != g.cell_text(before):
            stale.append(f"{ref} now holds {g.cell_text(cur) or '(empty)'!s}, the card said {g.cell_text(before) or '(empty)'!s}")
    if stale:
        return False, PERM + "nothing written: the sheet changed since the card. " + "; ".join(stale)[:300]
    if dry:
        return True, f"DRY_RUN_OK sheet_set {len(cells)} cell(s) still as on the card"
    written, bad = [], []
    for i, (ref, value, _before) in enumerate(cells):
        try:
            g.sheet_set(acct, sid, ref, [[value]])
        except Exception as e:  # noqa: BLE001
            return False, (PERM + f"wrote {i} of {len(cells)} cells ({', '.join(written)[:150]}), then {ref} failed "
                           f"({type(e).__name__}: {str(e)[:100]}); check the sheet before re-carding")
        written.append(ref)
    for ref, value, _before in cells:
        try:
            if g.cell_text(_current(ref)) != g.cell_text(value):
                bad.append(ref)
        except Exception:  # noqa: BLE001
            bad.append(ref + "?")
    return True, f"SHEET_DONE={len(written)} VERIFIED={'yes' if not bad else 'no:' + ','.join(bad)[:120]} " + " ".join(
        f"{ref}={g.cell_text(value)}" for ref, value, _ in cells)[:300]


EXECUTORS = {"agent_run": _exec_agent_run, "ads_apply": _exec_ads_apply, "local": _exec_local, "tracking_fix": _exec_tracking_fix, "email_send": _exec_email_send, "imessage_send": _exec_imessage_send, "calendar_hold": _exec_calendar_hold,
             "sheet_set": _exec_sheet_set}
# The organization's own kinds run through its plugins (config.json -> plugins.modules; this organization: the CRM's
# connector's executors, which call back into _run / PERM / UNVERIFIED here). alib.plugins skips a module that
# is not here, so the engine drains its own kinds alone.
try:
    import alib as _alib
    EXECUTORS.update(_alib.plugin_map("EXECUTORS"))
except Exception:  # noqa: BLE001
    _alib = None


def _plugin_call(fn, *args):
    return _alib.plugin_call(fn, *args) if _alib is not None else None


def _plugin_each(fn, *args):
    return _alib.plugin_each(fn, *args) if _alib is not None else []


# ------------------------------------------------------------------ surfacing
def _result_line(t, result) -> str:
    """One plain, evidence-carrying sentence for the owner after a send landed (the owner, 2026-10-14:
    confirm sends with evidence; 2026-10-15: "when it has evidence of completion report back")."""
    import re as _re
    k, out = str(t.get("kind") or ""), str(result or "")
    when = time.strftime("%I:%M %p").lstrip("0")
    p = t.get("payload") or {}
    if k == "email_send":
        m = _re.search(r"SENT=1\s+(\S+)", out)
        to = p.get("to")
        to = ", ".join(to) if isinstance(to, (list, tuple)) else str(to or "")
        line = f"Sent by email to {to} at {when}" + (f" (Message-ID {m.group(1)})" if m else "")
        # a reply is checked against the parent's Gmail thread; say so when it did not land there
        if "THREAD_MISMATCH=" in out:
            return line + ", but Gmail put it in a NEW thread, not as a reply in the original conversation."
        if "THREAD_UNVERIFIED=" in out:
            return line + "; I could not confirm it landed in the original thread, check Sent mail."
        return line + (", as a reply in the original thread." if "THREAD=ok" in out else ".")
    if k == "imessage_send":
        return f"Text to {p.get('to')} handed to the phone at {when}; the phone's Shortcut sends it."
    own = _plugin_call("result_line", t, result)          # the organization's own kinds (the CRM's) word their own
    if own:
        return str(own)
    if k == "sheet_set":
        m = _re.search(r"SHEET_DONE=(\d+) VERIFIED=(\S+)", out)
        n = int(m.group(1)) if m else len(p.get("cells") or [])
        name = str(p.get("sheet_name") or "The sheet")
        try:
            g = es._google_api()
            shown = [f"{ref} {g.cell_text(b) or '(empty)'} -> {g.cell_text(v)}" for ref, v, b in es.sheet_cells(p)]
        except Exception:  # noqa: BLE001
            shown = []
        tail = "; ".join(shown[:6]) + (f"; and {len(shown) - 6} more" if len(shown) > 6 else "")
        ok = bool(m) and m.group(2) == "yes"
        return (f"{name}: {n} cell{'s' if n != 1 else ''} written at {when}" + (f" ({tail})" if tail else "")
                + (", read back as written." if ok else ". The read-back did not match for some cells; open the sheet and check."))
    return ""


def _outcome(t, ok, result) -> dict:
    """The outcome ledger's row for this run (outcomes.py, 2026-10-24): what the executor's line PROVES, by
    kind. Never raises: a ledger fault never undoes a send that landed."""
    try:
        import outcomes
        return outcomes.from_exec(t, ok, result) or {}
    except Exception as e:  # noqa: BLE001
        es.ledger("outcome_error", ticket=str(t.get("id") or ""), error=f"{type(e).__name__}: {str(e)[:120]}")
        return {}


def _report_result(t, result):
    """Post the result line under the card the owner tapped (its Chat thread) and as a card note.
    Never raises; a send that landed is never undone by a reporting failure."""
    line = _result_line(t, result)
    if not line:
        return
    ref = str(t.get("approval_ref") or "")
    try:
        import cards
        import chat_push
        qid = ref.split("card:", 1)[1] if ref.startswith("card:") else ""
        card = (cards._read(cards.DONE / f"{qid}.json") or cards._read(cards.LIVE / f"{qid}.json")) if qid else None
        cards.note("drain", line + (f" (card {qid})" if qid else ""))
        if card:
            chat_push.push_result(card, line)
    except Exception:  # noqa: BLE001
        pass


def _reopen_card(t, result) -> bool:
    """'A decision is the final close to each unless they are not confirmed done' (the owner 2026-10-20):
    a card-approved ticket that failed PERMANENTLY before anything was handed over comes back as the
    SAME card (cards.reopen), headed by what went wrong, so his tap retries it - no operator ticket, no
    fresh card. Never for an UNVERIFIED result (the message may have landed: the operator checks Sent
    mail / the CRM first). -> True when the card is open again."""
    ref = str(t.get("approval_ref") or "")
    if not ref.startswith("card:") or "UNVERIFIED" in str(result).upper():
        return False
    try:
        import cards
        r = cards.reopen(ref.split("card:", 1)[1], str(result).replace(PERM, "", 1)[:160], by="drain")
        if r.get("same"):
            # the same failure twice (reflex, 2026-10-27): no third identical tap; one plain line says what only the
            # owner can change, and the card stays closed
            why = str(result).replace(PERM, "", 1)[:160]
            fix = ("set it up on Mission Control > Setup (System), then ask me again" if "NOT_CONFIGURED" in why.upper()
                   else "tell me how you'd rather handle it, or ask me again once it's changed")
            cards.say("drain", str(t.get("lane") or "personal"), f"This failed the same way twice ({why}), so I'm not trying it a third time; {fix}.")
            es.ledger("reopen_same_failure", ticket=str(t.get("id") or ""), why=why)
            return True
        if r.get("ok"):
            line = (f"Not done: {str(result).replace(PERM, '', 1)[:160]}. The card is back "
                    + ("on your phone." if r.get("delivery") != "weekly" else "in next week's approvals."))
            qid = ref.split("card:", 1)[1]
            cards.note("drain", line + f" (card {qid})")
            try:
                import chat_push
                old_card = cards._read(cards.ARCHIVE / f"{qid}.json")
                if old_card:
                    chat_push.push_result(old_card, line)
            except Exception:  # noqa: BLE001
                pass
        return bool(r.get("ok"))
    except Exception:  # noqa: BLE001
        return False


def _surface_exception(t, result):
    """An exception becomes an assistant ticket so the operator puts it in front of the owner."""
    try:
        import tickets
        tickets.add_many([{"source": "executor", "source_id": f"exc|{t['id']}", "lane": "personal",
                           "kind": "exec_exception", "title": f"Executor exception: {t['kind']} {t['id']}",
                           "summary": str(result)[:500],
                           "payload": {"ticket": t["id"], "kind": t["kind"], "approval_ref": t.get("approval_ref")}}],
                         "drain")
    except Exception:
        pass


def reap_stale_running(minutes=STALE_RUNNING_MIN):
    """A drain that died mid-ticket leaves it 'running' forever; hand it back or fail it."""
    now, n = time.time(), 0
    for t in es.running():
        try:
            ts = time.mktime(time.strptime(str(t.get("ts_run") or t.get("ts")), "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue
        if now - ts > minutes * 60:
            if t.get("kind") in es.OUTBOUND_KINDS:
                es.set_status(t["id"], "exception", "DELIVERY UNVERIFIED: drain died mid-send; check Sent mail before re-carding")
                _surface_exception(t, "DELIVERY UNVERIFIED: drain died mid-send")
            elif t.get("kind") == "ads_apply":
                es.set_status(t["id"], "exception", "AD CHANGE UNVERIFIED: drain died mid-write; check Ads Manager before re-carding")
                _surface_exception(t, "AD CHANGE UNVERIFIED: drain died mid-write")
            elif t.get("kind") == "agent_run":
                es.set_status(t["id"], "exception", "STEP UNVERIFIED: drain died mid-step; check the site before re-carding")
                _surface_exception(t, "STEP UNVERIFIED: drain died mid-step")
            elif _plugin_call("stale_reason", t.get("kind")):
                # the orphaned transport may have finished the write after drain was killed
                es.set_status(t["id"], "exception", str(_plugin_call("stale_reason", t.get("kind"))))
                _surface_exception(t, "WRITE UNVERIFIED: drain died mid-write")
            elif t.get("kind") == "sheet_set":
                es.set_status(t["id"], "exception", "WRITE UNVERIFIED: drain died mid-write; open the sheet and check the cells before re-carding")
                _surface_exception(t, "WRITE UNVERIFIED: drain died mid-write (sheet)")
            else:
                es.set_status(t["id"], "queued", "requeued after stale running")
            n += 1
    return n


def _age_s(t):
    try:
        return time.time() - time.mktime(time.strptime(str(t.get("ts")), "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 1e9


LOCK_HARD_MIN = 25          # a wedged but living owner blocks drains (and the phone fetch) at most this long
PASS_BUDGET_S = 240         # spine timeout 600 s - one worst-case ticket (300 s) - margin


def _pid_alive(pid) -> bool:
    """True when a process with this pid exists. (Never os.kill(pid, 0) on Windows: there it
    TERMINATES the process.) Unknown = assume alive, so a live drain is never trampled."""
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return True
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x1000, False, int(pid))          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5               # access denied = it exists
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    except Exception:
        return True


class _DrainLock:
    """One drain at a time across processes (spine every 10 min, operator tick, Mission
    Control's phone fetch). Two drains could both read a ticket as 'queued' and both run it."""
    def __enter__(self):
        self.held = False
        try:
            # A drain killed from outside (spine timeout, Task Scheduler) never runs __exit__.
            # Its lock is stale the moment its process is gone, not 20 minutes later: until
            # then every drain, including the phone fetch, would skip. LOCK_HARD_MIN is the
            # backstop for a pid that was reused or cannot be checked.
            if LOCK.exists():
                try:
                    pid = int(LOCK.read_text(encoding="utf-8").strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                if not _pid_alive(pid) or time.time() - LOCK.stat().st_mtime > LOCK_HARD_MIN * 60:
                    LOCK.unlink()
        except OSError:
            pass
        try:
            LOCK.parent.mkdir(parents=True, exist_ok=True)
            with open(LOCK, "x", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            self.held = True
        except OSError:
            pass
        return self.held

    def __exit__(self, *exc):
        if self.held:
            try:
                LOCK.unlink()
            except OSError:
                pass


def drain(lane="", dry=False, min_age_s=0):
    """min_age_s: skip tickets minted less than this long ago. A ticket approved on a CARD is
    always held for UNDO_FLOOR_S, whoever calls: the 30 s undo the owner is shown must be real
    (the spine used to send an email seconds after the tap). Standing-approval tickets (no
    card, nothing to undo) are not delayed."""
    with _DrainLock() as held:
        if not held:
            return {"ran": 0, "done": 0, "exception": 0, "held_quiet": 0, "reaped": 0, "skipped_busy": 1}
        return _drain_locked(lane, dry, min_age_s)


def _drain_locked(lane, dry, min_age_s):
    out = {"ran": 0, "done": 0, "exception": 0, "held_quiet": 0, "reaped": reap_stale_running()}
    quiet = _quiet_hours()
    t0 = time.time()
    probed = set()                     # tripped lanes that already had their one probe ticket this pass
    pend = es.pending(lane)
    for t in pend:
        if not es.due(t):                                   # approved for later: stays queued until that minute
            out["waiting"] = out.get("waiting", 0) + 1
            continue
        if time.time() - t0 > PASS_BUDGET_S and not dry:
            # one ticket can take 5 min; starting another now could run past the spine's kill
            # time and turn a finished write into a false "UNVERIFIED". The rest wait 10 minutes.
            out["deferred"] = out.get("deferred", 0) + 1
            continue
        floor = max(min_age_s, UNDO_FLOOR_S if str(t.get("approval_ref", "")).startswith("card:") and not dry else 0)
        if floor and _age_s(t) < floor:
            continue
        if t["kind"] in es.OUTBOUND_KINDS and quiet and not dry:
            out["held_quiet"] += 1
            continue
        fn = EXECUTORS.get(t["kind"])
        if not fn:
            es.set_status(t["id"], "exception", PERM + f"no executor for kind {t['kind']}")
            out["exception"] += 1
            continue
        tl = str(t.get("lane") or "")
        if not dry and breaker_tripped(tl):
            # the channel is down: one probe ticket per pass, the rest held with their attempts untouched (WP2)
            if tl in probed:
                out["held_breaker"] = out.get("held_breaker", 0) + 1
                continue
            probed.add(tl)
        # re-verify: the payload must still hash to what the owner approved
        if es.payload_sha(t.get("payload") or {}) != t.get("payload_sha"):
            es.set_status(t["id"], "exception", PERM + "payload sha mismatch (drifted after approval)")
            _surface_exception(t, "payload sha mismatch")
            out["exception"] += 1
            continue
        # an unfilled [the owner: ...] blank never leaves the machine, even on a ticket minted before the
        # mint-time guard existed (Marisa, 2026-10-18)
        if t["kind"] in es.OUTBOUND_KINDS and es.unfilled_blanks(t.get("payload") or {}):
            es.set_status(t["id"], "exception", PERM + "the message still has an unfilled blank")
            _surface_exception(t, "unfilled blank in the message")
            out["exception"] += 1
            continue
        # recall check: a cancel that landed after pending() was read still wins
        if (es.fold().get(t["id"]) or {}).get("status") != "queued":
            continue
        attempts = int(t.get("attempts") or 0) + 1
        es._append(es.TICKETS, {"id": t["id"], "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": "running",
                                "ts_run": time.strftime("%Y-%m-%dT%H:%M:%S"), "attempts": attempts})
        es.ledger("run", ticket=t["id"], kind=t["kind"], attempt=attempts, dry=dry)
        out["ran"] += 1
        try:
            pay = dict(t["payload"])
            if t["kind"] in ("imessage_send", "ads_apply"):
                pay["_ticket"] = t["id"]          # the outbox is idempotent per executor ticket
            ok, result = fn(pay, dry)
        except subprocess.TimeoutExpired:
            # a timed-out LOCAL op is safe to retry; anything that talks to the outside may
            # have completed, so it goes to a human instead of running twice
            ok, result = False, ("timeout" if t["kind"] == "local" or dry else UNVERIFIED + "timed out; check before re-carding")
        except Exception as e:
            ok, result = False, f"{type(e).__name__}: {e}"
        if not dry and _breaker_note(tl, ok, not (ok or str(result).startswith(PERM)),
                                     sum(1 for x in pend if str(x.get("lane") or "") == tl and x["id"] != t["id"])):
            probed.add(tl)             # tripped on this ticket: the rest of the lane waits for the next pass's probe
        orow = _outcome(t, ok, result) if (ok and not dry) else {}
        if ok and orow.get("state") == "failed":
            # the executor's own read-back disagrees (VERIFIED=no, MEMBER=no, ARCHIVED=no): a FAILURE, not a done
            # with a warning line (outcome ledger, 2026-10-24). The card comes back for a retry; nothing is marked done.
            detail = str((orow.get("checks") or [{}])[-1].get("detail") or "read-back disagrees")
            result = PERM + "READBACK: " + detail + " | " + str(result)[:200]
            es.set_status(t["id"], "exception", result, attempts)
            if not _reopen_card(t, result):
                _surface_exception(t, result)
            out["exception"] += 1
            continue
        if ok:
            es.set_status(t["id"], "done" if not dry else "queued", ("DRY " if dry else "") + result, attempts if not dry else 0)
            out["done"] += 1
            if not dry:
                _report_result(t, result)
            if "PARTIAL_REFUSED=" in str(result) and not dry:
                _surface_exception(t, "The email was sent, but the server refused some recipients: "
                                      + str(result).split("PARTIAL_REFUSED=", 1)[1][:200] + ". They did NOT receive it.")
            _plugin_each("after_done", t, result, dry)      # the organization's own follow-ups to a kind that landed
        elif result.startswith(PERM) or attempts >= MAX_ATTEMPTS:
            es.set_status(t["id"], "exception", result, attempts)
            if not dry:
                _outcome(t, False, result)
            if not _reopen_card(t, result):
                _surface_exception(t, result)
            out["exception"] += 1
        else:
            es.set_status(t["id"], "queued", "transient: " + result, attempts)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--lane", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--reset-breaker", default="", help="close a tripped channel breaker by lane name")
    a = ap.parse_args()
    if a.reset_breaker:
        breaker_reset(a.reset_breaker, by="cli")
        print(f"DRAIN breaker reset lane={a.reset_breaker}")
        return
    res = drain(a.lane, a.dry_run)
    print(json.dumps(res) if a.json else "DRAIN " + " ".join(f"{k}={v}" for k, v in res.items()))


if __name__ == "__main__":
    main()
