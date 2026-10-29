"""connect.py - set a connection up entirely from chat (the owner 2026-10-16: "make it stupid simple,
preferably from inside Sage chat with links and steps").

Generic, zero-LLM, driven by assistant/connections.json: a connection row with `setup.chat`
carries
  ask_words  a regex; an owner's message matching it gets `steps` back, with links, as one reply
  steps      the plain-English steps (links inline); a step may be {"text": ..., "if": "keys"} to show
             only when the connection's `state` module says so ("!keys" = only when not)
  state      optional script (relative to scripts/) whose status() dict feeds those "if" flags and
             fills {linked} (e.g. bank-plaid: skip the sign-up once the keys are stored)
  paste      what the owner pastes back and how code recognises it:
               kind base64_url  a base64 blob (>= min_len chars) that decodes to an https URL
                                containing url_contains (SimpleFIN's one-time setup token)
             handler            which claim routine stores it

A pasted secret of this shape is a ONE-TIME token: it dies the moment it is claimed, so it is the
one kind of secret allowed in chat. Passwords, app passwords and 2FA codes stay out of chat (the
Setup page handles those). After a claim the token is scrubbed from the agent's transcript and
inbox files, and the first pull runs so the reply can name the accounts.

Nothing here is a model turn; the bridge calls detect()/steps() before the brain ever sees the
message, and the claim itself runs setup_creds.py in the venv that holds keyring.

  python assistant/scripts/connect.py list                 # connections with chat setup
  python assistant/scripts/connect.py steps bank-simplefin # the reply an owner would get
  python assistant/scripts/connect.py detect "<text>"      # what a message would trigger (no claim)
  python assistant/scripts/connect.py brief                # the morning brief's CONNECTIONS line ('' when all is well)
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alib import DATA, ROOT, VAULT, VENV_PY, load_json  # noqa: E402

REGISTRY = ROOT / "connections.json"
AGENT_DATA = DATA / "agent"
SCRUBBED = "[one-time setup token: claimed and removed]"
_B64 = re.compile(r"[A-Za-z0-9+/_=-]{40,}")


def _rows() -> list:
    d = load_json(REGISTRY, {}) or {}
    return [r for r in d.get("connections") or [] if isinstance(r, dict)]


def chat_rows() -> list:
    return [r for r in _rows() if isinstance((r.get("setup") or {}).get("chat"), dict)]


def row(rid: str) -> dict | None:
    return next((r for r in _rows() if r.get("id") == rid), None)


# ------------------------------------------------------------------ steps
def _mc_url() -> str:
    try:
        import chat_push
        return str(chat_push.MC_URL).rstrip("/")
    except Exception:  # noqa: BLE001
        return ""


def _fill(step: str, st: dict | None = None) -> str:
    """{mc_url} in a step -> Mission Control's address (a phone page such as /setup/plaid);
    {linked} -> the banks already linked, from the state module."""
    linked = ", ".join(i.get("institution") or "bank" for i in (st or {}).get("items") or [])
    return str(step).replace("{mc_url}", _mc_url()).replace("{linked}", linked)


def _state(ch: dict) -> dict:
    """status() of the chat block's `state` script, {} when there is none or it fails (all steps show)."""
    rel = str(ch.get("state") or "")
    if not rel:
        return {}
    try:
        import importlib.util
        path = Path(__file__).resolve().parent / rel
        sys.path.insert(0, str(path.parent))
        spec = importlib.util.spec_from_file_location(path.stem, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.status() or {}
    except Exception:  # noqa: BLE001
        return {}


def _shown(step, st: dict) -> str:
    if not isinstance(step, dict):
        return str(step)
    cond, text = str(step.get("if") or ""), str(step.get("text") or "")
    if not cond:
        return text
    want = not cond.startswith("!")
    if not st:  # unknown state: the full first-time path, not the "already done" lines
        return "" if want else text
    return text if bool(st.get(cond.lstrip("!"))) == want else ""


def steps(rid: str) -> str:
    r = row(rid)
    ch = ((r or {}).get("setup") or {}).get("chat") or {}
    if not r or not ch.get("steps"):
        return ""
    st = _state(ch)
    shown = [t for t in (_shown(s, st) for s in ch["steps"]) if t]
    lines = [f"Here's how to connect {r.get('name', rid)}:"]
    lines += [f"{i}. {_fill(s, st)}" for i, s in enumerate(shown, 1)]
    return "\n".join(lines)


def asked(text: str) -> str:
    """The connection id whose ask_words the message matches, else ''."""
    t = str(text or "")
    for r in chat_rows():
        pat = (r["setup"]["chat"].get("ask_words") or "").strip()
        try:
            if pat and re.search(pat, t, re.I):
                return str(r["id"])
        except re.error:
            continue
    return ""


def _deferred(rid: str, text: str, status: dict | None = None) -> str:
    """The row a general ask should go to instead (`defer_to`), or ''. A message that never says the
    row's own name (`own_words`) is deferred while either feed is not working: "can I set up a bank
    account" right after the Relay feed went down got SimpleFIN steps only (the owner 2026-10-17).
    With no status recorded for either, nothing changes."""
    ch = ((row(rid) or {}).get("setup") or {}).get("chat") or {}
    to, own = str(ch.get("defer_to") or ""), str(ch.get("own_words") or "")
    if not to or not row(to):
        return ""
    try:
        if own and re.search(own, str(text or ""), re.I):
            return ""
    except re.error:
        return ""
    st = status if status is not None else (load_json(STATUS, {}) or {}).get("connections", {})
    st = st if isinstance(st, dict) else {}
    down = any(str((st.get(x) or {}).get("state") or "") in NOT_WORKING for x in (to, rid))
    return to if down else ""


def answer(text: str, status: dict | None = None) -> str:
    """The chat reply to a setup ask: the steps of the connection asked for (or the one it defers
    to, ending with a line naming the other), '' when the message is not a setup ask."""
    rid = asked(text)
    if not rid:
        return ""
    to = _deferred(rid, text, status)
    if not to:
        return steps(rid)
    say = str((((row(rid) or {}).get("setup") or {}).get("chat") or {}).get("say") or f"set up {rid.split('-')[-1]}")
    return steps(to) + f'\nMeant a different bank, not Relay? Say "{say}" for {row(rid).get("name", rid)}.'


# ------------------------------------------------------------------ the pasted token
def _decode_url(blob: str) -> str:
    try:
        s = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=False).decode("utf-8", errors="strict").strip()
    except Exception:  # noqa: BLE001
        try:
            s = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)).decode("utf-8", errors="strict").strip()
        except Exception:  # noqa: BLE001
            return ""
    return s if s.startswith("https://") and " " not in s else ""


def detect(text: str) -> tuple:
    """-> (connection id, the pasted value) when the message holds a recognised one-time token."""
    t = str(text or "")
    for r in chat_rows():
        p = r["setup"]["chat"].get("paste") or {}
        if p.get("kind") != "base64_url":
            continue
        need = str(p.get("url_contains") or "").lower()
        for m in _B64.finditer(t):
            blob = m.group(0)
            if len(blob) < int(p.get("min_len") or 40):
                continue
            url = _decode_url(blob)
            if url and (not need or need in url.lower()):
                return str(r["id"]), blob
    return "", ""


# ------------------------------------------------------------------ claim + store + first pull
def _run(argv: list, inp: str = "", timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=inp, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, cwd=str(VAULT))


def _py() -> str:
    return str(VENV_PY) if VENV_PY.exists() else sys.executable


def _claim_simplefin(value: str) -> tuple:
    r = _run([_py(), "assistant/scripts/setup_creds.py", "money", "simplefin", "--stdin"], inp=value + "\n", timeout=90)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or "stored" not in out.lower():
        why = out.strip().splitlines()[-1] if out.strip() else f"exit {r.returncode}"
        return False, why[:200]
    return True, ""


def _first_pull_simplefin() -> str:
    """Run the first pull now so the reply can name the accounts; the spine keeps pulling after."""
    try:
        r = _run([_py(), "assistant/scripts/money/simplefin.py", "--full"], timeout=150)
    except subprocess.TimeoutExpired:
        return "The first pull is still running; I'll have your accounts on the next pass."
    try:
        r2 = _run([_py(), "assistant/scripts/money/simplefin.py", "status"], timeout=60)
        st = json.loads(r2.stdout.strip().splitlines()[-1]) if r2.stdout.strip().startswith("{") or "\n{" in r2.stdout else {}
    except Exception:  # noqa: BLE001
        st = {}
    accts = st.get("accounts") or []
    if accts:
        names = ", ".join(str(a.get("name") or "?") for a in accts[:8])
        return f"Linked. I can see {len(accts)} account{'s' if len(accts) != 1 else ''}: {names}. {st.get('transactions', 0)} transactions so far; categorizing starts on the next pass and the first unknowns card will follow."
    tail = (r.stdout or r.stderr or "").strip().splitlines()
    return "Linked and stored. The first pull did not list accounts yet" + (f" ({tail[-1][:120]})" if tail else "") + "; I'll say what I see on the next pass."


HANDLERS = {"simplefin": (_claim_simplefin, _first_pull_simplefin)}


def claim(rid: str, value: str) -> str:
    """Claim + store + first pull. Returns the sentence for the owner. Never logs the value."""
    r = row(rid)
    p = (((r or {}).get("setup") or {}).get("chat") or {}).get("paste") or {}
    h = HANDLERS.get(str(p.get("handler") or ""))
    if not r or not h:
        return "I recognised a token but have no way to store it for that connection yet."
    ok, why = h[0](value)
    if not ok:
        return f"That token didn't take ({why}). A SimpleFIN setup token works once: make a fresh one with New App and paste it again."
    return h[1]()


# ------------------------------------------------------------------ scrub
def scrub(value: str) -> int:
    """Replace the pasted token in every agent record it could sit in. Returns files touched."""
    v = str(value or "")
    if len(v) < 20:
        return 0
    n = 0
    for p in list(AGENT_DATA.glob("*.jsonl")) + list(AGENT_DATA.glob("inbox*")) + list(AGENT_DATA.glob("gchat_inbox*")):
        try:
            t = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if v in t:
            p.write_text(t.replace(v, SCRUBBED), encoding="utf-8")
            n += 1
    return n


def paste_hint(rid: str) -> str:
    """The line after the steps when the owner pastes something BACK in chat (a one-time token). A
    connection whose secrets live on a page (Plaid) gets '' - "paste what it gives you here" sent the owner
    looking for something to paste into chat (2026-10-20)."""
    r = row(rid)
    p = (((r or {}).get("setup") or {}).get("chat") or {}).get("paste") or {}
    return "Paste what it gives you here and I'll take it from there." if p.get("kind") else ""


def chat_hint() -> str:
    """One line for the agent's CONNECTIONS block: which connections can be set up in chat."""
    names = [f"{r.get('name', r['id'])} (say: set up {r['id'].split('-')[-1]})" for r in chat_rows()]
    return ("Chat setup available for: " + "; ".join(names) + ". A one-time setup token they paste is claimed by code and removed from the record; passwords and codes still never.") if names else ""


# ------------------------------------------------------------------ the morning brief's Connections line
STATUS = DATA / "connections_status.json"
NOT_WORKING = ("not_configured", "dead")


def _fix_for(r: dict) -> str:
    """How the owner fixes one connection, in the words of the thing he taps or says: the chat
    phrase when the row has one (`setup.chat.say`), else the Setup-page button (setup_actions.recipe),
    else the renew text."""
    s = (r.get("setup") or {}) if isinstance(r.get("setup"), dict) else {}
    ch = s.get("chat") if isinstance(s.get("chat"), dict) else {}
    say = str(ch.get("say") or "").strip()
    if say:
        return f'say "{say}" here'
    label = ""
    try:
        import setup_actions
        rc = setup_actions.recipe(r)
        label = str(rc.get("page_label") or rc.get("label") or "").strip()
    except Exception:  # noqa: BLE001
        label = str(s.get("page_label") or s.get("label") or "").strip()
    if label:
        who = " (she signs in)" if str(r.get("owner") or "") == "owner6" else ""
        return f"Setup page: {label}{who}"
    renew = str(r.get("renew") or "").strip()
    return renew[:80] if renew else "no setup step recorded"


def brief_rows(status: dict | None = None, watch: list | None = None) -> list:
    """[(row, state)] for the connections the brief names: config.json -> brief.connections (ids,
    in that order) that are not working right now; with no list, every not_configured / dead row an
    owner can renew. Live rows are never mentioned."""
    st = status if status is not None else (load_json(STATUS, {}) or {}).get("connections", {})
    if watch is None:
        from alib import config
        b = config().get("brief") if isinstance(config().get("brief"), dict) else {}
        watch = b.get("connections") if isinstance(b.get("connections"), list) else None
    rows = {r["id"]: r for r in _rows() if r.get("id")}
    ids = [str(x) for x in watch] if watch is not None else [rid for rid in rows if str(rows[rid].get("owner") or "owner1") != "claude"]
    out = []
    for rid in ids:
        r, s = rows.get(rid), (st.get(rid) or {}) if isinstance(st, dict) else {}
        if not r:
            continue
        state = str(s.get("state") or ("not_configured" if not s else ""))
        if state in NOT_WORKING:
            out.append((r, state))
    return out


def brief_line(status: dict | None = None, watch: list | None = None) -> str:
    """ONE plain sentence for Sage's morning brief naming what is NOT connected and the chat phrase or
    Setup-page button that fixes each (plan 2026-10-18 section 2: these stop surfacing as failures
    mid-task). '' when everything watched is working."""
    rows = brief_rows(status, watch)
    tail = ""
    try:
        import outcomes
        op = outcomes.open_rows()
        bad = outcomes.recent(26, ("failed", "abandoned"))
        bits = []
        if op:
            bits.append(f"{len(op)} thing{'s' if len(op) != 1 else ''} I have not been able to confirm yet")
        if bad:
            bits.append(f"{len(bad)} that did not go through since yesterday")
        if bits:
            tail = "\nUNCONFIRMED: " + " and ".join(bits) + " (say /open or /failed for the list)."
    except Exception:  # noqa: BLE001
        tail = ""
    if not rows:
        return tail.strip()
    parts = []
    for r, state in rows:
        name = str(r.get("brief_name") or r.get("name") or r["id"])
        word = "stopped working" if state == "dead" else "not connected yet"
        parts.append(f"{name} ({word}: {_fix_for(r)})")
    return "CONNECTIONS: " + "; ".join(parts) + "." + tail


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "list":
        for r in chat_rows():
            print(r["id"], "-", r.get("name"))
    elif a[0] == "steps" and len(a) == 2:
        print(steps(a[1]) or "no chat steps for that connection")
    elif a[0] == "detect" and len(a) == 2:
        rid, val = detect(a[1])
        print(json.dumps({"asked": asked(a[1]), "token_for": rid, "token_len": len(val)}))
    elif a[0] == "brief":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        print(brief_line())
