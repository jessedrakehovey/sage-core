"""exec_store.py - the approved-actions queue (executor tickets).

Ported from the property OS (scripts/executor/ticket_store.py), trimmed to this OS's
kinds. Append-only JSONL folded latest-per-id. A ticket can ONLY be minted with
the owner-approval provenance (approved_by="the owner" + an approval_ref naming the card) and a
payload sha this module computes itself: the queue physically cannot hold unapproved
work. drain.py re-verifies the sha and hands the EXACT payload to a registered script.

Zero-LLM, stdlib-only. Never sends anything itself.

Kinds (whitelist):
  email_send     {from, to, subject, body, cc?, in_reply_to?, references?}  lane email   OUTBOUND
  imessage_send  {account, to, body}                                       lane phone   OUTBOUND
                 (handed to owner's iPhone outbox by phone_bridge.py; the phone's Shortcut sends it)
  calendar_hold  {title, date, time?, duration_min?, note?}                lane local   (own calendar, nobody notified)
  ads_apply      {proposal_id, total, steps[{op, target, amount?, desc}]}  lane ads     (Meta budget moves + pauses only)
  <connector kinds> the organization's CRM connector adds its own (KIND_LANES / REQUIRED_KEYS / validate;
                 assistant/CRM_CONTRACT.md): a note, a field update, a participant, a stage move, a message
  sheet_set      {account, spreadsheet_id, tab?, cells[{a1, value, before}], sheet_name?}  lane sheets
                 (Google Sheets cells by API, agent/google_api.sheet_set: RAW values, never a formula; drain re-reads
                 every cell and refuses the whole ticket if one no longer holds `before`)
  local          {op, args?}                                               lane local   (op = a key in assistant/jobs.json -> drain
                 launches that fixed command through jobs.py; any other op is only recorded)

A ticket may carry `not_before` ("YYYY-MM-DD HH:MM", local): a send or a job the owner approved
for LATER (2026-10-24, "schedule text sends"). drain leaves it queued until then; undo cancels it
until it runs. The card shows the time in words (when_text) and the tap mints it unchanged.

Also keeps the hash-chained audit ledger (assistant/data/audit_ledger.jsonl): every mint,
run and outcome is a link; `verify_ledger()` proves nothing was edited or removed.
"""
from __future__ import annotations

import hashlib
import json
import re
import os
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("ASSISTANT_DATA") or (HERE.parent / "data"))
TICKETS = DATA / "exec_tickets.jsonl"
LEDGER = DATA / "audit_ledger.jsonl"
_lock = threading.Lock()

KIND_LANES = {
    "email_send": "email",
    # iMessage from an owner's iPhone (2026-10-01): drain -> phone_bridge outbox -> the phone's Shortcut sends.
    "imessage_send": "phone",
    # The organization's CRM kinds (its connector, assistant/CRM_CONTRACT.md) come from
    # the plugins named in config.json -> plugins.modules and are merged in below (sage-core, 2026-10-28).
    # Cells on a Google Sheet by API (2026-10-18: the pricing sheet, after the doer could not drive the Sheets
    # UI). Not outbound (nobody is notified) but a write, so it is card-gated: every cell and value sits
    # verbatim on the card (cards._sanitize_exec) and drain re-reads each cell before writing.
    "sheet_set": "sheets",
    "calendar_hold": "local",
    # The doer (assistant/doer): an owner approved ONE irreversible browser step on a checkpoint
    # card (or typed a verification code). drain hands the exact step back to doer.py --approved.
    "agent_run": "doer",
    # Meta ad budget moves / pausing a losing ad (ads/scripts/apply.py --card-ticket). Never creates,
    # activates or deletes an ad; apply.py re-checks the $ caps and the steps against this payload.
    "ads_apply": "ads",
    "local": "local",
    # Record fixes from the weekly hygiene card (assistant/scripts/tracking/drift.py apply, 2026-10-15):
    # internal writes only (a delivery-ledger stage, closing a stale reminder). Never outbound.
    "tracking_fix": "local",
}
OUTBOUND_KINDS = {"email_send", "imessage_send"}
# The address of another business on this machine that must never be written to from this OS
# (the owner 2026-09-28: "WE CAN NEVER LET THAT EMAIL CROSS OVER").
FOREIGN_DOMAIN = "other-business.invalid"


def _alib():
    """assistant/scripts/alib.py (the plugin loader); None when it cannot be imported, so the engine's own kinds stand alone."""
    import sys as _sys
    if str(HERE) not in _sys.path:
        _sys.path.insert(0, str(HERE))
    try:
        import alib
        return alib
    except Exception:  # noqa: BLE001
        return None


def _people() -> dict:
    """assistant/agent/people.json: who the owners are (the keys the approval chain records)."""
    try:
        return json.loads((HERE.parent / "agent" / "people.json").read_text(encoding="utf-8-sig")) or {}
    except (OSError, ValueError):
        return {}


def _owner_keys() -> list:
    """The owners' keys, in people.json order (role owner first; every user when no role is set)."""
    users = _people().get("users") or {}
    owners = [k for k, u in users.items() if str((u or {}).get("role") or "") == "owner"]
    return [str(k) for k in (owners or list(users.keys()))]


def _owner_words() -> list:
    """The owners' keys and first names, upper-cased: the blanks a drafter leaves them ("[<OWNER>: confirm date]")."""
    out = []
    for k, u in (_people().get("users") or {}).items():
        for w in (str(k), str((u or {}).get("name") or "").split(" ")[0]):
            w = re.sub(r"[^A-Za-z0-9]", "", w).upper()     # digits kept: a template's owner1 is a whole word
            if w and w not in out:
                out.append(w)
    return out


# An approval is recorded under the FIRST owner's key (people.json; the cards and the standing rules mint with it):
# per-item approval is the only entry path, so enqueue refuses any other value. No owners on record = nothing mints.
APPROVER = (_owner_keys() or [""])[0]
# A blank a drafter left for an owner ("[<OWNER>: confirm date]", "[TODO]", "{{name}}") must never
# leave the machine. Marisa, 2026-10-18: a card's "Edit & send" button was really the one-tap
# approve, and the draft went to her with both owner-named blanks still in it.
BLANK_RE = re.compile(r"\[\s*(?:" + "|".join(_owner_words() + ["OWNER", "TODO", "TBD", "TK", "FILL", "INSERT", "CONFIRM", "PLACEHOLDER", "YOUR", "ADD"])
                      + r")\b[^\]]*\]|\{\{[^{}]*\}\}|<<[^<>]*>>", re.I)


def unfilled_blanks(payload) -> list:
    """Every placeholder still in an outbound payload's strings (all of them, nested too)."""
    found = []

    def walk(v):
        if isinstance(v, str):
            found.extend(m.group(0) for m in BLANK_RE.finditer(v))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
    walk(payload)
    return found


VALID_STATUS = {"queued", "running", "done", "exception", "cancelled"}
QUIET_START, QUIET_END = 21, 7          # outbound held 21:00-07:00
NOT_BEFORE_MAX_DAYS = 90                # a scheduled send further out than this is a mistake, not a plan
_NB_FMT = "%Y-%m-%d %H:%M"


def parse_not_before(s) -> str:
    """'YYYY-MM-DD HH:MM' (a 'T' is fine) -> the same, normalized; '' when empty. ValueError when it is
    malformed, already past, or more than NOT_BEFORE_MAX_DAYS away."""
    from datetime import datetime, timedelta
    s = str(s or "").strip()
    if not s:
        return ""
    try:
        d = datetime.strptime(s[:16].replace("T", " "), _NB_FMT)
    except ValueError:
        raise ValueError(f"send time {s[:30]!r} must be YYYY-MM-DD HH:MM") from None
    now = datetime.now()
    if d <= now:
        raise ValueError(f"send time {d:%Y-%m-%d %H:%M} has already passed")
    if d > now + timedelta(days=NOT_BEFORE_MAX_DAYS):
        raise ValueError(f"send time {d:%Y-%m-%d %H:%M} is more than {NOT_BEFORE_MAX_DAYS} days out")
    return d.strftime(_NB_FMT)


def when_text(s) -> str:
    """'2026-10-25 09:00' -> 'Sat Oct 25, 9:00 AM': the words the owner reads on the card."""
    from datetime import datetime
    d = datetime.strptime(str(s)[:16].replace("T", " "), _NB_FMT)
    return f"{d:%a %b} {d.day}, {d.strftime('%I').lstrip('0') or '12'}:{d:%M} {d:%p}"


def in_quiet(s) -> bool:
    from datetime import datetime
    h = datetime.strptime(str(s)[:16].replace("T", " "), _NB_FMT).hour
    return h >= QUIET_START or h < QUIET_END


def due(t: dict, now=None) -> bool:
    """A ticket with no not_before is due; one with it is due once that minute has come."""
    from datetime import datetime
    nb = str(t.get("not_before") or "")
    return not nb or nb <= (now or datetime.now()).strftime(_NB_FMT)

# The only identities this OS may ever send from. A other-business.invalid address can
# never appear here (the owner 2026-09-28: "WE CAN NEVER LET THAT EMAIL CROSS OVER").

def _orgcfg():
    """assistant/orgcfg.py (WP5): the organization's own values from assistant/org.json; the literal below is the fallback."""
    import sys as _sys
    from pathlib import Path as _P
    root = str(_P(__file__).resolve().parents[1])
    if root not in _sys.path:
        _sys.path.insert(0, root)
    import orgcfg
    return orgcfg


# assistant/org.json -> send_identities (WP5, 2026-10-24). No org.json = no identity = nothing can be sent (fail closed).
try:
    ALLOWED_FROM = _orgcfg().tuple_of("send_identities", ())
except Exception:  # noqa: BLE001
    ALLOWED_FROM = ()
# The only phones this OS may text from (each is a Shortcut on that phone pulling the outbox): org.json -> phone_accounts.
try:
    ALLOWED_PHONE_ACCOUNTS = tuple((_orgcfg().get("phone_accounts") or {}).keys())
except Exception:  # noqa: BLE001
    ALLOWED_PHONE_ACCOUNTS = ()

REQUIRED_KEYS = {
    "email_send": [("from",), ("to",), ("subject",), ("body",)],
    "imessage_send": [("account",), ("to",), ("body",)],
    "sheet_set": [("account",), ("spreadsheet_id",), ("cells",)],
    "calendar_hold": [("title",), ("date",)],
    "agent_run": [("ticket",), ("step",), ("resume",)],
    "ads_apply": [("proposal_id",), ("steps",), ("total",)],
    "local": [("op",)],
    "tracking_fix": [("fixes",)],
}
PAYLOAD_ALIASES = {
    ("email_send", "to"): ("recipient", "to_addr"),
    ("email_send", "from"): ("sender", "identity", "account"),
    ("email_send", "body"): ("text", "message"),
    ("imessage_send", "body"): ("text", "message"),
    ("imessage_send", "to"): ("recipient", "phone"),
}
# The organization's own kinds (config.json -> plugins.modules; this organization: the CRM's): each plugin's
# KIND_LANES / OUTBOUND_KINDS / REQUIRED_KEYS merge into the whitelist, its validate() and outbound_target() run after
# the engine's own checks. Without a plugin the engine's kinds stand alone.
if _alib() is not None:
    KIND_LANES.update(_alib().plugin_map("KIND_LANES"))
    OUTBOUND_KINDS |= _alib().plugin_set("OUTBOUND_KINDS")
    REQUIRED_KEYS.update(_alib().plugin_map("REQUIRED_KEYS"))


def _filled(payload, key):
    v = payload.get(key)
    if v is None or v is False:
        return False
    if isinstance(v, (str, bytes)):
        return bool(v.strip())
    if isinstance(v, (list, tuple, dict, set)):
        return bool(v)
    return True


def validate_payload(kind: str, payload: dict) -> str:
    """"" when the payload satisfies its kind's executor contract, else a one-line reason."""
    for group in REQUIRED_KEYS.get(kind, []):
        if any(_filled(payload, k) for k in group):
            continue
        need = " or ".join(group)
        hint = [a for a in PAYLOAD_ALIASES.get((kind, group[0]), ()) if _filled(payload, a)]
        if hint:
            return f"payload needs {need} (found {hint[0]!r} -- rename it to {group[0]!r})"
        return f"payload needs {need}"
    if kind == "email_send":
        frm = str(payload.get("from") or "").strip().lower()
        if frm not in ALLOWED_FROM:
            return f"email_send from={frm!r} is not an allowed identity {ALLOWED_FROM}"
        to = payload["to"]
        addrs = to if isinstance(to, (list, tuple)) else [to]
        if not all("@" in str(a) for a in addrs):
            return "email_send 'to' must be an email address"
        if any(FOREIGN_DOMAIN in str(a).lower() for a in addrs):
            return "email_send must not address a property-management identity from this OS"
    if kind == "imessage_send":
        if str(payload.get("account") or "").strip().lower() not in ALLOWED_PHONE_ACCOUNTS:
            return f"imessage_send account={payload.get('account')!r} is not an allowed phone {ALLOWED_PHONE_ACCOUNTS}"
        import phone_bridge
        if not phone_bridge.valid_handle(str(payload["to"])):
            return "imessage_send 'to' must be one phone number (+1XXXXXXXXXX) or one iMessage email"
    if kind == "sheet_set":
        return _validate_sheet_set(payload)
    if kind == "agent_run":
        if not re.fullmatch(r"[0-9a-f]{16}", str(payload.get("ticket") or "")):
            return "agent_run ticket must be the 16-hex assistant ticket id the doer is working"
        if not isinstance(payload.get("values", {}), dict):
            return "agent_run values must be an object of label -> value"
        if "code" in payload and not re.fullmatch(r"\d{4,10}", str(payload.get("code") or "")):
            return "agent_run: the verification code must be 4-10 digits (type it in the note box before Approve)"
    if kind == "ads_apply":
        steps = payload["steps"]
        if not isinstance(steps, list):
            return "ads_apply steps must be a list"
        for st in steps:
            if not isinstance(st, dict) or st.get("op") not in ("budget", "pause") or not st.get("target") or not st.get("desc"):
                return "ads_apply: each step needs op budget|pause, target and desc (only budget changes and pauses exist)"
            if st["op"] == "budget" and not isinstance(st.get("amount"), (int, float)):
                return "ads_apply: a budget step needs a numeric amount"
    if _alib() is not None:                     # the organization's own kinds check their own contracts
        for why in _alib().plugin_each("validate", kind, payload):
            if why:
                return str(why)
    return ""


SHEET_MAX_CELLS = 100


def _google_api():
    import sys
    agent = str(HERE.parent / "agent")
    if agent not in sys.path:
        sys.path.insert(0, agent)
    import google_api
    return google_api


def sheet_cells(payload: dict) -> list:
    """The payload's cells as [(full_a1, value, before)], the tab resolved (cell's own Tab!A1, else
    payload.tab). ValueError on a bad reference; validate_payload turns that into a reason."""
    g = _google_api()
    out = []
    for c in payload.get("cells") or []:
        tab, cell = g.a1_split(str((c or {}).get("a1") or ""))
        tab = tab or str(payload.get("tab") or "").strip()
        if not tab:
            raise ValueError(f"cell {cell} names no tab (use Tab!{cell}, or payload.tab)")
        out.append((g.a1_join(tab, cell), c.get("value"), c.get("before")))
    return out


def _validate_sheet_set(payload: dict) -> str:
    acct = str(payload.get("account") or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,40}", acct):
        return "sheet_set account must be a google.json account name (lowercase, like the keys in google.json)"
    try:
        g = _google_api()
    except Exception as e:  # noqa: BLE001
        return f"sheet_set: google_api unavailable ({type(e).__name__})"
    if acct not in g.accounts():
        return f"sheet_set account {acct!r} is not in assistant/agent/google.json"
    if g.SHEETS_SCOPE not in g.scopes_for(acct):
        return f"sheet_set account {acct!r} is not declared for Sheets (extra_scopes in google.json)"
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", str(payload.get("spreadsheet_id") or "")):
        return "sheet_set spreadsheet_id must be the id from the sheet's URL"
    cells = payload.get("cells")
    if not isinstance(cells, list) or not cells:
        return "sheet_set cells must be a non-empty list of {a1, value, before}"
    if len(cells) > SHEET_MAX_CELLS:
        return f"sheet_set writes at most {SHEET_MAX_CELLS} cells per card"
    for c in cells:
        if not isinstance(c, dict) or "before" not in c:
            return "sheet_set: each cell needs a1, value and before (the value it holds now, '' when empty)"
        v = c.get("value")
        if v is None or isinstance(v, (dict, list, tuple)) or not isinstance(v, (str, int, float, bool)):
            return f"sheet_set {c.get('a1')}: a value must be text, a number or TRUE/FALSE"
        if g.is_formula(v):
            return f"sheet_set {c.get('a1')}: value {str(v)[:30]!r} starts with '=' and sheet_set never writes a formula"
        if isinstance(v, str) and len(v) > 500:
            return f"sheet_set {c.get('a1')}: a text value is at most 500 characters"
        b = c.get("before")
        if b is not None and (isinstance(b, (dict, list, tuple)) or not isinstance(b, (str, int, float, bool))):
            return f"sheet_set {c.get('a1')}: before must be the cell's current text, number or TRUE/FALSE"
    try:
        refs = [r for r, _, _ in sheet_cells(payload)]
    except ValueError as e:
        return f"sheet_set: {e}"
    if len(set(refs)) != len(refs):
        return "sheet_set: the same cell is listed twice"
    return ""


def payload_sha(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _append(path: Path, row: dict):
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ audit ledger
def _last_ledger_hash() -> str:
    try:
        last = ""
        for ln in LEDGER.read_text(encoding="utf-8-sig").splitlines():
            if ln.strip():
                last = ln
        return json.loads(last).get("hash", "") if last else ""
    except (OSError, ValueError):
        return ""


class _LedgerLock:
    """Short cross-process mutex for the ledger append (Mission Control answering a card while
    a drain logs a run used to chain two links off the same prev: LEDGER_BROKEN forever, and
    a real tamper would look the same). Best effort: after 3 s it proceeds without the lock."""
    def __enter__(self):
        self.path, self.held = DATA / ".ledger.lock", False
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "x"):
                    pass
                self.held = True
                break
            except (FileExistsError, PermissionError):     # PermissionError: Windows, lock file mid-delete
                try:
                    if time.time() - self.path.stat().st_mtime > 30:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                time.sleep(0.05)
            except OSError:
                break
        return self.held

    def __exit__(self, *exc):
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass


def ledger(event: str, **fields):
    """Append one hash-chained link. Fail-open: a ledger error never blocks a ticket."""
    try:
        with _LedgerLock():       # read-prev + append must be one step across processes, or the chain forks
            prev = _last_ledger_hash()
            row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields, "prev": prev}
            row["hash"] = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            _append(LEDGER, row)
    except Exception:
        pass


def verify_ledger() -> tuple[bool, str]:
    prev, n = "", 0
    try:
        for ln in LEDGER.read_text(encoding="utf-8-sig").splitlines():
            if not ln.strip():
                continue
            row = json.loads(ln)
            h = row.pop("hash", "")
            if row.get("prev", "") != prev:
                return False, f"link {n}: prev pointer broken"
            if hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest() != h:
                return False, f"link {n}: hash mismatch (edited)"
            prev, n = h, n + 1
    except OSError:
        return True, "no ledger yet"
    except (ValueError, AttributeError):
        return False, f"link {n}: unreadable line (torn write or edit)"
    return True, f"{n} links intact"


# ------------------------------------------------------------------ duplicate guards
def _outbound_target(kind, payload):
    if kind == "email_send":
        to = payload.get("to")
        to = ",".join(sorted(str(a).strip().lower() for a in to)) if isinstance(to, (list, tuple)) else str(to or "").strip().lower()
        subj = " ".join(str(payload.get("subject") or "").split()).lower()
        return f"email|{to}|{payload.get('in_reply_to') or subj}"
    if kind == "imessage_send":
        import phone_bridge
        return f"imsg|{phone_bridge.norm_handle(payload.get('to'))}"
    if _alib() is not None:
        return str(_alib().plugin_call("outbound_target", kind, payload) or "")
    return ""


def _done_ts(t):
    try:
        return time.mktime(time.strptime(str(t.get("ts_done") or t.get("ts")), "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0.0


def recent_done_to_same_target(kind, payload, minutes=90.0):
    target = _outbound_target(kind, payload)
    if not target:
        return None
    cutoff = time.time() - minutes * 60
    hit = None
    for t in fold().values():
        if t.get("kind") == kind and t.get("status") == "done" and _done_ts(t) >= cutoff \
                and _outbound_target(kind, t.get("payload") or {}) == target:
            if not hit or _done_ts(t) > _done_ts(hit):
                hit = t
    return hit


def recent_done_by_payload_sha(kind, payload, days=30.0):
    sha, cutoff, hit = payload_sha(payload), time.time() - days * 86400, None
    for t in fold().values():
        if t.get("kind") == kind and t.get("status") == "done" and t.get("payload_sha") == sha \
                and _done_ts(t) >= cutoff and (not hit or _done_ts(t) > _done_ts(hit)):
            hit = t
    return hit


# ------------------------------------------------------------------ mint / status
def enqueue(kind: str, payload: dict, approved_by: str, approval_ref: str, resend_ok: bool = False,
            not_before: str = "") -> dict:
    """Create ONE ticket for ONE approved action. Hard-refuses without provenance. `not_before`
    ("YYYY-MM-DD HH:MM") holds it in the queue until then (a scheduled send / job)."""
    nb = parse_not_before(not_before)        # ValueError: the time is bad or has passed -> no ticket, the card says so
    if not APPROVER or approved_by != APPROVER:
        raise PermissionError(f"ticket refused: approved_by must be {APPROVER!r} (per-item approval is the only entry path)")
    if not str(approval_ref or "").strip():
        raise PermissionError("ticket refused: approval_ref is required (name the card)")
    if kind not in KIND_LANES:
        raise ValueError(f"unknown ticket kind {kind!r}; kinds are a whitelist: {sorted(KIND_LANES)}")
    if not isinstance(payload, dict) or not payload:
        raise ValueError("payload must be a non-empty dict of the exact approved content")
    why = validate_payload(kind, payload)
    if why:
        raise ValueError(f"ticket refused: {why}")
    if kind == "agent_run" and payload.get("ask_code") and not re.fullmatch(r"\d{4,10}", str(payload.get("code") or "")):
        raise ValueError("ticket refused: this step needs the verification code typed in the note box before Approve")
    blanks = unfilled_blanks(payload) if kind in OUTBOUND_KINDS else []
    if blanks:
        raise ValueError(f"ticket refused: the message still has {len(blanks)} unfilled blank(s) ({blanks[0][:60]}). "
                         "Fill every blank with Edit & send first; nothing goes out with a placeholder in it.")
    if kind in OUTBOUND_KINDS and not resend_ok:
        # texting is a conversation: same contact within 2 min / identical text within a day
        win_min, win_days = (2.0, 1.0) if kind == "imessage_send" else (90.0, 30.0)
        prior = recent_done_to_same_target(kind, payload, win_min)
        if prior:
            raise PermissionError(f"ticket refused: {kind} already went to the same recipient/thread at "
                                  f"{prior.get('ts_done')} (ticket {prior.get('id')}). A second message needs a "
                                  f"card that says RESEND.")
        prior = recent_done_by_payload_sha(kind, payload, win_days)
        if prior:
            raise PermissionError(f"ticket refused: this exact payload already went out at {prior.get('ts_done')} "
                                  f"(ticket {prior.get('id')}).")
    row = {"id": "tk-" + uuid.uuid4().hex[:12], "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "kind": kind, "lane": KIND_LANES[kind], "payload": payload, "payload_sha": payload_sha(payload),
           "approved_by": approved_by, "approval_ref": str(approval_ref)[:200],
           "status": "queued", "attempts": 0, "result": "", "ts_done": "", "not_before": nb}
    _append(TICKETS, row)
    ledger("mint", ticket=row["id"], kind=kind, sha=row["payload_sha"], approval_ref=row["approval_ref"], not_before=nb)
    return row


def set_status(ticket_id: str, status: str, result: str = "", attempts=None):
    if status not in VALID_STATUS:
        raise ValueError(f"bad status {status!r}")
    cur = fold().get(ticket_id) or {}
    if status == "cancelled" and cur.get("status") == "done":
        raise ValueError(f"ticket {ticket_id} already executed (done at {cur.get('ts_done')}); a sent action "
                         "cannot be cancelled. A correction is a NEW message on a new card.")
    if status == "cancelled" and cur.get("status") == "running":
        raise ValueError(f"ticket {ticket_id} is being executed right now; it can no longer be cancelled. "
                         "A correction is a NEW message on a new card.")
    row = {"id": ticket_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": status, "result": str(result)[:500]}
    if attempts is not None:
        row["attempts"] = int(attempts)
    if status in ("done", "exception", "cancelled"):
        row["ts_done"] = row["ts"]
    _append(TICKETS, row)
    ledger("status", ticket=ticket_id, status=status, result=str(result)[:200])
    return row


def mark_recalled(ticket_id: str, result: str = "recalled"):
    """A 'done' imessage_send whose text was proven pulled back out of the phone outbox before the
    phone fetched it (phone_bridge.recall): nothing left the machine, so it is cancelled after all.
    Only the undo path calls this, and only after recall() returned True."""
    row = {"id": ticket_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": "cancelled",
           "result": str(result)[:500], "recalled": True}
    row["ts_done"] = row["ts"]
    _append(TICKETS, row)
    ledger("recalled", ticket=ticket_id, result=str(result)[:200])
    return row


def fold() -> dict:
    out = {}
    if not TICKETS.exists():
        return out
    for ln in TICKETS.read_text(encoding="utf-8-sig").splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("id"):
            out[e["id"]] = {**out.get(e["id"], {}), **e}
    return out


def pending(lane: str = "") -> list:
    rows = [t for t in fold().values() if t.get("status") == "queued" and (not lane or t.get("lane") == lane)]
    rows.sort(key=lambda t: t.get("ts", ""))
    return rows


def running(lane: str = "") -> list:
    return sorted([t for t in fold().values() if t.get("status") == "running" and (not lane or t.get("lane") == lane)],
                  key=lambda t: t.get("ts", ""))


def counts() -> dict:
    c = {}
    for t in fold().values():
        c[t.get("status", "?")] = c.get(t.get("status", "?"), 0) + 1
    return c


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "counts":
        print(json.dumps(counts(), indent=1))
    elif len(sys.argv) > 1 and sys.argv[1] == "verify-ledger":
        ok, msg = verify_ledger()
        print(("LEDGER_OK " if ok else "LEDGER_BROKEN ") + msg)
        sys.exit(0 if ok else 1)
    elif len(sys.argv) > 2 and sys.argv[1] == "cancel":
        tid, reason = sys.argv[2], " ".join(sys.argv[3:]) or "recalled by the owner"
        cur = fold().get(tid)
        if not cur:
            print(f"NOT FOUND {tid}"); sys.exit(2)
        try:
            set_status(tid, "cancelled", reason)
        except ValueError as e:
            print(f"REFUSED {tid}: {e}"); sys.exit(3)
        print(f"CANCELLED {tid} ({cur.get('kind')}, was {cur.get('status')}): {reason}")
    else:
        print(json.dumps(pending(), indent=1))
