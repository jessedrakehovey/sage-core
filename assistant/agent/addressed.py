"""addressed.py - who is a message in the shared space for? Tier 2 of the addressing decision.

Ported from example's bridge (scripts/turn_agent/bridge.py `_classify_llm`, the owner 2026-09-29:
"there is a more intelligent way than just making a rule that all messages besides name
mentions go to example"), 2026-10-08. The certain rules stay zero-LLM in bridge.poll (an @mention or
"Sage" = direct; a line that starts with the other owner's name = theirs). Only the ambiguous
middle reaches this: a small model (Haiku, ~5 s, no tools) sees who is in the space, the
conversation so far, the sender, the line, and the labelled examples the owners gave when it got
one wrong. On any failure the fallback is the old rule (an owner's line in the home space is for
the agent), so a model hiccup can never drop a message.

  record_example(...)   every decision, so corrections have something to attach to
  label_example(...)    an owner's correction ("that was for Sage" / "that wasn't for you"),
                        from the agent's SUGGEST {"addressed": {...}}; the last 12 labelled
                        examples ride along in every later classification
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

CLAUDE = os.path.expandvars(r"%APPDATA%\npm\claude.cmd")
EXAMPLES = core.DATA / "address_examples.jsonl"
TIMEOUT_S = 45
MAX_EXAMPLES = 12

PROMPT = """You decide WHO a Google Chat message is addressed to, in the shared space of {name}, the household and
business assistant of {household}. The owners talk to {name} there AND to each other.
Return ONLY compact JSON: {{"to":"agent"|"person"|"group"|"unclear","person":"<name or ''>","confidence":0.0-1.0,"why":"<10 words>"}}
- "agent": the sender wants {name} to do or answer something (an instruction, a question, a reply to
  something {name} asked or posted such as a card, information for the record).
- "person": the message is meant for the other owner (a reply to what they just said, a task for them,
  a question only they can answer). Name them.
- "group": chit-chat or an announcement with no action for {name}.
- "unclear": you cannot tell.
Cues: a reply inside a thread continues that thread; a message right after the other owner's message
that answers it is for them; an imperative with no name in {name}'s space is usually for {name}; a line
about a card, a draft, a text or email to send, a lead, a wedding, a schedule or money that asks for
follow-up is usually for {name} unless the other owner was just asked it. Learn from the labelled
examples: they are corrections from the owners.
"""


def _examples() -> list:
    try:
        rows = [json.loads(l) for l in EXAMPLES.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, ValueError):
        return []
    labelled = [r for r in rows if r.get("label")][-MAX_EXAMPLES:]
    return [{"text": r.get("text", "")[:200], "context": r.get("context", "")[:200], "to": r.get("label"),
             "note": r.get("note", "")[:100]} for r in labelled]


def classify(text: str, sender: dict, history: str, in_thread: bool) -> dict | None:
    """-> {"to", "person", "confidence", "why"} or None when the model could not be asked."""
    if not Path(CLAUDE).exists() or not str(text or "").strip():
        return None
    p = core.profile()
    users = core.people().get("users") or {}
    roster = [{"name": u.get("name"), "role": u.get("role")} for u in users.values()]
    payload = {"roster": roster, "sender": {"name": (sender or {}).get("name"), "role": (sender or {}).get("role")},
               "in_thread": bool(in_thread), "conversation_so_far": (history or "")[-1500:],
               "message": str(text)[:1200], "labelled_examples": _examples()}
    try:
        import account
        env, _ = account.env()
    except Exception:  # noqa: BLE001
        env = dict(os.environ)
    try:
        r = _run([CLAUDE, "-p", "--model", "haiku", "--output-format", "json", "--disallowedTools", "*"],
                 PROMPT.format(name=p.get("name", "the agent"), household=p.get("household", "the household"))
                 + "\n" + json.dumps(payload, ensure_ascii=False), env)
        txt = str((json.loads(r.strip()) or {}).get("result") or "")
        m = re.search(r"\{.*\}", txt, re.S)
        cls = json.loads(m.group(0)) if m else {}
        if cls.get("to") not in ("agent", "person", "group", "unclear"):
            return None
        return cls
    except Exception as e:  # noqa: BLE001
        core.append(core.DATA / "address_errors.jsonl", {"ts": core.stamp(), "err": f"{type(e).__name__}: {str(e)[:160]}"})
        return None


def _run(cmd: list, prompt: str, env: dict) -> str:
    """One Haiku call, prompt on stdin (tests replace this)."""
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=TIMEOUT_S, cwd=str(core.VAULT), env=env, creationflags=core.NO_WINDOW)
    return r.stdout or ""


def decide(text: str, sender: dict, history: str, in_thread: bool) -> tuple:
    """-> (direct: bool, why: str). The tiered decision for the ambiguous middle."""
    cls = classify(text, sender, history, in_thread)
    if not cls:
        return True, "owner in the home space (rule fallback)"
    to, who, conf = cls.get("to"), str(cls.get("person") or "").strip(), float(cls.get("confidence") or 0)
    why = f"model: {str(cls.get('why') or '')[:60]} ({conf:.1f})"
    if to == "person" and who and conf >= 0.6:
        record_example(text, "person:" + who, history)
        return False, f"for {who}, not the agent; {why}"
    if to == "group" and conf >= 0.7:
        record_example(text, "group", history)
        return True, f"group talk, nobody asked the agent; {why} (weak)"
    record_example(text, "agent" if to == "agent" else "unclear", history)
    return True, f"owner in the home space; {why}"


def record_example(text: str, decision: str, context: str = "", label: str = "", note: str = "", source: str = "auto") -> None:
    try:
        core.append(EXAMPLES, {"ts": core.stamp(), "text": str(text)[:400], "context": str(context)[-400:],
                               "decision": decision, "label": label, "note": note[:200], "source": source})
    except Exception:  # noqa: BLE001
        pass


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(t or "").lower()).strip()


def label_example(fragment: str, should_be: str, note: str = "") -> bool:
    """An owner's correction: label the most recent example whose text contains the fragment,
    else add one. should_be: agent | person:<name> | group."""
    frag = _norm(fragment)
    rows = []
    try:
        rows = [json.loads(l) for l in EXAMPLES.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, ValueError):
        pass
    for r in reversed(rows):
        if frag and frag in _norm(r.get("text", "")):
            r.update(label=should_be, note=note[:200], labelled_ts=core.stamp())
            try:
                EXAMPLES.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n", encoding="utf-8")
            except OSError:
                return False
            return True
    record_example(fragment, "unknown", "", should_be, note, source="owner")
    return True


def history_block(convo: str, n: int = 12) -> str:
    rows = core.transcript_tail(convo, n)
    return "\n".join(f"[{r.get('ts', '')[11:]} {r.get('who')}] {str(r.get('text') or '')[:220]}" for r in rows)
