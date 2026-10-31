"""desk.py - the agent's full desk: a WORK ask typed in chat is done in the turn, by a Claude Code
session with write tools, instead of being relayed to a slower process that may never run.

Why (the owner, 2026-10-30, comparing this agent with the one that runs his other business): "When I tell
her to do something she follows through ... [this one] promises approval cards; I never get them. [It]
says it will build it, it gets built 15 minutes later. She acts. [This one] overpromises and under
delivers." The chat brain (brain.py) is a read-only turn: it can only
record deed intents, and code hands each one to another clock (the operator every 30 min, the doer and
drain every 10, the builder hourly with a daily cap, cards held for Monday's digest). The prompt even
scripts the words "the card is below" and "that's in to be built", so the model says them whether or
not the plumbing delivers. The records on 10-30: 111 open cards, 71 never in Chat; 42 of 95 build
promises unkept; 22 of 57 doer jobs failed; 17 of 251 owner lines "try again / do it / you promised".

What changes here, around the unchanged brain:
  * `wants()` sorts an owner's line by rules in milliseconds: a question, a "remember / from now on", a
    card's thread stay with the chat brain; an imperative ("fix", "text him back", "upload", "add a team member
    to the team", "try again") goes to the desk.
  * `start()` posts one receipt in the thread and launches the desk DETACHED (it survives the bridge
    restarting when the desk edits agent code): a `claude -p` turn in the organization's folder, with
    the vault's own hooks, no tool allow-list, the send belt denied (DENIED + the CRM's write tools +
    the gate's never-allow list) and desk_policy.py as a PreToolUse hook on every Bash/Write/Edit call
    (no send path, no executor, no card answering, no credentials, no push, no browser, no nested
    agent, no owner's path). The private deed server (gmcp.py) is there too, so `task`/`do`/`decide`/
    `learn` work exactly as in chat and are applied by code after the turn.
  * The desk posts a card with the exact content when a send or a CRM change is needed (only the tap
    sends), a milestone line only when something is in the owner's hands, and its reply in the thread.
    Code checks the reply against the record: "the card is below" with no card posted gets the truth
    appended; a failed or timed-out turn says so once, plainly.
  * `block()` puts the desk's recent work into the brain's context, so "is it done?" is answered from
    the record and the brain never claims or redoes the desk's work.
Switch: profile.json -> "desk": {"enabled", "model", "max_turns", "timeout_s", "notes_cap"}; env AGENT_DESK=off.
Generic: no organization, person or service is named here; names come from profile.json / people.json.

  python assistant/agent/desk.py classify "<line>"       # desk or brain, and why
  python assistant/agent/desk.py run <job id>            # the child process (started by start())
  python assistant/agent/desk.py note <job id> "<line>"  # a milestone line into the ask's thread
  python assistant/agent/desk.py status [--json]         # the jobs of the last day
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if str(HERE.parent / "scripts") not in sys.path:
    sys.path.append(str(HERE.parent / "scripts"))      # at the end: never shadow agent modules of the same name (followups.py)
import core  # noqa: E402

JOBS = core.DATA / "desk"
PROMPT = HERE / "desk_prompt.md"
POLICY = HERE / "desk_policy.py"
RECEIPT = "On it. I'll put what I did here when it's done."
DEFAULTS = {"enabled": True, "model": "opus", "max_turns": 80, "timeout_s": 1800, "notes_cap": 6}
# check_gates.py parses this shape (target agent-desk). The desk has NO allow-list: the vault's own
# settings apply. The belt grows at runtime with the CRM's write tools (_apply_crm_tools) and with
# the gate's never-allow list (_never_allow), so no connector or mail account is named here.
DENIED = ",".join(["Task", "Agent",
                   "mcp__gmail__send_email", "mcp__gmail__delete_email", "mcp__gmail__batch_delete_emails",
                   "mcp__playwright__browser_click", "mcp__playwright__browser_type", "mcp__playwright__browser_press_key",
                   "mcp__playwright__browser_file_upload", "mcp__playwright__browser_fill_form",
                   "mcp__playwright-cli__browser_click", "mcp__playwright-cli__browser_type", "mcp__playwright-cli__browser_press_key",
                   "mcp__playwright-cli__browser_file_upload", "mcp__playwright-cli__browser_fill_form"])


def log(msg: str) -> None:
    try:
        p = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "desk-wv.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def cfg() -> dict:
    d = dict(DEFAULTS)
    try:
        d.update({k: v for k, v in (core.profile().get("desk") or {}).items() if v is not None})
    except Exception:  # noqa: BLE001
        pass
    if os.environ.get("AGENT_DESK", "").lower() in ("off", "0", "false"):
        d["enabled"] = False
    return d


# ------------------------------------------------------------------ what is work
_VERBS = (r"fix|build|rebuild|make|create|set ?up|send|resend|text|email|e-mail|reply|respond|answer|follow[ -]?up|check|find|"
          r"look ?up|look into|pull|get|grab|book|rent|order|cancel|update|change|edit|add|remove|delete|purge|clear|upload|"
          r"download|copy|move|open|run|re-?run|re-?launch|launch|restart|install|register|write|draft|prepare|package|export|"
          r"import|sync|print|generate|do|redo|try|finish|complete|handle|deal with|sort out|take care of|figure out|solve|"
          r"debug|investigate|verify|test|go through|compile|put together|search|locate|keep|put|post|save|store|drop|turn|"
          r"switch|start|stop|list|summari[sz]e|compare|rename|organi[sz]e|archive|set|hold|renew|return|submit|fill|apply|"
          r"transcribe|convert|render|release|publish|calculate|work out|pay attention|reach out|ping|nudge|chase|invite|"
          r"replace|move|attach|link|share|forward|schedule a (?:meeting|call|time)")
_LEAD = (r"(?:(?:ok(?:ay)?|yes|yep|no|well|so|and|also|then|now|please|pls|just|hey|go ahead and|i need you to|i want you to|"
         r"i'?d like you to|you need to|you should|you have to|you can|can you|could you|would you|will you|make sure (?:to|you)|"
         r"need to|needs to|we need to|we should|let'?s|i need|i want|i'?d like|time to|we have to|you'?ll need to|"
         r"you'?re going to|go|be able to|promptly|quickly|immediately|right now|asap|first|kindly|carefully|finally|"
         r"actually|really|definitely|simply|then)[\s,.!:-]+)*")
_IMPERATIVE = re.compile(r"^\W*" + _LEAD + r"(?:" + _VERBS + r")\b", re.I)
_SHORT_DO = re.compile(r"^\W*(?:ok(?:ay)?[,.!]?\s*)?(?:well[,]?\s+)?(?:do it|do that|do this|go ahead|try again|again|make it so|"
                       r"get it done|just do it|do it now|do what i asked|do not fail|don'?t stop)\b", re.I)
_REQUEST_Q = re.compile(r"^\W*(?:can|could|would|will|why don'?t|why can'?t|why haven'?t|why hasn'?t) you\b", re.I)
# standing instructions, memory and reminders are the chat brain's instant deeds; a lone link is read by code
_NOT_WORK = re.compile(r"^\W*(?:remember|from now on|always|never|don'?t ever|remind me|forget|note that|fyi|stop sending|"
                       r"every ?time|each time|whenever|going forward|in the future|unsubscribe|mute|https?://)", re.I)
_LEARN_TAIL = re.compile(r"\b(?:remind me|from now on|every ?time|going forward)\b", re.I)
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def _sentences(t: str) -> list:
    return [s.strip() for s in _SENT.split(t) if s.strip()]


def _strip_name(t: str) -> str:
    """'<agent name>, fix the hook' -> 'fix the hook' (the name comes from profile.json, never a literal)."""
    try:
        name = str(core.profile().get("name") or "").strip()
    except Exception:  # noqa: BLE001
        name = ""
    if name:
        t = re.sub(r"^\W*(?:hey |hi |ok |okay )?" + re.escape(name) + r"\b[\s,.!:-]*", "", t, flags=re.I)
    return t


def classify(text: str) -> tuple[str, str]:
    """('desk'|'brain', why) from the words alone."""
    t = str(text or "").strip()
    if not t or t.startswith("/") or t.startswith("["):
        return "brain", "a command, a tap or nothing"
    t = _strip_name(t) or t
    if _NOT_WORK.match(t):
        return "brain", "a standing instruction, a reminder, a fact or a link: the chat brain's own deeds"
    for s in _sentences(t):
        if s.endswith("?") and not _REQUEST_Q.match(s):
            continue                                   # a question is a lookup
        if _SHORT_DO.match(s):
            return "desk", f"'{s[:40]}' is the ask again: do it"
        if _IMPERATIVE.match(s) and not _LEARN_TAIL.search(s):
            return "desk", f"'{s[:40]}' is work"
    return "brain", "a question or a statement"


def wants(text: str, msg: dict, person: dict | None, tcard: dict | None = None, correction_kind: str = "",
          card_here: bool = False, scope: str = "") -> str:
    """'' when the chat brain should take this line, else why the desk takes it."""
    if not cfg().get("enabled", True):
        return ""
    if not person or not core.is_owner(person):
        return ""
    if str(msg.get("channel") or "") not in ("gchat", "local"):
        return ""
    if not msg.get("direct", True) or str(msg.get("why") or "").endswith("(weak)"):
        return ""
    if tcard:
        return ""                                      # a card's thread: approvals and revisions are the brain's
    if scope == "business":
        return ""                                      # a team space: the brain's business-only turn
    t = str(text or "").strip()
    if not t or t.startswith("/") or t.startswith("["):
        return ""
    if correction_kind in ("card", "redo", "fix") and not card_here:
        return "correction: the ask comes back and the desk does it"
    kind, why = classify(t)
    return why if kind == "desk" else ""


# ------------------------------------------------------------------ jobs
def _path(jid: str) -> Path:
    return JOBS / f"{jid}.json"


def load_job(jid: str) -> dict:
    return core.load(_path(jid), {}) or {}


def _save(job: dict) -> None:
    JOBS.mkdir(parents=True, exist_ok=True)
    core.save(_path(job["id"]), job)


def jobs(hours: float = 24.0, convo: str = "") -> list:
    JOBS.mkdir(parents=True, exist_ok=True)
    now = time.time()
    out = []
    for p in JOBS.glob("d*.json"):
        j = core.load(p, {}) or {}
        if not j.get("id") or now - float(j.get("ts") or 0) > hours * 3600:
            continue
        if convo and j.get("convo") != convo:
            continue
        out.append(j)
    out.sort(key=lambda j: float(j.get("ts") or 0))
    return out


def _python() -> str:
    exe = Path(sys.executable)
    con = exe.with_name("python.exe")
    return str(con if exe.name.lower() == "pythonw.exe" and con.exists() else exe)


def start(msg: dict, person: dict, text: str, ctx: dict, spawn: bool = True) -> dict:
    """Record the job and launch the desk detached. `ctx`: convo, thread (the Chat thread the reply and
    the cards land in: the ask's thread, or the receipt's), space, channel, why, correction, ask."""
    jid = "d" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    job = {"id": jid, "ts": time.time(), "person": str(person.get("key") or ""), "name": str(person.get("name") or ""),
           "chat_user": str(person.get("chat_user") or ""), "text": str(text or "").strip(),
           "convo": str(ctx.get("convo") or ""), "thread": str(ctx.get("thread") or ""), "space": str(ctx.get("space") or ""),
           "channel": str(ctx.get("channel") or msg.get("channel") or ""), "why": str(ctx.get("why") or ""),
           "correction": str(ctx.get("correction") or ""), "ask": ctx.get("ask") or {},
           "status": "running", "notes": [], "cards": [], "reply": "", "pid": 0, "model": cfg().get("model")}
    _save(job)
    if spawn:
        env = {**os.environ, "AGENT_DESK_JOB": jid}
        flags = core.NO_WINDOW | (0x00000200 if os.name == "nt" else 0)          # CREATE_NEW_PROCESS_GROUP
        try:
            p = subprocess.Popen([_python(), str(HERE / "desk.py"), "run", jid], cwd=str(core.VAULT), env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 close_fds=True, creationflags=flags)
            job["pid"] = p.pid
        except Exception as e:  # noqa: BLE001
            job["status"], job["error"] = "failed", f"could not start: {type(e).__name__}: {str(e)[:120]}"
        _save(job)
    log(f"{jid} start {job['person']} {job['status']} pid={job['pid']} :: {job['text'][:100]!r}")
    return job


# ------------------------------------------------------------------ the prompt
def _tap_paths() -> str:
    try:
        import builder
        paths = sorted(set(builder.TAP_PATHS) | set(builder.HARD_PATHS))
    except Exception:  # noqa: BLE001
        paths = []
    return ", ".join(paths) or "(none listed)"


def _where(job: dict, person: dict) -> str:
    try:
        import place
        return place.block({"convo": job["convo"], "thread": job["thread"], "space": job["space"], "text": job["text"],
                            "channel": job["channel"], "direct": True, "why": "DM"}, person)
    except Exception as e:  # noqa: BLE001
        return f"(place unknown: {type(e).__name__})"


def _thread_text(job: dict) -> str:
    """The thread so far, from Chat itself when it can be read, else from the transcript."""
    rows = None
    if job.get("thread") and job.get("space"):
        try:
            import chat_thread
            if chat_thread.available():
                rows = chat_thread.thread_lines(job["space"], job["thread"])
                if rows:
                    return "THIS THREAD SO FAR:\n" + "\n".join(chat_thread.render(rows))
        except Exception:  # noqa: BLE001
            rows = None
    tail = [r for r in core.rows(core.TRANSCRIPT) if r.get("convo") == job["convo"]
            and (not job.get("thread") or r.get("thread") == job["thread"])][-12:]
    if not tail:
        return ""
    return "RECENTLY IN THIS CONVERSATION:\n" + "\n".join(f"- {str(r.get('ts') or '')[11:16]} {r.get('who')}: {str(r.get('text') or '')[:300]}" for r in tail)


def _running_text(job: dict) -> str:
    others = [j for j in jobs(6, job["convo"]) if j["id"] != job["id"] and j.get("status") == "running"]
    if not others:
        return ""
    lines = ["ALREADY RUNNING on this conversation (another desk turn; read its notes before redoing anything, never do the same work twice):"]
    for j in others:
        lines.append(f"- since {time.strftime('%H:%M', time.localtime(float(j['ts'])))}: {j['text'][:160]!r}" +
                     (f" notes: {' | '.join(n.get('text', '')[:80] for n in j.get('notes') or [])}" if j.get("notes") else ""))
    return "\n".join(lines)


def _correction_text(job: dict) -> str:
    if not job.get("correction"):
        return ""
    ask = job.get("ask") or {}
    s = f"THIS IS A CORRECTION ({job['correction']}): the owner's words say the earlier reply did not do it."
    if ask.get("text"):
        s += f" The original ask was: {str(ask['text'])[:400]!r}. Do THAT now, with the deed; one clause on what you did, no apology beyond three words."
    return s + "\n"


def render(job: dict, person: dict) -> str:
    p = core.profile()
    tpl = PROMPT.read_text(encoding="utf-8")
    try:
        rules = core.render_rules() or "(none yet)"
    except Exception:  # noqa: BLE001
        rules = "(none yet)"
    vals = {"name": str(p.get("name") or "the agent"), "household": str(p.get("household") or "the organization"),
            "person": str(person.get("name") or job.get("name") or "an owner"), "vault": str(core.VAULT),
            "ask": job["text"], "correction": _correction_text(job), "where": _where(job, person),
            "thread": _thread_text(job), "running": _running_text(job), "job": job["id"],
            "thread_name": job.get("thread") or "", "space": job.get("space") or "", "tap_paths": _tap_paths(), "rules": rules}
    for k, v in vals.items():
        tpl = tpl.replace("{" + k + "}", str(v))
    return tpl


# ------------------------------------------------------------------ the turn
def _never_allow() -> list:
    try:
        sys.path.insert(0, str(core.VAULT / "scripts" / "guardrails"))
        import check_gates
        return list(check_gates.CRITICAL_NEVER_ALLOW)
    except Exception:  # noqa: BLE001
        return []


def denied() -> str:
    import brain
    _allowed, d = brain._apply_crm_tools("", DENIED)
    toks = [t for t in d.split(",") if t]
    for t in _never_allow():
        if t not in toks:
            toks.append(t)
    return ",".join(toks)


def _settings(job_dir: Path) -> Path:
    py = Path(_python()).as_posix()
    hook = f'"{py}" "{POLICY.as_posix()}" --hook'
    cfg_ = {"hooks": {"PreToolUse": [
        {"matcher": "Bash|PowerShell", "hooks": [{"type": "command", "command": hook, "timeout": 20}]},
        {"matcher": "Write|Edit|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": hook, "timeout": 20}]},
    ]}}
    job_dir.mkdir(parents=True, exist_ok=True)
    f = job_dir / "settings.json"
    f.write_text(json.dumps(cfg_, indent=1), encoding="utf-8")
    return f


def _mcp(turn: Path) -> Path:
    import brain
    server = {"command": sys.executable, "args": [str(core.AGENT / "gmcp.py")]}
    env_t = {"AGENT_TURN": str(turn), "AGENT_DATA": str(core.DATA), "AGENT_STATE": os.environ.get("AGENT_STATE", ""),
             "ASSISTANT_DATA": os.environ.get("ASSISTANT_DATA", ""), "ASSISTANT_CARDS": os.environ.get("ASSISTANT_CARDS", "")}
    server["env"] = {k: v for k, v in env_t.items() if v}
    try:
        extra = brain._alib().plugin_map("MCP_SERVERS")
    except Exception:  # noqa: BLE001
        extra = {}
    f = Path(turn).with_suffix(".mcp.json")
    core.save(f, {"mcpServers": {brain.MCP_SERVER: server, **extra}})
    return f


def command(job: dict, turn: Path, job_dir: Path, model: str = "") -> list:
    import brain
    c = cfg()
    return [brain.CLAUDE, "-p", "--model", model or str(c.get("model") or "opus"), "--output-format", "json",
            "--max-turns", str(c.get("max_turns") or 80), "--dangerously-skip-permissions",
            "--settings", str(_settings(job_dir)), "--disallowedTools", denied(),
            "--mcp-config", str(_mcp(turn)), "--strict-mcp-config"]


def _run_claude_on(prompt: str, job: dict, turn: Path, acct: str | None = None) -> dict:
    import account
    import brain
    if not Path(brain.CLAUDE).exists():
        return {"ok": False, "error": "claude CLI not found", "text": ""}
    job_dir = JOBS / job["id"]
    cmd = command(job, turn, job_dir)
    env, note = account.env(acct)
    env = {**env, "AGENT_DESK_JOB": job["id"]}
    env.setdefault("WV_LOG_DIR", str(Path.home() / ".claude"))
    timeout = int(cfg().get("timeout_s") or 1800)
    log(f"{job['id']} turn model={cmd[3]} {note}")
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, cwd=str(core.VAULT), env=env, creationflags=core.NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "text": "", "account": note}
    out = (r.stdout or "").strip()
    for ln in reversed(out.splitlines() or [""]):
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict) and "result" in d:
            return {"ok": not d.get("is_error"), "text": str(d.get("result") or ""), "account": note,
                    "session": str(d.get("session_id") or ""), "error": str(d.get("result") or "")[:200] if d.get("is_error") else ""}
    return {"ok": False, "account": note, "text": "", "error": ((r.stderr or "") + out)[-300:] or f"exit {r.returncode}"}


def _run_claude(prompt: str, job: dict, turn: Path) -> dict:
    """The turn, on the account with room (account.order), like the brain. Tests replace this function."""
    import account
    r = None
    for acct in account.order():
        r = _run_claude_on(prompt, job, turn, acct=acct)
        if r.get("ok") or not account.out_until(r.get("error") or ""):
            return r
        account.note_limit(acct, r.get("error") or "")
    return r or {"ok": False, "error": "no account", "text": ""}


# ------------------------------------------------------------------ the record vs the words
_CARD_CLAIM = re.compile(r"\bcards?\b[^.!?\n]{0,60}\b(?:below|under this|is up|'s up|went up|is here|(?:is |'s |was )?posted|is waiting|"
                         r"ready for your tap|for your tap|waiting on your tap)\b|"
                         r"\b(?:posted|made|put up|put)\b[^.!?\n]{0,30}\b(?:on a |the |a )?card\b|\bon a card\b", re.I)


def posted_cards(jid: str) -> list:
    """The cards this job posted through cards.py (--owner desk:<job>)."""
    out = []
    try:
        import cards
        for p in cards.LIVE.glob("q*.json"):
            c = cards._read(p) or {}
            if str(c.get("owner") or "") == f"desk:{jid}":
                out.append(str(c.get("id") or p.stem))
    except Exception:  # noqa: BLE001
        pass
    return out


def finish_text(reply: str, r: dict, posted: list, lines: list, job: dict) -> str:
    if not r.get("ok"):
        err = str(r.get("error") or "")
        try:
            import voice
            plain = voice.plain(err)
        except Exception:  # noqa: BLE001
            plain = err[:160]
        last = (job.get("notes") or [{}])[-1].get("text", "") if job.get("notes") else ""
        if err == "timeout":
            return ("That one ran long and I stopped it. " + (f"Where it stands: {last}" if last else "Nothing from it is in your hands yet.")).strip()
        return ("I couldn't finish that one" + (f": {plain}" if plain else "") + ". " + (f"Where it stands: {last}" if last else "")).strip()
    out = str(reply or "").strip()
    if _CARD_CLAIM.search(out) and not posted:
        out = (out + " Correction: no card has actually gone up from this, so nothing is waiting on your tap yet.").strip()
    if lines:
        out = (out + "\n\n" if out else "") + " ".join(ln.strip() for ln in lines if ln.strip())
    return out or "Done."


# ------------------------------------------------------------------ posting
def _chat():
    if os.environ.get("CHAT_PUSH", "").lower() in ("off", "0", "false") or core.SCOPED:
        return None
    try:
        import bridge
        return bridge._gchat()
    except Exception:  # noqa: BLE001
        return None


def _post(job: dict, text: str, channel: str = "desk") -> str:
    name = core.profile().get("name", "Agent")
    to = job["convo"][3:] if str(job.get("convo") or "").startswith("dm:") else "space"
    core.say(job["convo"], name, text, channel=channel, meta={"thread": job.get("thread") or "", "desk": job["id"]})
    core.append(core.DATA / "outbox.jsonl", {"ts": core.stamp(), "to": to, "text": text, "via": channel, "convo": job["convo"],
                                             "thread": job.get("thread") or "", "desk": job["id"]})
    g = _chat()
    if g and job.get("channel") == "gchat" and job.get("space"):
        try:
            txt = text
            if job.get("chat_user"):
                txt = g.ensure_mention(text, job["space"], job["chat_user"])
            msg = g.post(job["space"], txt, job.get("thread") or "")
            if msg and not job.get("thread"):
                import bridge
                job["thread"] = bridge._thread_of(msg)
                core.note_thread_root(job["thread"], name, text, channel, job["convo"])
            return msg
        except Exception as e:  # noqa: BLE001
            log(f"{job['id']} chat post failed: {type(e).__name__}: {str(e)[:120]}")
    return ""


def note(jid: str, text: str) -> dict:
    """A milestone line into the ask's thread (the desk calls this from Bash). Progress chatter is dropped by
    the voice gate; the cap keeps a chatty turn from flooding the thread."""
    job = load_job(jid)
    text = str(text or "").strip()
    if not job or not text:
        return {"ok": False, "reason": "no such job" if not job else "empty"}
    try:
        import voice
        shown = voice.gate(text, "desk", job.get("space") or "")
    except Exception:  # noqa: BLE001
        shown = text
    if not shown:
        job.setdefault("notes", []).append({"ts": time.time(), "text": text, "quiet": True})
        _save(job)
        return {"ok": False, "reason": "progress chatter is never posted; say it when something is in their hands"}
    if len([n for n in job.get("notes") or [] if not n.get("quiet")]) >= int(cfg().get("notes_cap") or 6):
        job.setdefault("notes", []).append({"ts": time.time(), "text": text, "quiet": True})
        _save(job)
        return {"ok": False, "reason": "note cap reached; put the rest in your reply"}
    msg = _post(job, shown, channel="desk")
    job.setdefault("notes", []).append({"ts": time.time(), "text": shown, "msg": msg})
    _save(job)
    return {"ok": True, "posted": bool(msg)}


# ------------------------------------------------------------------ the child process
def run(jid: str) -> dict:
    job = load_job(jid)
    if not job:
        return {"ok": False, "error": "no such job"}
    import actions
    import commitments
    import intents
    person = core.person_for(handle=job["person"]) or {"key": job["person"], "name": job["name"], "chat_user": job.get("chat_user", "")}
    ctx = {"convo": job["convo"], "thread": job.get("thread") or "", "space": job.get("space") or "", "text": job["text"]}
    turn = intents.begin(person, job["convo"], job.get("thread") or "", None, job.get("space") or "")
    job["turn"] = str(turn)
    _save(job)
    prompt = render(job, person)
    t0 = time.time()
    r = _run_claude(prompt, job, turn)
    job = load_job(jid) or job                       # notes landed meanwhile
    job["seconds"] = round(time.time() - t0)
    reply, legacy = "", {}
    if r.get("ok"):
        try:
            import brain
            reply, legacy = brain.split_suggest(str(r.get("text") or ""))
        except Exception:  # noqa: BLE001
            reply, legacy = str(r.get("text") or ""), {}
    sug = {}
    try:
        sug = {**(legacy or {}), **(intents.collect(turn) or {})} if r.get("ok") else {}
    except Exception as e:  # noqa: BLE001
        log(f"{jid} collect failed: {type(e).__name__}: {str(e)[:120]}")
    lines, applied = [], []
    if sug:
        try:
            lines = actions.apply(sug, person, ctx=ctx) or []
            applied = list(actions.LAST_APPLIED)
            commitments.register_applied(applied, person, {"convo": job["convo"], "thread": job.get("thread") or ""})
        except Exception as e:  # noqa: BLE001
            log(f"{jid} apply failed: {type(e).__name__}: {str(e)[:120]}")
    posted = posted_cards(jid) + [str(a.get("ref") or "") for a in applied if a.get("kind") == "card"]
    out = finish_text(reply, r, posted, lines, job)
    try:
        import reflex
        out = reflex.fix_reply(out, applied, lines)
    except Exception:  # noqa: BLE001
        pass
    try:
        import voice
        out = voice.polish(out)
    except Exception:  # noqa: BLE001
        pass
    job.update({"status": "done" if r.get("ok") else "failed", "reply": out, "error": str(r.get("error") or ""),
                "account": str(r.get("account") or ""), "cards": posted, "applied": applied, "ended": time.time()})
    _save(job)
    msg = _post(job, out, channel="desk")
    job["reply_msg"] = msg
    _save(job)
    log(f"{jid} {job['status']} in {job['seconds']}s cards={len(posted)} deeds={len(applied)} :: {out[:160]!r}")
    return job


# ------------------------------------------------------------------ for the brain
def block(convo: str, hours: float = 24.0) -> str:
    rows = jobs(hours, convo)
    if not rows:
        return ""
    lines = ["DESK WORK (asks you handed to your hands; they did or are doing the work themselves and reply in the thread. "
             "Answer 'is it done?' / 'where is it?' from this record; never redo it, never claim it, never promise it again):"]
    for j in rows[-8:]:
        hm = time.strftime("%H:%M", time.localtime(float(j.get("ts") or 0)))
        st = j.get("status")
        if st == "running":
            tail = f"running since {hm}" + (f"; so far: {' | '.join(n.get('text', '')[:80] for n in j.get('notes') or [] if not n.get('quiet'))}" if any(not n.get("quiet") for n in j.get("notes") or []) else "")
        elif st == "done":
            tail = f"done {time.strftime('%H:%M', time.localtime(float(j.get('ended') or 0)))}: {str(j.get('reply') or '')[:200]}"
        else:
            tail = f"failed {time.strftime('%H:%M', time.localtime(float(j.get('ended') or 0)))}: {str(j.get('error') or '')[:120]}"
        lines.append(f"- {hm} {j.get('text', '')[:120]!r} -> {tail}")
    return "\n".join(lines)


def status_text(hours: float = 24.0) -> str:
    rows = jobs(hours)
    if not rows:
        return "no desk jobs in the last day"
    return "\n".join(f"{j['id']} {j.get('status')} {j.get('seconds', '')}s {j.get('person')} :: {j.get('text', '')[:90]!r}" for j in rows)


def main() -> int:
    a = sys.argv[1:]
    if a[:1] == ["classify"] and len(a) > 1:
        k, why = classify(" ".join(a[1:]))
        print(f"{k}: {why}")
        return 0
    if a[:1] == ["run"] and len(a) == 2:
        j = run(a[1])
        print(json.dumps({"id": j.get("id"), "status": j.get("status"), "reply": j.get("reply")}, ensure_ascii=False))
        return 0 if j.get("status") == "done" else 2
    if a[:1] == ["note"] and len(a) >= 3:
        print(json.dumps(note(a[1], " ".join(a[2:])), ensure_ascii=False))
        return 0
    if a[:1] == ["status"]:
        if "--json" in a:
            print(json.dumps(jobs(24), ensure_ascii=False, indent=1))
        else:
            print(status_text())
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
